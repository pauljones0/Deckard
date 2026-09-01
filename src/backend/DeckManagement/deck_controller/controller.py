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

One DeckController per physical deck, plus the table that maps an identifier
type to the controller-input class that drives it.

A controller owns the device handle and everything hung off it, and it owns
the lifecycle. It opens the deck with its transport lock already FIFO,
builds the inputs, starts the media writer, loads a page, and tears all of
that down in a fixed order at close(). It writes nothing to the device
itself. An input composes every paint and enqueues it for the media writer,
which is the sole writer.

Page loading is the busiest seam. load_page() serializes switches under
_load_page_lock, then bumps _page_load_generation and stamps every input
with the new generation inside one hold of _page_gen_lock. A paint queued
from another thread then carries the generation it was rendered for, and the
write boundary judges it against the present one. The writer reads that same
pair, and the screensaver bumps it too, because it swaps this object's
inputs and background out and puts them back on hide().

This module imports the media writer, the background media group and the
inputs, and none of them imports it back.
"""
import gc
import math
import os
import threading
import time
from contextlib import nullcontext
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from threading import Thread

from PIL import Image
from StreamDeck.Devices import StreamDeck
from StreamDeck.Devices.StreamDeckPlus import StreamDeckPlus
from StreamDeck.ImageHelpers import PILHelper
from loguru import logger as log

from src.backend.DeckManagement.BetterDeck import BetterDeck, open_device_handle
from src.backend.DeckManagement.InputIdentifier import Input, InputIdentifier
from src.backend.DeckManagement.Subclasses import cache_budget
from src.backend.DeckManagement.deck_controller.background_media import resolve_background_entries
from src.backend.DeckManagement.deck_controller.viewport import DEFAULT_VIEW
from src.backend.DeckManagement.Subclasses.ScreenSaver import ScreenSaver
from src.backend.DeckManagement.Subclasses.encoded_image_cache import EncodedImageCache
from src.backend.DeckManagement.Subclasses.native_tile_cache import NativeTileCache, native_tile_cache_max_bytes
from src.backend.DeckManagement.deck_controller.background_media import Background, BackgroundVideo
from src.backend.DeckManagement.deck_controller.inputs import ControllerDial, ControllerKey, ControllerTouchScreen
from src.backend.DeckManagement.deck_controller.input_latency import InputLatencyRun, dispatch_deck_event, make_input_latency_tracker, write_input_latency_report
from src.backend.DeckManagement.deck_events import DeckEvent, DialEvent, KeyEvent, TouchscreenEvent
from src.backend.DeckManagement.deck_controller.page_completion import PageLoadCompletion
from src.backend.DeckManagement.deck_controller.media_writer import (
    ClearAndCloseMsg,
    ClearMsg,
    MediaPlayerThread,
    SetBrightnessMsg,
    _install_fair_transport_lock,
)
from src.backend.DeckManagement.deck_controller.rotation import apply_rotation
from src.backend.PageManagement.Page import Page
from src.backend.deadline_pool import BatchWording, DeadlinePool
from src.backend.mem_telemetry import page_switches
from src.backend import control_plane, startup_queue, ui_port
from src.api import notify_active_page_changed
from src.Signals import Signals

import globals as gl

from collections.abc import Callable, Sequence
from typing import cast, TYPE_CHECKING, Any, overload
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckManager import DeckManager
    from src.backend.DeckManagement.Subclasses.FakeDeck import FakeDeck
    from src.backend.DeckManagement.Subclasses.RemoteDeck import RemoteDeck
    from src.backend.DeckManagement.deck_controller.gif_pipeline import GifBackground
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput


# The hashable signature of a deck's native key image format: size,
# format name, flip pair and rotation. Part of every native tile cache key.
NativeKeyFormatSig = tuple[tuple[int, int], str, tuple[bool, bool], int]

# Every input identifier class paired with the controller class that drives it.
CONTROLLER_CLASSES: dict[type[InputIdentifier], type[ControllerKey | ControllerDial | ControllerTouchScreen]] = {
    Input.Key: ControllerKey,
    Input.Dial: ControllerDial,
    Input.Touchscreen: ControllerTouchScreen,
}

# An input type missing here fails at import, instead of a KeyError deep
# inside DeckController.__init__ that reads as a silently skipped device.
assert set(CONTROLLER_CLASSES) == set(Input.All)


class DeckController:
    # Bound on the close() wait for plugin teardown hooks. It sits on the
    # class so the harness can tighten it.
    TEARDOWN_JOIN_TIMEOUT_S = 10.0

    def __init__(self, deck_manager: "DeckManager", deck: "StreamDeck.StreamDeck | FakeDeck | RemoteDeck"):
        self.deck_manager: "DeckManager" = deck_manager
        self.input_latency_run = InputLatencyRun.from_environment()
        self.input_latency = make_input_latency_tracker(self.input_latency_run)
        self.input_latency_model: str | None = None

        # Per-instance memo for stable deck properties. An lru_cache on an
        # instance method pins every self on the class and never evicts.
        self._serial_number: str | None = None
        self._key_image_size: tuple[int, int] | None = None
        self._touchscreen_image_size: tuple[int, int] | None = None
        self._native_key_format_sig: "NativeKeyFormatSig | None" = None

        # Order the transport mutex FIFO before open() starts the reader thread.
        _install_fair_transport_lock(deck)
        # Resume-from-suspend handle reopen is the library's only mode, and it is always on.
        open_device_handle(deck)

        # Wrap the open handle before the settings read below, so a raise in the bring-up gives the
        # device back here.
        self.deck: BetterDeck = BetterDeck(cast("StreamDeck.StreamDeck", deck))
        if self.input_latency_run is not None:
            self.input_latency_model = self.deck.deck_type()

        try:
            self.deck.set_rotation(gl.settings_manager.deck_view(self.get_deck_settings()).get("rotation"))
            # Clear the deck through the direct body, not the queue-routed clear().
            self._clear_direct()
        except Exception as e:
            log.error(f"Failed to bring up deck, maybe it's already connected to another instance? Skipping... Error: {e}")
            # Release the handle and raise, so the caller does not register a
            # half-built controller.
            self._release_handle()
            raise

        self.hold_time: float = gl.settings_manager.app().hold_time
        
        self.screen_saver = ScreenSaver(deck_controller=self)
        self.allow_interaction = True
        self.has_animated_keys = False

        # Every deck tiles its background at this spacing.
        raw_deck = getattr(self.deck, "deck", None)
        self.is_plus = isinstance(raw_deck, StreamDeckPlus)
        self.key_spacing = (116, 34) if self.is_plus else (36, 36)

        # Per-deck saturation boost, a PIL ImageEnhance.Color factor over the UI range 1.0 to 1.5.
        self.display_saturation: float = self._read_display_saturation()

        # {identifier: True} while the main window is hidden. The device composite runs every tick
        # whatever the window does, so a retained copy holds a big object alive for nothing.
        self.ui_image_changes_while_hidden: dict[InputIdentifier, bool] = {}

        # Set once under _close_lock to gate producers and make racing teardowns idempotent.
        self._closing: bool = False
        self._close_lock = threading.Lock()

        # Timestamp of the last post-load GC (see maybe_collect_garbage).
        self._last_gc_time: float = 0.0

        self.active_page: Page | None = None

        # Bumped on every load_page, so an overlapping load can tell whether
        # its queued paints are still current. See _page_is_current.
        self._page_load_generation: int = 0
        self._page_gen_lock = threading.Lock()
        # Serializes load_page's switch body so racing switches cannot interleave. An RLock, because
        # a ChangePage handler nests a load_page.
        self._load_page_lock = threading.RLock()
        # Page recorded by load_page's screensaver guard, consumed by hide().
        self._screensaver_pending_page: "Page | None" = None
        # Serializes background loads on the pool. A superseded load must not
        # overwrite a newer page's background.
        self._background_load_lock = threading.Lock()
        self._bg_future: "Future[None] | None" = None
        # Two dedicated workers let a new page decode while its superseded decode still runs.
        # This also isolates page backgrounds from unrelated application-pool work.
        try:
            _serial = self.serial_number()
        except Exception:
            _serial = "unknown"
        self._bg_decode_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix=f"bg-decode-{_serial}")
        self._page_completion: PageLoadCompletion | None = None
        self._input_load_done = control_plane.InputLoadBarrier()

        # Native encoded key image caches.
        self.encode_memo = EncodedImageCache(max_bytes=32 * 1024 * 1024)
        # native_tile_cache holds the same natives keyed by frame identity for the passthrough path.
        self.native_tile_cache = NativeTileCache(max_bytes=native_tile_cache_max_bytes())
        # Enrol both in the process-wide image-cache budget. Without it, total image-cache RAM
        # scales with the deck count and a cold deck's full memo never yields a byte to a hot one.
        try:
            self._register_image_caches()
        except Exception as e:
            log.warning(f"Could not register the image caches with the budget: {e}")

        # Heterogeneous registry.
        self.inputs: dict[type[InputIdentifier], list[Any]] = {}
        for i in Input.All:
            self.inputs[i] = []
        self.init_inputs()

        self.background = Background(self)

        self.deck.set_key_callback(self.key_event_callback)
        self.deck.set_dial_callback(self.dial_event_callback)
        self.deck.set_touchscreen_callback(self.touchscreen_event_callback)

        # Initialize writer-only recovery state before the media thread starts and reads it.
        # Only that thread changes this state, so it needs no lock.
        self._had_write_failure: bool = False
        self._full_repaint_pending: bool = False
        self._last_full_repaint_ts: float = 0.0

        self.media_player = MediaPlayerThread(deck_controller=self)
        self.media_player.start()

        # Everything below can still fail, and holds threads only this half-built object owns.
        try:
            # Register the sole expected device writer for the owner-assertion tooling in
            # BetterDeck.py. It does nothing unless DECKARD_ASSERT_DEVICE_OWNER is set.
            self.deck.set_expected_writer(self.media_player)

            # Bounded thread pool for the action callbacks, sized so every input runs its on_tick
            # concurrently. Lifecycle only: no deadline, and no replacement.
            total_inputs = sum(len(inputs) for inputs in self.inputs.values())
            # close() sets this to None, so every reader either
            # getattr-defaults or None-checks before it submits.
            self.action_executor: DeadlinePool | None = DeadlinePool(
                max_workers=max(8, total_inputs + 4),
                thread_name_prefix="action_cb",
            )

            # Persistent per-deck loader pool for load_all_inputs, sized so every input loads
            # concurrently.
            self.load_executor: DeadlinePool | None = DeadlinePool(
                max_workers=max(8, total_inputs),
                thread_name_prefix=f"load_{self.serial_number()}",
                replace_on_wedge=True,
                wording=BatchWording(
                    subject="Loading inputs",
                    pool_name=f"loader pool of deck {self.serial_number()}",
                    stuck_hint="a plugin callback is likely blocked",
                ),
            )

            self.keep_actions_ticking = True
            self.TICK_DELAY = 1
            # Lets close() interrupt the tick_actions sleep at once, instead of a wait of up to one
            # full TICK_DELAY before the loop reads keep_actions_ticking.
            self._tick_stop_event = threading.Event()
            self.tick_thread = Thread(target=self.tick_actions, name="tick_actions")
            self.tick_thread.start()

            self.page_auto_loaded: bool = False
            self.last_manual_loaded_page_path: str | None = None

            deck_settings = gl.settings_manager.deck_view(self.get_deck_settings())

            # None so the first set_brightness() below always writes to the device,
            # even when the stored value equals the skip-write guard's default.
            self.brightness: "float | None" = None
            brightness = deck_settings.get("brightness", "value")
            self.set_brightness(brightness)

            # Start the screensaver when the screen is locked. This happens
            # when the deck reconnects during the screensaver.
            if gl.screen_locked and gl.settings_manager.app().lock_on_lock_screen:
                self.allow_interaction = False
                # Apply the deck's own screensaver config first.
                self._apply_screensaver_config(deck_settings.section("screensaver"))
                self.screen_saver.show()
            else:
                # A transport failure says the device is not usable, and the connect path owns that
                # case.
                try:
                    self.load_default_page()
                except StreamDeck.TransportError:
                    raise
                except Exception as e:
                    log.error(f"Deck {self.serial_number()} registered without its boot page applied: {e}")
        except Exception:  # release them and re-raise; the connect path judges the failure
            self._teardown_failed_init()
            raise

    def _teardown_failed_init(self) -> None:
        """Release partial initialization so it leaves no writer, ticker, or pool behind."""
        self.keep_actions_ticking = False
        if getattr(self, "tick_thread", None) is not None and self.tick_thread.is_alive():
            self._tick_stop_event.set()  # created by the same statement pair as the thread
            self.tick_thread.join(2.0)
        self.media_player.stop(timeout=2.0)
        for pool in (getattr(self, "action_executor", None),
                     getattr(self, "load_executor", None),
                     getattr(self, "_bg_decode_pool", None)):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        if not self.media_player.running:  # otherwise the handle stays open
            self._release_handle()

    def _release_handle(self) -> None:
        """Stop the reader and close the device without raising from teardown."""
        deck = getattr(self, "deck", None)
        if deck is None:
            return
        try:
            deck.release_handle()
        except Exception:
            log.opt(exception=True).warning("Failed to release the deck handle")

    def init_inputs(self) -> None:
        # Build then swap. The media writer reads self.inputs concurrently, and a fill in place
        # gives it an empty or partial view, which raises a KeyError at screensaver entry.
        new_inputs: dict[type[InputIdentifier], list[Any]] = {}
        for i in Input.All:
            new_inputs[i] = []
            input_class = CONTROLLER_CLASSES[i]

            for k in input_class.Available_Identifiers(self.deck):
                # The dict key and the identifier constructor share the same input
                # type, so each input class receives its own identifier kind.
                controller_input = input_class(self, cast(Any, Input.FromTypeIdentifier(i.input_type, k)))
                # Stamp with the current generation so a paint from a freshly built input, such as
                # the screensaver's, is not dropped as stale.
                controller_input.config_gen = self._page_load_generation
                new_inputs[i].append(controller_input)
        self.inputs = new_inputs

    def get_inputs(self, identifier: InputIdentifier) -> list["ControllerInput[Any]"]:
        input_type = type(identifier)
        if input_type not in self.inputs:
            raise ValueError(f"Unknown input type: {input_type}")
        return self.inputs[input_type]

    # The key-dependent value type of the registry is expressible here, where a concrete identifier
    # class is in hand.
    @overload
    def get_input(self, identifier: Input.Key) -> "ControllerKey | None": ...

    @overload
    def get_input(self, identifier: Input.Dial) -> "ControllerDial | None": ...

    @overload
    def get_input(self, identifier: Input.Touchscreen) -> "ControllerTouchScreen | None": ...

    @overload
    def get_input(self, identifier: InputIdentifier) -> "ControllerInput[Any] | None": ...

    def get_input(self, identifier: InputIdentifier) -> "ControllerInput[Any] | None":
        for i in self.get_inputs(identifier):
            if i.identifier == identifier:
                return i
        return None

    def serial_number(self) -> str:
        if self._serial_number is None:
            self._serial_number = self.deck.get_serial_number()
        return self._serial_number
    
    def is_visual(self) -> bool:
        return self.deck.is_visual()

    def update_input(self, identifier: InputIdentifier) -> None:
        i = self.get_input(identifier)
        if not i:
            return
        i.update()

    @log.catch
    def update_all_inputs(self, gen: "int | None" = None) -> None:
        if not self._page_is_current(gen):
            return
        start = time.time()
        if not self.get_alive(): return
        if self.background.video is not None:
            log.debug("Skipping update_all_inputs (device keys) because there is a background video -- the per-frame video loop already paints the keys on the deck; a full key update() here would double-write and disturb that video. Dials + the in-app previews are still synced below.")

            for i in self.inputs[Input.Dial]:
                i.update()
            # UI-only mirror. The in-app KeyGrid is not the video device, so a push of the current
            # composite is a widget update and never a device write.
            for i in self.inputs[Input.Key]:
                try:
                    # First device paint for an opaque key.
                    state = i.get_active_state()
                    if state is not None and state.background_manager.get_composed_color()[-1] >= 255:
                        i.update()
                        continue
                except Exception:
                    log.exception(f"Opaque-key initial paint failed for {i.identifier}")
                try:
                    i.set_ui_key_image(i.get_current_image())
                except Exception:
                    log.exception(f"In-app preview sync failed for {i.identifier}")
            return
        for t in self.inputs:
            for i in self.inputs[t]:
                i.update()
        log.debug(f"Updating all inputs took {time.time() - start} seconds")

    def animations_gated(self) -> bool:
        """Skip animation only when presence is quiescent and no screensaver shows."""
        pm = getattr(gl, "presence_monitor", None)
        return pm is not None and pm.is_quiescent() and not self.screen_saver.showing

    def _reset_dedup_hashes(self) -> None:
        """Reset present hashes so visually identical content can repaint after device loss."""
        for key in self.inputs.get(Input.Key, []):
            key.present_state.reset()
        for touchscreen in self.inputs.get(Input.Touchscreen, []):
            touchscreen.present_state.reset()

    def _schedule_full_repaint(self) -> None:
        """Arm a repaint; rate limits defer it, and failures re-arm it until writes succeed."""
        self._full_repaint_pending = True

    def _run_pending_repaint(self) -> bool:
        """Run an armed repaint at least two seconds after the previous repaint."""
        if not self._full_repaint_pending:
            return False
        now = time.time()
        if now - self._last_full_repaint_ts < 2.0:
            return False
        self._full_repaint_pending = False
        self._last_full_repaint_ts = now
        self._reset_dedup_hashes()
        self.update_all_inputs()
        return True

    def _on_write_result(self, success: bool) -> None:
        """Record each device-write result and arm recovery after any failure.
        Only the media thread calls this method, so it needs no lock."""
        if success:
            if self._had_write_failure:
                self._had_write_failure = False
        else:
            self._had_write_failure = True
            self._full_repaint_pending = True

    def event_callback(self, ident: InputIdentifier, event: "DeckEvent") -> None:
        if not self.allow_interaction:
            return
        i = self.get_input(ident)
        if not i:
            return
        i.event_callback(event)

    def key_event_callback(self, deck: Any, key: int, state: bool) -> None:
        # key arrives already mapped into the logical grid.
        x, y = self.index_to_coords(key)
        dispatch_deck_event(self, Input.Key(f"{x}x{y}"), KeyEvent(pressed=bool(state)))

    def dial_event_callback(self, deck: Any, dial: Any, event_type: Any, value: Any) -> None:
        dispatch_deck_event(self, Input.Dial(str(dial)), DialEvent(kind=event_type, value=int(value)))

    def touchscreen_event_callback(self, deck: Any, event_type: Any, value: Any) -> None:
        dispatch_deck_event(self, Input.Touchscreen("sd-plus"), TouchscreenEvent(kind=event_type, value=value))

    ### Helper methods
    def generate_alpha_key(self) -> Image.Image:
        return Image.new("RGBA", self.get_key_image_size(), (0, 0, 0, 0))
    
    def get_key_image_size(self) -> tuple[int, int]:
        if self._key_image_size is not None:
            return self._key_image_size
        if not self.get_alive():
            # Dead or closing deck. Return the fallback without a memo, so a deck that comes back is
            # re-queried at its real size. Return a size and not None.
            return (72, 72)
        size = self.deck.key_image_format()["size"]
        size = max(size[0], 72), max(size[1], 72)
        self._key_image_size = size
        return size

    def native_key_format_sig(self) -> "NativeKeyFormatSig":
        """
        Hashable signature of the deck's native key image format. It is part of every native tile
        cache key, so bytes encoded for one device format can never be served for another.
        """
        sig = self._native_key_format_sig
        if sig is None:
            fmt = self.deck.key_image_format()
            sig = (fmt["size"], fmt["format"], fmt["flip"], fmt["rotation"])
            self._native_key_format_sig = sig
        return sig

    def clear_encoded_key_caches(self) -> None:
        """
        Drop every cached native key image, both the pixel-hash encode memo and the frame-identity
        native tiles.
        """
        for cache_name in ("encode_memo", "native_tile_cache"):
            cache = getattr(self, cache_name, None)
            if cache is not None:
                cache.clear()

    def _register_image_caches(self) -> None:
        """
        Enrol this deck's two native-image caches with the process-wide budget. The labels carry the
        serial, so a multi-deck rig's eviction and thrash logs are attributable.
        """
        serial = self.serial_number()
        cache_budget.register(self.encode_memo, label=f"encode_memo:{serial}")
        cache_budget.register(self.native_tile_cache, label=f"native_tiles:{serial}")

    def refresh_tile_cache_min_age(self, video: "BackgroundVideo | GifBackground | None" = None) -> None:
        """
        Retune how long the native tile cache shields its entries from global eviction, to the
        duration of the background video now playing, clamped to DEFAULT_MIN_AGE_S..MAX_MIN_AGE_S.
        """
        min_age = cache_budget.DEFAULT_MIN_AGE_S
        if video is not None:
            min_age = cache_budget.MAX_MIN_AGE_S
            try:
                # The GIF provider has no completion probe; it stays at the clamp maximum, as its
                # AttributeError under this try always left it.
                if isinstance(video, BackgroundVideo) and video.is_cache_complete():
                    fps = float(video.get_source_fps() or getattr(video, "fps", 0) or 0)
                    frames = int(getattr(video, "n_frames", 0) or 0)
                    if fps > 0 and frames > 0:
                        min_age = max(cache_budget.DEFAULT_MIN_AGE_S,
                                      min(cache_budget.MAX_MIN_AGE_S, frames / fps))
            except Exception:
                min_age = cache_budget.MAX_MIN_AGE_S
        cache = getattr(self, "native_tile_cache", None)
        if cache is not None:
            cache_budget.set_min_age(cache, min_age)

    def device_touchscreen_image_size(self) -> tuple[int, int]:
        """The strip size in the device's own frame; the band calibration is stated in it.
        A dead deck gets the SD+ fallback without a memo, like get_key_image_size."""
        size = self._touchscreen_image_size
        if size is None:
            if not self.get_alive():
                return (800, 100)
            device = self.deck.touchscreen_image_format()["size"]
            self._touchscreen_image_size = size = (max(device[0], 800), max(device[1], 100))
        return size

    def get_touchscreen_image_size(self) -> tuple[int, int]:
        # The size every strip composer draws at; the write task reads the device size itself.
        size = self.device_touchscreen_image_size()
        return (size[1], size[0]) if self.deck.strip_is_transposed() else size

    def logical_key_spacing(self) -> tuple[int, int]:
        """The key gaps in the frame the user sees, swapped at 90 and 270 like key_layout().
        The SD+ pair is asymmetric; a grid turned with the device pair misplaces every crop."""
        spacing = self.key_spacing
        return (spacing[1], spacing[0]) if self.deck.get_rotation() in (90, 270) else spacing

    # Page loading

    def load_default_page(self) -> None:
        if not self.get_alive(): return

        page_manager = gl.page_manager
        if page_manager is None:
            return

        queue = startup_queue.get()

        # A page change parked by the CLI for this serial. A claim removes it,
        # because the request is one-shot. See src/backend/startup_queue.py.
        api_page_path = queue.claim_page_request(self.serial_number())
        if api_page_path is not None:
            api_page_path = page_manager.find_matching_page_path(api_page_path)

        if api_page_path is None:
            default_page_path = page_manager.get_default_page(self.deck.get_serial_number())
        else:
            default_page_path = api_page_path

        if default_page_path is not None and not os.path.isfile(default_page_path):
            default_page_path = None
        if default_page_path is None:
            pages = page_manager.get_pages()
            if not pages:
                return
            default_page_path = pages[0]

        page = page_manager.get_page(default_page_path, self)
        if page is None:
            # None is a name that resolved but did not build; do not clear.
            return
        self.load_page(page)

        # Handle a state change request. This peeks now and resolves at the tail, so a failure in
        # between leaves the request parked.
        state_request = queue.peek_state_request(self.serial_number())
        if state_request is not None:
            result = control_plane.get().change_state_on(
                self,
                state_request["page_name"],
                state_request["coords"],
                state_request["state"],
            )
            if result.ok:
                log.info(result.message)
            else:
                log.error(f"State change failed on device {self.serial_number()}: {result.message}")

            queue.resolve_state_request(self.serial_number())

    @log.catch
    def load_background(self, page: Page, update: bool = True, gen: "int | None" = None) -> None:
        deck_background_settings = gl.settings_manager.deck_view(self.get_deck_settings()).section("background")
        page_background_settings = page.dict.get("settings", {}).get("background", {})

        log.info(f"Loading background in thread: {threading.get_ident()}")
        if deck_background_settings.get("enable", False) and not page_background_settings.get("overwrite", False):
            config = deck_background_settings
        elif page_background_settings.get("overwrite", False) and page_background_settings.get("show", False):
            config = page_background_settings
        else:
            config = {}

        # Serialize concurrent loads and drop superseded ones so an older switch
        # can't overwrite the newer page's background.
        with self._background_load_lock:
            if not self._page_is_current(gen):
                return
            # Set the flag first, with no repaint, so set_from_path renders
            # the tiles and the touchscreen slice at the correct geometry.
            self.background.set_extend_to_touchscreen(
                config.get("extend-to-touchscreen", False), update=False
            )
            # Two or more existing list images form a slideshow; one becomes the selected still.
            # With none, use the configured single path, which can be missing, or show nothing.
            pairs = resolve_background_entries(config)
            if len(pairs) >= 2:
                self.background.set_slideshow(
                    [p for p, _view in pairs],
                    interval=config.get("slideshow-interval", 10),
                    order=config.get("slideshow-order", "in-order"),
                    update=update,
                    views=[v for _path, v in pairs],
                )
            else:
                single, single_view = pairs[0] if pairs else (None, DEFAULT_VIEW)
                self.background.set_from_path(
                    path=single, update=update, view=single_view,
                    loop=config.get("loop", False),
                    fps=config.get("fps", 30),
                )

    @log.catch
    def load_brightness(self, page: Page) -> None:
        if not self.get_alive():
            return

        deck_brightness = gl.settings_manager.deck_view(self.get_deck_settings()).section("brightness")
        page_brightness = page.dict.get("settings",{}).get("brightness", {})

        if page_brightness.get("overwrite", False):
            value = page_brightness.get("value", 75)
        else:
            value = deck_brightness["value"]

        log.info(value)

        self.set_brightness(value)

    @log.catch
    def load_screensaver(self, page: Page) -> None:
        deck_screensaver_settings = gl.settings_manager.deck_view(self.get_deck_settings()).section("screensaver")
        page_screensaver_settings = page.dict.get("settings", {}).get("screensaver", {})

        log.info(f"Loading screensaver in thread: {threading.get_ident()}")
        if deck_screensaver_settings.get("enable", False) and not page_screensaver_settings.get("overwrite", False):
            config = deck_screensaver_settings
        elif page_screensaver_settings.get("overwrite", False) and page_screensaver_settings.get("enable", False):
            config = page_screensaver_settings
        else:
            config = {}

        self._apply_screensaver_config(config)

    def _apply_screensaver_config(self, config: dict[str, Any]) -> None:
        """
        Push one screensaver config onto the ScreenSaver.
        """
        self.screen_saver.set_media_path(config.get("media-path"))
        self.screen_saver.set_enable(config.get("enable", False))
        self.screen_saver.set_time(config.get("time-delay", 5))
        self.screen_saver.set_loop(config.get("loop", True))
        self.screen_saver.set_fps(config.get("fps", 30))
        self.screen_saver.set_brightness(config.get("brightness", 30))

    def _page_is_current(self, gen: "int | None") -> bool:
        # gen is None for a caller outside the page-load path, which always runs.
        # A paint that load_page issued is stale once a newer load_page bumped the generation.
        return gen is None or gen == self._page_load_generation

    # Deadline for load_all_inputs. An input load runs plugin callbacks that
    # can block forever, and none of them may wedge the media-player thread.
    LOAD_INPUTS_TIMEOUT = 10.0

    @log.catch
    def load_all_inputs(self, page: Page, update: bool = True, gen: "int | None" = None) -> None:
        if not self._page_is_current(gen):
            # This generation's rebuild will never run; publish it.
            self._input_load_done.publish(gen)
            return
        start = time.time()
        # The persistent per-deck pool: this runs on the media-player thread,
        # and a pool torn down per page switch is churn on the sole writer.
        executor = self.load_executor
        if executor is None:
            # close() set the pools to None; nothing will rebuild for this
            # generation now, so release a waiter on it.
            self._input_load_done.publish(gen)
            return
        # Each task re-checks the page generation before it touches an input, which lets the pool
        # abandon a wedged executor and drain late: a switch during the drain then lands nothing.
        tasks = [
            (str(controller_input.identifier),
             partial(self._load_input_if_current, controller_input, page, update, gen))
            for t in self.inputs
            for controller_input in self.inputs[t]
        ]
        executor.run_batch(tasks, deadline=self.LOAD_INPUTS_TIMEOUT)
        log.info(f"Loading all inputs took {time.time() - start} seconds")
        self._input_load_done.publish(gen)

    def _load_input_if_current(self, controller_input: "ControllerInput[Any]", page: Page, update: bool = True, gen: "int | None" = None) -> None:
        # A slower in-flight page load must not paint the previous page's images onto the current
        # page's keys, so skip when a newer load superseded this one.
        if not self._page_is_current(gen):
            return
        # The probe re-asks the generation inside the load, which the precheck
        # above stops covering once a plugin callback blocks and resumes.
        self.load_input(controller_input, page, update,
                        still_current=lambda: self._page_is_current(gen))

    def take_pending_screensaver_page(self) -> "Page | None":
        """Pop the page that load_page's screensaver guard recorded. None
        when no page change arrived while the screensaver was showing."""
        pending = self._screensaver_pending_page
        self._screensaver_pending_page = None
        return pending

    def load_input_from_identifier(self, identifier: InputIdentifier, page: Page, update: bool = True) -> None:
        controller_input = self.get_input(identifier)
        if controller_input is not None:
            self.load_input(controller_input, page, update)

    def load_input(self, controller_input: "ControllerInput[Any]", page: Page, update: bool = True, *,
                   still_current: "Callable[[], bool] | None" = None) -> None:
        config = controller_input.identifier.get_config(page)
        controller_input.load_from_input_dict(config, update, page=page,
                                              still_current=still_current)

    def close_image_ressources(self) -> None:
        """Release all input and background media resources."""
        for t in self.inputs:
            for i in self.inputs[t]:
                i.close_resources()

        # Hold _background_load_lock so a racing load cannot attach media after this sweep.
        with self._background_load_lock:
            if self.background.video is not None:
                self.background.video.close()
                self.background.video = None
            if self.background.image is not None:
                self.background.image.close()
                self.background.image = None

    # page None means clear the deck (the branch below). The store also answers
    # None for a build failure, which the switch and boot callers now intercept.
    @log.catch
    def load_page(self, page: Page | None, load_brightness: bool = True, load_screensaver: bool = True, load_background: bool = True, load_inputs: bool = True, allow_reload: bool = True) -> None:
        if not self.get_alive(): return
        if self._closing:
            # A straggling caller raced close(), from a screensaver follow-up, a plugin hook or a
            # DBus request.
            return

        start = time.time()

        # Serialize the whole switch body. The plugin-facing tail, the ChangePage signal and DBus,
        # stays outside, so a slow handler cannot block other callers on this lock.
        with self._load_page_lock:
            if self._closing:
                return
            if not allow_reload:
                if self.active_page is page:
                    return

            # Defer a non-None page while the screensaver owns the deck; do not change active_page.
            # Its video runs only while background.video.page is active_page; hide consumes pending.
            if self.screen_saver.showing:
                if page is not None:
                    self._screensaver_pending_page = page
                # A clear request, page=None, is dropped and not deferred.
                return

            # A monotonic counter that the mem_telemetry idle and trim gate reads.
            # Bump it once this call is a real switch, and not the no-op reload above.
            page_switches.bump()

            old_path = self.active_page.flush() if self.active_page is not None else None

            # Reset every key's pressed visual before the generation bump.
            for controller_key in self.inputs.get(Input.Key, []):
                controller_key.press_state = False

            # Set active_page and bump the generation together.
            with self._page_gen_lock:
                self.active_page = page
                self._page_load_generation += 1
                gen = self._page_load_generation
                # Key the input-load barrier to this generation under the bump.
                self._input_load_done.arm(gen, load_inputs and page is not None)

                # Stamp every input with the new generation now, under the same lock as the bump.
                for input_type in self.inputs:
                    for controller_input in self.inputs[input_type]:
                        controller_input.config_gen = gen

            if self._page_completion is not None:
                self._page_completion.cancel()

            # active_page protects the page now, so the fetch pin can release.
            if (manager := gl.page_manager) is not None:
                manager.pins.release_fetch(self)

            if page is None:
                self.clear()
                return

            log.info(f"Loading page {page.get_name()} on deck {self.deck.get_serial_number()}")

            # Stop the queued tasks. A newer switch that superseded this one
            # skips the stop.
            self.clear_media_player_tasks(gen)

            # Do not trigger the UI sync here. The new page's input states and actions do not exist
            # yet, so a sidebar rebuild renders the old page's data and nothing corrects it later.

            bg_future = None
            if load_background:
                # Decode the background off the media thread so it overlaps the input load.
                from src.backend.main_loop import log_future_exception
                if self._bg_future is not None:
                    self._bg_future.cancel()
                bg_future = self._bg_decode_pool.submit(
                    self.load_background, page, update=False, gen=gen)
                bg_future.add_done_callback(log_future_exception)
                self._bg_future = bg_future
            completion = PageLoadCompletion(self, gen, wait_for_inputs=load_inputs)
            self._page_completion = completion
            completion.watch_background(bg_future)
            if load_brightness:
                self.load_brightness(page)
            if load_screensaver:
                self.load_screensaver(page)
            if load_inputs:
                self.media_player.add_task(self.load_all_inputs, page, update=False, gen=gen)
                self.media_player.add_task(completion.inputs_finished)
            else:
                # No content reloads, but the generation bumped.
                # Advance each input's config_gen so its unchanged content is not dropped as stale.
                for input_type in self.inputs:
                    for controller_input in self.inputs[input_type]:
                        controller_input.config_gen = gen

        # Keep outside _load_page_lock: initialize_actions can wait on a main-thread page load.
        # Use page, not active_page; a newer switch can own active_page before this call.
        page.initialize_actions()

        # Second completion signal. action_objects exist now, so the sidebar's ActionManager can
        # render the new page's actions.
        ui_port.get().on_page_changed(self)

        # Use page.json_path because active_page can change or become None after lock release.
        gl.signal_manager.trigger_signal(Signals.ChangePage, self, old_path, page.json_path)

        # Notify DBus API of the page change
        notify_active_page_changed(self.serial_number(), page.get_name())

        log.info(f"Loaded page {page.get_name()} on deck {self.deck.get_serial_number()}")
        self.maybe_collect_garbage()

    # Minimum seconds between post-load garbage collections, so rapid page
    # switching does not pay a full GC pause on every switch.
    GC_MIN_INTERVAL = 10.0

    def maybe_collect_garbage(self) -> None:
        now = time.time()
        if now - self._last_gc_time < self.GC_MIN_INTERVAL:
            return
        self._last_gc_time = now
        gc.collect()

    def reload_page(self) -> None:
        self.load_page(
            page=self.active_page,
            allow_reload=True
        )

    def set_brightness(self, value: float) -> None:
        value = min(100, max(0, value))
        if not self.get_alive(): return
        if value == self.brightness:
            # The value is unchanged, so skip the queued device write.
            # The device stalls noticeably on a brightness write during an image-write burst.
            return
        # Route this through the media thread's control queue, so the device write runs on the sole
        # writer and not on the calling thread.
        self.brightness = value
        self.media_player.submit_control(SetBrightnessMsg(value))

    def set_rotation(self, value: int) -> None:
        """
        Turn the deck.
        """
        apply_rotation(self, value)

    # Longest quiet period between two tick-failure tracebacks, and the state that enforces it.
    TICK_ERROR_LOG_INTERVAL_S = 5.0
    _last_tick_error_log: float = 0.0
    _suppressed_tick_errors: int = 0

    def _note_tick_error(self, what: str) -> None:
        """
        Report a caught tick failure, at most one traceback per window. Suppressed repeats ride as a
        count on the next record, so the limit makes a flood quiet and never invisible.
        """
        now = time.time()
        if now - self._last_tick_error_log < self.TICK_ERROR_LOG_INTERVAL_S:
            self._suppressed_tick_errors += 1
            return
        suffix = (f" ({self._suppressed_tick_errors} earlier repeats were suppressed)"
                  if self._suppressed_tick_errors else "")
        # Arm the window before the log call, so a sink that raises cannot
        # leave it unarmed and turn the next failure into a storm.
        self._last_tick_error_log = now
        self._suppressed_tick_errors = 0
        log.opt(exception=True).error(
            f"action tick failed for {what} -- survived, continuing{suffix}")

    def tick_actions(self) -> None:
        # Event-based wait, as MediaPlayerThread._wake_event does.
        self._tick_stop_event.wait(self.TICK_DELAY)
        while self.keep_actions_ticking:
            start = time.time()
            ticked_page = self.mark_page_ready_to_clear(False)
            # A showing screensaver gets no per-input work from this loop.
            try:
                if not self.screen_saver.showing:
                    for t in self.inputs:
                        for i in self.inputs[t]:
                            # Guard each input, not the walk: a walk guard drops every input after
                            # the failing one.
                            try:
                                i.get_active_state().own_actions_tick_threaded()
                            except Exception:
                                self._note_tick_error(str(i.identifier))
            except Exception:
                # The walk's own steps, outside any one input: the screensaver read, and the guard
                # above when the identifier it names cannot be rendered.
                self._note_tick_error("the input walk")
            finally:
                # Reset the same page the False call marked. This runs in
                # finally, because a raising body pins the page forever.
                self.mark_page_ready_to_clear(True, ticked_page)

            end = time.time()
            wait = max(0.1, self.TICK_DELAY - (end - start))
            self._tick_stop_event.wait(wait)

    # Helper methods

    # Callers pass an "XxY" string, which Coords_To_Index splits itself, or an
    # already-split pair as a list or a tuple.
    def coords_to_index(self, coords: "str | Sequence[int] | Sequence[str]") -> int:
        return ControllerKey.Coords_To_Index(self.deck, coords)

    def index_to_coords(self, index: int) -> tuple[int, int]:
        return ControllerKey.Index_To_Coords(self.deck, index)

    def get_key_by_coords(self, coords: "str | Sequence[int] | Sequence[str]") -> "ControllerKey | None":
        index = self.coords_to_index(coords)
        return self.get_key_by_index(index)
    
    def get_key_by_index(self, index: int) -> "ControllerKey | None":
        keys = self.inputs.get(Input.Key, [])
        if index < 0 or index >= len(keys):
            return None
        return cast("ControllerKey | None", keys[index])

    def mark_page_ready_to_clear(self, ready_to_clear: bool, page: "Page | None" = None) -> "Page | None":
        """
        Pin the page that bracketed work must outlive with False, release it with True, and return
        it.
        """
        page = self.active_page if page is None else page
        return page if (pm := gl.page_manager) is None else pm.pins.bracket(page, ready_to_clear)
    
    def get_deck_settings(self) -> "dict[str, Any]":
        if not self.get_alive():
            return {}
        return gl.settings_manager.get_deck_settings(self.deck.get_serial_number())

    # Display saturation.
    DEFAULT_DISPLAY_SATURATION = 1.0
    # Valid range for the saturation factor, matching the UI scale DeckGroup.Saturation from 1.0 to
    # 1.5. A persisted value outside this range is corruption or a hand-edit, so clamp it.
    MIN_DISPLAY_SATURATION = 1.0
    MAX_DISPLAY_SATURATION = 1.5

    def _read_display_saturation(self) -> float:
        try:
            value = float(
                self.get_deck_settings().get("display", {}).get(
                    "saturation", self.DEFAULT_DISPLAY_SATURATION
                )
            )
        except (TypeError, ValueError):
            return self.DEFAULT_DISPLAY_SATURATION
        # float() accepts "nan" and "inf" without a raise.
        # Reject a non-finite value so it cannot reach an ImageEnhance factor or a cache key.
        if not math.isfinite(value):
            return self.DEFAULT_DISPLAY_SATURATION
        return min(self.MAX_DISPLAY_SATURATION, max(self.MIN_DISPLAY_SATURATION, value))

    def get_display_saturation(self) -> float:
        return self.display_saturation

    def set_display_saturation(self, value: float) -> None:
        """
        Persist the saturation factor to deck settings, refresh the cached value, and reload the
        active page so static media re-enhances at once.
        """
        value = round(float(value), 2)
        if abs(value - self.display_saturation) <= 0.001:
            # Same-value echo.
            return
        deck_settings = self.get_deck_settings()
        deck_settings.setdefault("display", {})["saturation"] = value
        gl.settings_manager.save_deck_settings(self.deck.get_serial_number(), deck_settings)

        self.display_saturation = value

        if self.active_page is not None:
            self.load_page(self.active_page, allow_reload=True)
    
    def get_own_deck_stack_child(self) -> "object | None":
        """Deprecated in-process shim for out-of-tree plugins.
        Return the attached UI child by controller identity, or None with no UI."""
        return ui_port.get().query_deck_widget(self, "deck_stack_child")

    def _write_blank_frames(self) -> None:
        """
        Write blank key images, and a blank touchscreen, directly to the device.
        """
        if not self.is_visual():
            return
        alpha_image = self.generate_alpha_key()
        native_image = PILHelper.to_native_key_format(self.deck, alpha_image.convert("RGB"))
        for i in range(self.deck.key_count()):
            self.deck.set_key_image(i, native_image)

        if self.deck.is_touch():
            touchscreen_size = self.deck.touchscreen_image_format()["size"]
            empty = Image.new("RGB", touchscreen_size, (0, 0, 0))
            native_image = PILHelper.to_native_touchscreen_format(self.deck, empty)

            self.deck.set_touchscreen_image(native_image, x_pos=0, y_pos=0, width=touchscreen_size[0], height=touchscreen_size[1])

    def _clear_direct(self) -> None:
        """Clear synchronously only for the pre-writer bootstrap liveness probe."""
        self._write_blank_frames()

    def clear(self, expects_repaint: bool = False) -> None:
        """Queue a seq-stamped clear after older paints; newer paints survive.
        Set expects_repaint only when later content must recover a late clear."""
        seq = self.media_player.next_submit_seq()
        self.media_player.submit_control(ClearMsg(seq=seq, expects_repaint=expects_repaint))

    def get_own_key_grid(self) -> "object | None":
        """Deprecated in-process shim for out-of-tree plugins.
        Return the attached UI key grid, or None when no UI is attached."""
        return ui_port.get().query_deck_widget(self, "key_grid")
    
    def clear_media_player_tasks(self, gen: "int | None" = None) -> None:
        # Skip the clear when a newer page load superseded this one, so a late clear cannot strand
        # the newer load's freshly queued tasks.
        with self._page_gen_lock:
            if gen is not None and gen != self._page_load_generation:
                return
            self.media_player.tasks.clear()
            # The writer's discard helper takes the slot lock, so this cannot
            # interleave with the drain or a producer assignment.
            self.media_player.discard_paint_tasks("controller_queue_cleared")

    def close(self, remove_media: bool, app_quit: bool = False) -> None:
        """Idempotent teardown: remove_media gates resources; app_quit skips action teardown.
        Device/thread/registration cleanup always runs; non-quit main-thread calls warn."""
        # Serialize state removal with page installation, then run blocking hooks outside the lock.
        load_page_lock = getattr(self, "_load_page_lock", None)
        with load_page_lock if load_page_lock is not None else nullcontext():
            with self._close_lock:
                if self._closing:
                    return
                self._closing = True

            page_gen_lock = getattr(self, "_page_gen_lock", None)
            if page_gen_lock is not None:
                with page_gen_lock:
                    self._page_load_generation += 1
            page_completion = getattr(self, "_page_completion", None)
            if page_completion is not None:
                page_completion.cancel()
                self._page_completion = None
            bg_future = getattr(self, "_bg_future", None)
            if bg_future is not None:
                bg_future.cancel()
            bg_decode_pool = getattr(self, "_bg_decode_pool", None)
            if bg_decode_pool is not None:
                # Non-blocking: queued decodes die, a running one finishes
                # into cancelled completion state and the worker exits.
                bg_decode_pool.shutdown(wait=False, cancel_futures=True)

        if not app_quit and threading.current_thread() is threading.main_thread():
            # A soft guard, not a hard failure.
            log.warning(
                f"DeckController.close() for "
                f"{getattr(self, '_serial_number', None) or '<unknown>'} called "
                "from the main thread with app_quit=False -- a wedged plugin "
                "teardown hook (step 6) would freeze the UI. Callers should "
                "dispatch this on its own thread."
            )

        # Step 2 defuses the screensaver directly. Never call set_enable(False) or hide() here.
        screen_saver = getattr(self, "screen_saver", None)
        if screen_saver is not None:
            if screen_saver.timer:
                screen_saver.timer.cancel()
            screen_saver.enable = False
            screen_saver.showing = False

        # Step 3 stops the library's read thread first, so a stray input callback cannot fire into
        # the teardown below and the resume-from-suspend loop cannot reopen the device.
        if getattr(self, "deck", None) is not None:
            try:
                self.deck.stop_read_thread()
            except Exception:
                log.opt(exception=True).warning("Failed to stop the deck's read thread during close()")

        # Step 4 stops and joins the tick thread before any action teardown.
        self.keep_actions_ticking = False
        tick_stop_event = getattr(self, "_tick_stop_event", None)
        if tick_stop_event is not None:
            tick_stop_event.set()
        tick_thread = getattr(self, "tick_thread", None)
        if tick_thread is not None and tick_thread is not threading.current_thread():
            tick_thread.join(2.0)

        # Step 5 runs a bounded terminal clear and close through the sole writer.
        media_player = getattr(self, "media_player", None)
        if media_player is not None:
            try:
                media_player.submit_control(ClearAndCloseMsg())
            except Exception:
                log.opt(exception=True).warning("Failed to submit ClearAndClose during close()")
            media_player.stop(timeout=2.0)
            media_player.discard_paint_tasks("close_cleanup")

        write_input_latency_report(self)

        # Skip plugin hooks on app quit; otherwise bound the join and continue after timeout.
        if not app_quit:
            teardown_thread = threading.Thread(
                target=self._teardown_actions,
                name=f"DeckCloseTeardown-{getattr(self, '_serial_number', None) or '?'}",
                daemon=True,
            )
            teardown_thread.start()
            teardown_thread.join(self.TEARDOWN_JOIN_TIMEOUT_S)
            if teardown_thread.is_alive():
                log.error(
                    f"close(): action teardown still running after "
                    f"{self.TEARDOWN_JOIN_TIMEOUT_S:.0f}s -- a plugin teardown "
                    f"hook is wedged; abandoning it and completing "
                    f"device/registration teardown"
                )

        # Step 7 sweeps the resources. The writer is stopped, so no
        # concurrent paint touches these caches and objects.
        if remove_media:
            try:
                self.close_image_ressources()
            except Exception:
                log.opt(exception=True).warning("Failed to close image resources during close()")
            self.clear_encoded_key_caches()
            if media_player is not None:
                media_player.tasks.clear()
                media_player.control_q.clear()
        # Fallback release. The writer normally released the device from step 5's ClearAndCloseMsg.
        # This matters only when that writer wedged and never processed it.
        self._release_handle()

        # Step 8 deregisters, and it also writes. It flushes every page still cached for this deck
        # before it drops the entries that hold them.
        page_manager = gl.page_manager
        if page_manager is not None:
            page_manager.discard_controller(self)
        self.active_page = None
        # A page change deferred while the screensaver showed otherwise pins its whole page object
        # graph on this dead controller.
        self._screensaver_pending_page = None

        # Step 9 shuts down the per-deck thread pools.
        action_executor = getattr(self, "action_executor", None)
        if action_executor is not None:
            # Do not wait. A misbehaving plugin callback can block a worker
            # forever, and the app's force_quit timer is the backstop.
            action_executor.shutdown(wait=False, cancel_futures=True)
            self.action_executor = None
        load_executor = getattr(self, "load_executor", None)
        if load_executor is not None:
            load_executor.shutdown(wait=False, cancel_futures=True)
            self.load_executor = None
        gc.collect()

    def _teardown_actions(self) -> None:
        """
        Tear down every action this controller ever cached a page for, not only active_page.
        """
        page_manager = gl.page_manager
        cached_pages = page_manager.pages_for_controller(self) if page_manager is not None else []
        for page in cached_pages:
            try:
                page.clear_action_objects()
            except Exception:
                log.opt(exception=True).warning(f"Failed to clear action objects for {page} during close()")

        screen_saver = getattr(self, "screen_saver", None)
        if screen_saver is None:
            return

        original_inputs = screen_saver.original_inputs
        if original_inputs:
            for inputs in list(original_inputs.values()):
                for controller_input in list(inputs):
                    try:
                        controller_input.close_resources()
                    except Exception:
                        log.opt(exception=True).warning("Failed to close a stashed screensaver input during close()")
            original_inputs.clear()

        original_background = screen_saver.original_background
        if original_background is not None:
            try:
                if getattr(original_background, "video", None) is not None:
                    original_background.video.close()
                if getattr(original_background, "image", None) is not None:
                    original_background.image.close()
            except Exception:
                log.opt(exception=True).warning("Failed to close the stashed screensaver background during close()")
            screen_saver.original_background = None

    def delete(self) -> None:
        """Thin alias for close(), kept for existing callers such as the
        harness teardown() helper."""
        self.close(remove_media=True, app_quit=False)

    def get_alive(self) -> bool:
        try:
            return self.deck.is_open()
        except Exception as e:
            log.debug(f"Cougth dead deck error. Error: {e}")
            return False
