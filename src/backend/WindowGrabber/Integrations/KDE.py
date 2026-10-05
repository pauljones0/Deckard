"""
Author: flifloo
Year: 2025

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import threading
from src.backend.WindowGrabber.Integration import Integration, WATCHER_STOP_TIMEOUT_S, communicate_bounded
from src.backend.WindowGrabber.Window import Window

from subprocess import Popen, CalledProcessError, PIPE
from loguru import logger as log

import globals as gl

import gi

gi.require_version("Xdp", "1.0")
from gi.repository import Xdp

from typing import TYPE_CHECKING, Optional, cast, override
if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber


class KDE(Integration):
    def __init__(self, window_grabber: "WindowGrabber"):
        super().__init__(window_grabber=window_grabber)

        portal = Xdp.Portal.new()
        self.flatpak = portal.running_under_flatpak()

        self.is_kdotool_installed = self.get_is_kdotool_installed()
        self.active_window_change_thread: "WatchForActiveWindowChange | None" = None

    @log.catch
    def _run_command(self, command: list[str]) -> Optional[Popen[bytes]]:
        if self.flatpak:
            command.insert(0, "flatpak-spawn")
            command.insert(1, "--host")
        try:
            return Popen(command, stdout=PIPE, cwd="/")
        except Exception as e:
            log.error(f"An error occurred while running {command}: {e}")
            return None

    @log.catch
    def get_is_kdotool_installed(self) -> bool:
        try:
            kdotool = self._run_command(["kdotool", "--version"])
            if kdotool is None:
                return False
            out_bytes = communicate_bounded(kdotool, "kdotool --version")
            if out_bytes is None:
                return False
            out = out_bytes.decode("utf-8")
            return out not in ("", None)
        except Exception as e:
            log.error(f"An error occurred while running kdotool: {e}")
            return False

    @log.catch
    @override
    def start_watching(self) -> None:
        if not self.is_kdotool_installed:
            return

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
            # This daemon can outlast the join while blocked in kdotool, then stops after the read.
            # Drop the reference so a later start builds a fresh thread.
            log.warning("The KDE active window watcher did not stop within the timeout")

    @log.catch
    @override
    def get_all_windows(self) -> list[Window]:
        windows: list[Window] = []

        try:
            root = self._run_command(["kdotool", "search", "."])
            if root is None:
                return []
            stdout = communicate_bounded(root, "kdotool search")
            if stdout is None:
                return windows

            window_ids = stdout.decode().strip().split("\n")
            if len(window_ids) < 2:
                return windows
        except CalledProcessError as e:
            log.error(f"An error occurred while running kdotool: {e}")
            return windows

        for window_id in window_ids:
            window = self.get_window(window_id)
            if window is not None:
                windows.append(window)

        return windows

    @log.catch
    def get_active_window_id(self) -> Optional[str]:
        try:
            kdotool = self._run_command(["kdotool", "getactivewindow"])
            if kdotool is None:
                return None
            stdout = communicate_bounded(kdotool, "kdotool getactivewindow")
            if stdout is None:
                return None
            window_id = stdout.decode().strip()
            if len(window_id) == 0:
                return None
            return cast("str | None", window_id)
        except CalledProcessError as e:
            log.error(f"An error occurred while running kdotool: {e}")
            return None

    @log.catch
    @override
    def get_active_window(self) -> Optional[Window]:
        window_id = self.get_active_window_id()
        if window_id is None:
            return None
        return self.get_window(window_id)

    @log.catch
    def get_window(self, window_id: str) -> Optional[Window]:
        title = self.get_title(window_id)
        class_name = self.get_class(window_id)
        if title is None or class_name is None:
            return None
        return Window(class_name, title)

    @log.catch
    def get_title(self, window_id: str) -> Optional[str]:
        try:
            kdotool = self._run_command(["kdotool", "getwindowname", window_id])
            if kdotool is None:
                return None
            out_bytes = communicate_bounded(kdotool, "kdotool getwindowname")
            if out_bytes is None:
                return None
            title = out_bytes.decode().strip()
            # Desktop focus often has an empty title; retain it so the default
            # page returns when the last application is minimised (upstream #640).
            return cast("str | None", title)
        except CalledProcessError as e:
            log.error(f"An error occurred while running kdotool: {e}")
            return None

    @log.catch
    def get_class(self, window_id: str) -> Optional[str]:
        try:
            kdotool = self._run_command(["kdotool", "getwindowclassname", window_id])
            if kdotool is None:
                return None
            out_bytes = communicate_bounded(kdotool, "kdotool getwindowclassname")
            if out_bytes is None:
                return None
            window_class = out_bytes.decode().strip()
            return cast("str | None", window_class)
        except CalledProcessError as e:
            log.error(f"An error occurred while running kdotool: {e}")
            return None


class WatchForActiveWindowChange(threading.Thread):
    def __init__(self, kde: KDE):
        super().__init__(name="WatchForActiveWindowChange", daemon=True)
        self.kde = kde
        self._stop_event = threading.Event()

        self.last_window_id: Optional[str] = None
        self.last_active_window: Optional[Window] = None

        window_id = self.kde.get_active_window_id()
        if window_id is not None:
            self.last_window_id = window_id
            self.last_active_window = self.kde.get_window(window_id)

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
            window_id = self.kde.get_active_window_id()
            if window_id is None:
                continue
            if window_id == self.last_window_id:
                continue

            self.last_window_id = window_id
            new_active_window = self.kde.get_window(window_id)
            if new_active_window is None:
                continue
            if new_active_window == self.last_active_window:
                continue

            self.last_active_window = new_active_window
            self.kde.window_grabber.on_active_window_changed(new_active_window)
