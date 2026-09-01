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
from gi.repository import Gio, GLib

from loguru import logger as log

from src.backend.WindowGrabber.Integration import QUERY_TIMEOUT_MS

class GnomeExtensions:
    def __init__(self) -> None:
        self.proxy: Gio.DBusProxy | None = None
        self.connect_dbus()

    def connect_dbus(self) -> None:
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.gnome.Shell",
                "/org/gnome/Shell",
                "org.gnome.Shell.Extensions",
                None
            )
            # Gio builds proxies for unowned names, so verify the owner here.
            # Later callers have no error path for an absent GNOME Shell.
            if self.proxy.get_name_owner() is None:
                self.proxy = None
                raise RuntimeError("nothing owns org.gnome.Shell on the session bus")
        except Exception as e:
            log.error(f"Failed to connect to D-Bus: {e}")
            pass

    def get_is_connected(self) -> bool:
        return self.proxy is not None

    def get_installed_extensions(self) -> list[str]:
        extensions: list[str] = []
        if not self.get_is_connected(): return extensions

        # a{sa{sv}} keyed by uuid; iterating the reply dict yields the uuids.
        proxy = self.proxy
        if proxy is None:
            return extensions
        # Bound this plain read so an unresponsive Shell cannot block forever.
        # Installation stays unbounded because it waits for the user dialog.
        reply = proxy.call_sync("ListExtensions", None, Gio.DBusCallFlags.NONE, QUERY_TIMEOUT_MS, None)
        extensions.extend(reply.unpack()[0])
        return extensions

    def request_installation(self, uuid: str) -> bool:
        if not self.get_is_connected(): return False
        # Keep the default timeout. GNOME Shell answers only after the user
        # dismisses its install confirmation dialog.
        proxy = self.proxy
        if proxy is None:
            return False
        reply = proxy.call_sync(
            "InstallRemoteExtension", GLib.Variant("(s)", (uuid,)), Gio.DBusCallFlags.NONE, -1, None
        )
        response = reply.unpack()[0]
        return True if response == "successful" else False
