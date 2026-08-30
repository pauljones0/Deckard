import os

from src.backend.LockScreenManager.LockScreenDetector import (
    INITIAL_STATE_TIMEOUT_MS,
    LockScreenDetector,
)

from typing import cast, TYPE_CHECKING, override
if TYPE_CHECKING:
    from src.backend.LockScreenManager.LockScreenManager import LockScreenManager

from gi.repository import Gio, GLib

from loguru import logger as log

class LogindLockScreenDetector(LockScreenDetector):
    """Fallback for Niri, Sway, and river through logind Lock and Unlock.
    It covers loginctl, lid, and idle policy; other lockers need desktop detectors."""

    def __init__(self, lock_screen_manager: "LockScreenManager", bus: Gio.DBusConnection | None = None):
        super().__init__(lock_screen_manager)
        # Stays None whenever resolution fails below. read_initial_lock_state
        # reads LockedHint from it.
        self.session_path: str | None = None
        self.setup_dbus(bus)

    def setup_dbus(self, bus: Gio.DBusConnection | None = None) -> None:
        try:
            # Keep the system-bus connection referenced for the subscription lifetime.
            # Compare the test seam with None so a falsy valid double stays in use.
            self.bus = bus if bus is not None else Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

            self.session_path = self.resolve_session_path()

            # The setup daemon has no thread-default context, so GDBus uses the
            # global default context and dispatches this callback on the GTK loop.
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
            # setup_dbus assigns the bus first; route an unusable connection
            # through its GLib.Error failure branch.
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

    @override
    def read_initial_lock_state(self) -> None:
        """Seed the startup lock from the session's current LockedHint.
        A lock that predates the subscription sends no Lock signal."""
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
