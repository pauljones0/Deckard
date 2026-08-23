"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
from collections.abc import Callable
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.LockScreenManager.LockScreenManager import LockScreenManager

from gi.repository import Gio, GLib

from loguru import logger as log

# Bound the one-shot startup property read, so a live but slow bus cannot
# hold the read past this. The event path stays unbounded.
INITIAL_STATE_TIMEOUT_MS = 5000

class LockScreenDetector:
    def __init__(self, lock_screen_manager: "LockScreenManager"):
        self.lock_screen_manager: "LockScreenManager" = lock_screen_manager
        # Stays None whenever the bus connection fails below.
        self.bus: Gio.DBusConnection | None = None
        # subscribe_to_screen_saver records the ScreenSaver object here, so
        # read_initial_lock_state can call GetActive on the same source it
        # subscribes to. A detector with no session-bus screen saver leaves
        # them None.
        self._screen_saver_object_path: str | None = None
        self._screen_saver_interface: str | None = None

    def subscribe_to_screen_saver(self, bus_name: str | None, object_path: str, interface: str, callback: Callable[..., Any]) -> None:
        """Listen for the ScreenSaver ActiveChanged signal on the session bus.

        bus_name is the sender to match. The desktop detectors pass None and
        accept any sender.
        """
        try:
            # Keep the connection referenced. The subscription below lives
            # exactly as long as the connection does.
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._screen_saver_object_path = object_path
            self._screen_saver_interface = interface

            # setup() runs on the manager's daemon thread, which has no
            # thread-default main context, so GDBus dispatches the callback on
            # the global default one, which is the GTK main loop.
            self.bus.signal_subscribe(
                bus_name,
                interface,
                "ActiveChanged",
                object_path,
                None,
                Gio.DBusSignalFlags.NONE,
                callback
            )
        except Exception as e:
            log.error(f"Failed to connect to D-Bus: {e}")

    def read_initial_lock_state(self) -> None:
        """Seed the lock from the screen saver's CURRENT state, once, at startup.

        The event path learns the lock only from the next ActiveChanged
        signal. A session already locked when the app starts sends no such
        signal, so the lock branch never engages and the decks stay lit
        behind the lock screen. Read GetActive here and drive the same
        lock() the signal path drives.

        The desktop detectors record the ScreenSaver object in
        subscribe_to_screen_saver. A detector with no session-bus screen
        saver leaves it unset, so this stays a no-op there and that
        detector's own source seeds the lock.
        """
        bus = self.bus
        object_path = self._screen_saver_object_path
        interface = self._screen_saver_interface
        if bus is None or object_path is None or interface is None:
            return

        # The three ScreenSaver services own a bus name equal to their
        # interface (org.gnome.ScreenSaver, org.freedesktop.ScreenSaver,
        # org.cinnamon.ScreenSaver), so the interface is the call destination.
        try:
            reply = bus.call_sync(
                interface,
                object_path,
                interface,
                "GetActive",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                INITIAL_STATE_TIMEOUT_MS,
                None,
            )
        except GLib.Error as e:
            # No running screen saver service, or it exposes no GetActive.
            # Stay in signal-driven mode; the next ActiveChanged still seeds it.
            log.info(f"Lock: screen saver GetActive unavailable, initial state unread ({e})")
            return

        if bool(reply.unpack()[0]):
            self.lock_screen_manager.lock(True)