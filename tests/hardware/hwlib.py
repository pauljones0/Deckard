"""Shared engine boot, write journal, synthetic data, and teardown helpers.
boot_engine imports globals after argv points at scratch data; each process boots one engine."""
import hashlib
import itertools
import json
import os
import shutil
import sys
import threading
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
WORK_ROOT = os.path.expanduser("~/.cache/deckard-hwverify/gated")


def wait_until(predicate, timeout=10.0, interval=0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _hash_bytes(data) -> str:
    if data is None:
        return "none"
    if isinstance(data, (bytes, bytearray)):
        return hashlib.sha1(bytes(data)).hexdigest()[:12]
    return hashlib.sha1(repr(data).encode()).hexdigest()[:12]


class Journal:
    """(t, seq, op, slot, hash, thread) per device write -- recorded AFTER
    the write returns, so a write that raised never appears as landed."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: list = []
        self._last: dict = {}
        self._seq = itertools.count(1)

    def record(self, op, slot, data):
        entry = (time.monotonic(), next(self._seq), op, slot,
                 _hash_bytes(data), threading.current_thread().name)
        with self._lock:
            self._entries.append(entry)
            self._last[slot] = entry

    def snapshot(self) -> list:
        with self._lock:
            return list(self._entries)

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def current_seq(self) -> int:
        with self._lock:
            return self._entries[-1][1] if self._entries else 0

    def last_op_for(self, slot):
        with self._lock:
            return self._last.get(slot)

    def after(self, seq: int) -> list:
        with self._lock:
            return self._entries[seq:]


def attach_journal(controller) -> Journal:
    """Journal the live BetterDeck write surface after each device write returns."""
    bd = controller.deck
    j = Journal()
    orig_key = bd.set_key_image
    orig_touch = bd.set_touchscreen_image
    orig_bright = bd.set_brightness

    def set_key_image(key, image):
        orig_key(key, image)
        j.record("set_key_image", f"key:{key}", image)

    def set_touchscreen_image(image, x_pos=0, y_pos=0, width=0, height=0):
        orig_touch(image, x_pos, y_pos, width, height)
        j.record("set_touchscreen_image", "touchscreen", image)

    def set_brightness(percent):
        orig_bright(percent)
        j.record("set_brightness", "brightness", percent)

    bd.set_key_image = set_key_image
    bd.set_touchscreen_image = set_touchscreen_image
    bd.set_brightness = set_brightness
    return j


def wait_quiet(journal: Journal, quiet_for=1.0, timeout=30.0) -> bool:
    """Wait until no device write has landed for quiet_for seconds."""
    deadline = time.monotonic() + timeout
    seen = journal.count()
    stable = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.05)
        now = journal.count()
        if now != seen:
            seen = now
            stable = time.monotonic()
        elif time.monotonic() - stable >= quiet_for:
            return True
    return False


class HwDeckManager:
    """Stands in for DeckManager, whose real __init__ starts a USB monitor
    and a portal probe. Only what the paths under test dereference."""

    def __init__(self):
        self.deck_controller: list = []
        self.remove_calls: list = []

    def remove_controller(self, dc):
        self.remove_calls.append(dc)
        if dc in self.deck_controller:
            self.deck_controller.remove(dc)

    def connect_new_decks(self):
        return 0

    def close_all(self):
        from src.backend.DeckManagement.DeckManager import close_all_controllers
        close_all_controllers(self.deck_controller)


def build_scratch(tag: str) -> str:
    """Build synthetic per-key icons and one page without reading real user data."""
    from PIL import Image, ImageDraw

    scratch = os.path.join(WORK_ROOT, tag)
    shutil.rmtree(scratch, ignore_errors=True)
    assets = os.path.join(scratch, "assets")
    pages_dir = os.path.join(scratch, "pages")
    os.makedirs(assets, exist_ok=True)
    os.makedirs(pages_dir, exist_ok=True)
    os.makedirs(os.path.join(scratch, "settings"), exist_ok=True)

    # Every key a different color, so per-key signatures never alias.
    keys = {}
    for i in range(32):
        img = Image.new("RGBA", (144, 144), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([16, 16, 128, 128],
                    fill=(40 + i * 6, 200 - (i * 5) % 180, 60 + (i * 3) % 150, 255))
        d.text((60, 60), str(i), fill=(255, 255, 255, 255))
        icon = os.path.join(assets, f"icon_{i}.png")
        img.save(icon)
        keys[f"{i % 8}x{i // 8}"] = {
            "states": {"0": {"media": {"path": icon}, "actions": []}}
        }

    page = os.path.join(pages_dir, "HWGate.json")
    with open(page, "w") as f:
        json.dump({"keys": keys, "dials": {}, "touchscreens": {}}, f, indent=4)
    return scratch


def default_page_for(scratch: str, serial: str) -> None:
    """Point the deck's default page at the synthetic one."""
    pages_file = os.path.join(scratch, "settings", "pages.json")
    cfg = {}
    if os.path.exists(pages_file):
        with open(pages_file) as f:
            cfg = json.load(f)
    cfg.setdefault("default-pages", {})[serial] = os.path.join(
        scratch, "pages", "HWGate.json")
    with open(pages_file, "w") as f:
        json.dump(cfg, f, indent=4)


def boot_engine(tag: str) -> dict:
    """Open exactly one real deck with a headless DeckController.
    Return globals, controller, journal, scratch data, serial, and key slots."""
    # Refuse concurrent access while the system instance owns the exclusive deck
    import hw_verify
    pid = hw_verify.dbus_owner_pid()
    if pid is not None:
        raise RuntimeError(
            f"the deck app is running (pid {pid}) and holds the device; run "
            f"through orchestrator.py, which takes the deck cleanly and gives "
            f"it back")

    scratch = build_scratch(tag)
    sys.path.insert(0, REPO)
    sys.argv = [sys.argv[0], "--data", scratch, "--devel",
                "--skip-load-hardware-decks"]

    import globals as gl
    if os.path.realpath(gl.DATA_PATH) != os.path.realpath(scratch):
        raise RuntimeError(f"DATA_PATH {gl.DATA_PATH} != scratch {scratch}")

    from src.backend.SettingsManager import SettingsManager
    from src.backend.PageManagement.PageManagerBackend import PageManagerBackend
    from src.Signals.SignalManager import SignalManager

    gl.settings_manager = SettingsManager()
    gl.signal_manager = SignalManager()
    gl.page_manager = PageManagerBackend(gl.settings_manager)
    gl.deck_manager = HwDeckManager()

    from StreamDeck.DeviceManager import DeviceManager
    from src.backend.DeckManagement.DeckController import DeckController

    decks = DeviceManager().enumerate()
    if len(decks) != 1:
        raise RuntimeError(f"expected exactly one deck, enumerated {len(decks)}")
    deck = decks[0]
    if not deck.is_open():
        deck.open(True)

    serial = str(deck.get_serial_number())
    default_page_for(scratch, serial)

    controller = DeckController(gl.deck_manager, deck)
    gl.deck_manager.deck_controller.append(controller)
    journal = attach_journal(controller)

    key_count = controller.deck.key_count()
    return {
        "gl": gl, "controller": controller, "journal": journal,
        "scratch": scratch, "serial": serial, "key_count": key_count,
        "key_slots": [f"key:{i}" for i in range(key_count)],
    }


def shutdown_engine(env: dict, timeout: float = 15.0) -> float:
    """Close through the production path and return the elapsed time.
    Raise if the media thread exceeds the bound or the device handle stays open."""
    controller = env["controller"]
    began = time.monotonic()
    env["gl"].threads_running = False
    env["gl"].deck_manager.close_all()
    media = controller.media_player
    if media is not None and media.is_alive():
        media.join(timeout)
        if media.is_alive():
            raise RuntimeError(f"the media writer outlived the {timeout:g}s teardown bound")
    # Stop the non-daemon tick thread because this process does not end with os._exit
    controller.keep_actions_ticking = False
    stop_event = getattr(controller, "_tick_stop_event", None)
    if stop_event is not None:
        stop_event.set()
    tick = getattr(controller, "tick_thread", None)
    if tick is not None and tick.is_alive():
        tick.join(5.0)
        if tick.is_alive():
            raise RuntimeError("the tick thread outlived the teardown")
    took = time.monotonic() - began
    if controller.deck.is_open():
        raise RuntimeError("the device handle is still open after close_all")
    return took
