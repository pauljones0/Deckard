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

import os
import select
import threading
from typing import TYPE_CHECKING

from loguru import logger as log

import globals as gl

from Xlib import X
from Xlib import display as xlib_display
from Xlib.error import XError

from src.backend.WindowGrabber.Integration import Integration, WATCHER_STOP_TIMEOUT_S
from src.backend.WindowGrabber.Window import Window

if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

# How long a select() parks before it re-reads the stop flag and
# gl.threads_running on its own. A stop() writes the wake pipe, so a stop
# returns at once and does not wait this out. This bounds only the case where
# gl.threads_running goes false during quit with no stop() call.
WATCH_SELECT_TIMEOUT_S = 1.0

# The root property that names the focused window, and the two properties that
# carry a window title. _NET_WM_NAME holds the UTF-8 title that modern window
# managers set; WM_NAME is the older latin-1 property, read only when the first
# is absent.
_NET_ACTIVE_WINDOW = "_NET_ACTIVE_WINDOW"
_NET_CLIENT_LIST = "_NET_CLIENT_LIST"
_TITLE_PROPERTIES = ("_NET_WM_NAME", "WM_NAME")


def _open_display():
    """Opens a connection to the X server, or None when none can be reached.

    None is the graceful fallback: a Wayland-only session with no Xwayland, or
    DISPLAY unset, has no X server, so the watcher reports nothing rather than
    raising, which is what the previous xprop poller did when it found no
    server.
    """
    try:
        return xlib_display.Display()
    except Exception as e:
        log.warning(f"Could not open the X display: {e}")
        return None


def _close_display(display) -> None:
    """Closes an X connection. Guards the close, because a connection already
    dropped by the server raises on close, and a teardown must not."""
    if display is None:
        return
    try:
        display.close()
    except Exception as e:
        log.debug(f"Closing the X display raised: {e}")


def _intern_title_atoms(display) -> tuple[int, ...]:
    return tuple(display.intern_atom(name) for name in _TITLE_PROPERTIES)


def _active_window_id(root, active_atom) -> int | None:
    """The id of the focused window from the root's _NET_ACTIVE_WINDOW, or None
    when nothing is focused or the property is absent."""
    try:
        prop = root.get_full_property(active_atom, X.AnyPropertyType)
    except XError:
        return None
    if prop is None:
        return None
    value = prop.value
    if value is None or len(value) == 0:
        return None
    window_id = int(value[0])
    if window_id == 0:
        return None
    return window_id


def _read_window_title(window, title_atoms) -> str | None:
    """The window title. Prefers the UTF-8 _NET_WM_NAME and falls back to
    WM_NAME. None when the window carries neither."""
    for atom in title_atoms:
        try:
            prop = window.get_full_property(atom, X.AnyPropertyType)
        except XError:
            prop = None
        if prop is None:
            continue
        value = prop.value
        if value is None or len(value) == 0:
            continue
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)
    return None


def _read_window_class(window) -> str | None:
    """The window class. get_wm_class returns (instance, class); the auto-change
    rules match against the class, so this returns the second string, the same
    one the previous xprop reader took."""
    try:
        pair = window.get_wm_class()
    except XError:
        return None
    if not pair or len(pair) < 2:
        return None
    window_class = pair[1]
    return window_class or None


def _read_window(display, window_id, title_atoms) -> Window | None:
    """The Window for one id, or None when the title or the class is missing.
    A window with neither read cannot match a rule."""
    window = display.create_resource_object("window", window_id)
    title = _read_window_title(window, title_atoms)
    window_class = _read_window_class(window)
    if title is None or window_class is None:
        return None
    return Window(window_class, title)


def _read_active_window(display, root, active_atom, title_atoms) -> Window | None:
    """The focused window as a Window, or None when nothing is focused or its
    title and class cannot be read."""
    window_id = _active_window_id(root, active_atom)
    if window_id is None:
        return None
    return _read_window(display, window_id, title_atoms)


def _is_active_window_change(event, root_id: int, active_atom: int) -> bool:
    """True when the event is the root's _NET_ACTIVE_WINDOW property changing,
    which is how the server reports a new focused window."""
    if event.type != X.PropertyNotify:
        return False
    if event.atom != active_atom:
        return False
    return event.window.id == root_id


def _is_title_change(event, window_id: int | None, title_atoms: tuple[int, ...]) -> bool:
    """True when the event is a title property changing on the window this
    watcher tracks, which reports a title change within the same focused
    window, such as a browser moving between tabs."""
    if window_id is None:
        return False
    if event.type != X.PropertyNotify:
        return False
    if event.window.id != window_id:
        return False
    return event.atom in title_atoms


class X11(Integration):
    def __init__(self, window_grabber: "WindowGrabber"):
        super().__init__(window_grabber=window_grabber)
        self.active_window_change_thread: "WatchForActiveWindowChange | None" = None

    @log.catch
    def start_watching(self) -> None:
        thread = self.active_window_change_thread
        if thread is not None and thread.is_alive():
            return

        # A Thread object cannot restart, so each start builds a fresh one,
        # which opens its own X connection again. The connection lives on the
        # watcher thread only; a one-shot query opens its own.
        thread = WatchForActiveWindowChange(self)
        self.active_window_change_thread = thread
        thread.start()

    @log.catch
    def stop_watching(self) -> None:
        thread = self.active_window_change_thread
        self.active_window_change_thread = None
        if thread is None:
            return

        thread.stop()
        if thread is threading.current_thread():
            # Never join the calling thread to itself. A window change can
            # reach a page write, and a page write re-gates. The loop ends at
            # its next stop check. The return also keeps the timeout warning
            # below for a real timeout, not for a skipped join.
            return

        thread.join(timeout=WATCHER_STOP_TIMEOUT_S)
        if thread.is_alive():
            # The thread is a daemon, and stop() wakes a select parked on the X
            # connection, so it unwinds on its own past the timeout. The
            # reference drops either way, so a later start builds a clean
            # thread.
            log.warning("The X11 active window watcher did not stop within the timeout")

    @log.catch
    def get_all_windows(self) -> list[Window]:
        display = _open_display()
        if display is None:
            return []

        windows: list[Window] = []
        try:
            root = display.screen().root
            title_atoms = _intern_title_atoms(display)
            client_list_atom = display.intern_atom(_NET_CLIENT_LIST)
            prop = root.get_full_property(client_list_atom, X.AnyPropertyType)
            window_ids = list(prop.value) if prop is not None else []
            for window_id in window_ids:
                window = _read_window(display, int(window_id), title_atoms)
                if window is not None:
                    windows.append(window)
        except Exception:
            log.opt(exception=True).error("Could not list windows over Xlib")
        finally:
            _close_display(display)

        return windows

    @log.catch
    def get_active_window(self) -> Window | None:
        display = _open_display()
        if display is None:
            return None

        try:
            root = display.screen().root
            active_atom = display.intern_atom(_NET_ACTIVE_WINDOW)
            title_atoms = _intern_title_atoms(display)
            return _read_active_window(display, root, active_atom, title_atoms)
        except Exception:
            log.opt(exception=True).error("Could not read the active window over Xlib")
            return None
        finally:
            _close_display(display)


class WatchForActiveWindowChange(threading.Thread):
    """Watch for active window changes over the X server's event stream.

    The thread opens its own X connection and asks the server to send a
    PropertyNotify when the root's _NET_ACTIVE_WINDOW changes, which is a new
    focused window, and when the focused window's title changes. It costs I/O
    wait only, and no CPU while the focused window stays. The previous reader
    ran five xprop processes every 200 ms, and five more under Flatpak through
    flatpak-spawn.

    It falls back to reporting nothing when it cannot open the X connection,
    which happens on a Wayland-only session with no Xwayland and with DISPLAY
    unset.

    display_factory builds the X connection. The default opens the real server;
    a test passes a stub that emits synthetic events, so the decode path runs
    with no real server.
    """

    def __init__(self, x11: X11, display_factory=None):
        super().__init__(name="WatchForActiveWindowChange", daemon=True)
        self.x11 = x11
        self._stop_event = threading.Event()
        self._display_factory = display_factory or _open_display

        # A byte written here wakes a select() parked on the X connection, so a
        # stop returns at once rather than waiting out the select timeout. A
        # lock pairs the write with the close: the write only runs while the
        # pipe is open, so a stop cannot write a file descriptor the teardown
        # has closed and the kernel has since handed to something else.
        self._wake_lock = threading.Lock()
        self._wake_read_fd, self._wake_write_fd = os.pipe()
        self._wake_closed = False

        self._display = None
        self._root_id: int | None = None
        self._active_atom: int | None = None
        self._title_atoms: tuple[int, ...] = ()
        # The window whose title this watcher tracks now. It follows the
        # focused window, so a title change on the same window still reports.
        self._tracked_window = None
        self._tracked_window_id: int | None = None
        self._last_reported: Window | None = None

    def stop(self) -> None:
        """Asks the loop to end. Returns at once, and the caller joins."""
        self._stop_event.set()
        with self._wake_lock:
            if self._wake_closed:
                return
            try:
                os.write(self._wake_write_fd, b"\x00")
            except OSError:
                # The pipe is full, which already wakes the select, or it is
                # gone. Either way the loop reaches its next stop check.
                pass

    def _close_wake_pipe(self) -> None:
        with self._wake_lock:
            if self._wake_closed:
                return
            self._wake_closed = True
            for fd in (self._wake_read_fd, self._wake_write_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass

    @log.catch
    def run(self) -> None:
        display = None
        try:
            display = self._display_factory()
        except Exception as e:
            log.warning(f"The X11 active window watcher could not open a display: {e}")

        if display is None:
            log.warning("The X11 active window watcher found no X server; it reports nothing")
            self._close_wake_pipe()
            return

        self._display = display
        try:
            self._watch(display)
        finally:
            # The connection is closed on the thread that opened it, so a stop
            # leaks neither the connection nor the thread. Closing the
            # connection ends every selection on it, so there is nothing else
            # to deselect first. The wake pipe is closed here too, so a
            # self-stop that skips the join still frees both descriptors.
            self._tracked_window = None
            _close_display(display)
            self._display = None
            self._close_wake_pipe()

    def _watch(self, display) -> None:
        root = display.screen().root
        self._root_id = root.id
        self._active_atom = display.intern_atom(_NET_ACTIVE_WINDOW)
        self._title_atoms = _intern_title_atoms(display)

        # Ask the server for PropertyNotify on the root, so a new focused
        # window arrives as an event.
        root.change_attributes(event_mask=X.PropertyChangeMask)
        display.flush()

        # Prime from the window focused now and track its title, so the first
        # real change is measured against the true starting point.
        self._track_focused_window(display, root)
        self._last_reported = _read_active_window(display, root, self._active_atom, self._title_atoms)

        display_fd = display.fileno()
        wake_fd = self._wake_read_fd

        while gl.threads_running and not self._stop_event.is_set():
            try:
                readable, _, _ = select.select([display_fd, wake_fd], [], [], WATCH_SELECT_TIMEOUT_S)
            except (OSError, ValueError):
                # A descriptor was closed under the select, which happens at
                # teardown. End the loop; the finally reaps the rest.
                break

            if self._stop_event.is_set():
                break
            if wake_fd in readable:
                self._drain_wake()
                if self._stop_event.is_set():
                    break
            if display_fd not in readable:
                continue

            # Catch per drain, like the Hyprland socket listener does. One
            # failed decode or page switch must not end this thread. An
            # exception that reaches @log.catch kills window-based page
            # switching until the next app start.
            try:
                self._drain_events(display, root)
            except Exception as e:
                log.opt(exception=True).error(
                    f"Unexpected error in the X11 active window watcher: {e}"
                )

    def _drain_events(self, display, root) -> None:
        while display.pending_events():
            event = display.next_event()
            self._handle_event(display, root, event)

    def _handle_event(self, display, root, event) -> None:
        root_id = self._root_id
        active_atom = self._active_atom
        if root_id is None or active_atom is None:
            # _watch sets both before the loop starts, so this cannot happen in
            # practice; the guard keeps the atom types honest for the decode.
            return
        if _is_active_window_change(event, root_id, active_atom):
            self._track_focused_window(display, root)
            self._report(display, root)
        elif _is_title_change(event, self._tracked_window_id, self._title_atoms):
            self._report(display, root)

    def _report(self, display, root) -> None:
        window = _read_active_window(display, root, self._active_atom, self._title_atoms)
        if window is None:
            return
        if window == self._last_reported:
            return
        self._last_reported = window
        self.x11.window_grabber.on_active_window_changed(window)

    def _track_focused_window(self, display, root) -> None:
        """Moves the title watch onto the focused window, so a title change on
        the same window reports too."""
        window_id = _active_window_id(root, self._active_atom)
        if window_id == self._tracked_window_id:
            return

        if self._tracked_window is not None:
            # Stop watching the window this leaves. It may be gone already, so
            # the request is allowed to fail.
            try:
                self._tracked_window.change_attributes(event_mask=X.NoEventMask)
            except XError:
                pass
        self._tracked_window = None
        self._tracked_window_id = window_id

        if window_id is None:
            return
        try:
            window = display.create_resource_object("window", window_id)
            window.change_attributes(event_mask=X.PropertyChangeMask)
            display.flush()
            self._tracked_window = window
        except XError:
            # The window vanished between the id read and the select. The next
            # active-window change re-tracks.
            pass

    def _drain_wake(self) -> None:
        try:
            os.set_blocking(self._wake_read_fd, False)
            while True:
                if os.read(self._wake_read_fd, 4096) == b"":
                    break
        except (BlockingIOError, OSError):
            pass
