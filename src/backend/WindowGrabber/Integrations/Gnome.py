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

from src.backend.WindowGrabber.Integration import Integration, QUERY_TIMEOUT_MS
from src.backend.WindowGrabber.Window import Window

import json
from loguru import logger as log

import globals as gl

from gi.repository import Gio, GLib

from typing import cast, TYPE_CHECKING, override
if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

class Gnome(Integration):
    def __init__(self, window_grabber: "WindowGrabber"):
        super().__init__(window_grabber=window_grabber)

        self.proxy: Gio.DBusProxy | None = None
        # GObject handler id 0 means no handler and thus not watching.
        # The FocusedWindowChanged subscription is this integration's complete watch state.
        self._signal_handler_id: int = 0
        self.connect_dbus()

    def install_extension(self) -> None:
        # Pass the bare UUID required by InstallRemoteExtension's (s) signature.
        # A UUID inside a list cannot match an installed-extension string.
        uuid = "streamcontroller@core447.com"
        installed_extensions = gl.gnome_extensions.get_installed_extensions()

        if uuid in installed_extensions:
            return

        gl.gnome_extensions.request_installation(uuid)


    def connect_dbus(self) -> None:
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                # The extension has no properties to cache.
                # Disable auto-start so an absent extension does not activate GNOME Shell.
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES | Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                None,
                "org.gnome.Shell",
                "/org/gnome/Shell/Extensions/StreamController",
                "org.gnome.Shell.Extensions.StreamController",
                None
            )
            # Gio builds proxies for unowned names, so verify the owner here.
            # An absent extension must make window queries return an empty result.
            if self.proxy.get_name_owner() is None:
                self.proxy = None
                raise RuntimeError("nothing owns org.gnome.Shell on the session bus")
        except Exception as e:
            log.error(f"Failed to connect to D-Bus: {e}")
            pass

    @log.catch
    @override
    def start_watching(self) -> None:
        proxy = self.proxy
        if proxy is None or self._signal_handler_id:
            return
        self._signal_handler_id = proxy.connect("g-signal", self.on_dbus_signal)

    @log.catch
    @override
    def stop_watching(self) -> None:
        """Drop the local window-change handler immediately but keep the proxy for one-shot page-editor queries.
        An existing proxy keeps its bus match rule, so delivered signals are discarded until watching resumes."""
        proxy = self.proxy
        handler_id = self._signal_handler_id
        self._signal_handler_id = 0
        if proxy is None or not handler_id:
            return
        proxy.disconnect(handler_id)


    def on_dbus_signal(self, proxy: Gio.DBusProxy, sender_name: str, signal_name: str,
                       parameters: GLib.Variant) -> None:
        if signal_name != "FocusedWindowChanged":
            return
        self.on_window_changed(parameters.unpack()[0])


    def on_window_changed(self, answer: str) -> None:
        parsed = json.loads(answer)
        window = Window(parsed.get("wm_class"), parsed.get("title"))
        self.window_grabber.on_active_window_changed(window=window)
        
    @override
    def get_all_windows(self) -> list[Window]:
        if not self.get_is_connected():
            return []
        
        try:
            answer = json.loads(self.call("GetAllWindows"))
        except (GLib.Error, IndexError, TypeError, json.JSONDecodeError):
            # Treat an absent or incompatible extension as no windows.
            # Calls can fail with GLib.Error, empty replies with IndexError, and invalid JSON inputs with TypeError or JSONDecodeError.
            return []
        windows: list[Window] = []
        
        for window in answer:
            wm_class = window.get("wm_class")
            title = window.get("title")
            windows.append(Window(wm_class, title))

        return windows
    
    @override
    def get_active_window (self) -> Window | None:
        if not self.get_is_connected():
            return None
        try:
            answer = json.loads(self.call("GetFocusedWindow"))
        except (GLib.Error, IndexError, TypeError, json.JSONDecodeError):
            # Handle the same extension and reply failures as get_all_windows.
            return None
        wm_class = answer.get("wm_class")
        title = answer.get("title")
        return Window(wm_class, title)

    def call(self, method_name: str) -> str:
        proxy = self.proxy
        if proxy is None:
            # The proxy can disappear after get_is_connected returns.
            # Raise GLib.Error because both callers handle it, unlike AttributeError.
            raise GLib.Error("no D-Bus proxy for the GNOME extension")
        # Bound Shell calls so the autoswitch poll or GTK main thread cannot block forever.
        # Timeout raises GLib.Error, which both callers handle.
        return cast(str, proxy.call_sync(method_name, None, Gio.DBusCallFlags.NONE, QUERY_TIMEOUT_MS, None).unpack()[0])

    def get_is_connected(self) -> bool:
        # Check the tracked live owner, not only that a proxy exists.
        # This becomes false when Shell or the extension exporter leaves the bus.
        return self.proxy is not None and self.proxy.get_name_owner() is not None
