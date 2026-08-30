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

import subprocess
import threading
import time

from loguru import logger as log

from src.backend.WindowGrabber.Window import Window

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

# Bound GTK-main-thread watcher joins; a subprocess read can outlast the wait.
# All watchers are daemons and unwind at their next stop check without blocking quit.
WATCHER_STOP_TIMEOUT_S = 2.0

# Bound one-shot kdotool, swaymsg, hyprctl, and synchronous D-Bus queries.
# Callers include the autoswitch poll and GTK main thread; D-Bus uses milliseconds.
QUERY_TIMEOUT_S = 5.0
QUERY_TIMEOUT_MS = int(QUERY_TIMEOUT_S * 1000)

# Rate limit for the timeout/kill warnings, so a helper that hangs on every
# poll logs once a minute and not once a second.
_QUERY_WARN_GAP_S = 60.0
_last_query_warn = 0.0
_query_warn_lock = threading.Lock()


def _warn_query_timeout(message: str) -> None:
    global _last_query_warn
    with _query_warn_lock:
        now = time.monotonic()
        if now - _last_query_warn < _QUERY_WARN_GAP_S:
            return
        _last_query_warn = now
    log.warning(message)


def communicate_bounded(popen: "subprocess.Popen[bytes]", label: str,
                        timeout_s: float = QUERY_TIMEOUT_S) -> "bytes | None":
    """Communicate before timeout, or terminate, kill, and reap before returning None.
    communicate(timeout) alone leaves the child running; timeout warnings are rate-limited."""
    try:
        out, _ = popen.communicate(timeout=timeout_s)
        return out
    except subprocess.TimeoutExpired:
        popen.terminate()
        try:
            popen.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            popen.kill()
            popen.communicate()
        _warn_query_timeout(
            f"{label} did not finish within {timeout_s:g}s and was killed")
        return None


class Integration:
    """One desktop's window source with a lightweight constructor for one-shot queries.
    A page rule gates the polling thread, IPC socket, or D-Bus subscription used for watching."""

    def __init__(self, window_grabber: "WindowGrabber") -> None:
        self.window_grabber = window_grabber

    def start_watching(self) -> None:
        """Begin reporting active-window changes to the grabber.
        Repeated calls are inert while running; a call after stop starts a fresh watcher."""

    def stop_watching(self) -> None:
        """Stop reporting and reap watcher resources within WATCHER_STOP_TIMEOUT_S.
        This is idempotent and leaves one-shot window queries available."""

    def get_all_windows(self) -> list[Window]:
        return []

    def get_active_window(self) -> Window | None:
        return None
