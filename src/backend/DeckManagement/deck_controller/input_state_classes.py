# The state persistence rules (which state an input opens on, and what its page keeps) live in input_state.py, its module docstring being the spec; this file holds the state CLASS family.
"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

The content half of a controller input. ControllerInputState and its three
subclasses own what an input shows and does: media, labels, layout and
background, plus the action dispatch that turns a physical event into plugin
callbacks. The hardware-facing half, ControllerInput and its subclasses, lives
in inputs.py and imports these back. An input keeps one state object per
configured state index and delegates to whichever is current.

Nothing here runs on a thread it owns. The HID reader delivers events, the
media thread drives on_media_player_tick, plugin callbacks arrive on the
action pool, and page loads arrive on the loader pool. Nothing here writes to
the deck either. A paint is encoded and enqueued for the media thread, which
is the sole writer.
"""
import os
import threading
import time

from PIL import Image, ImageEnhance, ImageOps
from loguru import logger as log

from src.backend.DeckManagement.HelperMethods import is_video
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent
from src.backend.DeckManagement.Subclasses.ActionPermissionManager import ActionPermissionManager
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo
from src.backend.DeckManagement.deck_controller.cover_cache import CoveredComposite
from src.backend.DeckManagement.deck_controller.gif_pipeline import GifBackground, GifBudgetExceeded, KeyGIF
from src.backend.DeckManagement.deck_controller.label_engine import BackgroundManager, LabelManager, LayoutManager
from src.backend.PageManagement.Page import ActionOutdated, NoActionHolderFound
from src.backend.PluginManager.ActionCore import ActionCore
from src.backend import timer_wheel
from src.backend import ui_port

import globals as gl

from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future

    from src.backend.DeckManagement.deck_controller.inputs import (
        ControllerDial,
        ControllerInput,
        ControllerKey,
        ControllerTouchScreen,
    )


class ControllerInputState:
    def __init__(self, controller_input: "ControllerInput[Any]", state: int):
        self.controller_input = controller_input
        self.deck_controller = controller_input.deck_controller
        self.state = state
        self._overlay: Image.Image | None = None
        self.hide_overlay_timer: "timer_wheel.TimerHandle | None" = None

        # True while this state's on_tick is still running. The next tick is
        # dropped and not queued. See own_actions_tick_threaded.
        self._tick_running: bool = False
        self._tick_started_at: float = 0.0
        self._tick_stuck_warned: bool = False

        # managers
        self.layout_manager = LayoutManager(self.controller_input)
        self.label_manager = LabelManager(self.controller_input)
        self.background_manager = BackgroundManager(self.controller_input)

        self.action_permission_manager = ActionPermissionManager(self)

    def __int__(self) -> int:
        return self.state
    
    def ready(self) -> None:
        pass

    def stop_overlay_timer(self) -> None:
        if self.hide_overlay_timer is not None:
            self.hide_overlay_timer.cancel()
            self.hide_overlay_timer = None

    def show_overlay(self, image: Image.Image, duration: int = -1) -> None:
        """
        duration: -1 for infinite
        """
        if duration == 0:
            self.stop_overlay_timer()
            self._overlay = None
            self.update()
        elif duration > 0:
            # Cancel any in-flight hide timer first, so a repeated overlay
            # does not orphan its thread.
            self.stop_overlay_timer()
            self._overlay = image
            self.update()
            self.hide_overlay_timer = timer_wheel.schedule(duration, self.hide_error, name="OverlayHideTimer")
        else:
            self._overlay = image
            self.update()

    def hide_overlay(self) -> None:
        # Set None, not False. The tile-passthrough fast path in
        # ControllerKey.get_current_image tests state._overlay is None.
        self._overlay = None
        self.update()

    def show_error(self, duration: int = -1) -> None:
        error_img = Image.open(os.path.join("Assets", "images", "error.png"))
        self.show_overlay(error_img, duration=duration)

    def hide_error(self) -> None:
        self.hide_overlay()

    def close_resources(self) -> None:
        pass

    def clear(self) -> None:
        """Reset this state to blank for a fresh page load: release its media
        and drop its page-owned labels, layout and background colour.

        ControllerInput.clear() drives this through get_active_state(), so the
        declaration here keeps the call checkable against the base type. Each
        state class overrides it; the three do, and none reaches this body."""
        raise NotImplementedError

    def get_own_actions(self) -> list["ActionCore | NoActionHolderFound | ActionOutdated"]:
        # The page's action table holds placeholders next to live actions: a
        # NoActionHolderFound for a missing plugin and an ActionOutdated for an
        # incompatible one. Callers isinstance-filter for what they drive.
        if not self.deck_controller.get_alive(): return []
        # Snapshot once and use the snapshot throughout. Other threads null
        # or swap active_page, from close() and from load_page, so a re-read
        # of the live attribute after the None check races that window and
        # raises AttributeError out of every own_actions_ caller.
        active_page = self.deck_controller.active_page
        if active_page is None:
            return []
        return active_page.get_all_actions_for_input(self.controller_input.identifier, self.state)

    def update(self) -> None:
        if self.controller_input.state == self.state:
            self.controller_input.update()
    
    def own_actions_update(self) -> None:
        for action in self.get_own_actions():
            if not isinstance(action, ActionCore):
                continue
            # Gate on ready_finished, not on ready_called. The default
            # on_update calls on_ready for compatibility, so a dispatch here
            # during initialization runs a second on_ready beside the pool's
            # first one, which duplicates backend processes. A skip loses
            # nothing, because the initial ready sequence ends with its own
            # on_update.
            if not action.on_ready_finished:
                continue
            action.on_update()

    @log.catch
    def own_actions_tick(self) -> None:
        for action in self.get_own_actions():
            if not isinstance(action, ActionCore):
                continue
            # on_ready_called is true from schedule time, so a tick must wait
            # for on_ready to finish.
            if not action.on_ready_finished:
                continue
            action.on_tick()

    @log.catch
    def own_actions_event_callback(self, event: InputEvent | None, data: dict[str, Any] | None = None, show_notifications: bool = False, actions: list[Any] | None = None) -> None:
        # actions lets the caller pin the dispatch to a list resolved
        # earlier, such as the DOWN-time gesture snapshot of ControllerKey. By
        # default it resolves here, when the pool worker runs, which reads
        # deck_controller.active_page and so tracks any page swap between the
        # event and this dispatch.
        if actions is None:
            actions = self.get_own_actions()
        for action in actions:
            plugin_manager = gl.plugin_manager
            if isinstance(action, ActionOutdated):
                if show_notifications and plugin_manager is not None:
                    plugin_id = plugin_manager.get_plugin_id_from_action_id(action.id)
                    if plugin_id is not None:
                        ui_port.get().notify_plugin_problem(plugin_id, "outdated")
                continue
            if isinstance(action, NoActionHolderFound):
                if show_notifications and plugin_manager is not None:
                    plugin_id = plugin_manager.get_plugin_id_from_action_id(action.id)
                    if plugin_id is not None:
                        ui_port.get().notify_plugin_problem(plugin_id, "missing")
                continue

            # parsed_event = event
            # if action.allow_event_configuration:
                # parsed_event = action.event_manager.get_event_assigner_for_event(event)

            if event is None:
                continue

            if not isinstance(action, ActionCore):
                continue

            # A pinned snapshot, the DOWN-time gesture list of ControllerKey,
            # can outlive its page's cache entry. The key handler's hold on
            # the pressed page ends when the DOWN callback returns, not at
            # gesture end, so a mid-hold eviction, a remove_page or a
            # reload-diff can run ActionCore.teardown on a snapshot member
            # while its UP is still owed. Never dispatch into a torn-down
            # action. _cleaned_up is the idempotency marker of clean_up(), set
            # under _cleanup_lock. The lock-free read here is benign. At
            # worst one event reaches an action during teardown, the same
            # envelope live resolution always had.
            if getattr(action, "_cleaned_up", False):
                continue

            # Isolate each action. The method-level @log.catch aborts this
            # whole loop at the first raiser and starves every later action in
            # the list of its event.
            try:
                action._raw_event_callback(event, data)
            except Exception:
                log.opt(exception=True).error(
                    f"Action {getattr(action, 'action_id', action)} raised handling {event}"
                )

    def _submit_action_callback(self, fn: "Callable[..., None]", *args: object) -> "Future[None] | None":
        """Route an action callback through the deck's bounded thread pool.
        Returns the Future, or None when the pool cannot take the work."""
        executor = getattr(self.deck_controller, "action_executor", None)
        if executor is None:
            return None
        try:
            future: "Future[None] | None" = executor.submit(fn, *args)
        except RuntimeError as error:
            log.warning(f"The action pool refused a callback for {self.controller_input.identifier}: {error!r}. A pool that is shutting down returns None instead, so this one is live and out of threads, and this input loses the update.")
            return None
        if future is None:
            return None
        future.add_done_callback(self._log_callback_exception)
        return future

    def own_actions_update_threaded(self) -> None:
        self._submit_action_callback(self.own_actions_update)

    def own_actions_tick_threaded(self) -> None:
        # Drop this tick, and do not queue it, while the previous one still
        # runs, so a slow plugin on_tick() cannot pile up callbacks.
        if self._tick_running:
            if not self._tick_stuck_warned and time.monotonic() - self._tick_started_at > 10.0:
                self._tick_stuck_warned = True
                log.warning(f"on_tick for {self.controller_input.identifier} has been running >10s; this input's updates are paused until it returns")
            return
        # Submit only what the worker would run: it skips every entry that is
        # not an ActionCore, and the pool it runs on retires no worker. This
        # sits after the stuck-tick warning and before the flag a return strands.
        try:
            has_action = any(isinstance(a, ActionCore) for a in self.get_own_actions())
        except Exception:
            has_action = True  # the worker repeats the read; log.catch reports it
        if not has_action: return
        self._tick_running = True
        self._tick_stuck_warned = False
        self._tick_started_at = time.monotonic()
        # The flag is armed above and the completion callback below is what
        # disarms it, so every path between the two owns the flag. A raise in
        # that window, or a submit that took no worker, leaves this input
        # armed for good and silences it until the page reloads. Whoever
        # reaches the completion callback hands the flag to it; nobody else
        # leaves the window with the flag still set.
        handed_over = False
        try:
            future = self._submit_action_callback(self.own_actions_tick)
            if future is not None:
                future.add_done_callback(self._on_tick_done)
                handed_over = True
        finally:
            if not handed_over:
                self._tick_running = False

    def _on_tick_done(self, _future: "Future[None]") -> None:
        self._tick_running = False

    def _log_callback_exception(self, future: "Future[None]") -> None:
        try:
            exc = future.exception()
        except Exception:
            return
        if exc is not None:
            log.opt(exception=exc).error(f"Action callback for {self.controller_input.identifier} raised")

    def own_actions_event_callback_threaded(self, event: InputEvent, data: dict[str, Any] | None = None, show_notifications: bool = False, actions: list[Any] | None = None) -> None:
        self._submit_action_callback(self.own_actions_event_callback, event, data, show_notifications, actions)

    def set_image(self, image: "InputImage | None", /, update: bool = True) -> None:
        """Attach this state's still media, or clear it with None.

        This is the media protocol that ActionCore.set_media drives an input
        state through. ControllerKeyState and ControllerDialState implement
        it, and ControllerTouchScreenState does not. Nothing reaches this base
        body, because ActionCore.set_media returns early for any identifier
        outside Input.Key and Input.Dial. The declaration exists so the
        protocol is checkable at that call site. A touchscreen media route
        must override it rather than inherit this.
        """
        raise NotImplementedError

    def set_video(self, video: "InputVideo | KeyGIF", /) -> None:
        """Attach this state's animated media. See set_image for who
        implements it and why the base body is unreachable. It accepts both
        providers; the .gif route builds a KeyGIF and every other route builds
        an InputVideo."""
        raise NotImplementedError

    def remove_media(self) -> None:
        page = self.controller_input.deck_controller.active_page
        if page is None:
            return

        # A None path clears the media.
        page.set_media_path(identifier=self.controller_input.identifier, state=self.state, path=None)

        self.update()


class ControllerTouchScreenState(ControllerInputState):
    # set_current_image() creates this lazily, so close_resources() guards it
    # with getattr; a state closed before its first render never has one. It
    # is declared and not assigned, so that contract stands at runtime.
    current_image: Image.Image | None

    def __init__(self, controller_touch: "ControllerTouchScreen", state: int):
        super().__init__(controller_touch, state)

        self.controller_touch = controller_touch

        # (key, fitted-image-or-None) for _get_fitted_background_image.
        self._fitted_background_cache: "tuple[tuple[str, float, tuple[int, int], float] | None, Image.Image | None]" = (None, None)

        # Playback state for a video configured as this touchscreen's
        # background. It is an InputVideo over a strip-sized shared frame
        # cache, which the media tick advances.
        # _get_background_video_frame manages it, and get_current_image
        # releases it once the background stops being a video. The lock
        # covers the create and the release, because a composite can run on
        # the media thread and on a load or UI thread at the same time. The
        # .gif route builds a GifBackground and every other route builds an
        # InputVideo. Both answer the get_next_frame, close and video_path
        # surface that _get_background_video_frame drives them through.
        self.background_video: "InputVideo | GifBackground | None" = None
        self._background_video_failed: str | None = None
        self._background_video_lock = threading.Lock()
        # The display-saturation factor that background_video was built at,
        # and that acquired its shared tile cache. The keep-check in
        # _get_background_video_frame uses it. The factor bakes into the cache
        # at construction and set_playback never revisits it, so a reuse of
        # the video across a saturation change keeps serving frames enhanced
        # at the old factor.
        self._background_video_saturation: float | None = None
        # Timestamp gate for the fps render cap in on_media_player_tick.
        self._last_background_video_render: float = 0.0

        # Media set on this touchscreen through set_image/set_video, the
        # touchscreen twin of the key and dial slots. get_current_image
        # composites it over the strip background, beneath the dial overlays,
        # and close_resources releases it. ActionCore.set_media returns early
        # for a touchscreen, so a plugin that drives these directly owns them.
        self.image: "InputImage | None" = None
        self.video: "InputVideo | KeyGIF | None" = None
        self.media_owner_action: "ActionCore | None" = None

    def set_current_image(self, image: Image.Image) -> None:
        self.current_image = image

        self.update()

    def set_image(self, image: "InputImage | None", update: bool = True) -> None:
        """Attach this touchscreen's still media, or clear it with None. This
        matches the key and dial slots. get_current_image composites the media
        over the strip background."""
        if self.image is not None:
            self.image.close()
        if self.video is not None:
            # A switch from a video to an image without a close leaks the
            # video's capture and its shared-cache attachment.
            self.video.close()
        self.image = image
        self.video = None
        self.media_owner_action = None
        if update:
            self.update()

    def set_video(self, video: "InputVideo | KeyGIF") -> None:
        """Attach this touchscreen's animated media, matching the key and dial
        slots. Both providers expose get_next_frame and close."""
        if self.video is not None:
            self.video.close()
        self.video = video
        if self.image is not None:
            self.image.close()
        self.image = None
        self.media_owner_action = None

    def _get_fitted_background_image(self, path: str, size: tuple[int, int]) -> Image.Image | None:
        # Decode and fit once per (path, mtime, size, saturation), then
        # cache. This runs on every composite, 30 times a second while a
        # background video plays, and a failed decode must not log per frame.
        # A video takes the playback path in _get_background_video_frame.
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None

        # The saturation boost bakes into the cached fitted image, on the
        # same one-time contract BackgroundImage uses for the key grid, so
        # the factor is part of the cache key. A saturation change must not
        # keep serving the stale enhancement. The value rounds to the
        # persisted 2-decimal precision, because set_display_saturation stores
        # round(v, 2), so an unrounded caller cannot mint a near-duplicate
        # float key that misses the cache on every composite.
        saturation = round(self.controller_touch.deck_controller.get_display_saturation(), 2)

        key = (path, mtime, size, saturation)
        cached_key, cached_image = self._fitted_background_cache
        if cached_key == key:
            # A caller pastes dial images onto the returned image in place,
            # so hand out a copy and keep the cached one clean.
            return cached_image.copy() if cached_image is not None else None

        image = None
        try:
            with Image.open(path) as img:
                image = img.copy()
        except Exception as e:
            log.error(f"Error loading touchscreen background image {path}: {e}")

        fitted = None
        if image is not None:
            fitted = ImageOps.fit(image, size, Image.Resampling.LANCZOS).convert("RGBA")
            if abs(saturation - 1.0) > 0.001:
                fitted = ImageEnhance.Color(fitted).enhance(saturation)

        # The cache also holds a failure, so a bad file logs once and not on
        # every frame.
        self._fitted_background_cache = (key, fitted)
        return fitted.copy() if fitted is not None else None

    def _get_background_video_frame(self, path: str, fps: int = 30, loop: bool = True) -> Image.Image | None:
        # The InputVideo owns a strip-sized shared frame cache. It picks
        # frames by wall clock, clamps a gap, and runs at the source fps, so
        # neither the composite rate nor the fps setting changes playback
        # speed. fps and loop come from the page's background settings. loop
        # wraps playback, and fps only caps the strip's re-render rate; see
        # ControllerTouchScreen.on_media_player_tick.
        with self._background_video_lock:
            if path == self._background_video_failed:
                return None

            # The saturation is part of the keep-check. The factor bakes into
            # the video's shared tile cache at construction, and set_playback
            # only updates fps and loop, so a factor change forces a rebuild
            # for the same path. The key-grid BackgroundVideo keep-check and
            # the fitted-image cache key one method up work the same way, and
            # this uses the same 0.001 tolerance.
            saturation = self.controller_touch.deck_controller.get_display_saturation()

            video = self.background_video
            # Both reads stay inside the short-circuit.
            # _background_video_saturation exists only once a video attaches;
            # the keepcheck scenario builds this state through __new__ and
            # sets only the attributes the no-video path touches.
            if (video is None or video.video_path != path
                    or self._background_video_saturation is None
                    or abs(self._background_video_saturation - saturation) > 0.001):
                if video is not None:
                    video.close()
                video = None
                if os.path.splitext(path)[1].lower() == ".gif":
                    # A .gif goes to the PIL provider. It fits each frame to
                    # exactly the strip size, because the alpha_composite in
                    # get_current_image needs same-size RGBA, and alpha and
                    # the per-frame delays survive. A budget or decode failure
                    # falls back to the opaque source-fps InputVideo path
                    # below, as the deck-background route in
                    # prebuild_from_path does.
                    try:
                        video = GifBackground(
                            self.controller_touch.deck_controller, path,
                            loop=loop, fps=fps,
                            canvas_size=self.controller_touch.get_screen_dimensions(),
                        )
                    except GifBudgetExceeded as e:
                        log.warning(f"GIF strip background over budget, falling back to the opaque cv2 path: {e}")
                    except Exception:
                        log.opt(exception=True).warning(f"GIF strip background decode failed, falling back to the opaque cv2 path: {path}")
                if video is None:
                    video = InputVideo(
                        controller_input=self.controller_touch,
                        video_path=path,
                        fps=fps,
                        loop=loop,
                        natural_speed=True,
                    )
                self.background_video = video
                self._background_video_saturation = saturation
            else:
                video.set_playback(fps=fps, loop=loop)

            frame = video.get_next_frame()
            if frame is None:
                # n_frames is known from construction, because the reader
                # opens its source eagerly, so a value of 0 or less names a
                # bad file. Fail it once instead of a retry and a log per
                # frame. A transient miss on a healthy file retries on the
                # next tick. This applies to InputVideo only. GifBackground
                # has no video_cache, because a bad GIF already fell back at
                # construction, and a None after close is transient and
                # self-heals on the rebuild above.
                if isinstance(video, InputVideo) and (video.video_cache is None or video.video_cache.n_frames <= 0):
                    log.error(f"Could not decode touchscreen background video {path}")
                    video.close()
                    self.background_video = None
                    self._background_video_failed = path
                return None

            # convert() copies. A caller pastes dial images onto the returned
            # composite in place, and the cached payload must stay clean.
            return frame.convert("RGBA")

    def _release_background_video(self) -> None:
        with self._background_video_lock:
            if self.background_video is not None:
                self.background_video.close()
                self.background_video = None

    def tick_background_video(self, media_player_fps: int) -> bool:
        """Whether the strip needs a re-composite for the background video this
        media tick. The read of background_video and the read-and-write of the
        fps-cap timestamp both run under _background_video_lock, so a
        concurrent _release_background_video() cannot null the video between the
        check and the timestamp, and two ticks cannot tear the timestamp."""
        with self._background_video_lock:
            bg_video = self.background_video
            if bg_video is None:
                return False
            # The configured fps is a render cap. The playback position follows
            # the wall clock at the source's native fps, so a skipped tick here
            # drops a frame and does not slow the video down.
            cap_fps = min(media_player_fps, max(1, bg_video.fps or 30))
            now = time.time()
            if now - self._last_background_video_render < 1.0 / cap_fps:
                return False
            self._last_background_video_render = now
            return True

    def get_current_image(self) -> Image.Image:
        screen_width, screen_height = self.controller_touch.get_screen_dimensions()

        # Start with the background image, when one is set.
        background: Image.Image | None = None
        # Snapshot it and guard it. load_page(None) and close() null
        # active_page from other threads while the writer composites, and a
        # blank strip is the only sensible frame then.
        active_page = self.controller_touch.deck_controller.active_page
        if active_page is None:
            return Image.new("RGBA", (screen_width, screen_height), (0, 0, 0, 255))
        background_image_path = active_page.get_background_image(
            identifier=self.controller_touch.identifier, 
            state=self.state
        )
        
        has_video_background = bool(
            background_image_path
            and os.path.isfile(background_image_path)
            and is_video(background_image_path)
        )
        if not has_video_background:
            # The background stopped being a video, because something cleared
            # it or swapped an image in. Detach its frame cache so the tick
            # predicate goes quiet.
            self._release_background_video()

        if background_image_path and os.path.isfile(background_image_path):
            if has_video_background:
                background = self._get_background_video_frame(
                    background_image_path,
                    fps=active_page.get_background_fps(identifier=self.controller_touch.identifier, state=self.state),
                    loop=active_page.get_background_loop(identifier=self.controller_touch.identifier, state=self.state),
                )
            else:
                background = self._get_fitted_background_image(background_image_path, (screen_width, screen_height))

        # A deck background extended onto the strip is the bottom layer. An
        # explicit per-touchscreen background image takes precedence over it.
        if background is None:
            deck_background = self.controller_touch.deck_controller.background.get_touchscreen_image()
            if deck_background is not None:
                # convert() copies, because the slice is shared and a caller
                # pastes dial images onto the returned image in place. It also
                # normalizes an RGB video-frame slice for the
                # alpha_composite below.
                background = deck_background.convert("RGBA")

        # Take the background color from the state's background_manager.
        background_color = self.background_manager.get_composed_color()
        
        # With no background image, start empty or colored.
        if background is None:
            # A background color with alpha below 255 starts transparent.
            if background_color[-1] < 255:
                background = self.controller_touch.generate_empty_image()
            
            # A background color with alpha above 0 gets a colored canvas.
            if background_color[-1] > 0:
                background_color_img = Image.new("RGBA", (screen_width, screen_height), color=tuple(background_color))
                
                if background is None:
                    # Use the color as the only background. This happens at
                    # a background color alpha of 255.
                    background = background_color_img
                else:
                    # Paste the color onto the transparent background.
                    background.paste(background_color_img, (0, 0), background_color_img)
            
            # With no background color, use the empty image.
            if background is None:
                background = self.controller_touch.generate_empty_image()
        else:
            # A background image exists, so apply the color overlay if set.
            if background_color[-1] > 0:
                background_color_img = Image.new("RGBA", (screen_width, screen_height), color=tuple(background_color))
                # Blend the color over the image.
                background = Image.alpha_composite(background, background_color_img)

        # Composite this touchscreen's own media over the background, beneath
        # the dial overlays. A plugin attaches it through set_image/set_video.
        # A None frame leaves the background unchanged.
        media_frame: Image.Image | None = None
        if self.video is not None:
            media_frame = self.video.get_next_frame()
        elif self.image is not None:
            media_frame = self.image.image
        if media_frame is not None:
            background = self.layout_manager.add_image_to_background(media_frame, background)

        # Paste the dial images on top of the background.
        for dial in self.controller_touch.deck_controller.inputs[Input.Dial]:
            state = dial.get_active_state()
            image_area = self.controller_touch.get_dial_image_area(dial.identifier)
            dial_image = state.get_rendered_touch_image()

            background.paste(dial_image, image_area, dial_image)

        return background


    def update(self) -> None:
        if self.controller_touch.get_active_state() is self:
            self.controller_touch.update()

    

    def set_dial_image(self, identifier: Input.Dial, image: Image.Image, update: bool = True) -> None:
        # Disabled. An implementation composites the image into
        # get_dial_image_area(identifier) over get_empty_dial_image() and
        # calls update().
        return


    def clear(self) -> None:
        # Release any plugin-set media, as the key and dial clear() do, then
        # reset the mirror image.
        if self.video is not None:
            self.video.close()
        if self.image is not None:
            self.image.close()
        self.image = None
        self.video = None
        self.media_owner_action = None
        self.set_current_image(self.controller_touch.generate_empty_image())

    def close_resources(self) -> None:
        # Only set_current_image() sets current_image. A touchscreen state
        # closed before its first render never gets one, such as a
        # screensaver-stash sweep of a page that never painted, or a fresh
        # state right after create_n_states(). An unconditional dereference
        # raises AttributeError, so the getattr and the None guard make this
        # safe to call any number of times.
        current_image = getattr(self, "current_image", None)
        if current_image is not None:
            current_image.close()
        self.current_image = None
        # Release plugin-set media, as ControllerKeyState and
        # ControllerDialState release theirs.
        if self.image is not None:
            self.image.close()
            self.image = None
        if self.video is not None:
            self.video.close()
            self.video = None
        self.media_owner_action = None
        # Detach the background video's shared-cache reader, as
        # ControllerKeyState and ControllerDialState release their videos.
        self._release_background_video()

class ControllerDialState(ControllerInputState):
    def __init__(self, dial: "ControllerDial", state: int):
        self.dial = dial

        self.image: InputImage | None = None
        # Typed to the provider union of the base protocol; see
        # ControllerInputState.set_video. Only the key route builds a KeyGIF,
        # because the .gif branch of ActionCore guards on ControllerKey, but
        # the slot and the render path handle either provider.
        self.video: "InputVideo | KeyGIF | None" = None

        self.touch_image: Image.Image | None = None

        # The ActionCore that set the current image or video through
        # set_media(), or None when the page or the user owns the media. Every
        # other media writer resets it to None, and set_media() stamps it again
        # after the write. ControllerDial.load_from_input_dict uses it to carry
        # action-owned media across the create_n_states wipe, as the key does.
        self.media_owner_action: "ActionCore | None" = None

        super().__init__(dial, state)

    def set_image(self, image: "InputImage | None", update: bool = True) -> None:
        if self.image is not None:
            self.image.close()

        self.image = image
        self.media_owner_action = None

        if update:
            self.update()

    def set_video(self, video: "InputVideo | KeyGIF") -> None:
        if self.video is not None:
            self.video.close()

        self.video = video
        self.media_owner_action = None

    def clear(self) -> None:
        # The dial twin of ControllerKeyState.clear(): release action-owned
        # media and reset the page-owned layers so a fresh page load starts
        # blank. ControllerInput.clear() drives it.
        if self.video is not None:
            # Close the video here; a bare drop leaks its capture.
            self.video.close()
        if self.image is not None:
            self.image.close()
        self.image = None
        self.video = None
        self.media_owner_action = None
        self.label_manager.clear_labels()
        self.layout_manager.clear()
        self.background_manager.set_page_color(None)

    def close_resources(self) -> None:
        # The base class default does nothing, so this override releases a
        # dial's InputImage and InputVideo from
        # ControllerInput.close_resources(), as
        # ControllerKeyState.close_resources does for a key.
        if self.image is not None:
            self.image.close()
            self.image = None
        if self.video is not None:
            self.video.close()
            self.video = None
        self.media_owner_action = None


    def get_rendered_touch_image(self) -> Image.Image:
        touch_screen = self.dial.get_touch_screen()
        if touch_screen is None:
            # A dial without a strip has nowhere to render. get_image_size()
            # reports (0, 0) for exactly this deck shape.
            return Image.new("RGBA", self.dial.get_image_size(), (0, 0, 0, 0))

        background: Image.Image | None = None

        background_color = self.background_manager.get_composed_color()

        if background_color[-1] < 255:
            background = touch_screen.get_empty_dial_image()
        if background_color[-1] > 0:
            background_color_img = Image.new("RGBA", self.dial.get_image_size(), color=tuple(background_color))

            if background is None:
                # Use the color as the only background. This happens at a
                # background color alpha of 255.
                background = background_color_img
            else:
                background.paste(background_color_img, (0, 0), background_color_img)
        

        if background is None:
            # Unreachable, because every alpha from 0 to 255 satisfies one of
            # the two branches above. ControllerKey.get_current_image keeps
            # the same fallback, so the composite below always has a canvas.
            background = touch_screen.get_empty_dial_image()

        image: Image.Image | None = None
        if self.video is not None:
            image = self.video.get_next_frame()
        elif self.image is not None:
            image = self.image.image

        # rotation = self.deck_controller.get_deck_settings().get("rotation", {}).get("value", 0)

        composed = self.layout_manager.add_image_to_background(image, background)
        return self.label_manager.add_labels_to_image(composed)

class ControllerKeyState(ControllerInputState):
    def __init__(self, controller_key: "ControllerKey", state: int):
        super().__init__(controller_key, state)

        self.key_image: InputImage | None = None
        # A .gif key media builds a KeyGIF and every other media builds an
        # InputVideo. Both expose the get_raw_image and close surface that the
        # key paint path and close_resources drive them through.
        self.key_video: "InputVideo | KeyGIF | None" = None
        # The composite this state last produced, kept only while its own
        # foreground hides the background and nothing else about it moved.
        self.cover_cache = CoveredComposite()
        # The ActionCore that set the current key_image or key_video through
        # set_media(), or None when the page or the user owns the media. Every
        # other media writer resets it to None, and set_media() stamps it
        # again after the write. ControllerKey.load_from_input_dict uses it to
        # carry action-owned media across the create_n_states wipe.
        self.media_owner_action: "ActionCore | None" = None

    def close_resources(self) -> None:
        if self.key_image is not None:
            self.key_image.close()
            self.key_image = None
        if self.key_video is not None:
            self.key_video.close()
            self.key_video = None
        self.media_owner_action = None
        self.cover_cache.invalidate()

    def set_image(self, key_image: "InputImage | None", update: bool = True) -> None:
        if self.key_image is not None:
            self.key_image.close()
        if self.key_video is not None:
            # A drop of key_video here without a close leaks its tile-cache
            # registry attachment and its VideoCapture on every switch from a
            # video to an image.
            self.key_video.close()

        self.key_image = key_image
        self.key_video = None
        self.media_owner_action = None
        # The kept composite belongs to the media that just went away. A paint
        # would drop it, but a key that keeps no media stops reaching the paint
        # path that does: over a background video a bare key is served straight
        # from the frame identity instead.
        self.cover_cache.invalidate()

        if update:
            self.update()

    def set_video(self, key_video: "InputVideo | KeyGIF") -> None:
        if self.key_video is not None:
            # Close the previous video before this one overwrites it.
            self.key_video.close()
        self.key_video = key_video
        if self.key_image is not None:
            self.key_image.close()
        self.key_image = None
        self.media_owner_action = None
        self.cover_cache.invalidate()

    def clear(self) -> None:
        if self.key_video is not None:
            # Close key_video here; a bare drop leaks its capture.
            self.key_video.close()
        self.key_image = None
        self.key_video = None
        self.media_owner_action = None
        self.cover_cache.invalidate()
        self.label_manager.clear_labels()
        self.layout_manager.clear()
        self.background_manager.set_page_color(None)

