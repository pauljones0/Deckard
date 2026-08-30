"""Verify DeckManager boot rescans after an empty initial enumeration.
Rescans use bounded backoff, register each deck once, and stop promptly on shutdown."""
import threading
import time
import types

import fixtures  # must be first; isolates DATA_PATH before import globals
import globals as gl

from StreamDeck.Transport.Transport import TransportError

from faulty_fake_deck import FaultyFakeDeck

import src.backend.DeckManagement.DeckManager as dm_module


class StubUSBMonitor:
    """usbmonitor.USBMonitor stand-in with no udev and no threads."""

    def __init__(self, *args, **kwargs):
        self.on_connect = None
        self.on_disconnect = None

    def start_monitoring(self, on_connect=None, on_disconnect=None):
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

    def stop_monitoring(self, timeout=None):
        pass


class StubPortal:
    def running_under_flatpak(self):
        return False


class ScriptedDeviceManager:
    """Provide a class-scripted StreamDeck.DeviceManager stand-in.
    Fresh instances share results because production builds a new manager per enumeration."""

    results: list = []
    enumerate_calls: int = 0
    _lock = threading.Lock()

    def enumerate(self):
        with ScriptedDeviceManager._lock:
            ScriptedDeviceManager.enumerate_calls += 1
            return list(ScriptedDeviceManager.results)


class FlakyOpenDeck(FaultyFakeDeck):
    """Raise TransportError for the first fail_opens calls and report the real open state.
    FakeDeck always reports open, which would bypass the initialization path under test."""

    def __init__(self, *args, fail_opens: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self._remaining_open_failures = fail_opens
        self._really_open = False
        self.open_attempts = 0

    def is_open(self):
        return self._really_open

    def open(self, *args, **kwargs):
        self.open_attempts += 1
        if self._remaining_open_failures > 0:
            self._remaining_open_failures -= 1
            raise TransportError("boot-storm open flake (-1)")
        self._really_open = True
        return super().open(*args, **kwargs)

    def close(self):
        self._really_open = False
        return super().close()


def make_deck_manager() -> "dm_module.DeckManager":
    manager = dm_module.DeckManager()
    # Shrink the backoff so the scenario runs in seconds, not minutes.
    manager.BOOT_RESCAN_DELAYS = (0.1, 0.15, 0.2, 0.3, 0.5)
    return manager


def phase_pickup_exactly_once() -> None:
    gl.deck_manager = manager = make_deck_manager()

    ScriptedDeviceManager.results = []
    ScriptedDeviceManager.enumerate_calls = 0

    manager.load_hardware_decks()  # empty enumeration -> arms the rescan
    assert manager._boot_rescan_thread is not None, "rescan not armed on empty enumeration"
    assert manager._boot_rescan_thread.is_alive(), "rescan thread not running"
    assert not manager.deck_controller

    # Let at least one empty retry complete. The startup call was the first.
    ok = fixtures.wait_until(lambda: ScriptedDeviceManager.enumerate_calls >= 2, timeout=3)
    assert ok, "rescan never re-enumerated"
    assert not manager.deck_controller, "controller appeared from an empty enumeration"

    # Race USB-monitor connect_new_decks against the next rescan after enumeration.
    # phase_concurrent_callers_exactly_once supplies the deterministic same-window race.
    deck = FaultyFakeDeck(serial_number="boot-rescan-1", deck_type="Fake Deck")
    ScriptedDeviceManager.results = [deck]

    hotplug = threading.Thread(target=manager.connect_new_decks, name="hotplug-sim")
    hotplug.start()

    assert fixtures.wait_until(lambda: len(manager.deck_controller) >= 1, timeout=10), \
        "deck was never registered"
    hotplug.join(timeout=10)
    assert not hotplug.is_alive(), "simulated hotplug call did not return"

    # The rescan must observe the registered deck and stop.
    assert fixtures.wait_until(lambda: not manager._boot_rescan_thread.is_alive(), timeout=10), \
        "rescan thread did not stop after the deck appeared"

    # Give a late duplicate registration a moment to land before the
    # exactly-once assertion.
    time.sleep(0.3)
    assert len(manager.deck_controller) == 1, \
        f"expected exactly 1 controller, got {len(manager.deck_controller)} (double-register)"
    assert manager.deck_controller[0].serial_number() == "boot-rescan-1"

    # Re-arming after a deck is present must not re-add it either.
    n_before = ScriptedDeviceManager.enumerate_calls
    manager.start_boot_rescan()
    assert fixtures.wait_until(lambda: not manager._boot_rescan_thread.is_alive(), timeout=10)
    assert ScriptedDeviceManager.enumerate_calls > n_before, "re-armed rescan never enumerated"
    assert len(manager.deck_controller) == 1, "re-armed rescan duplicated the deck"

    for controller in list(manager.deck_controller):
        fixtures.teardown(controller)


def phase_flaky_open_still_registered() -> None:
    """Continue rescanning when an enumerated deck fails to open.
    Retry within each round; only registration, not enumeration, stops rescanning."""
    gl.deck_manager = manager = make_deck_manager()
    # Enough rounds after the deck appears for a fully failed pickup round,
    # which runs three in-round open retries, plus the round that succeeds.
    manager.BOOT_RESCAN_DELAYS = (0.1, 0.1, 0.3, 0.3, 0.5, 0.5)

    ScriptedDeviceManager.results = []
    ScriptedDeviceManager.enumerate_calls = 0

    manager.load_hardware_decks()
    assert manager._boot_rescan_thread.is_alive()

    # Three failures exhaust first-round retries, so only a later rescan can register.
    # This covers both in-round retry and the registration-based stop condition.
    deck = FlakyOpenDeck(serial_number="flaky-open-1", deck_type="Fake Deck", fail_opens=3)
    ScriptedDeviceManager.results = [deck]

    assert fixtures.wait_until(lambda: len(manager.deck_controller) == 1, timeout=20), (
        "deck enumerated with a flaky open was never registered -- rescan "
        "stopped on enumerability instead of registration"
    )
    assert manager.deck_controller[0].serial_number() == "flaky-open-1"
    assert deck.open_attempts >= 4, f"expected >=4 open attempts, got {deck.open_attempts}"

    assert fixtures.wait_until(lambda: not manager._boot_rescan_thread.is_alive(), timeout=10), \
        "rescan did not stop after late registration"
    time.sleep(0.3)
    assert len(manager.deck_controller) == 1, "flaky deck registered more than once"

    for controller in list(manager.deck_controller):
        fixtures.teardown(controller)


def phase_concurrent_callers_exactly_once() -> None:
    """Make two barrier-synchronized callers register one deck exactly once.
    The barrier reliably exposes a weakened _connect_decks_lock that a timing-only race can miss."""
    gl.deck_manager = manager = make_deck_manager()

    TRIALS = 8
    for trial in range(TRIALS):
        deck = FaultyFakeDeck(serial_number=f"barrier-{trial}", deck_type="Fake Deck")
        ScriptedDeviceManager.results = [deck]
        before = len(manager.deck_controller)

        barrier = threading.Barrier(2)

        def caller():
            barrier.wait()
            manager.connect_new_decks()

        threads = [
            threading.Thread(target=caller, name=f"barrier-caller-{trial}-{i}")
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert all(not t.is_alive() for t in threads), f"trial {trial}: caller did not return"

        added = len(manager.deck_controller) - before
        assert added == 1, (
            f"trial {trial}: {added} controllers registered for one deck -- "
            f"connect_new_decks is not exactly-once under concurrent callers"
        )

    ScriptedDeviceManager.results = []
    for controller in list(manager.deck_controller):
        fixtures.teardown(controller)


def phase_clean_shutdown_during_backoff() -> None:
    manager = make_deck_manager()
    manager.BOOT_RESCAN_DELAYS = (30.0, 30.0)  # park it deep in backoff

    ScriptedDeviceManager.results = []
    manager.load_hardware_decks()
    assert manager._boot_rescan_thread is not None and manager._boot_rescan_thread.is_alive()

    start = time.monotonic()
    manager.stop_boot_rescan()
    elapsed = time.monotonic() - start
    assert not manager._boot_rescan_thread.is_alive(), \
        "rescan thread still alive after stop_boot_rescan()"
    assert elapsed < 2.5, f"stop_boot_rescan took {elapsed:.2f}s -- backoff sleep not interruptible"

    # Idempotent and safe with no rescan running.
    manager.stop_boot_rescan()


def main() -> None:
    fixtures.start_watchdog(60, "scenario_boot_enumeration")
    fixtures._install_integration_globals()
    fixtures.seed_page("Main")

    # Stub the environment-touching collaborators before any DeckManager is
    # constructed.
    dm_module.USBMonitor = StubUSBMonitor
    dm_module.Xdp = types.SimpleNamespace(Portal=types.SimpleNamespace(new=lambda: StubPortal()))
    dm_module.DeviceManager = ScriptedDeviceManager

    phase_pickup_exactly_once()
    phase_flaky_open_still_registered()
    phase_concurrent_callers_exactly_once()
    phase_clean_shutdown_during_backoff()

    print("PASS: scenario_boot_enumeration")


if __name__ == "__main__":
    main()
