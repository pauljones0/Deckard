import os

from src.backend.LockScreenManager.LockScreenDetector import (
    INITIAL_STATE_TIMEOUT_MS,
    LockScreenDetector,
)

from typing import cast, TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.LockScreenManager.LockScreenManager import LockScreenManager

from gi.repository import Gio, GLib

from loguru import logger as log

class LogindLockScreenDetector(LockScreenDetector):
    """Fallback for a session with no desktop-specific detector (Niri, Sway,
    river). logind sends Lock and Unlock for loginctl lock-session, lid
    switches and idle policy, apart from any session-bus screen saver. Both
    are lock requests, so this detector misses a locker that never calls logind,
    so the desktop detectors stay first in the chain."""

    def __init__(self, lock_screen_manager: "LockScreenManager", bus: Gio.DBusConnection | None = None):
        super().__init__(lock_screen_manager)
        # Stays None whenever resolution fails below. read_initial_lock_state
        # reads LockedHint from it.
        self.session_path: str | None = None
        self.setup_dbus(bus)

    def setup_dbus(self, bus: Gio.DBusConnection | None = None) -> None:
        try:
            # logind lives on the system bus; the desktop detectors use the
            # session bus. Keep the connection referenced, because the
            # subscription below lives as long as the connection does. bus is a test
            # seam and production passes None. Compare against None, because a
            # falsy but valid double must not pull in the real system bus.
            self.bus = bus if bus is not None else Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

            self.session_path = self.resolve_session_path()

            # setup() runs on the manager's daemon thread, which has no
            # thread-default main context, so GDBus dispatches the callback on
            # the global default one, which is the GTK main loop.
            self.bus.signal_subscribe(
                "org.freedesktop.login1",
                "org.freedesktop.login1.Session",
                None,
                self.session_path,
                None,
                Gio.DBusSignalFlags.NONE,
                self.on_dbus_signal
            )
        except GLib.Error as e:
            log.error(f"Failed to connect to logind: {e}")

    def resolve_session_path(self) -> str:
        bus = self.bus
        if bus is None:
            # setup_dbus assigns self.bus immediately before it calls this, so
            # nothing reaches here. Raise GLib.Error to route an unusable
            # connection into setup_dbus's own failure branch.
            raise GLib.Error("logind D-Bus connection unavailable")

        session_id = os.getenv("XDG_SESSION_ID")
        if session_id:
            method = "GetSession"
            args = GLib.Variant("(s)", (session_id,))
        else:
            method = "GetSessionByPID"
            args = GLib.Variant("(u)", (os.getpid(),))

        reply = bus.call_sync(
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            method,
            args,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None
        )
        return cast(str, reply.unpack()[0])

    def on_dbus_signal(self, connection: Gio.DBusConnection, sender_name: str,
                       object_path: str, interface_name: str, signal_name: str,
                       parameters: GLib.Variant) -> None:
        if signal_name == "Lock":
            self.lock_screen_manager.lock(True)
        elif signal_name == "Unlock":
            self.lock_screen_manager.lock(False)

    def read_initial_lock_state(self) -> None:
        """Seed the lock from the session's current LockedHint, once, at startup.

        logind sets LockedHint while the session is locked, and sends no Lock
        signal for a lock that predates the subscription. A process that
        starts into an already-locked session reads the hint here and drives
        the same lock() the signal path drives, rather than wait for a Lock
        that never arrives while the session stays locked.
        """
        bus = self.bus
        session_path = self.session_path
        if bus is None or session_path is None:
            return

        try:
            reply = bus.call_sync(
                "org.freedesktop.login1",
                session_path,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", ("org.freedesktop.login1.Session", "LockedHint")),
                None,
                Gio.DBusCallFlags.NONE,
                INITIAL_STATE_TIMEOUT_MS,
                None,
            )
        except GLib.Error as e:
            # logind is unreachable or hides the property. Stay signal-driven.
            log.info(f"Lock: logind LockedHint unavailable, initial state unread ({e})")
            return

        if bool(reply.unpack()[0]):
            # initial=True: this read runs on the setup daemon thread, so the
            # deck work goes to the main loop.
            self.lock_screen_manager.lock(True, initial=True)
