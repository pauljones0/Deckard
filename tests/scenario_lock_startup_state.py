"""Read the initial lock state from logind or the screen saver at startup.
The fake bus prevents access to real system and session buses."""
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
        self.initial_flags = []

    def lock(self, active, initial=False):
        self.lock_calls.append(active)
        self.initial_flags.append(initial)


class FakeBus:
    """Serve the session resolver, LockedHint, and GetActive bus calls.
    Signal subscriptions are recorded but stay inert."""

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
    # The startup read marks the lock initial, so its deck work is marshalled.
    assert manager.initial_flags == [True], manager.initial_flags

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


def check_logind_read_without_session_is_inert():
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
    assert manager.initial_flags == [True], manager.initial_flags

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


def check_sourceless_base_read_is_inert():
    from src.backend.LockScreenManager.LockScreenDetector import LockScreenDetector

    # A detector with no session-bus screen saver (Hyprland shape) recorded no
    # object, so the base read is a no-op and its own source owns the lock.
    manager = RecordingManager()
    detector = LockScreenDetector(manager)
    assert detector.bus is None
    detector.read_initial_lock_state()  # must not raise
    assert manager.lock_calls == [], manager.lock_calls
    print("PASS: a detector with no screen saver source reads nothing")


def check_setup_reads_initial_lock_state():
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


def check_startup_unlock_releases_decks():
    """Require the first unlock to release decks enumerated after a startup lock.
    State must remain synchronized when the initial read precedes the deck manager."""
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


def check_initial_deck_work_runs_on_main():
    """Marshal initial deck lock work to the main loop when the bus read is late.
    Screen-saver and interaction changes must not run on the setup thread."""
    import threading

    import globals as gl
    from src.backend.LockScreenManager.LockScreenManager import LockScreenManager
    from src.backend.LockScreenManager.Detectors.Logind import LogindLockScreenDetector

    os.environ["XDG_SESSION_ID"] = "7"

    class RecordingScreenSaver:
        def __init__(self):
            self.show_threads = []

        def show(self):
            self.show_threads.append(threading.current_thread())

        def hide(self):
            pass

    class RecordingController:
        def __init__(self):
            self.allow_interaction = True
            self.screen_saver = RecordingScreenSaver()

    class FakeDeckManager:
        def __init__(self, controllers):
            self.deck_controller = controllers

    class FakeApp:
        lock_on_lock_screen = True

    class FakeSettingsManager:
        def app(self):
            return FakeApp()

    manager = LockScreenManager.__new__(LockScreenManager)
    manager.locked = False
    manager.detector = None
    detector = LogindLockScreenDetector(manager, bus=FakeBus(locked_hint=True))
    manager.detector = detector

    controllers = [RecordingController(), RecordingController()]
    saved = (gl.deck_manager, gl.settings_manager, gl.presence_monitor,
             gl.screen_locked)
    loop = GLib.MainLoop()
    try:
        gl.presence_monitor = None
        gl.settings_manager = FakeSettingsManager()
        # The slow-bus order: the deck manager already exists when the read
        # lands.
        gl.deck_manager = FakeDeckManager(controllers)
        gl.screen_locked = False

        def read_on_worker():
            detector.read_initial_lock_state()
            # Queued after any deck work the read queued, so the loop stops
            # only once that work has run.
            GLib.idle_add(lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])

        worker = threading.Thread(target=read_on_worker, name="lock_read")
        # A missing quit must fail the scenario rather than hang it.
        GLib.timeout_add_seconds(10, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
        worker.start()
        loop.run()
        worker.join(timeout=10)

        assert gl.screen_locked is True, gl.screen_locked
        for c in controllers:
            assert c.allow_interaction is False, (
                "the initial lock must block interaction on the decks")
            assert c.screen_saver.show_threads, (
                "the initial lock must show the screen saver on the decks")
            for t in c.screen_saver.show_threads:
                assert t is threading.main_thread(), (
                    f"the initial lock's deck work ran on {t.name}, not the main loop")
    finally:
        (gl.deck_manager, gl.settings_manager, gl.presence_monitor,
         gl.screen_locked) = saved

    print("PASS: the initial lock's deck work runs on the main loop")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_lock_startup_state")

    logind_reads_locked_hint()
    check_logind_read_without_session_is_inert()
    screen_saver_reads_get_active()
    check_sourceless_base_read_is_inert()
    check_setup_reads_initial_lock_state()
    check_startup_unlock_releases_decks()
    check_initial_deck_work_runs_on_main()

    print("PASS: scenario_lock_startup_state")


if __name__ == "__main__":
    main()
