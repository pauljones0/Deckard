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

The controller inputs are the objects a deck's keys, dials and touchscreen
are made of. This module owns the hardware-facing half, ControllerInput and
its subclasses: the identifier, the event callbacks the HID reader drives, and
the paint path that composites, encodes and hands a frame to the media
thread. The content half, ControllerInputState and its subclasses, lives in
input_state_classes.py and is imported back here. An input keeps one state
object per configured state index and delegates to whichever is current.
StateT keeps that delegation typed, so ControllerKey.get_active_state() gives
a ControllerKeyState with no cast at the call site.

Nothing here runs on a thread it owns. The HID reader delivers key, dial and
touchscreen events, the media thread drives on_media_player_tick, plugin
callbacks arrive on the action pool, and page loads arrive on the loader
pool. The DOWN-time gesture snapshot, the two hashes of a present state and
_states_lock all exist for that, each documented at the code it protects.
Nothing here writes to the deck either. A paint is encoded and enqueued for
the media thread, which is the sole writer.
"""
import os
import threading
import time
from copy import copy

from PIL import Image, ImageDraw
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from loguru import logger as log

from src.backend.DeckManagement.HelperMethods import SVG_RASTER_WIDTH_PX, centered_paste_offset, is_image, is_svg, is_video, svg_to_pil
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent, InputIdentifier
from src.backend.DeckManagement.Media.MediaConfig import MediaConfig
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo
from src.backend.DeckManagement.Subclasses.media_pipeline_profiler import media_prof
from src.backend.DeckManagement.deck_controller import cover_cache, press_look
from src.backend.DeckManagement.deck_controller.gif_pipeline import KeyGIF
from src.backend.DeckManagement.deck_controller.input_state import PersistedState
from src.backend.DeckManagement.deck_controller.input_state_classes import (
    ControllerDialState,
    ControllerInputState,
    ControllerKeyState,
    ControllerTouchScreenState,
)
from src.backend.DeckManagement.deck_controller.native_encode import (
    _encode_key_native,
    _encode_strip_native,
    _encode_tile_native,
)
from src.backend.DeckManagement.deck_controller.paint_protocol import (
    KeyPresentState,
    PresentState,
    TouchscreenPresentState,
)
from src.backend.PageManagement import page_pins
from src.backend.PageManagement.Page import ActionOutdated, NoActionHolderFound, Page
from src.backend.PluginManager.ActionCore import ActionCore
from src.backend import timer_wheel
from src.backend import ui_port
from src.Signals import Signals

import globals as gl

from typing import Any, TYPE_CHECKING, Generic, TypeVar
if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from src.backend.DeckManagement.BetterDeck import BetterDeck
    from threading import Timer

    from src.backend.DeckManagement.deck_controller.controller import DeckController


#: The state class an input owns. Each ControllerInput subclass pins exactly
#: one, so ControllerKey pins ControllerKeyState. That lets the shared state
#: plumbing in the base class stay generic without erasing the subclass's
#: state type at every get_active_state() call.
StateT = TypeVar("StateT", bound=ControllerInputState)

#: The share of a key tile an overlay covers. An overlay marks a transient
#: condition, so it is drawn inside the picture it covers rather than over all
#: of it, and the margin keeps the key's own content readable behind it.
OVERLAY_TILE_FRACTION = 0.75


def _build_page_video_media(controller_input: "ControllerInput[Any]", path: str,
                            media: MediaConfig) -> "InputVideo | KeyGIF":
    """The video media a page load builds for a path, shared by the key and
    dial loaders. A .gif goes to KeyGIF, which keeps the RGBA alpha the cv2
    demuxer drops and honors the per-frame delays and the fps render cap; a
    decode failure falls back to the opaque cv2 path so one bad asset does not
    take the page load down. Page media plays at natural speed, and the dict
    fps is a render cap, not a playback rate."""
    if os.path.splitext(path)[1].lower() == ".gif":
        try:
            return KeyGIF(controller_key=controller_input, gif_path=path,
                          loop=media.loop, fps=media.fps)
        except Exception:
            log.opt(exception=True).warning(
                f"GIF decode failed during page load, falling back to the "
                f"opaque cv2 path: {path}")
    return InputVideo(controller_input=controller_input, video_path=path,
                      loop=media.loop, fps=media.fps, natural_speed=True)


class ControllerInput(Generic[StateT]):
    # What this input's device slot shows, and what is on its way to it. A
    # key and the touchscreen own a slot and assign one in their own
    # __init__, the key narrowing this type because its paint path reads the
    # key index back off it. A dial composites into the touchscreen and owns
    # no slot, so this declaration binds nothing at runtime.
    present_state: PresentState

    # The event a completed hold dispatches into the DOWN-time snapshot. A key
    # and a dial arm a hold timer and each names its own event here, so
    # on_hold_timer_end below serves both. The touchscreen arms no timer and
    # names none. This is a declaration and not an assignment, so an input type
    # that arms a timer without naming its event raises AttributeError instead
    # of sending a key event from a dial.
    HOLD_START_EVENT: InputEvent

    def __init__(self, deck_controller: "DeckController", state_class: type[StateT], identifier: InputIdentifier):
        self.deck_controller = deck_controller
        self.state = 0
        self.hide_error_timer: Timer | None = None
        self.hold_start_timer: "timer_wheel.TimerHandle | None" = None
        self.ControllerStateClass = state_class
        self.identifier: InputIdentifier = identifier
        self.persisted_state = PersistedState(identifier)
        self.media_ticks: int = 0
        # Generation of the content this input holds. A paint tags it at
        # render start, and the write boundary drops that paint once a newer
        # generation supersedes it.
        self.config_gen: int = 0

        # When the in-flight gesture went down, or None outside one. The hold
        # branch of the release compares against it. A key and a dial keep it;
        # the touchscreen dispatches no gesture and leaves it None.
        self.down_start_time: float | None = None

        # DOWN-time gesture snapshot, a (state, actions) pair captured when the
        # input went down, or None outside a gesture. The rest of the gesture,
        # HOLD_START, HOLD_STOP or SHORT_UP, and UP, dispatches to this
        # snapshot, and not to whatever the input resolves to at release time.
        # A ChangePage action on this input swaps active_page, and rebuilds
        # this input's states, synchronously during the DOWN dispatch. Live
        # resolution would then send the UP to the new page's actions, so the
        # old page's actions never see their release and a registered-down
        # latch jams shut, while the new page's actions get a SHORT_UP for a
        # press that was not theirs.
        #
        # It is one attribute and not one per field, so a writer clears it in
        # one atomic store and the hold-timer callback, which can race the UP
        # branch past its cancel(), reads a coherent pair or None and never a
        # torn half. The deck's serialized input-callback path writes it, and
        # so does the cancel_gesture sweep of ScreenSaver.show(), which runs
        # under _load_page_lock after this input left the live input set and
        # can receive no further event.
        self._gesture: "tuple[StateT, list[ActionCore | NoActionHolderFound | ActionOutdated]] | None" = None

        self.is_visual: bool = True

        self.enable_states: bool = True

        # Serializes state-object replacement, from create_n_states during a
        # load, against an action media write from ActionCore.set_media. A
        # paint must land fully before the wipe, so the load's stash and
        # restore carries it over, or fully after it, on the recreated state
        # object. It must never land on a destroyed state.
        self._states_lock = threading.RLock()

        # Serializes one input's compose-to-offer span, so the order paints are
        # composed in is the order they reach the writer.
        #
        # The media tick, the main thread's preview push, a plugin worker after
        # a media or label write, the loader pool and the deck's input callback
        # all paint the same input. Each reads the state, composes a picture
        # and offers it to the writer, whose per-target slot keeps the offer
        # that arrives last. Compose order and offer order are independent
        # without this lock, so a slow compose of an older state can offer
        # after a fast compose of a newer one and take the slot from it. The
        # offer stamps its own hash as in flight while it does, and no later
        # paint corrects it, because nothing about the input has changed. The
        # visible form is a key that stays pressed after the finger left.
        #
        # Lock order: _states_lock, then this, then the writer's slot lock, and
        # never the reverse. Both holders of _states_lock release it before
        # they call update(), and nothing a paint reaches takes it. The writer
        # takes its slot lock inside the offer and releases it before it runs a
        # task. _load_page_lock sits above all of it, held across the paints of
        # a page load and a screensaver flip, and nothing a paint reaches takes
        # that either. A paint takes no device lock: it hands encoded bytes to
        # a slot instead of writing, and the deck reads it makes are lock-free.
        # It blocks on no thread, because the UI mirror converts on the calling
        # thread and arms an idle callback rather than waiting on the main
        # loop. It is re-entrant so a paint that re-enters on its own thread
        # cannot wedge itself; one thread's paints are ordered already.
        self._paint_lock = threading.RLock()

        self.states: dict[int, StateT] = {
            0: self.ControllerStateClass(self, 0),
        }

        self.states[self.state].ready()

    @staticmethod
    def Available_Identifiers(deck: "BetterDeck") -> "Iterable[str]":
        raise AttributeError

    def update(self) -> None:
        pass

    def cancel_gesture(self) -> None:
        """End an in-flight gesture without a dispatch of its release events.

        It drops the DOWN-time snapshot, the gesture clock and the pending hold
        timer. It serves the paths where the physical release can never reach
        this input. ScreenSaver.show() confiscates the whole input set mid-hold,
        and the release then lands on the replacement input and is swallowed.
        Without this call, the hold timer stays armed, fires HOLD_START into the
        pinned snapshot after the finger left, and pins that snapshot's action
        objects forever.

        An input type that dispatches no gesture keeps the base state this
        clears, so the sweeps that call it need no per-type test.
        """
        self.down_start_time = None
        self.stop_hold_timer()
        self._gesture = None

    def on_hold_timer_end(self) -> None:
        """Dispatch this input's hold-start event into the DOWN-time snapshot.

        HOLD_START_EVENT names the event, so a key sends the key event and a
        dial the dial one. An input type that arms no hold timer never reaches
        the dispatch: its snapshot is None and the guard below returns first.
        """
        gesture = self._gesture
        if gesture is None:
            # The gesture already ended. The UP branch or a cancel_gesture()
            # raced this callback past the timer's cancel(). A late
            # HOLD_START must not fire, and must never live-resolve onto
            # whatever page is active now.
            return
        gesture_state, gesture_actions = gesture
        gesture_state.own_actions_event_callback_threaded(
            event=self.HOLD_START_EVENT,
            actions=gesture_actions,
        )

    def event_callback(self, *args: Any, **kwargs: Any) -> None:
        pass

    def start_hold_timer(self) -> None:
        self.stop_hold_timer()

        self.hold_start_timer = timer_wheel.schedule(self.deck_controller.hold_time, self.on_hold_timer_end, name="HoldTimer")

    def stop_hold_timer(self) -> None:
        if self.hold_start_timer is None:
            return
        
        self.hold_start_timer.cancel()
        self.hold_start_timer = None

    def create_n_states(self, n: int) -> None:
        if not self.enable_states:
            n = 1

        for state in self.states.values():
            state.close_resources()
        self.states.clear()

        for i in range(n):
            self.states[i] = self.ControllerStateClass(self, i)

    def load_from_page(self, page: Page) -> None:
        config = self.identifier.get_config(page)
        self.load_from_input_dict(config, page=page)

    def _recreate_states_keeping_action_media(self, n_states: int) -> "set[int]":
        """Rebuild this input's states for a page load, carrying action-owned
        media across the rebuild. It answers the state indexes whose media it
        put back, which the caller must not then reset the action layout on.

        create_n_states destroys every state object and closes its action-set
        media. Only on_update() repaints afterwards, and an action that dedups
        there never does, so the input settles permanently blank. So the media
        and its action layout come off before the wipe, and go back on only
        where the same action object still drives the recreated state. A
        same-page reload reuses the action objects, so the identity matches and
        the paint returns. A cross-page load builds new ones, so the identity
        differs, the media closes and nothing bleeds.

        Under _states_lock a concurrent set_media paint lands fully before the
        wipe, where the stash carries it over, or fully after it on the
        recreated state, and never on a destroyed state object.
        """
        with self._states_lock:
            stashed: "dict[int, tuple[ActionCore, InputImage | None, InputVideo | KeyGIF | None, ImageLayout]]" = {}
            for index, old_state in self.states.items():
                owner = old_state.media_owner_action
                if owner is None:
                    continue
                image, video = old_state.detach_action_media()
                if image is None and video is None:
                    continue
                stashed[index] = (owner, image, video,
                                  old_state.layout_manager.action_layout)
                old_state.media_owner_action = None

            self.create_n_states(max(1, n_states))

            restored: set[int] = set()
            for index, (owner, image, video, action_layout) in stashed.items():
                new_state = self.states.get(index)
                if new_state is not None and owner in new_state.get_own_actions():
                    new_state.attach_action_media(image, video)
                    new_state.media_owner_action = owner
                    new_state.layout_manager.set_action_layout(action_layout, update=False)
                    restored.add(index)
                else:
                    if image is not None:
                        image.close()
                    if video is not None:
                        video.close()
            return restored

    def _tick_animation_clocks(self) -> "tuple[StateT, bool]":
        """Advance this input's tick count and its rolling labels, and answer
        the state that tick read beside whether a scroll offset visibly moved.

        A rolling label advances here, on the tick, whether or not anything
        else forces a repaint, because rendering is pure. The input then
        re-renders only when a scroll offset moved, instead of producing thirty
        frames a second for the hash de-dup to discard.

        The state comes back with the answer so the caller judges the rest of
        its tick against the object this advanced. A page load that lands
        mid-tick would otherwise let the two read different states.
        """
        self.media_ticks += 1
        state = self.get_active_state()
        if state.label_manager.get_has_scroll_labels():
            return state, state.label_manager.tick_scroll_labels()
        return state, False

    def get_current_image(self) -> "Image.Image":
        """The input's current composition. The key and touchscreen inputs
        implement it; a dial composes into the touchscreen strip and has no
        image of its own. The UI mirror reads it on map."""
        raise NotImplementedError

    def load_from_input_dict(self, input_dict: "dict[str, Any]", update: bool = True, page: "Page | None" = None, *,
                             still_current: "Callable[[], bool] | None" = None) -> None:
        """still_current, when given, is re-asked at every mutation boundary.
        A load whose page was superseded mid-flight must stop mutating this
        shared input: the pool's deadline abandons the wait, not the task, so
        a plugin callback that blocked past it resumes on an input the next
        page has already re-stamped."""
        pass

    def add_new_state(self, switch: bool = True) -> None:
        if not self.enable_states:
            if len(self.states) >= 1:
                return
            
        page = self.deck_controller.active_page
        if page is None:
            # No page is loaded, at boot or during teardown, so there is
            # nothing to persist the new state onto.
            return
        d = self.identifier.get_config(page)

        self.states[len(self.states)] = self.ControllerStateClass(self, len(self.states))
        for state in self.states:
            d["states"].setdefault(str(state), {})

        page.save()

        self.update_state_switcher()

        if switch:
            log.info(f"Switching to state: {len(self.states)-1}")
            self.set_state(len(self.states)-1)

    def remove_state(self, state: int) -> None:
        page = self.deck_controller.active_page
        if page is None:
            # As in add_new_state, no page means nothing to edit.
            return
        d = self.identifier.get_config(page)

        if str(state) in d["states"]:
            d["states"].pop(str(state))

        old_loaded_state = int(self.state)

        state_to_remove = self.states.get(state)
        if state_to_remove:
            state_to_remove.close_resources()
            self.states.pop(state)

        # Fill gaps in self.states
        sorted_state_keys = sorted(self.states.keys())

        new_states: dict[int, StateT] = {}
        state_map = {}
        for new_key, old_key in enumerate(sorted_state_keys):
            state_map[old_key] = new_key
            self.states[old_key].state = new_key

            if self.get_active_state() is self.states[old_key]:
                self.state = new_key

            new_states[new_key] = self.states[old_key]

        self.states = new_states

        new_states_dict = {}
        for new_key, old_key in enumerate(d["states"].keys()):
            new_states_dict[str(new_key)] = d["states"][old_key]

        d["states"] = new_states_dict

        page.save()

        self.update_state_switcher()

        # Update - TODO: test
        if state == self.state:
            sort = sorted(list(self.states.keys()))
            sort.reverse()
            for s in sort:
                if s <= state:
                    self.set_state(s, allow_reload=True)
                    break

        # One write, and after the two moves above: the remap moves the shown
        # state down, and a removed state that was the shown one moves it
        # again. set_state already wrote in that second case, so this records
        # the remap alone.
        self.persisted_state.write(self, self.state)

        gl.signal_manager.trigger_signal(Signals.RemoveState, state, state_map)

    def update_state_switcher(self) -> None:
        """Kept as the plugin-facing name; the widget work belongs to the
        adapter. The adapter guards the sidebar reach and marshals it to the
        main thread. A plugin or action thread therefore cannot raise
        AttributeError before the window exists, and cannot mutate a widget
        off main after it.
        """
        ui_port.get().on_input_states_changed(
            self.deck_controller, self.identifier, len(self.states))

    def get_active_state(self) -> StateT:
        state = self.states.get(self.state)
        return state if state is not None else self.ControllerStateClass(self, -1)

    def set_state(self, state: int, update_sidebar: bool = True, allow_reload: bool = False) -> None:
        if state == self.state and not allow_reload:
            return

        if state not in self.states:
            log.error(f"Invalid state: {state}, must be one of {list(self.states.keys())}")
            return
        self.state = state

        # Only a real state change reaches here: a load selects its own state
        # without going through this.
        self.persisted_state.write(self, state)

        self.get_active_state().update()

        if update_sidebar:
            self.reload_sidebar()

    def reload_sidebar(self) -> None:
        """Kept as the plugin-facing name; the widget work belongs to the
        adapter. The visible-child read runs inside the adapter's idle
        callback, together with the refresh, so no caller reads GTK off the
        main thread.
        """
        ui_port.get().on_input_state_selected(
            self.deck_controller, self.identifier, self.state)

    def load_from_config(self, config: "dict[str, Any]", update: bool = True) -> None:
        n_states = len(config.get("states", {}))
        self.create_n_states(max(1, n_states))

        old_state_index = self.state

        self.state = 0

        #TODO: Reset states
        for state_key in config.get("states", {}):
            state = self.states.get(int(state_key))
            if state is None:
                continue

            state_dict = config["states"][str(state.state)]

            if update:
                self.set_state(old_state_index)
                self.update()

    def clear(self, update: bool = True) -> None:
        active_state = self.get_active_state()
        # Every state class defines clear(): ControllerKeyState,
        # ControllerTouchScreenState and ControllerDialState each release
        # their media and reset the page-owned layers.
        active_state.clear()
        if update:
            self.update()

    def close_resources(self) -> None:
        """Framework teardown hook that releases every state's media
        resources. It serves the input's own end of life, at a deck close or
        a screensaver-stash sweep. clear() serves a fresh page load instead.
        This hook never triggers a repaint."""
        for state in self.states.values():
            state.close_resources()

    def has_unavailable_action(self) -> bool:
        for action in self.get_active_state().get_own_actions():
            if isinstance(action, ActionOutdated):
                return True
            if isinstance(action, NoActionHolderFound):
                return True
            
        return False
    
    def get_empty_background(self) -> Image.Image | None:
        # No ControllerInput subclass overrides this, so every caller gets
        # the base None. KeyImage tolerates it.
        return None

    def get_image_size(self) -> tuple[int, int]:
        # ControllerKey, ControllerTouchScreen and ControllerDial each
        # override this, so the base never answers.
        raise NotImplementedError

class ControllerKey(ControllerInput["ControllerKeyState"]):
    # The narrowing the base declaration describes. It sits here, and not on
    # the __init__ assignment, because an assignment annotation leaves a
    # reader of the attribute on the base declaration, which owns no key index.
    present_state: KeyPresentState

    HOLD_START_EVENT = Input.Key.Events.HOLD_START

    def __init__(self, deck_controller: "DeckController", ident: Input.Key):
        super().__init__(deck_controller, ControllerKeyState, ident)
        self.index = ident.get_index(deck_controller)
        self.present_state = KeyPresentState(self.index)
        # Seed the cached press state from the device so event_callback can
        # compare against it. key_states() is indexed logically, with the
        # rotation applied there, so self.index selects this key's own state.
        self.press_state: bool = self.deck_controller.deck.key_states()[self.index]

    @staticmethod
    def Available_Identifiers(deck: "BetterDeck") -> "Iterable[str]":
        return map(lambda x: f"{x[0]}x{x[1]}", map(lambda x: ControllerKey.Index_To_Coords(deck, x), range(deck.key_count())))

    @staticmethod
    def Index_To_Coords(deck: "BetterDeck", index: int) -> "tuple[int, int]":
        # deck is the wrapper, whose key_layout is the logical one. Every
        # caller passes it: the registry that names the keys, the key-press
        # path that has to reach those names, and the controller's own
        # helpers. A raw device handle reports the unrotated layout and
        # decodes a logical index against the wrong row length.
        rows, cols = deck.key_layout()
        y = index // cols
        x = index % cols
        return x, y
    
    @staticmethod
    def Coords_To_Index(deck: "BetterDeck", coords: "str | Sequence[int] | Sequence[str]") -> int:
        parts: "Sequence[int] | Sequence[str]"
        if isinstance(coords, str):
            parts = coords.split("x")
        else:
            parts = coords
        x, y = map(int, parts)
        rows, cols = deck.key_layout()
        return y * cols + x

    def update(self, force: bool = False) -> None:
        # One paint of this key at a time, spanning the whole compose and offer
        # below. See _paint_lock. The lock is released before the caller
        # dispatches anything, so no action callback runs under it.
        with self._paint_lock:
            self._paint(force)

    def _paint(self, force: bool) -> None:
        # Capture the page and the generation before the render, so a switch
        # mid-render invalidates this paint at the write boundary.
        page = self.deck_controller.active_page
        config_gen = self.config_gen

        # Frame-identity fast path. A passthrough key over a video background
        # composites to exactly the shared tile, so its native bytes are a
        # pure function of the frame they came from, and nothing has to
        # serialize, hash or re-encode a pixel to know what belongs on the
        # device. Steady-state playback of a loop then costs one dict lookup
        # and the USB write.
        if self.deck_controller.native_tile_cache.enabled and self._tile_passthrough_ok(self.get_active_state()):
            identified = self.deck_controller.background.get_identified_tile(self.index)
            if identified is not None:
                self._update_from_tile_identity(identified, page, config_gen, force)
                return

        if cover_cache.present(self, page, config_gen, force):
            return
        _t0 = _t1 = _t2 = 0.0  # definite binding; every read sits under the same media_prof guard as its write
        if media_prof:
            _t0 = time.perf_counter()
        image = self.get_current_image()
        if media_prof:
            _t1 = time.perf_counter()
            media_prof.add("composite", _t1 - _t0)

        img_hash = hash(image.tobytes())
        if media_prof:
            media_prof.add("hash", time.perf_counter() - _t1)

        # The offer hash-skips an unchanged composite, encodes the rest and
        # hands it to the writer. A deck with no screens short-circuits it and
        # falls through to the preview below. Judging the hashes first gave the
        # same result: on such a deck both stay None forever, because only an
        # enqueue stamps one and only a device write stamps the other, and no
        # image hash is None.
        if self.deck_controller.is_visual() and not self.present_state.offer(
                self.deck_controller.media_player, page=page, config_gen=config_gen,
                img_hash=img_hash, force=force, encode=lambda: _encode_key_native(self, image, img_hash)):
            if media_prof:
                media_prof.count("hash_skip")
            image.close()
            return

        self.set_ui_key_image(image)

    def _to_rotated_rgb(self, image: Image.Image) -> Image.Image:
        """The device-ready RGB form of a composited key image. It
        composites RGBA onto RGB to keep the edges smooth. It never mutates
        image, because both branches build a new one. The frame-identity
        path can therefore pass the shared background tile in with no copy
        first."""
        rotation = self.deck_controller.deck.get_rotation()
        if image.mode == "RGBA":
            rgb_background = Image.new("RGB", image.size, (0, 0, 0))
            rgb_background.paste(image, (0, 0), image)
            return rgb_background.rotate(rotation)
        return image.convert("RGB").rotate(rotation)

    def _update_from_tile_identity(self, identified: "tuple[Image.Image, tuple[str, int]]", page: "Page | None", config_gen: "int | None", force: bool) -> None:
        """Present a passthrough key straight from its frame identity; see
        update(). identified is the (tile, (video md5, frame index)) pair that
        Background handed out as one read."""
        tile, (video_md5, frame_index) = identified

        # This stands in for the pixel hash wherever the write-boundary
        # bookkeeping needs one. It is stable for a frame and distinct across
        # frames and keys, so the present state judges it as it judges a pixel
        # hash, and nothing has to serialize a tile to know what it holds.
        img_hash = hash(("vidtile", video_md5, frame_index, self.present_state.key_index))
        # is_visual short-circuits the offer as in update(), equivalently.
        if self.deck_controller.is_visual() and not self.present_state.offer(
                self.deck_controller.media_player, page=page, config_gen=config_gen,
                img_hash=img_hash, force=force, encode=lambda: _encode_tile_native(self, tile, video_md5, frame_index)):
            if media_prof:
                media_prof.count("hash_skip")
            return

        # The in-app preview wants a PIL image, and every other reader of
        # this frame shares the tile, so hand the UI its own copy.
        self.set_ui_key_image(copy(tile))

    def get_active_state(self) -> "ControllerKeyState":
        return super().get_active_state()

    def on_media_player_tick(self, now: float, bg_frame_new: bool) -> None:
        state, scroll_moved = self._tick_animation_clocks()
        needs_update = False

        # Decide on an update from the content type.
        if state.key_video is not None and state.key_video.frame_due(now):
            # InputVideo and KeyGIF pick their current frame from their own
            # timeline. The deadline recomposites only when that timeline
            # can hold a new frame, not on every loop tick.
            needs_update = True
        elif scroll_moved:
            needs_update = True
        elif bg_frame_new and self.deck_controller.background.video is not None:
            # An opaque background color hides the video tile, as
            # get_current_image shows, so that key cannot change per frame,
            # and no key changes on a tick the background rendered no frame.
            if state.background_manager.get_composed_color()[-1] < 255:
                needs_update = True

        if needs_update:
            self.update()

    def event_callback(self, press_state: bool) -> None:
        screensaver_was_showing = self.deck_controller.screen_saver.showing
        if press_state:
            # Only on key down. This lets a plugin control the screensaver
            # without a direct deactivation.
            self.deck_controller.screen_saver.on_key_change()
        if screensaver_was_showing:
            if not press_state:
                # A release the screensaver swallows still ends the physical
                # gesture. Without this, a snapshot pinned by a DOWN before
                # the screensaver is never dropped and its hold timer keeps
                # running, so HOLD_START fires after the finger left. show()
                # already cancels gestures on the input set it stashes, so a
                # live gesture on this key means the screensaver engaged
                # without the swap. Keep the two paths independent.
                self.cancel_gesture()
            return

        # Hold the page this press landed on for the whole callback. A press
        # that changes pages still owes its remaining events to the page it
        # was pressed on. The context manager releases it, because a raising
        # body that skipped a hand-written release would pin that page against
        # eviction for the life of the process, once per press.
        with page_pins.holding(self.deck_controller.active_page):
            self.press_state = press_state

            self.update()

            active_state = self.get_active_state()
            if press_state: # Key down
                self.down_start_time = time.time()
                # Snapshot the state and its resolved actions here; see
                # __init__. Every event of this gesture goes to the actions
                # that were on the key when the finger landed, whatever page
                # swaps happen in between. That includes this DOWN, which
                # otherwise resolves actions when the pool worker runs.
                gesture_actions = active_state.get_own_actions()
                self._gesture = (active_state, gesture_actions)
                self.start_hold_timer()
                active_state.own_actions_event_callback_threaded(
                    event=Input.Key.Events.DOWN,
                    show_notifications=True,
                    actions=gesture_actions
                )

            elif self.down_start_time is not None: # Key up
                gesture = self._gesture
                if gesture is not None:
                    gesture_state, gesture_actions = gesture
                else:
                    gesture_state, gesture_actions = active_state, None
                if time.time() - self.down_start_time >= self.deck_controller.hold_time:
                    gesture_state.own_actions_event_callback_threaded(
                        event=Input.Key.Events.HOLD_STOP,
                        actions=gesture_actions
                    )
                else:
                    gesture_state.own_actions_event_callback_threaded(
                        event=Input.Key.Events.SHORT_UP,
                        actions=gesture_actions
                    )
                self.down_start_time = None
                self.stop_hold_timer()
                gesture_state.own_actions_event_callback_threaded(
                    event=Input.Key.Events.UP,
                    show_notifications=False,
                    actions=gesture_actions
                )
                # The gesture is complete. Drop the snapshot in one atomic
                # store, so a superseded page's action objects are not pinned
                # past their last event.
                self._gesture = None

            else: # Key up with no gesture clock
                # The screensaver swallowed the matching DOWN, or something
                # already cleared its bookkeeping; a screensaver show and hide
                # cycle mid-hold resets down_start_time on the live keys.
                # There is nothing to dispatch, but a hold timer still armed,
                # or a snapshot still pinned, from that orphaned DOWN must not
                # outlive the physical release.
                self.cancel_gesture()

    def _tile_passthrough_ok(self, state: "ControllerKeyState") -> bool:
        """Whether this key composites to exactly the shared background
        tile, with no color layer, media, label or marker over it. It gates
        the composite fast path in get_current_image and the frame-identity
        fast path in update(). One definition keeps the two from disagreeing
        about which keys are bare."""
        return (state.background_manager.get_composed_color()[-1] == 0
                and state._overlay is None
                and state.key_image is None
                and state.key_video is None
                and not state.label_manager.get_has_visible_labels()
                and not self.is_pressed()
                and not (self.has_unavailable_action() and not self.deck_controller.screen_saver.showing))

    def get_current_image(self) -> Image.Image:
        state = self.get_active_state()
        cover_pre = cover_cache.precheck(self, state)

        # A bare key's composite is the shared background tile, so return a
        # copy of it directly. That saves work per frame over an animated
        # background.
        if self._tile_passthrough_ok(state):
            tile = self.deck_controller.background.tiles[self.index]
            if tile is not None:
                if media_prof:
                    media_prof.count("tile_passthrough")
                return copy(tile)

        background_color = state.background_manager.get_composed_color()

        _t0 = _t1 = _t2 = 0.0  # definite binding; every read sits under the same media_prof guard as its write
        if media_prof:
            _t0 = time.perf_counter()

        background: Image.Image | None = None
        # Load the background image only when the background color does not
        # hide it.
        if background_color[-1] < 255:
            background = copy(self.deck_controller.background.tiles[self.index])

        if background_color[-1] > 0:
            background_color_img = Image.new("RGBA", self.deck_controller.get_key_image_size(), color=tuple(background_color))
            if background is None:
                # Use the color as the only background. This happens at a
                # background color alpha of 255.
                background = background_color_img
            else:
                background.paste(background_color_img, (0, 0), background_color_img)

        if background is None:
            background = self.deck_controller.generate_alpha_key().copy()

        if media_prof:
            _t1 = time.perf_counter()
            media_prof.add("c_tile", _t1 - _t0)

        if state._overlay:
            height = round(self.deck_controller.get_key_image_size()[1] * OVERLAY_TILE_FRACTION)
            img = state._overlay.resize((height, height))
            background.paste(img, centered_paste_offset(self.deck_controller.get_key_image_size(), img.size), img)
            return background

        key_image: Image.Image | None = None
        if state.key_image is not None:
            image = state.key_image.get_raw_image()
            key_image = state.layout_manager.add_image_to_background(
                image=image,
                background=background,
                # A static asset has a cacheable resize; a video or GIF does
                # not.
                cache_token=state.key_image
            )
        elif state.key_video is not None:
            image = state.key_video.get_raw_image()
            key_image = state.layout_manager.add_image_to_background(
                image=image,
                background=background)
        else:
            key_image = background

        if media_prof:
            _t2 = time.perf_counter()
            media_prof.add("c_layout", _t2 - _t1)

        labeled_image = state.label_manager.add_labels_to_image(key_image)

        if media_prof:
            media_prof.add("c_labels", time.perf_counter() - _t2)

        # A gate that draws into the picture decides the store as well.
        if self.is_pressed():
            labeled_image, cover_pre = press_look.apply(self, labeled_image), cover_cache.NO_STORE

        if self.has_unavailable_action() and not self.deck_controller.screen_saver.showing:
            labeled_image, cover_pre = self.add_warning_point(labeled_image), cover_cache.NO_STORE

        # A key with no visible label gets its own composite back, because
        # add_labels_to_image skips the copy, and with no media key_image is
        # background. An unconditional close on either would hand the media
        # thread an image whose buffer is already released.
        if background is not None and background is not labeled_image:
            background.close()

        if key_image is not labeled_image:
            key_image.close()

        return cover_cache.remember(self, state, labeled_image, cover_pre)
    
    def add_warning_point(self, image: Image.Image, margin: int = 10, size: int = 10, color: tuple[int, int, int] = (255, 150, 80)) -> Image.Image:
        draw = ImageDraw.Draw(image)

        # Find the coordinates of the top right circle.
        width, height = image.size
        top_right_x = width - margin - size
        top_right_y = margin

        draw.ellipse((top_right_x, top_right_y, top_right_x + size, top_right_y + size), fill=color, outline=(0, 0, 0), width=2)

        del draw
        return image
    

    def is_pressed(self) -> bool:
        return self.press_state
    
    def add_border(self, image: Image.Image) -> Image.Image:
        image = image.copy()
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((-1, -1, image.width, image.height), fill=None, outline=(255, 105, 0), width=8, radius=8)

        return image

    def shrink_image(self, image: Image.Image, factor: float = 0.7) -> Image.Image:
        image = image.copy()
        width = int(image.width * factor)
        height = int(image.height * factor)
        image = image.resize((width, height))

        background = Image.new("RGBA", self.deck_controller.get_key_image_size(), (0, 0, 0, 0))

        # The mask is the image itself only when it carries alpha. Passing an
        # image with no alpha as its own mask raises.
        background.paste(image, centered_paste_offset(background.size, image.size),
                         image if image.has_transparency_data else None)

        image.close()

        return background
    
    def load_from_input_dict(self, input_dict: "dict[str, Any]", update: bool = True, page: "Page | None" = None, load_labels: bool = True, load_media: bool = True, load_background_color: bool = True, *,
                             still_current: "Callable[[], bool] | None" = None) -> None:
        """
        Disabling load_media can also disable custom user assets.
        """
        n_states = len(input_dict.get("states", {}))

        restored = self._recreate_states_keeping_action_media(n_states)

        self.state = self.persisted_state.on_load(self, input_dict, page)

        #TODO: Reset states
        for state_key in input_dict.get("states", {}):
            # Re-check the load's currency at every mutation boundary. The
            # loader pool's deadline abandons the wait, not the task, so a
            # plugin callback in own_actions_update below can block past a
            # page switch and resume here after the next page re-stamped this
            # same object. Stopping mid-load leaves partially loaded states,
            # which the superseding load rewrites; continuing would mutate the
            # new page's live input.
            if still_current is not None and not still_current():
                return
            state = self.states.get(int(state_key))
            if state is None:
                continue

            state_dict = input_dict["states"][str(state.state)]

            if load_labels:
                state.label_manager.clear_labels()

            # Reset the action layout, except on a state whose action-owned
            # media _recreate_states_keeping_action_media restored. Its action
            # layout belongs to the same action, which is still present, and a
            # reset would restore the image but lose the alignment and the size.
            if state.state not in restored:
                layout = ImageLayout()
                state.layout_manager.set_action_layout(layout, update=False)

            state.own_actions_update() # Why not threaded? Because this would mean that some image changing calls might get executed after the next lines which blocks custom assets

            # The call above is the one that runs plugin code inline and can
            # block for the whole superseding window; ask again before the
            # label and media writes below land on a re-stamped input.
            if still_current is not None and not still_current():
                return

            ## Load labels
            if load_labels:
                for label in state_dict.get("labels", []):
                    key_label = KeyLabel(
                        controller_input=self,
                        text=state_dict["labels"][label].get("text"),
                        font_size=state_dict["labels"][label].get("font-size"),
                        font_name=state_dict["labels"][label].get("font-family"),
                        font_weight=state_dict["labels"][label].get("font-weight"),
                        style=state_dict["labels"][label].get("style"),
                        color=state_dict["labels"][label].get("color"),
                        outline_width=state_dict["labels"][label].get("outline_width"),
                        outline_color=state_dict["labels"][label].get("outline_color"),
                        alignment=state_dict["labels"][label].get("alignment")
                    )
                    state.label_manager.set_page_label(label, key_label, update=False)

            ## Load media
            if load_media:
                media = MediaConfig.from_dict(state_dict.get("media", {}))
                path = media.path
                if path not in ["", None]:
                    if is_image(path):
                        with Image.open(path) as image:
                            state.set_image(InputImage(
                                controller_input=self,
                                image=image.copy(),
                                path=path,
                            ), update=False)
                            
                    elif is_svg(path):
                        img = svg_to_pil(path, SVG_RASTER_WIDTH_PX)
                        state.set_image(InputImage(
                            controller_input=self,
                            image=img
                        ), update=False)

                    elif is_video(path):
                        # A GIF builds a KeyGIF with the fps render cap, falling
                        # back to the opaque cv2 path on a decode failure so one
                        # bad asset does not take the page load down. Shared with
                        # the dial loader; see _build_page_video_media.
                        state.set_video(_build_page_video_media(self, path, media))
                    # This chain ends here. Do not add an elif that calls
                    # self.set_key_image(), which ControllerKey does not
                    # define. Such a branch fires on the normal
                    # load_media=True path whenever path is a non-empty string
                    # that is not a valid image, svg or video, such as a
                    # dangling config path, and it raises AttributeError. A
                    # bad path must stay a no-op.

                layout = ImageLayout(
                    fill_mode=media.fill_mode,
                    size=media.size,
                    valign=media.valign,
                    halign=media.halign,
                )
                state.layout_manager.set_page_layout(layout, update=False)

            if load_background_color:
                state.background_manager.set_page_color(state_dict.get("background", {}).get("color"), update=False)

        if update:
            self.persisted_state.sync_sidebar(self)
            self.update()

    def set_ui_key_image(self, image: Image.Image | None) -> None:
        if image is None:
            return

        if not ui_port.get().push_input_image(self.deck_controller, self.identifier, image):
            # The port refused the push, because there is no UI, the window
            # is unmapped or the grid is mid-rebuild, or the push raised. Mark
            # the input dirty only. KeyGrid.load_from_changes recomposites a
            # fresh image on map instead of replaying this one. A frame the
            # port accepts and later drops marks itself; see
            # ui_adapter.mark_dirty.
            self.deck_controller.ui_image_changes_while_hidden[self.identifier] = True


    def get_own_ui_key(self) -> "object | None":
        """Deprecated in-process shim. The attached UI resolves its own
        widget for this input. Returns None when headless."""
        return ui_port.get().query_input_widget(self.deck_controller, self.identifier)
    
    def get_image_size(self) -> tuple[int, int]:
        return self.deck_controller.get_key_image_size()

class ControllerTouchScreen(ControllerInput["ControllerTouchScreenState"]):
    def __init__(self, deck_controller: "DeckController", ident: InputIdentifier):
        super().__init__(deck_controller, ControllerTouchScreenState, ident)
        self.present_state = TouchscreenPresentState()

        self.enable_states = False

    @staticmethod
    def Available_Identifiers(deck: "BetterDeck") -> "Iterable[str]":
        if deck.is_touch():
            return ["sd-plus"]
        return []

    def update(self) -> None:
        # One paint of the strip at a time, for the reason ControllerKey.update
        # gives. The strip carries more producers than any key: every dial
        # composites into it, and so does a background video extended onto it.
        with self._paint_lock:
            self._paint()

    def _paint(self) -> None:
        page = self.deck_controller.active_page  # capture at render start (see ControllerKey.update)
        config_gen = self.config_gen
        image = self.get_current_image()

        # The offer hash-skips an unchanged composite, which saves a redundant
        # 800x100 JPEG encode and write, the largest single write on the deck.
        # It also finishes every device read of image before the UI mirror
        # below gets it, so GTK never copies it under the media thread.
        img_hash = hash(image.tobytes())
        if not self.present_state.offer(
                self.deck_controller.media_player, page=page, config_gen=config_gen,
                img_hash=img_hash, encode=lambda: _encode_strip_native(self, image)):
            image.close()
            return

        self.set_ui_image(image)

    def generate_empty_image(self) -> Image.Image:
        return Image.new("RGBA", self.get_screen_dimensions(), (0, 0, 0, 0))

    def get_image_size(self) -> tuple[int, int]:
        # InputVideo sizes its frame cache from this. For the touchscreen
        # that is the full strip.
        return self.get_screen_dimensions()

    def on_media_player_tick(self, now: float) -> bool:
        # A per-touchscreen background video advances on the media tick, as
        # dial content does, and the caller re-composites the shared
        # touchscreen once per frame. The screensaver owns the strip while it
        # shows. The state decides the rate-capped render under its
        # background-video lock, so a concurrent _release_background_video()
        # cannot race the read of the video or its deadline.
        if self.deck_controller.screen_saver.showing:
            return False
        return self.get_active_state().tick_background_video(now)

    def get_dial_image_area(self, identifier: Input.Dial) -> tuple[int, int, int, int]:
        width, height = self.get_screen_dimensions()

        n_dials = len(self.deck_controller.inputs[Input.Dial])
        dial_index = identifier.index

        start_x = int((dial_index / n_dials) * width)
        start_y = 0
        end_x = int(((dial_index + 1) / n_dials) * width)
        end_y = height

        return start_x, start_y, end_x, end_y
    
    def get_dial_image_area_size(self) -> tuple[int, int]:
        width, height = self.get_screen_dimensions()

        n_dials = len(self.deck_controller.inputs[Input.Dial])

        return int(width / n_dials), height
    
    def get_empty_dial_image(self) -> Image.Image:
        screen_width, screen_height = self.get_screen_dimensions()

        n_dials = len(self.deck_controller.inputs[Input.Dial])

        return Image.new("RGBA", (screen_width // n_dials, screen_height), (0, 0, 0, 0))

    def set_ui_image(self, image: Image.Image) -> None:
        if not ui_port.get().push_input_image(self.deck_controller, self.identifier, image):
            # Mark the input dirty only. ScreenBar.load_from_changes
            # recomposites a fresh image on map instead of replaying this one.
            # The adapter owns the preview throttle and its tail flush, which
            # re-marks a frame the window unmapped out from under.
            self.deck_controller.ui_image_changes_while_hidden[self.identifier] = True

    def get_current_image(self) -> Image.Image:
        active_state = self.get_active_state()
        return active_state.get_current_image()

    def event_callback(self, event_type: TouchscreenEventType, value: "dict[str, int]") -> None:
        screensaver_was_showing = self.deck_controller.screen_saver.showing
        if event_type in (TouchscreenEventType.SHORT, TouchscreenEventType.LONG, TouchscreenEventType.DRAG):
            self.deck_controller.screen_saver.on_key_change()
        if screensaver_was_showing:
            return
        
        # Touchscreen events arrive pre-classified from the library, as
        # SHORT, LONG or DRAG. They are single events with no DOWN and UP
        # tail, so there is no gesture snapshot to keep. The default dispatch
        # resolves the target actions against active_page when the pool worker
        # runs, so a page swap between the event and the worker redirects the
        # event to the new page's actions, as it does for a dial TURN.
        # Resolve at read time instead, here on the deck's input thread.
        active_state = self.get_active_state()
        if event_type == TouchscreenEventType.DRAG:
            drag_actions = active_state.get_own_actions()
            # Check whether the drag went left to right, or the other way.
            if value['x'] > value['x_out']:
                active_state.own_actions_event_callback_threaded(
                    Input.Touchscreen.Events.DRAG_LEFT,
                    actions=drag_actions
                )
            else:
                active_state.own_actions_event_callback_threaded(
                    Input.Touchscreen.Events.DRAG_RIGHT,
                    actions=drag_actions
                )


        #TODO get matching actions from the dials
        elif event_type in (TouchscreenEventType.SHORT, TouchscreenEventType.LONG):
            dial = self.get_dial_for_touch_x(value['x'])
            if dial is not None:
                dial_active_state = dial.get_active_state()
                if dial_active_state is not None:

                    event = Input.Dial.Events.SHORT_TOUCH_PRESS
                    if event_type == TouchscreenEventType.LONG:
                        event = Input.Dial.Events.LONG_TOUCH_PRESS

                    touch_actions = dial_active_state.get_own_actions()
                    dial_active_state.own_actions_event_callback_threaded(
                        event,
                        data={"x": value['x'], "y": value['y']},
                        show_notifications=True,
                        actions=touch_actions
                    )

    def get_dial_for_touch_x(self, touch_x: float) -> "ControllerDial | None":
        screen_width = self.deck_controller.get_touchscreen_image_size()[0]
        n_dials = len(self.deck_controller.inputs[Input.Dial])
        dial_index = int((touch_x / screen_width) * n_dials)

        return self.deck_controller.get_input(Input.Dial(str(dial_index)))
    
    def get_screen_dimensions(self) -> tuple[int, int]:
        return self.deck_controller.get_touchscreen_image_size()

class ControllerDial(ControllerInput["ControllerDialState"]):
    HOLD_START_EVENT = Input.Dial.Events.HOLD_START

    def __init__(self, deck_controller: "DeckController", ident: InputIdentifier):
        super().__init__(deck_controller, ControllerDialState, ident)

    def get_touch_screen(self) -> "ControllerTouchScreen | None":
        return self.deck_controller.get_input(Input.Touchscreen("sd-plus"))

    @staticmethod
    def Available_Identifiers(deck: "BetterDeck") -> "Iterable[str]":
        return map(str, range(deck.dial_count()))

    def event_callback(self, event_type: DialEventType, value: int) -> None:
        screensaver_was_showing = self.deck_controller.screen_saver.showing
        if event_type == DialEventType.TURN:
            self.deck_controller.screen_saver.on_key_change()
        if event_type == DialEventType.PUSH and value:
            # Only on push, not on hold. That lets an action enable the
            # screensaver without waking it again at once.
            self.deck_controller.screen_saver.on_key_change()
        if screensaver_was_showing:
            if event_type == DialEventType.PUSH and not value:
                # A release the screensaver swallows still ends the physical
                # gesture; see the matching branch in
                # ControllerKey.event_callback. show() already cancels
                # gestures on the input set it stashes, and this covers the
                # case where the screensaver engaged without the swap.
                self.cancel_gesture()
            return

        active_state = self.get_active_state()
        if event_type == DialEventType.PUSH:
            if value:
                self.down_start_time = time.time()
                # Snapshot the state and its resolved actions here; see
                # __init__. Every event of this gesture goes to the actions
                # that were on the dial when it was pressed, whatever page
                # swaps happen in between. That includes this DOWN, which
                # otherwise resolves actions when the pool worker runs.
                gesture_actions = active_state.get_own_actions()
                self._gesture = (active_state, gesture_actions)
                self.start_hold_timer()
                active_state.own_actions_event_callback_threaded(
                    event=Input.Dial.Events.DOWN,
                    show_notifications=True,
                    actions=gesture_actions
                )
            elif self.down_start_time is not None:
                gesture = self._gesture
                if gesture is not None:
                    gesture_state, gesture_actions = gesture
                else:
                    gesture_state, gesture_actions = active_state, None
                self.stop_hold_timer()
                if time.time() >= self.down_start_time + self.deck_controller.hold_time:
                    gesture_state.own_actions_event_callback_threaded(
                        event=Input.Dial.Events.HOLD_STOP,
                        actions=gesture_actions
                    )
                else:
                    gesture_state.own_actions_event_callback_threaded(
                        event=Input.Dial.Events.SHORT_UP,
                        actions=gesture_actions
                    )
                self.down_start_time = None
                gesture_state.own_actions_event_callback_threaded(
                    event=Input.Dial.Events.UP,
                    actions=gesture_actions
                )
                # The gesture is complete. Drop the snapshot in one atomic
                # store, so a superseded page's action objects are not pinned
                # past their last event.
                self._gesture = None
            else:
                # Release with no gesture clock. Something swallowed the
                # matching DOWN, or already cleared its bookkeeping. There is
                # nothing to dispatch, but a hold timer still armed, or a
                # snapshot still pinned, from that orphaned DOWN must not
                # outlive the release.
                self.cancel_gesture()

        elif event_type == DialEventType.TURN:
            # Resolve the target actions at read time. A turn is a single
            # event, but the default dispatch resolves against active_page
            # when the pool worker runs, and a page swap in that window
            # redirects the turn to the new page's actions.
            turn_actions = active_state.get_own_actions()
            # value is the signed detent count of the HID report. A fast
            # rotation coalesces several detents into one report, so forward
            # the magnitude instead of one single event.
            if value < 0:
                active_state.own_actions_event_callback_threaded(
                    event=Input.Dial.Events.TURN_CCW,
                    data={"ticks": -value},
                    actions=turn_actions
                )
            else:
                active_state.own_actions_event_callback_threaded(
                    event=Input.Dial.Events.TURN_CW,
                    data={"ticks": value},
                    actions=turn_actions
                )

    def load_from_input_dict(self, input_dict: "dict[str, Any]", update: bool = True, page: "Page | None" = None, *,
                             still_current: "Callable[[], bool] | None" = None) -> None:
        n_states = len(input_dict.get("states", {}))

        restored = self._recreate_states_keeping_action_media(n_states)

        self.state = self.persisted_state.on_load(self, input_dict, page)

        for state_key in input_dict.get("states", {}):
            # As on the key loader: a load superseded mid-flight stops
            # mutating this shared input at the next boundary.
            if still_current is not None and not still_current():
                return
            state = self.states.get(int(state_key))
            if state is None:
                continue

            state_dict = input_dict["states"][str(state.state)]

            # Reset the action layout, except on a state whose action-owned
            # media _recreate_states_keeping_action_media restored: that layout
            # belongs to the same still-present action, and a reset would lose
            # its alignment and size.
            if state.state not in restored:
                layout = ImageLayout()
                state.layout_manager.set_action_layout(layout, update=False)

            state.own_actions_update() # Why not threaded? Because this would mean that some image changing calls might get executed after the next lines which blocks custom assets

            # The plugin-blocking call; ask again before the writes below.
            if still_current is not None and not still_current():
                return

            ## Load labels
            for label in state_dict.get("labels", []):
                key_label = KeyLabel(
                    controller_input=self,
                    text=state_dict["labels"][label].get("text"),
                    font_size=state_dict["labels"][label].get("font-size"),
                    font_name=state_dict["labels"][label].get("font-family"),
                    font_weight=state_dict["labels"][label].get("font-weight"),
                    style=state_dict["labels"][label].get("style"),
                    color=state_dict["labels"][label].get("color"),
                    alignment=state_dict["labels"][label].get("alignment"),
                )
                state.label_manager.set_page_label(label, key_label, update=False)

            ## Load media
            media = MediaConfig.from_dict(state_dict.get("media", {}))
            path = media.path
            if path not in ["", None]:
                if is_image(path):
                    image = InputImage(
                        controller_input=self,
                        image=Image.open(path),
                        path=path,
                    )
                    state.set_image(image, update=False)
                elif is_svg(path):
                    img = svg_to_pil(path, SVG_RASTER_WIDTH_PX)
                    state.set_image(InputImage(
                        controller_input=self,
                        image=img
                    ), update=False)

                elif is_video(path):
                    # KeyGIF sizes its frames to the key tile, which is the
                    # size a dial composites onto the strip too.
                    state.set_video(_build_page_video_media(self, path, media))

            layout = ImageLayout(
                fill_mode=media.fill_mode,
                size=media.size,
                valign=media.valign,
                halign=media.halign,
            )
            state.layout_manager.set_page_layout(layout, update=False)

            state.background_manager.set_page_color(state_dict.get("background", {}).get("color", [0, 0, 0, 0]), update=False)

        if update:
            self.persisted_state.sync_sidebar(self)
            self.update()

    def update(self) -> None:
        if self.deck_controller.deck.is_touch():
            touch_screen = self.get_touch_screen()
            if touch_screen is not None:
                touch_screen.update()

    def get_active_state(self) -> "ControllerDialState":
        return super().get_active_state()

    def on_media_player_tick(self, now: float) -> bool:
        # Report whether a redraw is needed instead of painting. A dial has no
        # slot of its own, so the caller renders the shared touchscreen once
        # per frame rather than once per dial. The video's own deadline
        # decides whether this tick can show a new frame.
        state, scroll_moved = self._tick_animation_clocks()
        video_due = state.video is not None and state.video.frame_due(now)
        return video_due or scroll_moved

    def get_image_size(self) -> tuple[int, int]:
        if self.deck_controller.deck.is_touch():
            touch_screen = self.get_touch_screen()
            if touch_screen is not None:
                return touch_screen.get_dial_image_area_size()
        # (0, 0) is the established answer for a dial with no strip, which
        # means no visual target. KeyImage._budget_size keys off exactly it.
        return (0, 0)
    

