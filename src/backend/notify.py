"""Route notifications from any thread through readiness and GLib; use desktop delivery unless the window is visible.
This module imports globals, so globals must not import it; main.create_global_objects fills the gl.notify slot."""
from gi.repository import GLib

import appinfo
import globals as gl
from src.backend import startup_queue


class Notify:
    def info(self, text: str, title: str | None = None) -> None:
        """Non-urgent feedback. Toast while the window is up, desktop
        notification otherwise. Safe to call from any thread."""
        self._dispatch(False, text, title)

    def error(self, text: str, title: str | None = None) -> None:
        """Something the user asked for did not happen. Same routing as
        info(), with the error presentation. Safe to call from any thread."""
        self._dispatch(True, text, title)

    def _dispatch(self, is_error: bool, text: str, title: str | None) -> None:
        # A false result transfers delivery to the startup queue for replay.
        # startup_queue serializes append against the main-thread drain.
        if not startup_queue.get().when_app_ready(
                lambda: self._dispatch(is_error, text, title)):
            return
        GLib.idle_add(self._deliver, is_error, text, title)

    def _deliver(self, is_error: bool, text: str, title: str | None) -> bool:
        # Main thread only; bind gl.app once because this runs after scheduling.
        # The desktop branch must use the same application instance.
        app = gl.app
        main_win = getattr(app, "main_win", None)
        if main_win is not None and main_win.is_visible():
            if is_error:
                main_win.show_error_toast(text)
            else:
                main_win.show_info_toast(text)
        elif app is not None:
            icon = "dialog-error-symbolic" if is_error else "dialog-information-symbolic"
            app.send_notification(icon, title or appinfo.APP_NAME, text)
        return GLib.SOURCE_REMOVE
