"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
# Import python modules
import time
from collections.abc import Callable
from loguru import logger as log

# Import typing
from typing import Any, TYPE_CHECKING, cast

import globals as gl

from src.backend.DeckManagement.media_loop import MEDIA_LOOP_FPS
from src.backend.DeckManagement.InputIdentifier import Input, InputIdentifier
from src.backend import timer_wheel
if TYPE_CHECKING:
    from src.backend.PageManagement.Page import Page
    from src.backend.DeckManagement.deck_controller.background_media import Background
    from src.backend.DeckManagement.deck_controller.controller import DeckController

class ScreenSaver:
    def __init__(self, deck_controller: "DeckController"):
        self.deck_controller: "DeckController" = deck_controller

        # Stashed controller input mapping; close() can inspect it before show().
        self.original_inputs: dict[type[InputIdentifier], list[Any]] = {}
        self.original_background: "Background | None" = None
        self.original_brightness: "float | None" = 0

        # Time when last key state changed
        self.last_key_change_time = time.time()

        self.time_delay = 5

        self.enable: bool = False
        self.showing: bool = False

        self.media_path: str | None = None
        # Match the deck-settings schema when no configuration load runs.
        self.brightness: int = 30
        self.fps: int = MEDIA_LOOP_FPS
        self.loop: bool = True
        # Non-None only while armed, that is enabled and not showing. See
        # set_time and set_enable.
        self.timer: "timer_wheel.TimerHandle | None" = None
        # Configuration calls set_enable() before set_time(); only set_time() can
        # arm the first timer with the loaded delay.
        self._timer_initialized: bool = False

    def _arm_timer(self) -> None:
        # *60 converts minutes, the stored unit, into the seconds the timer
        # needs.
        self.timer = timer_wheel.schedule(self.time_delay * 60, self.on_timer_end, name="ScreenSaverTimer")

    def set_time(self, time_delay: int) -> None:
        time_delay = max(1, time_delay) # Minimum 1 minute. A smaller value shows the screensaver instantly and causes errors
        if time_delay != self.time_delay:
            log.info(f"Setting screen saver time delay to {time_delay} minutes")
        self.time_delay = time_delay
        self._timer_initialized = True
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.enable and not self.showing:
            self._arm_timer()

    def set_media_path(self, media_path: str | None) -> None:
        # None is the ordinary case. Every config without a chosen media says
        # None, and the background layer reads it as "blank".
        self.media_path = media_path

        if self.showing:
            self.deck_controller.background.set_from_path(self.media_path)

    def set_enable(self, enable: bool) -> None:
        self.enable = enable

        if not self._timer_initialized:
            return

        if self.showing and not enable:
            self.hide()

        if enable:
            if self.timer is None and not self.showing:
                self._arm_timer()
        else:
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None

    def on_timer_end(self) -> None:
        self.show()

    def show(self) -> None:
        """Prebuild the background lock-free, then install it under _load_page_lock.
        The locked phase performs no file I/O, GTK marshaling, or plugin callback."""
        if getattr(self.deck_controller, "_closing", False):
            # A timer that races close() must not restore transient state.
            return
        log.info("Showing screen saver")

        # Hash and open video before _load_page_lock because both can take seconds.
        kind, payload = self.deck_controller.background.prebuild_from_path(
            self.media_path, fps=self.fps, loop=self.loop
        )
        if kind == "noop":
            # Missing screensaver media must blank and release the page video.
            kind = "blank"

        with self.deck_controller._load_page_lock:
            # Coalesce concurrent show requests.
            if self.showing:
                if payload is not None and hasattr(payload, "close"):
                    cast("Callable[[], None]", payload.close)()
                return

            # Bump only the generation so pre-transition frames become stale.
            # Keep active_page because this is not a page switch.
            with self.deck_controller._page_gen_lock:
                self.deck_controller._page_load_generation += 1
                gen = self.deck_controller._page_load_generation

            # Stop the timer, because a caller can invoke show() manually.
            if self.timer:
                self.timer.cancel()
            self.showing = True

            self.original_inputs = self.deck_controller.inputs
            # Keep a complete input mapping visible while init_inputs builds its replacement.
            # Cancel every stashed gesture before the swap can strand its release event.
            for stashed_inputs in self.original_inputs.values():
                for stashed_input in stashed_inputs:
                    stashed_input.cancel_gesture()
            self.deck_controller.init_inputs()

            self.original_background = self.deck_controller.background
            self.original_brightness = self.deck_controller.brightness

            self.deck_controller.set_brightness(self.brightness)

            # expects_repaint is True, because the paints that install the screensaver background follow immediately below.
            self.deck_controller.clear(expects_repaint=True)
            # ClearMsg omits generic tasks, which can still hold work from the old page.
            # The page lock prevents this generation from being superseded during the wipe.
            self.deck_controller.clear_media_player_tasks()

            # Preserve lock order: page lock before background lock.
            # Recheck generation so an older worker cannot overwrite the screensaver.
            with self.deck_controller._background_load_lock:
                if self.deck_controller._page_is_current(gen):
                    self.deck_controller.background.apply_prebuilt(
                        kind, payload, fps=self.fps, loop=self.loop, update=True
                    )
                elif payload is not None and hasattr(payload, "close"):
                    # Close an unexpectedly superseded payload to release its capture.
                    cast("Callable[[], None]", payload.close)()

            # Release keys
            for key in self.deck_controller.inputs[Input.Key]:
                key.down_start_time = None
                key.press_state = False

            # Snapshot the stash under the lock before queued release.
            stashed_inputs = self.original_inputs

        # Release stashed input media through the writer after any tick using it.
        # Keep original_background because it aliases the live screensaver background.
        if stashed_inputs:
            # Import lazily to keep the controller package dependency one-way.
            from src.backend.DeckManagement.deck_controller.media_writer import (
                ReleaseStashedInputsMsg,
            )
            self.deck_controller.media_player.submit_control(
                ReleaseStashedInputsMsg(stashed_inputs)
            )

    def hide(self) -> None:
        """Restore screensaver state under _load_page_lock.
        Reload the page and reset the timer only after releasing the lock."""
        if getattr(self.deck_controller, "_closing", False):
            # Do not let the follow-up load_page resurrect a closing controller.
            return
        log.info("Hiding screen saver")

        follow_up = None
        with self.deck_controller._load_page_lock:
            # Coalesce concurrent hide requests.
            if not self.showing:
                return

            # Same atomic bump-only pattern as show(). See its comment.
            with self.deck_controller._page_gen_lock:
                self.deck_controller._page_load_generation += 1

            self.original_inputs.clear()
            # Blank the transition so zero brightness cannot expose a saver frame.
            # The follow-up page load supplies the expected repaint.
            self.deck_controller.clear(expects_repaint=True)
            self.showing = False

            # Prefer a page switch deferred while the saver showed; otherwise reload active_page.
            # Consume the pending slot under the page lock before the lock-free load.
            pending = self.deck_controller.take_pending_screensaver_page()
            # Reserve a consumed pending page until the follow-up installs it.
            if pending is not None and gl.page_manager is not None:
                gl.page_manager.pins.reserve_fetch(pending, self.deck_controller)
            active_page = pending if pending is not None else self.deck_controller.active_page
            time_delay = self.time_delay
            follow_up = lambda: self._hide_followup(active_page, time_delay)

        # Keep load_page outside the outer RLock hold.
        # initialize_actions or ChangePage under that hold can deadlock main-thread marshaling.
        follow_up()

    def _hide_followup(self, active_page: "Page | None", time_delay: int) -> None:
        if active_page:
            self.deck_controller.load_page(active_page, allow_reload=True)
        else:
            self.deck_controller.load_default_page()
        self.set_time(time_delay)

    def on_key_change(self) -> None:
        if getattr(self.deck_controller, "_closing", False):
            # Ignore an in-flight input event after teardown starts.
            return
        self.last_key_change_time = time.time()
        # All deck interaction reaches this funnel, but not the compositor.
        # The presence monitor is optional.
        if gl.presence_monitor is not None:
            gl.presence_monitor.notify_activity()
        if self.showing:
            self.hide()
        else:
            self.set_time(self.time_delay)

    def set_brightness(self, brightness: float) -> None:
        self.brightness = int(brightness)

        if self.showing:
            self.deck_controller.set_brightness(self.brightness)

    def set_fps(self, fps: int) -> None:
        self.fps = fps
        if not self.showing:
            return
        if self.deck_controller.background.video is not None:
            self.deck_controller.background.video.fps = fps

    def set_loop(self, loop: bool) -> None:
        self.loop = loop
        if not self.showing:
            return
        if self.deck_controller.background.video is not None:
            self.deck_controller.background.video.loop = loop
