"""
Author: Qalthos
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import threading
from src.backend.WindowGrabber.Integration import Integration, WATCHER_STOP_TIMEOUT_S, QUERY_TIMEOUT_S
from src.backend.WindowGrabber.Window import Window

import subprocess
import json
from loguru import logger as log

import globals as gl

import gi

gi.require_version("Xdp", "1.0")
from gi.repository import Xdp

from typing import TYPE_CHECKING, Any, override
if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

class Sway(Integration):
    def __init__(self, window_grabber: "WindowGrabber"):
        super().__init__(window_grabber=window_grabber)

        self.command_prefix: list[str] = []
        portal = Xdp.Portal.new()
        if portal.running_under_flatpak():
            self.command_prefix = ["flatpak-spawn", "--host"]

        self.active_window_change_thread: "WatchForActiveWindowChange | None" = None

    @log.catch
    @override
    def start_watching(self) -> None:
        thread = self.active_window_change_thread
        if thread is not None and thread.is_alive():
            return

        # Threads cannot restart, so each start builds a fresh watcher.
        # It primes the last window from current focus, not a pre-stop reading.
        thread = WatchForActiveWindowChange(self)
        self.active_window_change_thread = thread
        thread.start()

    @log.catch
    @override
    def stop_watching(self) -> None:
        thread = self.active_window_change_thread
        self.active_window_change_thread = None
        if thread is None:
            return

        thread.stop()
        if thread is threading.current_thread():
            # A window-triggered page write can re-gate from this thread, so never join it.
            # Its next stop check ends the loop; reserve the warning for an attempted join timeout.
            return

        thread.join(timeout=WATCHER_STOP_TIMEOUT_S)
        if thread.is_alive():
            # This daemon can outlast the join while blocked in swaymsg, then stops after the read.
            # Drop the reference so a later start builds a fresh thread.
            log.warning("The Sway active window watcher did not stop within the timeout")

    @override
    def get_all_windows(self) -> list[Window]:
        return [self._parse_window(client) for client in self._get_windows()]

    @override
    def get_active_window(self) -> Window | None:
        window_list = self._get_windows()

        for client in window_list:
            if not client["focused"]:
                continue
            return self._parse_window(client)

        return None

    def _walk_tree(self, node: Any, windows: list[dict[str, Any]]) -> None:
        if "window_properties" in node or "app_id" in node:
           windows.append(node)

        if "nodes" in node:
           for child in node.get("nodes"):
               self._walk_tree(child, windows)
           for child in node.get("floating_nodes"):
               self._walk_tree(child, windows)

    def _get_windows(self) -> list[dict[str, Any]]:
        windows: list[dict[str, Any]] = []
        try:
            output = subprocess.check_output([*self.command_prefix, "swaymsg", "-t", "get_tree"], text=True, cwd="/", timeout=QUERY_TIMEOUT_S).strip()
            clients = json.loads(output)

            for output in clients.get("nodes", []):
                for workspace in output.get("nodes", []):
                    self._walk_tree(workspace, windows)

        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as e:
            # OSError covers a missing binary. The argv list runs no shell,
            # which would turn that into a 127 CalledProcessError.
            log.error(f"An error occurred while running swaymsg: {e}")
        except json.JSONDecodeError as e:
            log.error(f"Failed to parse JSON: {e}")

        return windows

    def _parse_window(self, client: dict[str, Any]) -> Window:
        if "window_properties" in client:
            # An XWindow client keeps its class and title one level deeper.
            props = client["window_properties"]
            return Window(props["class"], props["title"])
        else:
            return Window(client.get("app_id", ""), client["name"])

class WatchForActiveWindowChange(threading.Thread):
    def __init__(self, sway: Sway):
        super().__init__(name="WatchForActiveWindowChange", daemon=True)
        self.sway = sway
        self._stop_event = threading.Event()

        self.last_active_window = sway.get_active_window()

    def stop(self) -> None:
        """Asks the loop to end. Returns at once, and the caller joins."""
        self._stop_event.set()

    @log.catch
    @override
    def run(self) -> None:
        while gl.threads_running and not self._stop_event.is_set():
            # Wait on the stop event so stop can end the loop before the poll interval.
            # One elapsed wait can still dispatch after stop; routing re-reads and finds no rule.
            if self._stop_event.wait(0.2):
                break
            new_active_window = self.sway.get_active_window()
            if new_active_window is None:
                continue
            if new_active_window == self.last_active_window:
                continue

            self.last_active_window = new_active_window
            self.sway.window_grabber.on_active_window_changed(new_active_window)
