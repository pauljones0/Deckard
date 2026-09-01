"""Verify PresenceMonitor gating and wake fan-out.
Screensaver never gates; system-idle uses lock and logind idle with deck-press grace.
"""

# Every transition wakes every deck. No deck, no GTK and no real bus run here.
import fixtures  # noqa: F401  (isolates gl.DATA_PATH before anything reads it)

import os
import time

import globals as gl
from gi.repository import GLib

from src.backend.PresenceMonitor.PresenceMonitor import (  # noqa: E402
    LOGIND_SESSION_IFACE,
    MODE_SCREENSAVER,
    MODE_SYSTEM_IDLE,
    PresenceMonitor,
)

SESSION_PATH = "/org/freedesktop/login1/session/_31"


class WakeRecorder:
    """Stands in for a deck's MediaPlayerThread on the fan-out path."""

    def __init__(self, raises: bool = False):
        self.wakes = 0
        self._raises = raises

    def wake(self) -> None:
        self.wakes += 1
        if self._raises:
            raise RuntimeError("media thread is gone")


class StubController:
    def __init__(self, raises: bool = False):
        self.media_player = WakeRecorder(raises=raises)


def install_controllers(*controllers):
    gl.deck_manager.deck_controller[:] = list(controllers)
    return controllers


def make_monitor(mode=MODE_SYSTEM_IDLE, minutes=1) -> PresenceMonitor:
    # enable_idle_detector=False everywhere except the fake-bus checks below. The
    # harness must never reach for the real system bus.
    return PresenceMonitor(
        mode=mode,
        idle_minutes=minutes,
        enable_idle_detector=False,
    )


def set_locked(monitor: PresenceMonitor, locked: bool) -> None:
    """What LockScreenManager.lock does. Publish the global, then notify."""
    gl.screen_locked = locked
    monitor.on_lock_changed(locked)


def check_default_mode_never_gates() -> None:
    monitor = make_monitor(mode=MODE_SCREENSAVER)
    assert monitor.is_quiescent() is False

    set_locked(monitor, True)
    assert monitor.is_quiescent() is False, "mode 'screensaver' must ignore lock"

    monitor.on_idle_hint_changed(True, idle_since=time.time() - 3600)
    assert monitor.is_quiescent() is False, "mode 'screensaver' must ignore idle"

    monitor.notify_activity()
    assert monitor.is_quiescent() is False

    set_locked(monitor, False)
    monitor.stop()
    print("PASS: default mode 'screensaver' never reports quiescent")


def check_lock_gates_and_unlock_clears() -> None:
    first_controller, second_controller = install_controllers(StubController(), StubController())
    monitor = make_monitor()
    assert monitor.is_quiescent() is False

    # Check only the transition increment because prior monitor deadlines can
    # add late wakes through the shared controller list under load.
    first_wakes, second_wakes = first_controller.media_player.wakes, second_controller.media_player.wakes
    set_locked(monitor, True)
    assert monitor.is_quiescent() is True, "lock must gate in system-idle mode"
    assert first_controller.media_player.wakes > first_wakes and second_controller.media_player.wakes > second_wakes, (
        f"gate transition must wake every deck: {first_controller.media_player.wakes}, "
        f"{second_controller.media_player.wakes}"
    )

    # Re-notifying the same state is not a transition and must not re-wake. No
    # deadline is armed here, so no late fan-out can land in this window.
    first_wakes = first_controller.media_player.wakes
    monitor.on_lock_changed(True)
    assert first_controller.media_player.wakes == first_wakes, "a no-op re-evaluation must not wake"

    first_wakes, second_wakes = first_controller.media_player.wakes, second_controller.media_player.wakes
    set_locked(monitor, False)
    assert monitor.is_quiescent() is False, "unlock must clear the gate"
    assert first_controller.media_player.wakes > first_wakes and second_controller.media_player.wakes > second_wakes, (
        "the ungate transition must wake every deck too"
    )
    monitor.stop()
    print("PASS: lock gates, unlock clears, both directions wake every deck")


def check_unlock_counts_as_activity() -> None:
    """Treat unlock as activity when the desktop leaves IdleHint set.
    Otherwise a stale idle hint keeps the deck gated across unlock."""
    monitor = make_monitor(minutes=1)
    monitor.on_idle_hint_changed(True, idle_since=time.time() - 600)
    set_locked(monitor, True)
    assert monitor.is_quiescent() is True

    set_locked(monitor, False)
    assert monitor.is_quiescent() is False, (
        "a stale IdleHint kept the deck gated straight through the unlock"
    )
    assert monitor._deadline is not None, (
        "the residual idle deadline was not re-armed -- the unlock cleared the "
        "gate but nothing would re-engage it"
    )
    # Re-armed from the unlock, not from the ten-minute-old IdleSinceHint.
    time.sleep(0.3)
    assert monitor.is_quiescent() is False
    monitor.stop()
    print("PASS: an unlock counts as activity and outlives a stale IdleHint")


def check_idle_arithmetic() -> None:
    monitor = make_monitor(minutes=1)

    # An idle well past the 1-minute residual gates on arrival.
    monitor.on_idle_hint_changed(True, idle_since=time.time() - 600)
    assert monitor.is_quiescent() is True, "an already-elapsed idle must gate at once"

    monitor.on_idle_hint_changed(False)
    assert monitor.is_quiescent() is False, "IdleHint clearing must ungate"

    # An idle that started 59.6s ago leaves about 0.4s of residual, so the
    # monitor is not quiescent yet and the deadline must fire on its own.
    monitor.on_idle_hint_changed(True, idle_since=time.time() - 60 + 0.4)
    assert monitor.is_quiescent() is False, (
        "an idle whose residual has not elapsed must not gate yet"
    )
    assert fixtures.wait_until(monitor.is_quiescent, timeout=3.0), (
        "the armed residual deadline never fired"
    )
    monitor.stop()
    print("PASS: idle arithmetic gates immediately or on the armed deadline")


def check_deck_activity_clears_and_rearms() -> None:
    (controller,) = install_controllers(StubController())
    monitor = make_monitor(minutes=1)

    # Assert the wake increment, not an exact total: a prior monitor's late
    # deadline can fan out through the shared controller list under load.
    prior_wakes = controller.media_player.wakes
    monitor.on_idle_hint_changed(True, idle_since=time.time() - 600)
    assert monitor.is_quiescent() is True
    assert controller.media_player.wakes > prior_wakes

    # The compositor still reports idle, because deck presses are invisible
    # to it. The press alone must clear the gate and restart the clock.
    prior_wakes = controller.media_player.wakes
    monitor.notify_activity()
    assert monitor.is_quiescent() is False, (
        "a deck press must clear the gate even while IdleHint is still true"
    )
    assert controller.media_player.wakes > prior_wakes, "clearing the gate must wake the deck"

    # It must not re-gate at once. The deadline now runs from the press, not
    # from the much older IdleSinceHint.
    time.sleep(0.3)
    assert monitor.is_quiescent() is False, "the deadline must re-arm from the press"
    monitor.stop()
    print("PASS: deck activity clears the gate and re-arms the deadline")


def check_deck_activity_outranks_lock() -> None:
    """With lock-on-lock-screen off, let deck activity outrank lock until grace expires.
    The gate must re-engage without another lock or idle event."""
    (controller,) = install_controllers(StubController())
    monitor = make_monitor()
    monitor.DECK_ACTIVITY_GRACE_S = 0.4  # the shipped 30s, tightened

    # With _last_deck_activity at 0.0, startup lock gates immediately; check
    # wake increments because prior deadlines can add wakes under load.
    prior_wakes = controller.media_player.wakes
    set_locked(monitor, True)
    assert monitor.is_quiescent() is True, (
        "a lock with no deck activity behind it must still gate immediately"
    )
    assert controller.media_player.wakes > prior_wakes

    prior_wakes = controller.media_player.wakes
    monitor.notify_activity()
    assert monitor.is_quiescent() is False, (
        "a deck press must un-gate even while the screen is locked -- the deck "
        "is live on lock whenever lock-on-lock-screen is off"
    )
    assert controller.media_player.wakes > prior_wakes, "un-gating must wake the deck"

    # The grace deadline re-gates without further input and sets quiescence
    # before its timer thread fans out, so wait separately for the wake.
    prior_wakes = controller.media_player.wakes
    assert fixtures.wait_until(monitor.is_quiescent, timeout=3.0), (
        "the grace expired but the gate never re-engaged -- its deadline was "
        "not armed"
    )
    assert fixtures.wait_until(lambda: controller.media_player.wakes > prior_wakes, timeout=1.0), (
        "the re-gate transition must wake the deck"
    )

    set_locked(monitor, False)
    monitor.stop()
    print("PASS: a deck press outranks the lock for the grace, then re-gates")


def check_set_mode_reevaluates() -> None:
    (controller,) = install_controllers(StubController())
    monitor = make_monitor(mode=MODE_SCREENSAVER)
    set_locked(monitor, True)
    assert monitor.is_quiescent() is False

    # Assert the wake increment, not an exact total: a prior monitor's late
    # deadline can fan out through the shared controller list under load.
    prior_wakes = controller.media_player.wakes
    monitor.set_mode(MODE_SYSTEM_IDLE, 5)
    assert monitor.is_quiescent() is True, "switching to system-idle while locked must gate"
    assert monitor.idle_minutes == 5
    assert controller.media_player.wakes > prior_wakes

    prior_wakes = controller.media_player.wakes
    monitor.set_mode(MODE_SCREENSAVER)
    assert monitor.is_quiescent() is False, "switching back must ungate immediately"
    assert controller.media_player.wakes > prior_wakes

    # An unknown value degrades to the conservative default rather than
    # leaving gating on.
    monitor.set_mode("nonsense")
    assert monitor.mode == MODE_SCREENSAVER
    assert monitor.is_quiescent() is False

    # Minutes are clamped to >= 1 (the SpinRow's floor).
    monitor.set_mode(MODE_SYSTEM_IDLE, 0)
    assert monitor.idle_minutes == 1, f"minutes not clamped: {monitor.idle_minutes}"

    set_locked(monitor, False)
    monitor.stop()
    print("PASS: set_mode re-evaluates and sanitizes its arguments")


def check_constructor_seeds_from_settings() -> None:
    """A restart while the screen is already locked must come up gated. The
    seed is what makes the setting survive a restart."""
    gl.settings_manager._app_settings.setdefault("performance", {}).update({
        "animation-pause-mode": MODE_SYSTEM_IDLE,
        "animation-idle-minutes": 9,
    })
    gl.screen_locked = True
    try:
        monitor = PresenceMonitor(enable_idle_detector=False)
        assert monitor.mode == MODE_SYSTEM_IDLE, (
            f"mode not seeded from AppSettings: {monitor.mode!r}"
        )
        assert monitor.idle_minutes == 9, (
            f"minutes not seeded from AppSettings: {monitor.idle_minutes}"
        )
        assert monitor.is_quiescent() is True, (
            "the constructor must evaluate against the CURRENT gl.screen_locked"
        )
        monitor.stop()
    finally:
        gl.screen_locked = False
        gl.settings_manager._app_settings["performance"].clear()

    # And with the shipped default the same restart changes nothing.
    gl.screen_locked = True
    try:
        monitor = PresenceMonitor(enable_idle_detector=False)
        assert monitor.mode == MODE_SCREENSAVER
        assert monitor.is_quiescent() is False, (
            "the DEFAULT mode must not gate on a locked-at-startup session"
        )
        monitor.stop()
    finally:
        gl.screen_locked = False
    print("PASS: constructor seeds mode/minutes and evaluates the current lock state")


def check_fan_out_snapshot_and_failure_isolation() -> None:
    """Contain list mutation and wake failures during controller fan-out.
    Unplug and close threads can mutate the list while torn-down controllers raise."""
    survivor = StubController()
    exploder = StubController(raises=True)

    class SelfRemovingController(StubController):
        def __init__(self):
            super().__init__()
            self.media_player = self

        def wake(self):
            # Mutates the very list the fan-out is iterating.
            gl.deck_manager.deck_controller.clear()
            self.wakes = getattr(self, "wakes", 0) + 1

    remover = SelfRemovingController()
    install_controllers(remover, exploder, survivor)

    monitor = make_monitor()
    set_locked(monitor, True)

    # Each must be woken at least once. An exact count is spurious under load,
    # where a prior monitor's late deadline fans out over the same list.
    assert remover.wakes >= 1, "the mutating controller itself was not woken"
    assert exploder.media_player.wakes >= 1, "a raising wake() was not attempted"
    assert survivor.media_player.wakes >= 1, (
        "the fan-out did not reach every controller -- it either iterated the "
        "live list or aborted on the first failure"
    )
    set_locked(monitor, False)
    monitor.stop()
    print("PASS: the wake fan-out iterates a snapshot and contains failures")


# logind IdleHint detector over a fake system bus.

class FakeSystemBus:
    """Record the three Gio.DBusConnection methods used by LogindIdleDetector.
    Calls expose the selected resolver method and arguments."""

    def __init__(self, idle_hint: bool = False, idle_since: float = 0.0,
                 fail_on: set = None):
        self.idle_hint = idle_hint
        self.idle_since_usec = int(idle_since * 1_000_000)
        self.calls: list = []
        self.subscriptions: list = []
        self.unsubscribed: list = []
        self._fail_on = fail_on or set()
        self._next_id = 100

    def call_sync(self, name, path, iface, method, args, reply_type, flags,
                  timeout, cancellable):
        unpacked = args.unpack() if args is not None else None
        self.calls.append((method, unpacked))
        if method in self._fail_on:
            raise GLib.Error(f"fake bus refuses {method}")
        if method in ("GetSession", "GetSessionByPID"):
            return GLib.Variant("(o)", (SESSION_PATH,))
        if method == "Get":
            prop = unpacked[1]
            if prop == "IdleHint":
                return GLib.Variant("(v)", (GLib.Variant("b", self.idle_hint),))
            if prop == "IdleSinceHint":
                return GLib.Variant("(v)", (GLib.Variant("t", self.idle_since_usec),))
        raise AssertionError(f"unexpected D-Bus call: {method} {unpacked}")

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, callback):
        self.subscriptions.append((sender, iface, member, path, arg0, callback))
        self._next_id += 1
        return self._next_id

    def signal_unsubscribe(self, subscription_id):
        self.unsubscribed.append(subscription_id)

    def emit(self, changed: dict, iface: str = LOGIND_SESSION_IFACE) -> None:
        """Fires PropertiesChanged at every subscriber, exactly as GDBus
        would from the GLib main context."""
        params = GLib.Variant("(sa{sv}as)", (iface, changed, []))
        for sub in self.subscriptions:
            sub[5](self, ":1.7", SESSION_PATH, "org.freedesktop.DBus.Properties",
                   "PropertiesChanged", params)


def with_session_id(value):
    """Sets/clears XDG_SESSION_ID and returns the previous value."""
    previous = os.environ.get("XDG_SESSION_ID")
    if value is None:
        os.environ.pop("XDG_SESSION_ID", None)
    else:
        os.environ["XDG_SESSION_ID"] = value
    return previous


def check_detector_built_lazily() -> None:
    """Build the logind detector only after system-idle mode is selected.
    Repeated mode toggles must reuse the same detector and subscription."""
    bus = FakeSystemBus(idle_hint=False)
    previous = with_session_id("31")
    try:
        monitor = PresenceMonitor(mode=MODE_SCREENSAVER, idle_minutes=1, bus=bus)
        assert monitor.idle_detector is None, (
            "the default pause mode built a logind detector"
        )
        assert bus.calls == [] and bus.subscriptions == [], (
            f"the default pause mode reached for the bus anyway: {bus.calls}"
        )

        monitor.set_mode(MODE_SYSTEM_IDLE, 1)
        detector = monitor.idle_detector
        assert detector is not None, "opting in did not build the detector"
        assert len(bus.subscriptions) == 1, (
            f"the opted-in detector did not subscribe: {bus.subscriptions}"
        )
        calls = len(bus.calls)

        # The detector stays inert in screensaver mode and is reused when
        # system-idle mode is selected again.
        monitor.set_mode(MODE_SCREENSAVER)
        monitor.set_mode(MODE_SYSTEM_IDLE, 1)
        assert monitor.idle_detector is detector, "the detector was rebuilt"
        assert len(bus.subscriptions) == 1 and len(bus.calls) == calls, (
            f"a mode toggle churned the logind subscription: "
            f"{len(bus.subscriptions)} subs, {len(bus.calls)} calls"
        )
    finally:
        with_session_id(previous)
    monitor.stop()
    print("PASS: the detector is built on the opt-in, once, never by default")


def check_detector_resolves_by_session_id() -> None:
    """Seed gating from IdleHint and IdleSinceHint during construction.
    An already-idle session must not wait for a later PropertiesChanged signal."""
    bus = FakeSystemBus(idle_hint=True, idle_since=time.time() - 600)
    previous = with_session_id("31")
    try:
        monitor = PresenceMonitor(mode=MODE_SYSTEM_IDLE, idle_minutes=1, bus=bus)
    finally:
        with_session_id(previous)

    assert bus.calls[0] == ("GetSession", ("31",)), (
        f"XDG_SESSION_ID must resolve via GetSession: {bus.calls[0]}"
    )
    assert len(bus.subscriptions) == 1, "the detector did not subscribe"
    _sender, iface, member, path, arg0, _cb = bus.subscriptions[0]
    assert member == "PropertiesChanged" and arg0 == LOGIND_SESSION_IFACE, (
        f"wrong subscription filter: {iface}/{member}/{arg0}"
    )
    assert path == SESSION_PATH, f"subscribed to {path}, not the resolved session"
    assert monitor.is_quiescent() is True, (
        "a session already idle past the deadline must gate at construction"
    )

    monitor.stop()
    assert bus.unsubscribed, "stop() left the signal subscription behind"
    print("PASS: detector resolves via GetSession and seeds the initial idle state")


def check_detector_falls_back_to_caller_pid() -> None:
    """Resolve the caller through logind with PID 0 when XDG_SESSION_ID is absent.
    A sandbox PID from os.getpid() does not identify the host session."""
    bus = FakeSystemBus(idle_hint=False)
    previous = with_session_id(None)
    try:
        monitor = PresenceMonitor(mode=MODE_SYSTEM_IDLE, idle_minutes=1, bus=bus)
    finally:
        with_session_id(previous)

    method, args = bus.calls[0]
    assert method == "GetSessionByPID", (
        f"without XDG_SESSION_ID the resolver must fall back to GetSessionByPID, "
        f"got {method}"
    )
    assert args == (0,), (
        f"GetSessionByPID must ask for the caller (0), not a namespaced pid: {args}"
    )
    assert monitor.is_quiescent() is False
    monitor.stop()
    print("PASS: detector falls back to GetSessionByPID(0) -- the caller, not our pid")


def check_detector_dispatches_property_changes() -> None:
    bus = FakeSystemBus(idle_hint=False)
    previous = with_session_id("31")
    try:
        monitor = PresenceMonitor(mode=MODE_SYSTEM_IDLE, idle_minutes=1, bus=bus)
    finally:
        with_session_id(previous)
    assert monitor.is_quiescent() is False

    # A change on an unrelated interface must be ignored outright.
    bus.emit({"IdleHint": GLib.Variant("b", True)}, iface="org.freedesktop.login1.User")
    assert monitor.is_quiescent() is False, "a foreign interface's signal was acted on"

    # An idle of ten minutes against a one-minute residual gates.
    bus.idle_since_usec = int((time.time() - 600) * 1_000_000)
    bus.emit({"IdleHint": GLib.Variant("b", True)})
    assert monitor.is_quiescent() is True, "PropertiesChanged did not reach the monitor"

    # The signal that carries IdleSinceHint wins over a property read, and a
    # just-started idle must not gate yet.
    bus.emit({"IdleHint": GLib.Variant("b", False)})
    assert monitor.is_quiescent() is False
    bus.emit({
        "IdleHint": GLib.Variant("b", True),
        "IdleSinceHint": GLib.Variant("t", int(time.time() * 1_000_000)),
    })
    assert monitor.is_quiescent() is False, (
        "an idle that started just now must wait out the residual delay"
    )

    # A malformed signal must not escape into the GLib main context.
    bus.emit({"IdleHint": GLib.Variant("s", "not-a-bool")})

    monitor.stop()
    print("PASS: PropertiesChanged dispatch, interface filter and signal-carried timestamp")


def check_detector_inert_on_dbus_failure() -> None:
    """When D-Bus resolution fails, log once and keep the detector inert.
    Construction returns, and lock-based gating continues."""
    bus = FakeSystemBus(fail_on={"GetSession", "GetSessionByPID"})
    previous = with_session_id("31")
    try:
        monitor = PresenceMonitor(mode=MODE_SYSTEM_IDLE, idle_minutes=1, bus=bus)
    finally:
        with_session_id(previous)

    assert bus.subscriptions == [], "a failed resolve must not subscribe"
    assert monitor.is_quiescent() is False

    set_locked(monitor, True)
    assert monitor.is_quiescent() is True, (
        "lock gating must survive an unavailable idle detector"
    )
    set_locked(monitor, False)
    monitor.stop()
    print("PASS: an unreachable logind leaves the idle half inert and lock gating live")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_presence_monitor")
    fixtures.install_stub_globals()

    check_default_mode_never_gates()
    check_lock_gates_and_unlock_clears()
    check_unlock_counts_as_activity()
    check_idle_arithmetic()
    check_deck_activity_clears_and_rearms()
    check_deck_activity_outranks_lock()
    check_set_mode_reevaluates()
    check_constructor_seeds_from_settings()
    check_fan_out_snapshot_and_failure_isolation()
    check_detector_built_lazily()
    check_detector_resolves_by_session_id()
    check_detector_falls_back_to_caller_pid()
    check_detector_dispatches_property_changes()
    check_detector_inert_on_dbus_failure()

    print("\nALL PASS: scenario_presence_monitor")


if __name__ == "__main__":
    main()
