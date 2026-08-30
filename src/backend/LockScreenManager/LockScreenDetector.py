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
        # Keep the subscribed ScreenSaver source for the initial GetActive call.
        # Detectors without a session-bus screen saver leave both values unset.
        self._screen_saver_object_path: str | None = None
        self._screen_saver_interface: str | None = None

    def subscribe_to_screen_saver(self, bus_name: str | None, object_path: str, interface: str, callback: Callable[..., Any]) -> None:
        """Listen for ScreenSaver ActiveChanged on the session bus.
        A None bus_name accepts any sender, as required by desktop detectors."""
        try:
            # Keep the connection referenced. The subscription below lives
            # exactly as long as the connection does.
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._screen_saver_object_path = object_path
            self._screen_saver_interface = interface

            # The setup daemon has no thread-default context, so GDBus uses the
            # global default context and dispatches this callback on the GTK loop.
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
        """Seed startup with GetActive because an existing lock sends no ActiveChanged.
        Detectors without a session-bus screen saver use their own source."""
        bus = self.bus
        object_path = self._screen_saver_object_path
        interface = self._screen_saver_interface
        if bus is None or object_path is None or interface is None:
            return

        # GNOME, freedesktop, and Cinnamon ScreenSaver services use their
        # interface name as the bus name, so it is also the call destination.
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
            # initial=True: this read runs on the setup daemon thread, so the
            # deck work goes to the main loop.
            self.lock_screen_manager.lock(True, initial=True)
