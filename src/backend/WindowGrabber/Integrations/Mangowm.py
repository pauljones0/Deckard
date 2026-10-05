"""MangoWM focus events, adapted from M-Pistillucci's upstream integration.

Preserve Deckard's rule-gated watcher lifecycle and bounded subprocess queries.
"""
import json
import os
import socket
import subprocess
import threading
from typing import Any, TYPE_CHECKING, override

from loguru import logger as log

import globals as gl
from src.backend.WindowGrabber.Integration import Integration, WATCHER_STOP_TIMEOUT_S, communicate_bounded
from src.backend.WindowGrabber.Window import Window

if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber


def window_from_message(data: Any) -> Window | None:
    if not isinstance(data, dict):
        return None
    appid, title = data.get("appid"), data.get("title", "")
    if not isinstance(appid, str) or not appid or not isinstance(title, str):
        return None
    return Window(appid, title)


class MangoWM(Integration):
    def __init__(self, window_grabber: "WindowGrabber") -> None:
        super().__init__(window_grabber)
        self._watcher: FocusWatcher | None = None
        self._prefix = ["flatpak-spawn", "--host"] if os.path.exists("/.flatpak-info") else []

    def _query(self, name: str) -> Any:
        try:
            process = subprocess.Popen([*self._prefix, "mmsg", "get", name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd="/")
            output = communicate_bounded(process, f"mmsg get {name}")
            if output is None or process.returncode != 0:
                return None
            return json.loads(output)
        except (OSError, ValueError) as error:
            log.warning(f"Could not read MangoWM focus: {error}")
            return None

    @override
    def get_active_window(self) -> Window | None:
        return window_from_message(self._query("focusing-client"))

    @override
    def get_all_windows(self) -> list[Window]:
        data = self._query("all-clients")
        if not isinstance(data, list):
            return []
        return [window for entry in data if (window := window_from_message(entry)) is not None]

    def _socket_path(self) -> str | None:
        explicit = os.environ.get("MANGO_INSTANCE_SIGNATURE")
        if explicit and os.path.exists(explicit):
            return explicit
        directory = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return None
        return next((os.path.join(directory, name) for name in names if name.startswith("mango-") and name.endswith(".sock")), None)

    @override
    def start_watching(self) -> None:
        if self._watcher is not None and self._watcher.is_alive():
            return
        self._watcher = FocusWatcher(self)
        self._watcher.start()

    @override
    def stop_watching(self) -> None:
        watcher, self._watcher = self._watcher, None
        if watcher is None:
            return
        watcher.stop_event.set()
        if watcher is not threading.current_thread():
            watcher.join(WATCHER_STOP_TIMEOUT_S)
            if watcher.is_alive():
                log.warning("MangoWM watcher did not stop within the timeout")


class FocusWatcher(threading.Thread):
    def __init__(self, integration: MangoWM) -> None:
        super().__init__(name="mangowm-focus", daemon=True)
        self.integration = integration
        self.stop_event = threading.Event()
        self.last_window: Window | None = None

    def _publish(self, window: Window | None) -> None:
        if self.stop_event.is_set() or window is None or window == self.last_window:
            return
        self.last_window = window
        self.integration.window_grabber.on_active_window_changed(window)

    @override
    def run(self) -> None:
        while gl.threads_running and not self.stop_event.is_set():
            path = self.integration._socket_path()
            if path is None:
                self._publish(self.integration.get_active_window())
                self.stop_event.wait(0.2)
                continue
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(0.2)
                    client.connect(path)
                    client.sendall(b"watch focusing-client\n")
                    self._publish(self.integration.get_active_window())
                    pending = b""
                    while gl.threads_running and not self.stop_event.is_set():
                        try:
                            data = client.recv(4096)
                        except socket.timeout:
                            continue
                        if not data:
                            break
                        pending += data
                        if len(pending) > 1024 * 1024:
                            raise ValueError("MangoWM event exceeds 1 MiB")
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            try:
                                self._publish(window_from_message(json.loads(line)))
                            except (ValueError, UnicodeError):
                                continue
            except (OSError, ValueError) as error:
                log.debug(f"MangoWM socket unavailable: {error}")
            self._publish(self.integration.get_active_window())
            self.stop_event.wait(2)
