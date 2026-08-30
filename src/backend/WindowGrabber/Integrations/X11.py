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

import contextlib
import os
import select
import threading
from typing import TYPE_CHECKING, cast, override

from loguru import logger as log

import globals as gl

from Xlib import X
from Xlib import display as xlib_display
from Xlib.error import ConnectionClosedError, XError

from src.backend.WindowGrabber.Integration import Integration, WATCHER_STOP_TIMEOUT_S
from src.backend.WindowGrabber.Window import Window

if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

# Bound select when quit sets gl.threads_running false without calling stop.
# stop writes the wake pipe and does not wait for this timeout.
WATCH_SELECT_TIMEOUT_S = 1.0

# Repeated readable drains with no event indicate an EOF socket that would spin at full CPU.
# Permit one empty partial read before treating the connection as dead.
_MAX_EMPTY_READABLE_DRAINS = 2

# _NET_ACTIVE_WINDOW names focus; _NET_WM_NAME carries the preferred UTF-8 title.
# Fall back to the older Latin-1 WM_NAME only when the preferred property is absent.
_NET_ACTIVE_WINDOW = "_NET_ACTIVE_WINDOW"
_NET_CLIENT_LIST = "_NET_CLIENT_LIST"
_TITLE_PROPERTIES = ("_NET_WM_NAME", "WM_NAME")


def _open_display():
    """Open an X connection, or return None without raising.
    Wayland sessions without Xwayland and sessions without DISPLAY therefore report no windows."""
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
    return cast("tuple[int, ...]", tuple(display.intern_atom(name) for name in _TITLE_PROPERTIES))


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
    """Read the preferred UTF-8 _NET_WM_NAME, then WM_NAME, or return None.
    Strip a non-conformant trailing NUL so title regular expressions can match."""
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
            return cast(str, value.decode("utf-8", errors="replace").rstrip("\x00"))
        return str(value).rstrip("\x00")
    return None


def _read_window_class(window) -> str | None:
    """Return the class from get_wm_class's (instance, class) pair.
    Auto-change rules match the second value."""
    try:
        pair = window.get_wm_class()
    except XError:
        return None
    if not pair or len(pair) < 2:
        return None
    window_class = pair[1]
    return cast("str | None", window_class or None)


def _read_window(display, window_id, title_atoms) -> Window | None:
    """Return the Window for one id, or None when either title or class is missing."""
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
    return cast(bool, event.window.id == root_id)


def _is_title_change(event, window_id: int | None, title_atoms: tuple[int, ...]) -> bool:
    """Return true for a tracked focused-window title change.
    This includes title changes within one window, such as browser tab changes."""
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
    @override
    def start_watching(self) -> None:
        thread = self.active_window_change_thread
        if thread is not None and thread.is_alive():
            return

        # Threads cannot restart, so each start builds a watcher with its own X connection.
        # The watcher thread owns that connection; one-shot queries open separate connections.
        thread = WatchForActiveWindowChange(self)
        self.active_window_change_thread = thread
        try:
            thread.start()
        except RuntimeError:
            # run did not start to close the wake pipe, so close it here.
            # Drop the reference so later stop is inert and does not join an unstarted thread.
            log.opt(exception=True).error("Could not start the X11 active window watcher")
            self.active_window_change_thread = None
            thread._close_wake_pipe()

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
            # This daemon unwinds after stop wakes select, even past the join timeout.
            # Drop the reference so a later start builds a fresh thread.
            log.warning("The X11 active window watcher did not stop within the timeout")

    @log.catch
    @override
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
    @override
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
    """Watch root focus and focused-window title PropertyNotify events on a thread-owned X connection.
    Report nothing without an X server; display_factory permits synthetic event tests without one."""

    def __init__(self, x11: X11, display_factory=None):
        super().__init__(name="WatchForActiveWindowChange", daemon=True)
        self.x11 = x11
        self._stop_event = threading.Event()
        self._display_factory = display_factory or _open_display

        # A locked wake-pipe write interrupts select without racing a close and reused file descriptor.
        # The non-blocking writer cannot stall; a full pipe already contains the required wake byte.
        self._wake_lock = threading.Lock()
        self._wake_read_fd, self._wake_write_fd = os.pipe()
        os.set_blocking(self._wake_write_fd, False)
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
            # A full non-blocking pipe raises but already contains a wake byte.
            # In either case, the loop reaches its next stop check.
            with contextlib.suppress(OSError):
                os.write(self._wake_write_fd, b"\x00")

    def _close_wake_pipe(self) -> None:
        with self._wake_lock:
            if self._wake_closed:
                return
            self._wake_closed = True
            for fd in (self._wake_read_fd, self._wake_write_fd):
                with contextlib.suppress(OSError):
                    os.close(fd)

    @log.catch
    @override
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
        connection_lost = False
        try:
            connection_lost = self._watch(display)
        finally:
            # Close the connection on its owner thread; this also ends every selection.
            # Close the wake pipe here so a self-stop that skips join still frees both descriptors.
            self._tracked_window = None
            _close_display(display)
            self._display = None
            self._close_wake_pipe()

        if connection_lost:
            # Re-evaluate watcher rules as this thread ends after X disappears.
            # A quick X restart is found now; a longer outage is retried after the next rule edit.
            self._request_recovery()

    def _watch(self, display) -> bool:
        """Runs the event loop. Returns True when it ends because the X
        connection dropped, and False for a normal stop."""
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
        empty_readable_drains = 0

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
                empty_readable_drains = 0
                continue

            try:
                drained = self._drain_events(display, root)
            except ConnectionClosedError:
                # Server, Xwayland, display-manager, and SSH forwarding loss can close this connection.
                # Stop before select spins on the dead socket; the caller requests recovery.
                log.warning("The X connection closed; the X11 active window watcher stops")
                return True

            if drained == 0:
                # A repeated readable drain with no event is the library's zero-read EOF case.
                # Stop before select spins, but permit one partial read to complete.
                empty_readable_drains += 1
                if empty_readable_drains >= _MAX_EMPTY_READABLE_DRAINS:
                    log.warning("The X connection reached end of file; the X11 active window watcher stops")
                    return True
            else:
                empty_readable_drains = 0

        return False

    def _drain_events(self, display, root) -> int:
        """Read all queued events and return the count; zero on a readable connection indicates EOF.
        Propagate ConnectionClosedError, but isolate each event's decode and routing failure."""
        count = 0
        while display.pending_events():
            event = display.next_event()
            count += 1
            try:
                self._handle_event(display, root, event)
            except Exception as e:
                log.opt(exception=True).error(
                    f"Unexpected error in the X11 active window watcher: {e}"
                )
        return count

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

    def _request_recovery(self) -> None:
        """Ask the grabber to re-evaluate rules so a dropped X connection can restart its watch.
        No X server starts no watcher or retry loop; later rule changes provide another check."""
        grabber = getattr(self.x11, "window_grabber", None)
        refresh = getattr(grabber, "refresh_watch_state", None)
        if refresh is None:
            return
        try:
            refresh()
        except Exception:
            log.debug("Could not request a window watcher re-check after the X connection dropped")

    def _track_focused_window(self, display, root) -> None:
        """Moves the title watch onto the focused window, so a title change on
        the same window reports too."""
        window_id = _active_window_id(root, self._active_atom)
        if window_id == self._tracked_window_id:
            return

        if self._tracked_window is not None:
            # Stop watching the window this leaves. It may be gone already, so
            # the request is allowed to fail.
            with contextlib.suppress(XError):
                self._tracked_window.change_attributes(event_mask=X.NoEventMask)
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
