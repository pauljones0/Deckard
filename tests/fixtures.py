"""Shared fixtures that set a temporary DATA_PATH for the headless harness.
Import before src or globals because globals.py reads argv at import."""
import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import time

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Isolated data dir, set before the first import globals.
DATA_DIR = tempfile.mkdtemp(prefix="sc_harness_")
sys.argv = [
    sys.argv[0] if sys.argv else "test",
    "--data", DATA_DIR,
    "--devel",
    "--skip-load-hardware-decks",
]


def _cleanup_data_dir():
    shutil.rmtree(DATA_DIR, ignore_errors=True)


atexit.register(_cleanup_data_dir)

import globals as gl  # noqa: E402  (must follow the argv setup above)

if gl.DATA_PATH != DATA_DIR:
    raise RuntimeError(
        f"globals.DATA_PATH ({gl.DATA_PATH!r}) did not pick up the harness's "
        f"temp dir ({DATA_DIR!r}) -- something imported `globals` before "
        f"`fixtures` did in this process. Make sure test/scenario scripts "
        f"`import fixtures` first."
    )

_REAL_DATA_ROOTS = (
    os.path.expanduser("~/.var/app/io.github.nazbert.Deckard"),
    # Keep the pre-rename data dir guarded while it or its symlink exists.
    os.path.expanduser("~/.var/app/com.core447.StreamController"),
)
if gl.DATA_PATH.startswith(_REAL_DATA_ROOTS):
    raise RuntimeError("refusing to run the harness against the real user data dir")

from src.backend.DeckManagement.InputIdentifier import Input  # noqa: E402
from src.backend.DeckManagement.deck_controller.paint_protocol import (  # noqa: E402
    KeyPresentState, TouchscreenPresentState,
)
from src.backend.settings_store import DeckSettings  # noqa: E402
from faulty_fake_deck import FaultyFakeDeck  # noqa: E402


# Stub gl.* collaborators

class _StubDeckSettings(DeckSettings):
    """Deck-settings view that saves into a StubSettingsManager dict.

    The unit tier has no settings files.
    """

    def __init__(self, data: dict, serial: str, manager: "StubSettingsManager"):
        super().__init__(data, serial)
        self._manager = manager

    def save(self) -> None:
        self._manager.save_deck_settings(self.serial, self.data)


class StubSettingsManager:
    """Unit-tier settings manager for app and deck operations under test."""

    def __init__(self, app_settings: dict = None):
        self._app_settings = app_settings if app_settings is not None else {}
        self._deck_settings: dict[str, dict] = {}

    def get_app_settings(self) -> dict:
        return self._app_settings

    def app(self):
        from src.backend.SettingsManager import AppSettings
        return AppSettings(self._app_settings)

    def get_deck_settings(self, serial_number: str) -> dict:
        return self._deck_settings.setdefault(serial_number, {})

    def save_deck_settings(self, serial_number: str, settings: dict) -> None:
        self._deck_settings[serial_number] = settings

    def deck(self, serial_number: str) -> "_StubDeckSettings":
        return _StubDeckSettings(self.get_deck_settings(serial_number), serial_number, self)

    def deck_view(self, settings: dict) -> DeckSettings:
        return DeckSettings(settings)


class StubDeckManager:
    """Deck manager stand-in that records connection and removal calls."""

    def __init__(self):
        self.deck_controller: list = []
        self.remove_calls: list = []
        self.connect_calls: int = 0

    def remove_controller(self, deck_controller) -> None:
        self.remove_calls.append(deck_controller)
        if deck_controller in self.deck_controller:
            self.deck_controller.remove(deck_controller)

    def connect_new_decks(self) -> None:
        self.connect_calls += 1

    def close_all(self) -> None:
        """Delegate to the production close_all_controllers protocol."""
        from src.backend.DeckManagement.DeckManager import close_all_controllers

        close_all_controllers(self.deck_controller)


# Unit and integration tiers install incompatible gl.* graphs.
# Refuse mixed installation before order-dependent collaborator failures occur.
_stub_globals_installed = False
_integration_globals_installed = False


def install_stub_globals(app_settings: dict = None) -> StubDeckManager:
    """Install unit-tier globals and return the deck manager.
    Refuse installation after the incompatible integration tier."""
    global _stub_globals_installed
    if _integration_globals_installed:
        raise RuntimeError(
            "install_stub_globals() called after the INTEGRATION tier is already "
            "installed in this process. The unit and integration gl.* graphs are "
            "incompatible -- a scenario must use exactly one tier. Split the mixed "
            "assertions into separate scenario_*.py files (each runs in its own "
            "subprocess) instead of calling both installers here."
        )
    gl.settings_manager = StubSettingsManager(app_settings=app_settings)
    deck_manager = StubDeckManager()
    gl.deck_manager = deck_manager
    _stub_globals_installed = True
    return deck_manager


# Unit tier

class StubBackground:
    def __init__(self):
        self.video = None
        self.slideshow = None

    def slideshow_tick(self, now=None):
        # Match a real background with no slideshow
        return False


class StubScreenSaver:
    """Screensaver stand-in whose only read attribute is showing.

    DeckController.animations_gated() reads showing as its second term.
    """

    def __init__(self):
        self.showing = False


class _QuietInputState:
    """Input state with no video or scrolling labels for key-tick tests."""
    key_video = None
    video = None
    background_video = None

    class _NoScrollLabels:
        @staticmethod
        def get_has_scroll_labels() -> bool:
            return False

    label_manager = _NoScrollLabels()


_QUIET_STATE = _QuietInputState()


class StubInput:
    """Input stand-in with production present-state tracking.
    update() forces paint; the unit tier has no content to deduplicate."""

    def __init__(self, controller: "StubDeckController", index: int, touchscreen: bool = False):
        self.controller = controller
        self.index = index
        self.touchscreen = touchscreen
        self.present_state = (TouchscreenPresentState() if touchscreen
                              else KeyPresentState(index))

    def get_active_state(self) -> _QuietInputState:
        return _QUIET_STATE

    def update(self) -> None:
        img = make_native_image(fill=self.index)
        self.present_state.offer(
            self.controller.media_player,
            page=self.controller.active_page,
            config_gen=self.controller._page_load_generation,
            img_hash=hash(img),
            encode=lambda: img,
            force=True,
        )


class StubDeckController:
    """Unit-tier controller with production write and repaint methods."""

    def __init__(self, deck=None, serial: str = "stub-serial-1", n_keys: int = 0, has_touchscreen: bool = False):
        self.deck = deck if deck is not None else FaultyFakeDeck(serial_number=serial)
        self._serial = serial
        self.active_page = object()  # opaque page sentinel, compared by identity
        self._page_load_generation = 0
        self._page_gen_lock = threading.Lock()
        self.background = StubBackground()
        self.screen_saver = StubScreenSaver()
        self.inputs = {
            Input.Key: [StubInput(self, i) for i in range(n_keys)],
            Input.Dial: [],
            Input.Touchscreen: [StubInput(self, 0, touchscreen=True)] if has_touchscreen else [],
        }
        self.media_player = None  # set by make_stub_controller()
        self._had_write_failure = False
        self._full_repaint_pending = False
        self._last_full_repaint_ts = 0.0
        self.repaint_count = 0

    def serial_number(self) -> str:
        return self._serial

    def get_touchscreen_image_size(self):
        return (800, 100)

    def is_visual(self) -> bool:
        return self.deck.is_visual()

    def _release_handle(self) -> None:
        """Release the raw unit-tier deck through the BetterDeck helper.
        Swallow close failures so ClearAndClose cannot stop the writer."""
        from src.backend.DeckManagement.BetterDeck import release_device_handle

        try:
            release_device_handle(self.deck)
        except Exception as e:
            print(f"stub _release_handle: failed to release the deck handle: {e}")

    def _write_blank_frames(self) -> None:
        """Write a blank marker to each key and the touchscreen."""
        if not self.is_visual():
            return
        for i in range(self.deck.key_count()):
            self.deck.set_key_image(i, b"\x00" * 16)
        if self.deck.is_touch():
            size = self.get_touchscreen_image_size()
            self.deck.set_touchscreen_image(b"\x00" * 16, x_pos=0, y_pos=0, width=size[0], height=size[1])

    # Bind write and repaint methods to DeckController; do not copy them

    def update_all_inputs(self, gen=None) -> None:
        """Update all inputs and count each full repaint."""
        for t in self.inputs:
            for i in self.inputs[t]:
                i.update()
        self.repaint_count += 1

    def new_page(self):
        """A fresh opaque page sentinel, distinct from .active_page."""
        return object()

    def bump_generation(self) -> int:
        with self._page_gen_lock:
            self._page_load_generation += 1
            return self._page_load_generation


_stub_methods_bound = False


def _bind_real_deckcontroller_methods() -> None:
    """Bind the production write-result and repaint protocol to the stub.
    Import lazily to keep psutil and mem_telemetry out of D-Bus scenarios."""
    global _stub_methods_bound
    if _stub_methods_bound:
        return
    from src.backend.DeckManagement.DeckController import DeckController as _RealDeckController

    StubDeckController._reset_dedup_hashes = _RealDeckController._reset_dedup_hashes
    StubDeckController._schedule_full_repaint = _RealDeckController._schedule_full_repaint
    StubDeckController._run_pending_repaint = _RealDeckController._run_pending_repaint
    StubDeckController._on_write_result = _RealDeckController._on_write_result
    StubDeckController.animations_gated = _RealDeckController.animations_gated
    _stub_methods_bound = True


def make_stub_controller(serial: str = "stub-serial-1", n_keys: int = 0, has_touchscreen: bool = False):
    """Build a stub controller with an unstarted MediaPlayerThread.
    Unit scenarios drive perform_media_player_tasks() directly."""
    from src.backend.DeckManagement.DeckController import MediaPlayerThread

    _bind_real_deckcontroller_methods()
    deck_manager = install_stub_globals()
    controller = StubDeckController(serial=serial, n_keys=n_keys, has_touchscreen=has_touchscreen)
    media_player = MediaPlayerThread(deck_controller=controller)
    controller.media_player = media_player
    return controller, media_player, deck_manager


def make_native_image(size=(72, 72), fill: int = 0) -> bytes:
    """Cheap stand-in for an encoded key image.

    The write path only hashes the payload; it never decodes JPEG.
    """
    return bytes([fill]) * (size[0] * size[1])


# Integration tier

def make_test_png(path: str, size=(72, 72), color=(255, 0, 0)) -> str:
    """Write a tiny solid-color PNG to path and return the path."""
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")
    return path


def make_test_mp4(path: str, size=(200, 100), n_frames=30, fps=15,
                  color=(64, 128)) -> str:
    """Build a video with distinct frame hashes and a fixed green-red pair."""
    import cv2
    import numpy as np
    os.makedirs(os.path.dirname(path), exist_ok=True)
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    green, red = color
    for i in range(n_frames):
        frame = np.full((size[1], size[0], 3), (i * 8 % 255, green, red), dtype=np.uint8)
        writer.write(frame)
    writer.release()
    assert os.path.getsize(path) > 0
    return path


def seed_page_with_background(page_name: str, media_path: str, data_dir: str = None,
                              loop: bool = False, fps: int = 30) -> str:
    """Seed a page that overwrites the deck background with media_path.

    Two such pages hash differently in the journal.
    """
    data_dir = data_dir if data_dir is not None else gl.DATA_PATH
    pages_dir = os.path.join(data_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({
            "keys": {}, "dials": {}, "touchscreens": {},
            "settings": {
                "background": {
                    "overwrite": True,
                    "show": True,
                    "media-path": media_path,
                    "loop": loop,
                    "fps": fps,
                },
            },
        }, f)
    return path


def seed_page_with_background_and_screensaver(
    page_name: str, media_path: str, screensaver_media_path: str,
    screensaver_time_delay: int = 60, data_dir: str = None,
) -> str:
    """Seed background and screensaver settings in one page.
    Persist the path because each load overwrites screensaver media_path."""
    data_dir = data_dir if data_dir is not None else gl.DATA_PATH
    pages_dir = os.path.join(data_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({
            "keys": {}, "dials": {}, "touchscreens": {},
            "settings": {
                "background": {
                    "overwrite": True,
                    "show": True,
                    "media-path": media_path,
                    "loop": False,
                    "fps": 30,
                },
                "screensaver": {
                    "overwrite": True,
                    "enable": True,
                    "media-path": screensaver_media_path,
                    "time-delay": screensaver_time_delay,
                    "loop": False,
                    "fps": 30,
                    "brightness": 30,
                },
            },
        }, f)
    return path


def seed_page(page_name: str = "Main", data_dir: str = None) -> str:
    """Idempotently write an action-free page and return its path.
    Empty inputs prevent load_action_objects() from using gl.plugin_manager."""
    data_dir = data_dir if data_dir is not None else gl.DATA_PATH
    pages_dir = os.path.join(data_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump({"keys": {}, "dials": {}, "touchscreens": {}}, f)
    return path


def _install_integration_globals() -> None:
    """Idempotently install the globals required by DeckController.
    Refuse installation after the incompatible unit tier."""
    global _integration_globals_installed
    if _integration_globals_installed:
        return
    if _stub_globals_installed:
        raise RuntimeError(
            "make_headless_controller()/_install_integration_globals() called "
            "after the UNIT tier (install_stub_globals / make_stub_controller) is "
            "already installed in this process. The unit and integration gl.* "
            "graphs are incompatible -- a scenario must use exactly one tier. "
            "Split the mixed assertions into separate scenario_*.py files (each "
            "runs in its own subprocess) instead of calling both installers here."
        )

    from src.backend.SettingsManager import SettingsManager
    from src.backend.PageManagement.PageManagerBackend import PageManagerBackend
    from src.Signals.SignalManager import SignalManager

    gl.settings_manager = SettingsManager()
    gl.signal_manager = SignalManager()
    gl.page_manager = PageManagerBackend(gl.settings_manager)
    gl.deck_manager = StubDeckManager()
    _integration_globals_installed = True


def make_headless_controller(serial: str = "headless-1", key_layout=None, page_name: str = "Main",
                             model=None):
    """Build an integration-tier DeckController with a seeded empty page.
    model accepts a preset name, FakeDeckModel, or None for the default."""
    _install_integration_globals()
    seed_page(page_name)

    from src.backend.DeckManagement.DeckController import DeckController

    deck = FaultyFakeDeck(serial_number=serial, deck_type="Fake Deck", key_layout=key_layout,
                          model=model)
    controller = DeckController(gl.deck_manager, deck)
    gl.deck_manager.deck_controller.append(controller)
    return controller


def raw_deck(controller) -> FaultyFakeDeck:
    """Unwrap the BetterDeck around controller.deck, return the FaultyFakeDeck.

    Integration tier only. The unit-tier stub holds the FaultyFakeDeck already.
    """
    return controller.deck.deck


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Poll predicate() until it is true or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def start_watchdog(seconds: float, label: str = "scenario") -> None:
    """Exit after a bounded delay and identify a deadlocked scenario."""
    def _fire():
        time.sleep(seconds)
        print(f"FAIL: {label} watchdog fired after {seconds}s -- likely deadlock", flush=True)
        os._exit(1)

    t = threading.Thread(target=_fire, name=f"{label}-watchdog", daemon=True)
    t.start()


def has_usable_display() -> bool:
    """Return whether GTK has both successful initialization and a display.
    Import gi lazily so non-GTK scenarios do not load it."""
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gtk
    return bool(Gtk.init_check()) and Gdk.Display.get_default() is not None


def teardown(controller) -> None:
    """Bounded shutdown that mirrors DeckManager.remove_controller.

    The UI-stack removal is absent, because the null port no-ops it here.
    """
    try:
        if controller in gl.deck_manager.deck_controller:
            gl.deck_manager.deck_controller.remove(controller)
    except Exception:
        pass
    try:
        controller.keep_actions_ticking = False
        controller.delete()
    except Exception:
        pass
    tick_thread = getattr(controller, "tick_thread", None)
    if tick_thread is not None:
        tick_thread.join(timeout=2.0)


# Stub plugin manager and latch action for wipe-restore scenarios
# Action pages need this graph because the harness has no plugin manager.

STUB_ACTION_ID = "dev_test_LatchAction"


def make_latch_action_class():
    """Return a fresh ActionCore subclass that paints state 1 only once."""
    # Import here, not at module scope. ActionCore is dead weight for the unit
    # tier, and its module graph must stay out of the pure-DBus scenarios.
    from src.backend.PluginManager.ActionCore import ActionCore

    class LatchAction(ActionCore):
        # Set by make_stub_plugin_manager's factory before construction.
        icon_path = None

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.current_state = -1

        def load_event_overrides(self):
            pass

        def load_initial_generative_ui(self):
            pass

        def has_image_control(self):
            return True

        def on_ready(self):
            self.on_tick()

        def on_tick(self):
            if self.current_state == 1:
                return
            self.current_state = 1
            self.set_media(media_path=type(self).icon_path, size=0.8)

    return LatchAction


class _StubActionHolder:
    """ActionHolder stand-in for initial and repeated page loads.
    Exposes action_core so the loader can reuse a compatible slot object."""

    def __init__(self, action_cls, action_id: str, icon_path):
        self.action_core = action_cls
        self._action_id = action_id
        self._icon_path = icon_path

    def get_is_compatible(self):
        return True

    def init_and_get_action(self, deck_controller, page, state, input_ident):
        self.action_core.icon_path = self._icon_path
        return self.action_core(
            action_id=self._action_id, action_name="LatchAction",
            deck_controller=deck_controller, page=page,
            plugin_base=_FAKE_PLUGIN_BASE, state=state, input_ident=input_ident,
        )


class _StubPluginManager:
    """Minimal gl.plugin_manager stand-in for action-loading scenarios.

    Implements the three methods Page.load_action_objects() dereferences.
    """

    def __init__(self, action_holder, action_id: str):
        self._holder = action_holder
        self._action_id = action_id

    def get_action_holder_from_id(self, action_id):
        return self._holder if action_id == self._action_id else None

    def get_plugin_id_from_action_id(self, action_id):
        return "dev_test"

    def get_is_plugin_out_of_date(self, plugin_id):
        return False


import types as _types  # noqa: E402  (local to this additive section)

_FAKE_PLUGIN_BASE = _types.SimpleNamespace(PATH="/tmp", backend=None)


def install_stub_plugin_manager(action_cls, icon_path, action_id: str = STUB_ACTION_ID):
    """Install a plugin manager that maps action_id to action_cls.
    Install it before DeckController.__init__ loads the default page."""
    holder = _StubActionHolder(action_cls, action_id, icon_path)
    gl.plugin_manager = _StubPluginManager(holder, action_id)
    return gl.plugin_manager


def seed_action_page(page_name: str, key_ident: str, action_id: str = STUB_ACTION_ID,
                     data_dir: str = None) -> str:
    """Seed a page whose single key carries action_id as its image control.

    State 0. Companion to seed_page() and seed_empty_action_page().
    """
    data_dir = data_dir if data_dir is not None else gl.DATA_PATH
    pages_dir = os.path.join(data_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({"keys": {key_ident: {"states": {"0": {
            "actions": [{"id": action_id, "settings": {}}],
            "image-control-action": 0,
        }}}}}, f)
    return path


def seed_empty_action_page(page_name: str, key_ident: str, data_dir: str = None) -> str:
    """Seed a page whose same key carries no action and no image control.

    Loading it must clear the slot.
    """
    data_dir = data_dir if data_dir is not None else gl.DATA_PATH
    pages_dir = os.path.join(data_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({"keys": {key_ident: {"states": {"0": {"actions": []}}}}}, f)
    return path
