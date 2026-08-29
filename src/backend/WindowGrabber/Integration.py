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

# How long a stop waits for a watcher to notice and unwind. A page-editor edit
# stops the watcher on the GTK main thread, so the join must be bounded. A
# watcher parked in a subprocess read is abandoned to its own next stop check.
# Every watcher is a daemon thread and cannot hold up quit.
WATCHER_STOP_TIMEOUT_S = 2.0

# Deadline for a one-shot window query (a kdotool/swaymsg/hyprctl invocation,
# or a synchronous D-Bus call). A stuck compositor helper must not park the
# caller, which runs on the autoswitch poll or the GTK main thread. Its
# millisecond form is for the D-Bus call flags.
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
    """communicate() with a deadline. On timeout, terminate then kill and reap
    the child, and return None. Popen.communicate(timeout=...) alone raises but
    leaves the child running, so a stuck helper would otherwise leak a process
    per poll. Warnings are rate-limited."""
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
    """One desktop's window source.

    A one-shot window query can construct this, so the constructor keeps its
    side effects light and does not start a watcher. A watcher costs a polling
    thread, an IPC socket, or a D-Bus subscription; a page rule gates it.
    """

    def __init__(self, window_grabber: "WindowGrabber") -> None:
        self.window_grabber = window_grabber

    def start_watching(self) -> None:
        """Begins reporting active-window changes to the grabber.

        Idempotent. A call while the watcher already runs does nothing, and a
        call after stop_watching() starts a fresh watcher.
        """

    def stop_watching(self) -> None:
        """Stops reporting and reaps what the watcher holds, within
        WATCHER_STOP_TIMEOUT_S. Idempotent. Only the watcher stops, so
        get_all_windows and get_active_window keep working.
        """

    def get_all_windows(self) -> list[Window]:
        return []

    def get_active_window(self) -> Window | None:
        return None