"""A session already locked at app launch is detected at startup.

The event path learns the lock only from the next ActiveChanged/Lock signal.
A session already locked when the app starts sends no such signal, so without
a startup read the lock branch never engages and the decks stay lit behind the
lock screen. Each detector reads its own source once at startup (logind
LockedHint, or the screen saver's GetActive) and drives the same lock() the
signal path drives. LockScreenManager.setup() calls that read after it builds
the detector.

A fake bus stands in, so no real system or session bus is touched.
"""
import os

import fixtures  # must be first; isolates DATA_PATH

from gi.repository import GLib

LOGIN1 = "org.freedesktop.login1"
LOGIND_SESSION_IFACE = f"{LOGIN1}.Session"
PROPS_IFACE = "org.freedesktop.DBus.Properties"


class RecordingManager:
    """Stands in for LockScreenManager. The detector read only calls .lock()."""

    def __init__(self):
        self.lock_calls = []

    def lock(self, active):
        self.lock_calls.append(active)


class FakeBus:
    """Duck-types the one Gio.DBusConnection method the reads use.

    call_sync answers the logind session resolver, the logind LockedHint
    property Get, and the screen saver GetActive. The signatures match
    positionally. signal_subscribe records, so a detector wires up inertly.
    """

    def __init__(self, session_path="/org/freedesktop/login1/session/_31",
                 locked_hint=False, screen_saver_active=False):
        self.session_path = session_path
        self.locked_hint = locked_hint
        self.screen_saver_active = screen_saver_active
        self.calls = []
        self.subscriptions = []

    def call_sync(self, bus_name, object_path, interface_name, method_name,
                  parameters, reply_type, flags, timeout_msec, cancellable):
        self.calls.append((bus_name, object_path, interface_name, method_name,
                           None if parameters is None else parameters.unpack()))
        if method_name in ("GetSession", "GetSessionByPID"):
            return GLib.Variant("(o)", (self.session_path,))
        if method_name == "Get":
            # Properties.Get returns the value wrapped in a variant.
            return GLib.Variant("(v)", (GLib.Variant("b", self.locked_hint),))
        if method_name == "GetActive":
            return GLib.Variant("(b)", (self.screen_saver_active,))
        raise AssertionError(f"unexpected method {method_name}")

    def signal_subscribe(self, sender, interface_name, member, object_path,
                         arg0, flags, callback):
        self.subscriptions.append((sender, interface_name, member, object_path))
        return len(self.subscriptions)


def logind_reads_locked_hint():
    from src.backend.LockScreenManager.Detectors.Logind import LogindLockScreenDetector

    os.environ["XDG_SESSION_ID"] = "7"

    # Locked session: the read drives lock(True).
    manager = RecordingManager()
    detector = LogindLockScreenDetector(manager, bus=FakeBus(locked_hint=True))
    detector.read_initial_lock_state()
    assert manager.lock_calls == [True], manager.lock_calls

    # The property read hit LockedHint on the resolved session path.
    prop_calls = [c for c in detector.bus.calls if c[3] == "Get"]
    assert len(prop_calls) == 1, prop_calls
    _, path, iface, _, args = prop_calls[0]
    assert path == detector.bus.session_path, path
    assert iface == PROPS_IFACE, iface
    assert args == (LOGIND_SESSION_IFACE, "LockedHint"), args
    print("PASS: logind LockedHint=True drives lock(True) at startup")

    # Unlocked session: the read stays quiet, so the event path owns the lock.
    manager = RecordingManager()
    detector = LogindLockScreenDetector(manager, bus=FakeBus(locked_hint=False))
    detector.read_initial_lock_state()
    assert manager.lock_calls == [], manager.lock_calls
    print("PASS: logind LockedHint=False leaves the lock to the event path")


def logind_read_is_inert_without_a_session():
    from src.backend.LockScreenManager.Detectors.Logind import LogindLockScreenDetector

    # A failed resolution leaves session_path None. The read must not raise
    # and must not lock.
    os.environ["XDG_SESSION_ID"] = "7"
    detector = LogindLockScreenDetector.__new__(LogindLockScreenDetector)
    detector.lock_screen_manager = RecordingManager()
    detector.bus = None
    detector.session_path = None
    detector.read_initial_lock_state()  # must not raise
    assert detector.lock_screen_manager.lock_calls == [], detector.lock_screen_manager.lock_calls
    print("PASS: an inert logind detector reads nothing and does not raise")


def screen_saver_reads_get_active():
    from src.backend.LockScreenManager.LockScreenDetector import LockScreenDetector
    from src.backend.LockScreenManager.Detectors.Gnome import GnomeLockScreenDetector

    # The base read calls GetActive on the recorded screen saver object. The
    # desktop detectors inherit it unchanged.
    assert GnomeLockScreenDetector.read_initial_lock_state is LockScreenDetector.read_initial_lock_state, (
        "the desktop detectors must inherit the base GetActive read"
    )

    # Active screen saver: the read drives lock(True).
    manager = RecordingManager()
    detector = LockScreenDetector.__new__(LockScreenDetector)
    detector.lock_screen_manager = manager
    detector.bus = FakeBus(screen_saver_active=True)
    detector._screen_saver_object_path = "/org/gnome/ScreenSaver"
    detector._screen_saver_interface = "org.gnome.ScreenSaver"
    detector.read_initial_lock_state()
    assert manager.lock_calls == [True], manager.lock_calls

    # The call went to GetActive on the recorded object, destination equal to
    # the interface.
    active_calls = [c for c in detector.bus.calls if c[3] == "GetActive"]
    assert len(active_calls) == 1, active_calls
    dest, path, iface, _, _ = active_calls[0]
    assert dest == "org.gnome.ScreenSaver", dest
    assert path == "/org/gnome/ScreenSaver", path
    assert iface == "org.gnome.ScreenSaver", iface
    print("PASS: screen saver GetActive=True drives lock(True) at startup")

    # Inactive screen saver: the read stays quiet.
    manager = RecordingManager()
    detector = LockScreenDetector.__new__(LockScreenDetector)
    detector.lock_screen_manager = manager
    detector.bus = FakeBus(screen_saver_active=False)
    detector._screen_saver_object_path = "/org/gnome/ScreenSaver"
    detector._screen_saver_interface = "org.gnome.ScreenSaver"
    detector.read_initial_lock_state()
    assert manager.lock_calls == [], manager.lock_calls
    print("PASS: screen saver GetActive=False leaves the lock to the event path")


def base_read_is_inert_without_a_source():
    from src.backend.LockScreenManager.LockScreenDetector import LockScreenDetector

    # A detector with no session-bus screen saver (Hyprland shape) recorded no
    # object, so the base read is a no-op and its own source owns the lock.
    manager = RecordingManager()
    detector = LockScreenDetector(manager)
    assert detector.bus is None
    detector.read_initial_lock_state()  # must not raise
    assert manager.lock_calls == [], manager.lock_calls
    print("PASS: a detector with no screen saver source reads nothing")


def manager_setup_calls_the_read():
    """setup() must invoke read_initial_lock_state on the chosen detector."""
    import src.backend.LockScreenManager.LockScreenManager as lsm_mod
    from src.backend.LockScreenManager.LockScreenManager import LockScreenManager

    class FakeDetector:
        def __init__(self, manager):
            self.manager = manager
            self.read_called = False

        def read_initial_lock_state(self):
            self.read_called = True

    saved = lsm_mod.LogindLockScreenDetector
    saved_env = os.environ.pop("XDG_CURRENT_DESKTOP", None)
    try:
        lsm_mod.LogindLockScreenDetector = FakeDetector  # unset desktop -> logind branch
        manager = LockScreenManager.__new__(LockScreenManager)
        manager.locked = False
        manager.detector = None
        manager.setup()
        assert isinstance(manager.detector, FakeDetector), type(manager.detector)
        assert manager.detector.read_called, "setup() must read the initial lock state"
    finally:
        lsm_mod.LogindLockScreenDetector = saved
        if saved_env is not None:
            os.environ["XDG_CURRENT_DESKTOP"] = saved_env
    print("PASS: LockScreenManager.setup() reads the initial lock state")


def real_startup_lock_then_unlock_engages_the_branch():
    """A real startup lock must disengage on the first real unlock.

    This drives the whole path through the real LockScreenManager.lock():
    the locked-session read runs while gl.deck_manager is still None (the
    startup order), and a later real Unlock signal drives lock(False). The
    tracked lock state must stay in step with gl.screen_locked across the
    deck-manager-None read, or the first unlock reads as a no-op and the decks
    that enumerated in the meantime stay behind the screen saver.

    Earlier checks in this scenario record lock() calls on a stub, so they
    cannot see that the real lock() swallows the first unlock. This one uses
    the real manager and asserts the deck branch runs.
    """
    import globals as gl
    from src.backend.LockScreenManager.LockScreenManager import LockScreenManager
    from src.backend.LockScreenManager.Detectors.Logind import LogindLockScreenDetector

    os.environ["XDG_SESSION_ID"] = "7"

    class FakeScreenSaver:
        def __init__(self):
            self.shown = False

        def show(self):
            self.shown = True

        def hide(self):
            self.shown = False

    class FakeController:
        def __init__(self):
            # A deck that enumerated after the startup read comes up locked:
            # interaction blocked and the screen saver shown.
            self.allow_interaction = False
            self.screen_saver = FakeScreenSaver()

    class FakeDeckManager:
        def __init__(self, controllers):
            self.deck_controller = controllers

    class FakeApp:
        lock_on_lock_screen = True

    class FakeSettingsManager:
        def app(self):
            return FakeApp()

    # A real manager with a real locked-session detector, no setup thread.
    manager = LockScreenManager.__new__(LockScreenManager)
    manager.locked = False
    manager.detector = None
    detector = LogindLockScreenDetector(manager, bus=FakeBus(locked_hint=True))
    manager.detector = detector

    saved = (gl.deck_manager, gl.settings_manager, gl.presence_monitor,
             gl.screen_locked)
    try:
        gl.presence_monitor = None
        gl.settings_manager = FakeSettingsManager()

        # Startup: the locked session is read while no deck manager exists.
        gl.deck_manager = None
        gl.screen_locked = False
        detector.read_initial_lock_state()
        assert gl.screen_locked is True, gl.screen_locked

        # Decks enumerate after the read. They pick up the seeded
        # gl.screen_locked at init and come up on the screen saver.
        controllers = [FakeController(), FakeController()]
        for c in controllers:
            c.screen_saver.show()
        gl.deck_manager = FakeDeckManager(controllers)

        # The first real unlock arrives on the signal path.
        detector.on_dbus_signal(None, None, None, None, "Unlock", None)
        assert gl.screen_locked is False, gl.screen_locked
        for c in controllers:
            assert c.allow_interaction is True, (
                "the first unlock must re-enable interaction on the decks")
            assert c.screen_saver.shown is False, (
                "the first unlock must hide the screen saver on the decks")
    finally:
        (gl.deck_manager, gl.settings_manager, gl.presence_monitor,
         gl.screen_locked) = saved

    print("PASS: a real startup lock disengages on the first real unlock")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_lock_startup_state")

    logind_reads_locked_hint()
    logind_read_is_inert_without_a_session()
    screen_saver_reads_get_active()
    base_read_is_inert_without_a_source()
    manager_setup_calls_the_read()
    real_startup_lock_then_unlock_engages_the_branch()

    print("PASS: scenario_lock_startup_state")


if __name__ == "__main__":
    main()
