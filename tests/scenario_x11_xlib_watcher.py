"""
The X11 active-window watcher reads focus changes from the X event stream with
python-xlib, in place of a poll that spawned xprop processes.

A real X server is not reachable headlessly, so a stub display stands in. It
emits synthetic PropertyNotify events and answers the property reads the decode
path makes, which lets the whole watcher thread run: it opens the display,
selects events, decodes a change, reports it through the grabber interface, and
tears down clean.

The checks cover:
  * a focus change reports the new window through the grabber;
  * a title change on the same focused window reports too;
  * the loop survives an exception raised while routing one change;
  * teardown closes the display and joins the thread, leaking neither;
  * no reachable X server makes the watcher report nothing rather than raise;
  * the decode helpers turn events and properties into the right Window.
"""
import fixtures  # noqa: F401  (must be imported first: isolates DATA_PATH)

import os
import threading
import time
import types

import globals as gl

from Xlib import X
from Xlib.error import ConnectionClosedError

from src.backend.WindowGrabber.Window import Window
from src.backend.WindowGrabber.Integrations.X11 import (
    WatchForActiveWindowChange,
    _is_active_window_change,
    _read_active_window,
    _read_window_class,
)

ROOT_ID = 0x1


# Stub Xlib objects. Each answers exactly the calls the decode path makes.

class FakeProperty:
    def __init__(self, value):
        self.value = value


class FakeWindow:
    """One client window. get_full_property answers _NET_WM_NAME with the
    title, and get_wm_class answers (instance, class)."""

    def __init__(self, display: "FakeDisplay", wid: int):
        self.display = display
        self.id = wid
        self.selected_mask = None

    def _spec(self):
        return self.display.windows.get(self.id)

    def get_full_property(self, atom, prop_type):
        spec = self._spec()
        if spec is None:
            return None
        if atom == self.display.intern_atom("_NET_WM_NAME"):
            title = spec[0]
            if title is None:
                return None
            return FakeProperty(title.encode("utf-8"))
        # WM_NAME is left unset, so the fallback path is exercised too.
        return None

    def get_wm_class(self):
        spec = self._spec()
        if spec is None or spec[1] is None:
            return None
        return ("instance", spec[1])

    def change_attributes(self, onerror=None, **keys):
        self.selected_mask = keys.get("event_mask")


class FakeRoot(FakeWindow):
    def get_full_property(self, atom, prop_type):
        if atom == self.display.intern_atom("_NET_ACTIVE_WINDOW"):
            active = self.display.active_id
            if active is None:
                return None
            return FakeProperty([active])
        if atom == self.display.intern_atom("_NET_CLIENT_LIST"):
            return FakeProperty(list(self.display.windows))
        return None


class FakeScreen:
    def __init__(self, root):
        self.root = root


class FakeDisplay:
    """Stands in for an Xlib display connection. fileno() returns a real pipe
    read end, so the watcher's select() blocks on it; emit() queues an event
    and writes the pipe to wake that select."""

    def __init__(self, windows: dict, active_id):
        self.windows = dict(windows)
        self.active_id = active_id
        self.closed = False
        self.primed = threading.Event()

        self._atoms: dict = {}
        self._next_atom = 1000
        self._queue: list = []
        self._lock = threading.Lock()
        self._read_fd, self._write_fd = os.pipe()
        self._root = FakeRoot(self, ROOT_ID)

    def intern_atom(self, name):
        if name not in self._atoms:
            self._atoms[name] = self._next_atom
            self._next_atom += 1
        return self._atoms[name]

    def screen(self):
        return FakeScreen(self._root)

    def create_resource_object(self, kind, wid):
        return FakeWindow(self, wid)

    def fileno(self):
        # The watcher reads this once, after it primes and before the first
        # select. That is the moment the loop is ready for events.
        self.primed.set()
        return self._read_fd

    def pending_events(self):
        # Drain the wake bytes the emitter wrote, then report the queue depth.
        try:
            os.set_blocking(self._read_fd, False)
            while os.read(self._read_fd, 4096):
                pass
        except (BlockingIOError, OSError):
            pass
        with self._lock:
            return len(self._queue)

    def next_event(self):
        with self._lock:
            return self._queue.pop(0)

    def flush(self):
        pass

    def close(self):
        self.closed = True
        for fd in (self._read_fd, self._write_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    # Test-side controls

    def set_window(self, wid, title, wm_class):
        self.windows[wid] = (title, wm_class)

    def emit(self, event):
        with self._lock:
            self._queue.append(event)
        os.write(self._write_fd, b"\x00")


def _property_event(window_id, atom):
    return types.SimpleNamespace(
        type=X.PropertyNotify,
        window=types.SimpleNamespace(id=window_id),
        atom=atom,
    )


class Recorder:
    """The grabber stand-in. Records each reported window, and can raise on a
    marked class to model a routing that fails. It also records the re-check
    request the watcher makes when the X connection drops."""

    def __init__(self, raise_on_class=None):
        self.calls: list[Window] = []
        self._lock = threading.Lock()
        self._raise_on_class = raise_on_class
        self.recovery_requested = threading.Event()

    def on_active_window_changed(self, window: Window) -> None:
        with self._lock:
            self.calls.append(window)
        if self._raise_on_class is not None and window.wm_class == self._raise_on_class:
            raise RuntimeError(f"routing failed for {window.wm_class}")

    def refresh_watch_state(self) -> None:
        self.recovery_requested.set()

    def snapshot(self) -> list[Window]:
        with self._lock:
            return list(self.calls)


class EofRaiseDisplay(FakeDisplay):
    """A display whose connection drops and reports the drop by raising
    ConnectionClosedError from pending_events, the way python-xlib does. Its fd
    stays readable, so the watcher's select fires and reaches the drain."""

    def __init__(self):
        super().__init__(windows={0x10: ("Window", "app")}, active_id=0x10)
        self.pending_calls = 0
        os.write(self._write_fd, b"\x00")  # keep the fd readable for select

    def pending_events(self):
        self.pending_calls += 1
        raise ConnectionClosedError("server")


class EofZeroDisplay(FakeDisplay):
    """A display whose connection drops but reports the drop as end of file: a
    readable fd that yields zero events, with no error. The fd is kept readable
    and never drained, so a watcher that does not stop would spin on it."""

    def __init__(self):
        super().__init__(windows={0x10: ("Window", "app")}, active_id=0x10)
        self.pending_calls = 0
        os.write(self._write_fd, b"\x00")  # keep the fd readable for select

    def pending_events(self):
        self.pending_calls += 1
        return 0


def _fake_x11(recorder):
    return types.SimpleNamespace(window_grabber=recorder)


def _wait_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _teardown(watcher, display=None) -> None:
    watcher.stop()
    watcher.join(timeout=5.0)
    assert not watcher.is_alive(), "the watcher thread must join after stop()"
    assert watcher._wake_closed, "the wake pipe must be closed on teardown; a leak otherwise"
    if display is not None:
        assert display.closed, "the display must be closed on teardown; a leaked X connection otherwise"


# Check 1. A focus change reports the new window through the grabber

def check_reports_focus_change() -> None:
    display = FakeDisplay(
        windows={0x10: ("Terminal", "kitty"), 0x20: ("Mozilla Firefox", "firefox")},
        active_id=0x10,
    )
    recorder = Recorder()
    gl.threads_running = True
    watcher = WatchForActiveWindowChange(_fake_x11(recorder), display_factory=lambda: display)
    watcher.start()
    try:
        assert display.primed.wait(5.0), "the watcher must open the display and prime"

        display.active_id = 0x20
        display.emit(_property_event(ROOT_ID, display.intern_atom("_NET_ACTIVE_WINDOW")))

        assert _wait_until(lambda: len(recorder.snapshot()) >= 1), (
            "a focus change must report the new window"
        )
        reported = recorder.snapshot()[-1]
        assert reported == Window("firefox", "Mozilla Firefox"), (
            f"the reported window must carry the new class and title, got {reported}"
        )
    finally:
        gl.threads_running = False
        _teardown(watcher, display)


# Check 2. A title change on the same focused window reports too

def check_reports_title_change() -> None:
    display = FakeDisplay(windows={0x10: ("Firefox", "firefox")}, active_id=0x10)
    recorder = Recorder()
    gl.threads_running = True
    watcher = WatchForActiveWindowChange(_fake_x11(recorder), display_factory=lambda: display)
    watcher.start()
    try:
        assert display.primed.wait(5.0), "the watcher must open the display and prime"

        # The focused window stays; only its title changes.
        display.set_window(0x10, "YouTube - Firefox", "firefox")
        display.emit(_property_event(0x10, display.intern_atom("_NET_WM_NAME")))

        assert _wait_until(lambda: len(recorder.snapshot()) >= 1), (
            "a title change on the focused window must report"
        )
        reported = recorder.snapshot()[-1]
        assert reported == Window("firefox", "YouTube - Firefox"), (
            f"the reported window must carry the new title, got {reported}"
        )
    finally:
        gl.threads_running = False
        _teardown(watcher, display)


# Check 3. The loop survives an exception raised while routing one change

def check_survives_raising_route() -> None:
    display = FakeDisplay(
        windows={
            0x10: ("Startup", "startup"),
            0x20: ("Terminal", "kitty"),
            0x30: ("Mozilla Firefox", "firefox"),
        },
        active_id=0x10,
    )
    recorder = Recorder(raise_on_class="kitty")
    gl.threads_running = True
    watcher = WatchForActiveWindowChange(_fake_x11(recorder), display_factory=lambda: display)
    watcher.start()
    try:
        assert display.primed.wait(5.0), "the watcher must open the display and prime"

        active_atom = display.intern_atom("_NET_ACTIVE_WINDOW")

        display.active_id = 0x20  # routing raises on this one
        display.emit(_property_event(ROOT_ID, active_atom))
        assert _wait_until(lambda: len(recorder.snapshot()) >= 1), (
            "the crashing change must reach routing"
        )

        display.active_id = 0x30  # must still be routed after the raise
        display.emit(_property_event(ROOT_ID, active_atom))
        assert _wait_until(lambda: len(recorder.snapshot()) >= 2), (
            "the watch loop must keep routing after a routed change raised"
        )

        assert recorder.snapshot() == [Window("kitty", "Terminal"), Window("firefox", "Mozilla Firefox")], (
            f"both changes must route in order, got {recorder.snapshot()}"
        )
        assert watcher.is_alive(), "the watcher thread must survive a raising route"
    finally:
        gl.threads_running = False
        _teardown(watcher, display)


# Check 4. No reachable X server makes the watcher report nothing, not raise

def check_fallback_no_display() -> None:
    recorder = Recorder()
    gl.threads_running = True
    watcher = WatchForActiveWindowChange(_fake_x11(recorder), display_factory=lambda: None)
    watcher.start()
    watcher.join(timeout=5.0)

    assert not watcher.is_alive(), "the watcher must exit at once when no display opens"
    assert recorder.snapshot() == [], "the watcher must report nothing with no display"
    assert watcher._wake_closed, "the wake pipe must be closed even on the no-display path"


# Check 5. The decode helpers turn events and properties into the right Window

def check_decode_helpers() -> None:
    display = FakeDisplay(windows={0x20: ("Mozilla Firefox", "firefox")}, active_id=0x20)
    root = display.screen().root
    active_atom = display.intern_atom("_NET_ACTIVE_WINDOW")
    title_atoms = (display.intern_atom("_NET_WM_NAME"), display.intern_atom("WM_NAME"))

    window = _read_active_window(display, root, active_atom, title_atoms)
    assert window == Window("firefox", "Mozilla Firefox"), (
        f"the decode must read the active window's class and title, got {window}"
    )

    # The class is the second WM_CLASS string, not the instance. A decode that
    # returns the instance is a defect this pins.
    only_window = display.create_resource_object("window", 0x20)
    assert _read_window_class(only_window) == "firefox", (
        "the window class must be the WM_CLASS class string, not the instance"
    )

    # The predicate accepts only the root's _NET_ACTIVE_WINDOW property change.
    assert _is_active_window_change(_property_event(ROOT_ID, active_atom), ROOT_ID, active_atom), (
        "a root _NET_ACTIVE_WINDOW change must be recognised"
    )
    assert not _is_active_window_change(_property_event(ROOT_ID, active_atom + 7), ROOT_ID, active_atom), (
        "a change of a different property must not be taken for a focus change"
    )
    assert not _is_active_window_change(_property_event(0x20, active_atom), ROOT_ID, active_atom), (
        "a property change on a non-root window must not be taken for a focus change"
    )
    non_property = types.SimpleNamespace(type=X.KeyPress, window=types.SimpleNamespace(id=ROOT_ID), atom=active_atom)
    assert not _is_active_window_change(non_property, ROOT_ID, active_atom), (
        "a non-PropertyNotify event must not be taken for a focus change"
    )


# Check 6. A dropped connection that raises stops the watcher and asks for a
# restart, rather than logging a traceback per pass forever

def check_connection_drop_raises_stops() -> None:
    display = EofRaiseDisplay()
    recorder = Recorder()
    gl.threads_running = True
    watcher = WatchForActiveWindowChange(_fake_x11(recorder), display_factory=lambda: display)
    watcher.start()
    try:
        assert display.primed.wait(5.0), "the watcher must open the display and prime"

        # No stop() is called; the watcher must stop itself on the drop.
        exited = _wait_until(lambda: not watcher.is_alive(), timeout=5.0)
        assert exited, "a raised connection drop must stop the watcher, not loop on it"
        assert display.pending_calls <= 4, (
            f"the watcher must stop on the first raised drop, not retry in a tight "
            f"loop; pending_events was called {display.pending_calls} times"
        )
        assert display.closed, "the display must be closed after the connection drops"
        assert watcher._wake_closed, "the wake pipe must be closed after the connection drops"
        assert recorder.recovery_requested.wait(3.0), (
            "the watcher must ask the grabber to re-decide the watch after a drop"
        )
    finally:
        gl.threads_running = False
        watcher.join(timeout=5.0)


# Check 7. A dropped connection seen as end of file (readable, zero events)
# stops the watcher within a bounded number of passes rather than spinning

def check_connection_drop_eof_stops() -> None:
    display = EofZeroDisplay()
    recorder = Recorder()
    gl.threads_running = True
    watcher = WatchForActiveWindowChange(_fake_x11(recorder), display_factory=lambda: display)
    watcher.start()
    try:
        assert display.primed.wait(5.0), "the watcher must open the display and prime"

        exited = _wait_until(lambda: not watcher.is_alive(), timeout=5.0)
        assert exited, "an end-of-file connection must stop the watcher, not spin at full CPU"
        # A bound well under a spin. The watcher stops after a couple of empty
        # readable passes; a spin would run this into the thousands.
        assert display.pending_calls <= 8, (
            f"the watcher must stop within a few passes, not spin; pending_events "
            f"was called {display.pending_calls} times"
        )
        assert display.closed, "the display must be closed after the connection drops"
        assert watcher._wake_closed, "the wake pipe must be closed after the connection drops"
        assert recorder.recovery_requested.wait(3.0), (
            "the watcher must ask the grabber to re-decide the watch after a drop"
        )
    finally:
        gl.threads_running = False
        watcher.join(timeout=5.0)


def main() -> None:
    fixtures.start_watchdog(90, label="scenario_x11_xlib_watcher")
    check_reports_focus_change()
    check_reports_title_change()
    check_survives_raising_route()
    check_fallback_no_display()
    check_decode_helpers()
    check_connection_drop_raises_stops()
    check_connection_drop_eof_stops()
    print("PASS: scenario_x11_xlib_watcher")


if __name__ == "__main__":
    main()
