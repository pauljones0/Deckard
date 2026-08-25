"""The sidebar FPS row must cover GIF media and offer a revert.

A GIF is video media, so the row appears for it like any other video. The
revert arrow appears only while the page carries an explicit rate, clears the
key when clicked, and leaves the row showing the rate the media itself runs
at. The row must stay wired across all of that, or a later edit is dropped
silently.

This harness builds no real GTK widget. It drives the real row and expander
methods on duck-typed stand-ins, the same pattern scenario_editor_reconnect
uses.
"""

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import os

import globals as gl

from src.backend import services
from src.backend.DeckManagement.InputIdentifier import Input
from src.windows.mainWindow.elements.Sidebar.elements.BackgroundEditor import (
    BackgroundExpanderRow,
    VideoFpsRow,
)

KEY = Input.Key("0x0")
DIAL = Input.Dial("0")
TOUCHSCREEN = Input.Touchscreen("0")


def media_file(name: str) -> str:
    """A real file, because is_video() stats the path before it looks at the
    extension."""
    path = os.path.join(gl.DATA_PATH, "media", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"")
    return path


class FakeSpinner:
    """A spin button that records its handlers by id, as GTK does."""

    def __init__(self) -> None:
        self._handlers: dict[int, object] = {}
        self._next = 1
        self._value = 0.0

    def connect(self, signal, handler):
        hid = self._next
        self._next += 1
        self._handlers[hid] = handler
        return hid

    def disconnect(self, hid):
        del self._handlers[hid]

    def set_value(self, value):
        self._value = value

    def get_value(self):
        return self._value

    def fire(self):
        """Deliver a value-changed to every connected handler, as GTK would."""
        for handler in list(self._handlers.values()):
            handler(self)

    def handler_count(self):
        return len(self._handlers)


class FakeRevert:
    def __init__(self) -> None:
        self.visible = None

    def set_visible(self, value):
        self.visible = value


class FakePage:
    """The page seam the row reads and writes.

    A rate of None means the page carries no key for it, which is what a
    revert must leave behind.
    """

    def __init__(self, media_path=None, background_image=None,
                 media_fps=None, background_fps=None, native=None) -> None:
        self.media_path = media_path
        self.background_image = background_image
        self.media_fps = media_fps
        self.background_fps = background_fps
        self.native = native
        self.writes: list[tuple] = []
        self.native_reads = 0

    def get_media_path(self, identifier, state):
        return self.media_path

    def get_background_image(self, identifier, state):
        return self.background_image

    def has_media_fps(self, identifier, state):
        return self.media_fps is not None

    def get_media_fps(self, identifier, state):
        return 30 if self.media_fps is None else self.media_fps

    def get_media_native_fps(self, identifier, state):
        self.native_reads += 1
        return self.native

    def set_media_fps(self, identifier, state, fps, update=True):
        self.media_fps = fps
        self.writes.append(("media", fps))

    def has_background_fps(self, identifier, state):
        return self.background_fps is not None

    def get_background_fps(self, identifier, state):
        return 30 if self.background_fps is None else self.background_fps

    def set_background_fps(self, identifier, state, fps, update=True):
        self.background_fps = fps
        self.writes.append(("background", fps))

    def get_background_loop(self, identifier, state):
        return True


class FakeFpsRow:
    """The real VideoFpsRow logic on a duck-typed body."""

    MIN_FPS = VideoFpsRow.MIN_FPS
    MAX_FPS = VideoFpsRow.MAX_FPS
    load_for_identifier = VideoFpsRow.load_for_identifier
    on_change = VideoFpsRow.on_change
    on_revert = VideoFpsRow.on_revert
    connect_signals = VideoFpsRow.connect_signals
    disconnect_signals = VideoFpsRow.disconnect_signals
    _uses_media_fps = VideoFpsRow._uses_media_fps
    _write_fps = VideoFpsRow._write_fps
    _stored_fps = VideoFpsRow._stored_fps
    _has_override = VideoFpsRow._has_override
    _displayed_fps = VideoFpsRow._displayed_fps

    def __init__(self) -> None:
        self.spinner = FakeSpinner()
        self.revert_button = FakeRevert()
        self.active_identifier = None
        self.active_state = None
        self._change_handler = None
        self.visible = None
        self.connect_signals()

    def set_visible(self, value):
        self.visible = value


class FakeLoopRow:
    def __init__(self) -> None:
        self.visible = None

    def set_visible(self, value):
        self.visible = value

    def load_for_identifier(self, identifier, state):
        pass


class FakeExpander:
    """The real row-visibility rule on a duck-typed body."""

    update_video_rows = BackgroundExpanderRow.update_video_rows

    def __init__(self, fps_row, loop_row) -> None:
        self.video_fps_row = fps_row
        self.video_loop_row = loop_row
        self.active_identifier = None
        self.active_state = None


class FakeMainWindow:
    def __init__(self, page) -> None:
        self._page = page

    def get_active_page(self):
        return self._page


def install(page) -> None:
    window = FakeMainWindow(page)
    services.require_main_window = lambda: window
    # The loaders reach the window through gl.app, and the change handlers
    # through services. Both must answer with the same page.
    gl.app = type("FakeApp", (), {"main_win": window})()


def check_gif_media_shows_the_row() -> int:
    """A GIF on a key is video media, so the row must appear for it.

    A dial keeps the exclusion: its page load cannot build a GIF at all yet,
    so a rate offered there would edit media that never reaches the dial.
    """
    cases = [
        (KEY, "clip.gif", True, "a GIF on a key"),
        (KEY, "clip.mp4", True, "an mp4 on a key"),
        (KEY, "still.png", False, "a still image on a key"),
        (KEY, None, False, "no media at all on a key"),
        (DIAL, "clip.gif", False, "a GIF on a dial"),
        (DIAL, "clip.mp4", True, "an mp4 on a dial"),
    ]
    for identifier, name, expected, description in cases:
        page = FakePage(media_path=media_file(name) if name else None)
        install(page)
        row = FakeFpsRow()
        expander = FakeExpander(row, FakeLoopRow())
        expander.active_identifier = identifier
        expander.active_state = 0
        expander.update_video_rows()
        if row.visible is not expected:
            print(f"FAIL(visible): the FPS row was "
                  f"{'shown' if row.visible else 'hidden'} for {description}; "
                  f"it must be {'shown' if expected else 'hidden'}")
            return 1
        if expander.video_loop_row.visible is not False:
            print(f"FAIL(visible): the loop row appeared for {description}; "
                  f"the media loop stays a page-dict and plugin concern")
            return 1
    print("PASS: the FPS row covers GIF media on a key, not on a dial, and "
          "only video media")
    return 0


def check_revert_hidden_until_a_rate_is_set() -> int:
    """The arrow means there is something to revert to."""
    page = FakePage(media_path=media_file("clip.gif"), native=9.6)
    install(page)
    row = FakeFpsRow()
    row.load_for_identifier(KEY, 0)
    if row.revert_button.visible is not False:
        print("FAIL(revert): the revert arrow shows on a page that carries no "
              "explicit frame rate -- there is nothing to revert to")
        return 1
    if row.spinner.get_value() != 10:
        print(f"FAIL(revert): with no rate stored the row must show the rate "
              f"the media runs at (9.6 fps rounds to 10), got "
              f"{row.spinner.get_value()!r}")
        return 1

    page.media_fps = 12
    row.load_for_identifier(KEY, 0)
    if row.revert_button.visible is not True:
        print("FAIL(revert): a page that carries an explicit rate must show "
              "the revert arrow")
        return 1
    if row.spinner.get_value() != 12:
        print(f"FAIL(revert): the row must show the stored rate 12, got "
              f"{row.spinner.get_value()!r}")
        return 1
    print("PASS: the revert arrow tracks whether the page carries a rate")
    return 0


def check_native_rate_is_clamped_into_range() -> int:
    """A media rate outside the spinner's range must not corrupt the row."""
    for native, expected in ((87.5, 30), (0.4, 1), (None, 30)):
        page = FakePage(media_path=media_file("clip.gif"), native=native)
        install(page)
        row = FakeFpsRow()
        row.load_for_identifier(KEY, 0)
        if row.spinner.get_value() != expected:
            print(f"FAIL(clamp): media running at {native!r} fps put "
                  f"{row.spinner.get_value()!r} in the row; the range is "
                  f"{FakeFpsRow.MIN_FPS} to {FakeFpsRow.MAX_FPS} and an "
                  f"unknown rate falls back to {expected}")
            return 1
    print("PASS: a media rate outside the spinner's range is clamped")
    return 0


def check_change_then_revert_round_trip() -> int:
    """An edit stores a rate; a revert clears it and shows the media's own."""
    page = FakePage(media_path=media_file("clip.gif"), native=9.6)
    install(page)
    row = FakeFpsRow()
    row.load_for_identifier(KEY, 0)

    row.spinner.set_value(6)
    row.spinner.fire()
    if page.media_fps != 6 or page.writes[-1] != ("media", 6):
        print(f"FAIL(edit): the spinner change did not reach the page: "
              f"{page.writes}")
        return 1
    if row.revert_button.visible is not True:
        print("FAIL(edit): setting a rate must reveal the revert arrow")
        return 1

    row.on_revert()
    if page.writes[-1] != ("media", None):
        print(f"FAIL(revert): the revert wrote {page.writes[-1]!r}; it must "
              f"clear the key, not store a number")
        return 1
    if page.media_fps is not None:
        print(f"FAIL(revert): the page still carries {page.media_fps!r}")
        return 1
    if row.revert_button.visible is not False:
        print("FAIL(revert): the arrow must go once the rate is cleared")
        return 1
    if row.spinner.get_value() != 10:
        print(f"FAIL(revert): after a revert the row must show the media's own "
              f"rate (9.6 rounds to 10), got {row.spinner.get_value()!r}")
        return 1
    print("PASS: an edit stores a rate and a revert clears it back to native")
    return 0


def check_top_of_range_stores_no_rate() -> int:
    """The top of the range limits nothing, so choosing it must clear the key.

    Storing it would leave the page carrying a rate with no effect, and the
    row offering a revert for something that does nothing.
    """
    page = FakePage(media_path=media_file("clip.gif"), media_fps=8, native=9.6)
    install(page)
    row = FakeFpsRow()
    row.load_for_identifier(KEY, 0)

    row.spinner.set_value(FakeFpsRow.MAX_FPS)
    row.spinner.fire()
    if page.writes[-1] != ("media", None):
        print(f"FAIL(top): choosing {FakeFpsRow.MAX_FPS} wrote "
              f"{page.writes[-1]!r}; the top of the range must clear the key, "
              f"as the revert control does")
        return 1
    if row.revert_button.visible is not False:
        print("FAIL(top): the revert arrow stayed after the rate was cleared")
        return 1
    print("PASS: choosing the top of the range clears the rate")
    return 0


def check_stored_top_reads_as_no_rate() -> int:
    """A page written before this rule can carry the ceiling. It limits
    nothing, so the row must treat it as no rate at all."""
    page = FakePage(media_path=media_file("clip.gif"),
                    media_fps=FakeFpsRow.MAX_FPS, native=12.0)
    install(page)
    row = FakeFpsRow()
    row.load_for_identifier(KEY, 0)
    if row.revert_button.visible is not False:
        print(f"FAIL(stored-top): a stored {FakeFpsRow.MAX_FPS} showed the "
              f"revert arrow, offering to revert a rate that limits nothing")
        return 1
    if row.spinner.get_value() != 12:
        print(f"FAIL(stored-top): with a stored {FakeFpsRow.MAX_FPS} the row "
              f"must show the media's own rate (12), got "
              f"{row.spinner.get_value()!r}")
        return 1
    print("PASS: a stored ceiling reads as no rate at all")
    return 0


def check_revert_leaves_the_row_wired() -> int:
    """A revert must not silently unwire the spinner.

    The revert writes into the spinner, so it disconnects first. A handler
    left off makes every later edit vanish without a word.
    """
    page = FakePage(media_path=media_file("clip.gif"), media_fps=20, native=10.0)
    install(page)
    row = FakeFpsRow()
    row.load_for_identifier(KEY, 0)

    row.on_revert()
    if row.spinner.handler_count() != 1:
        print(f"FAIL(wired): after a revert the spinner carries "
              f"{row.spinner.handler_count()} handlers, not 1")
        return 1

    before = len(page.writes)
    row.spinner.set_value(8)
    row.spinner.fire()
    if len(page.writes) != before + 1 or page.media_fps != 8:
        print(f"FAIL(wired): an edit after a revert was dropped: {page.writes}")
        return 1

    # A revert with no page behind it must return before it disconnects.
    services.require_main_window = lambda: FakeMainWindow(None)
    row.on_revert()
    if row.spinner.handler_count() != 1:
        print(f"FAIL(wired): a revert with no active page left "
              f"{row.spinner.handler_count()} handlers on the spinner")
        return 1
    print("PASS: a revert leaves the row wired, page or no page")
    return 0


def check_touchscreen_uses_the_background_seam() -> int:
    """The same row edits a touchscreen background, through its own keys."""
    page = FakePage(background_image=media_file("wall.mp4"), background_fps=15)
    install(page)
    row = FakeFpsRow()
    row.load_for_identifier(TOUCHSCREEN, 0)
    if row.spinner.get_value() != 15 or row.revert_button.visible is not True:
        print(f"FAIL(touchscreen): the row showed {row.spinner.get_value()!r} "
              f"with arrow={row.revert_button.visible!r} for a background "
              f"carrying 15")
        return 1
    if page.native_reads:
        print("FAIL(touchscreen): the row asked for a MEDIA rate while editing "
              "a background")
        return 1

    row.on_revert()
    if page.writes[-1] != ("background", None) or page.background_fps is not None:
        print(f"FAIL(touchscreen): the revert wrote {page.writes[-1]!r} and "
              f"left {page.background_fps!r}")
        return 1
    if page.media_fps is not None:
        print("FAIL(touchscreen): editing a background wrote a media rate")
        return 1
    print("PASS: a touchscreen background reverts through its own page keys")
    return 0


def main() -> int:
    fixtures.start_watchdog(60, "background_fps_row")
    rc = check_gif_media_shows_the_row()
    rc |= check_revert_hidden_until_a_rate_is_set()
    rc |= check_native_rate_is_clamped_into_range()
    rc |= check_change_then_revert_round_trip()
    rc |= check_top_of_range_stores_no_rate()
    rc |= check_stored_top_reads_as_no_rate()
    rc |= check_revert_leaves_the_row_wired()
    rc |= check_touchscreen_uses_the_background_seam()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
