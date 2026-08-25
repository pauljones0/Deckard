"""The sidebar FPS row must cover GIF media and offer a revert.

A GIF is video media, so the row appears for it like any other video. The
revert arrow appears only while the page carries an explicit rate, clears the
key when clicked, and leaves the row showing the rate the media itself runs
at. The row must stay wired across all of that, or a later edit is dropped
silently.

The arrow arrives late. A spin button steps its value many times a second, so
an arrow that came at once would appear and go again through a burst of steps.
The row therefore arms a timeout and reveals the arrow when the rate settles.
A reveal armed that way outlives the binding that armed it, because the
expander hides this row without loading it when the next input carries no
video. The reveal must therefore read the page again when it runs, and both
ends of it must leave no source id behind.

This harness builds no real GTK widget. It drives the real row and expander
methods on duck-typed stand-ins, the same pattern scenario_editor_reconnect
uses. The main loop is a stand-in too, so a delay passes on demand.
"""

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import os
import sys

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

# The row reads GLib out of its own module at call time, so a stand-in put
# here reaches every method the checks below borrow.
ROW_MODULE = sys.modules[VideoFpsRow.__module__]


class FakeGLib:
    """The main loop the row arms its reveal on.

    It holds each armed timeout until a check asks for the delay to pass, so
    the reveal runs at a point the check chooses. It also refuses a removal of
    a source that is not armed, which is the shape of a bug GLib answers with a
    warning and a dead timer.
    """

    SOURCE_REMOVE = False
    SOURCE_CONTINUE = True

    def __init__(self) -> None:
        self.armed: dict[int, tuple[int, object]] = {}
        self.fires = 0
        self.idles: list[object] = []
        self._next = 100

    def timeout_add(self, delay_ms, callback, *args):
        source_id = self._next
        self._next += 1
        self.armed[source_id] = (delay_ms, callback)
        return source_id

    def idle_add(self, callback, *args):
        """Run the callback at once, and keep it for a check to read.

        The image row hands work to the main loop this way. Nothing in this
        file reaches that yet, so this stands in front of the AttributeError a
        check that does would otherwise die on.
        """
        source_id = self._next
        self._next += 1
        self.idles.append(callback)
        callback(*args)
        return source_id

    def source_remove(self, source_id):
        if source_id not in self.armed:
            raise AssertionError(
                f"source_remove({source_id!r}) hit a source that is not armed; "
                f"armed: {sorted(self.armed)}")
        del self.armed[source_id]

    def elapse(self) -> None:
        """Let every armed delay pass, as the main loop would."""
        for source_id, (_delay, callback) in list(self.armed.items()):
            del self.armed[source_id]
            self.fires += 1
            callback()

    def only_delay(self) -> int:
        """The delay of the single armed timeout."""
        delays = [delay for delay, _callback in self.armed.values()]
        if len(delays) != 1:
            raise AssertionError(f"expected one armed timeout, got {len(delays)}")
        return delays[0]


def install_glib() -> FakeGLib:
    """Give the row a fresh main loop stand-in and hand it to the check."""
    glib = FakeGLib()
    ROW_MODULE.GLib = glib
    return glib


install_glib()


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
    """The revert arrow, which build() starts hidden."""

    def __init__(self) -> None:
        self.visible = False

    def set_visible(self, value):
        self.visible = value

    def get_visible(self):
        return self.visible


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
    REVEAL_DELAY_MS = VideoFpsRow.REVEAL_DELAY_MS
    load_for_identifier = VideoFpsRow.load_for_identifier
    on_change = VideoFpsRow.on_change
    on_revert = VideoFpsRow.on_revert
    cancel_reveal = VideoFpsRow.cancel_reveal
    connect_signals = VideoFpsRow.connect_signals
    disconnect_signals = VideoFpsRow.disconnect_signals
    _reveal_revert = VideoFpsRow._reveal_revert
    _request_revert = VideoFpsRow._request_revert
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
        self._reveal_source = None
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
    glib = install_glib()
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
    glib.elapse()
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


def check_the_arrow_waits_for_the_delay() -> int:
    """An edit must not put the arrow on screen in the same frame.

    The arrow shares a linked box with the spinner, so it moves the spinner
    sideways the moment it appears. It waits instead, and arrives once the
    rate has held.
    """
    glib = install_glib()
    page = FakePage(media_path=media_file("clip.gif"), native=9.6)
    install(page)
    row = FakeFpsRow()
    row.load_for_identifier(KEY, 0)

    row.spinner.set_value(6)
    row.spinner.fire()
    if row.revert_button.visible is not False:
        print("FAIL(delay): the revert arrow appeared in the same frame as the "
              "edit; it must wait, or a spin through the range flashes it")
        return 1
    if glib.only_delay() != FakeFpsRow.REVEAL_DELAY_MS:
        print(f"FAIL(delay): the reveal was armed for {glib.only_delay()!r} ms, "
              f"not the row's {FakeFpsRow.REVEAL_DELAY_MS} ms")
        return 1
    glib.elapse()
    if row.revert_button.visible is not True:
        print("FAIL(delay): the revert arrow never arrived once the delay "
              "passed")
        return 1
    print("PASS: the revert arrow arrives a delay after the edit, not with it")
    return 0


def check_the_delay_outlasts_a_burst_of_steps() -> int:
    """The delay is a number the flash depends on, so it takes a floor.

    A held spinner arrow repeats about every 50 ms, and a hand that flicks the
    scroll wheel back reaches the top of the range inside about 150 ms. A
    shorter delay reveals the arrow inside such a pass, which is the flash.
    A much longer one stops reading as the answer to the edit at all.
    """
    if not 150 <= VideoFpsRow.REVEAL_DELAY_MS <= 500:
        print(f"FAIL(delay-value): the reveal delay is "
              f"{VideoFpsRow.REVEAL_DELAY_MS} ms; under 150 ms it fires inside "
              f"a burst of spinner steps and flashes the arrow, over 500 ms it "
              f"stops reading as the answer to the edit")
        return 1
    print(f"PASS: the reveal delay of {VideoFpsRow.REVEAL_DELAY_MS} ms outlasts "
          f"a burst of spinner steps")
    return 0


def check_a_quick_pass_reveals_nothing() -> int:
    """A rate that comes and goes inside the delay must show no arrow at all.

    This is the flash: a spin off the ceiling and back, which every pass down
    the range starts with.
    """
    glib = install_glib()
    page = FakePage(media_path=media_file("clip.gif"), native=9.6)
    install(page)
    row = FakeFpsRow()
    row.load_for_identifier(KEY, 0)

    for value in (29, 28, 27, 28, 29, FakeFpsRow.MAX_FPS):
        row.spinner.set_value(value)
        # Each step drops the reveal the step before it armed. A step that
        # drops some other source leaves its own reveal running, and the main
        # loop answers a stale id with a warning and nothing else.
        try:
            row.spinner.fire()
        except AssertionError as e:
            print(f"FAIL(flash): a spinner step removed the wrong source: {e}")
            return 1
        if row.revert_button.visible is not False:
            print(f"FAIL(flash): the revert arrow appeared at {value} fps, "
                  f"partway through a pass that ends back at the ceiling")
            return 1
    glib.elapse()
    if glib.fires:
        print(f"FAIL(flash): {glib.fires} reveal(s) ran after a pass that ended "
              f"back at the ceiling; the pass left nothing to revert")
        return 1
    if row.revert_button.visible is not False:
        print("FAIL(flash): the arrow is on screen after a pass that ended back "
              "at the ceiling")
        return 1
    print("PASS: a pass down the range and back reveals no arrow")
    return 0


def hidden_row_after_an_edit(rate_survives):
    """Arm a reveal, then hide the row the way the expander hides it.

    The expander hides this row when the input it shows carries no video, and
    it does not load the row for that input, so the row stays bound to the one
    before and the reveal armed for it stays armed. rate_survives says whether
    that binding still carries a rate by the time the reveal runs.
    """
    glib = install_glib()
    page = FakePage(media_path=media_file("clip.gif"), native=9.6)
    install(page)
    row = FakeFpsRow()
    expander = FakeExpander(row, FakeLoopRow())
    expander.active_identifier = KEY
    expander.active_state = 0
    row.load_for_identifier(KEY, 0)

    row.spinner.set_value(6)
    row.spinner.fire()

    # The media goes, so the expander hides the row with no load behind it.
    page.media_path = None
    expander.update_video_rows()
    if not rate_survives:
        page.media_fps = None
    return glib, row


def check_a_hidden_row_reveals_for_its_own_binding() -> int:
    """A reveal must read the page when it runs, not trust the edit that
    armed it.

    A hide leaves the row bound to the input before it, and the rate that
    input carries can be gone by the time the reveal runs. An arrow put up
    blind then states a rate that is not there.
    """
    glib, row = hidden_row_after_an_edit(rate_survives=False)
    if row.visible is not False:
        print("FAIL(hidden): the row stayed on screen with no video to rate")
        return 1
    if not glib.armed:
        print("FAIL(hidden): the hide dropped the reveal; this check needs it "
              "to survive, because surviving is the state it must handle")
        return 1
    glib.elapse()
    if row.revert_button.visible is not False:
        print("FAIL(hidden): the reveal put up the arrow for an input that "
              "carries no rate; it trusted the edit that armed it instead of "
              "reading the page")
        return 1

    glib, row = hidden_row_after_an_edit(rate_survives=True)
    glib.elapse()
    if row.revert_button.visible is not True:
        print("FAIL(hidden): the reveal dropped an arrow the input still "
              "earns")
        return 1
    if glib.fires != 1 or glib.armed:
        print(f"FAIL(hidden): {glib.fires} reveal(s) ran and "
              f"{len(glib.armed)} stayed armed; one must run and none stay")
        return 1
    print("PASS: a reveal on a hidden row states what its input carries now")
    return 0


def check_a_spent_or_cancelled_reveal_leaves_no_id() -> int:
    """Both ends of a reveal must clear the source id.

    A kept id makes the next cancel remove a source the main loop has already
    dropped. The loop answers that with a warning and nothing else, so the
    reveal that cancel was meant to drop keeps running.
    """
    glib = install_glib()
    page = FakePage(media_path=media_file("clip.gif"), native=9.6)
    install(page)
    row = FakeFpsRow()
    row.load_for_identifier(KEY, 0)
    row.spinner.set_value(6)
    row.spinner.fire()
    row.load_for_identifier(KEY, 0)
    try:
        row.load_for_identifier(KEY, 1)
    except AssertionError as e:
        print(f"FAIL(stale-id): a cancel kept the id of a source it had "
              f"already dropped: {e}")
        return 1

    glib = install_glib()
    page = FakePage(media_path=media_file("clip.gif"), native=9.6)
    install(page)
    row = FakeFpsRow()
    row.load_for_identifier(KEY, 0)
    row.spinner.set_value(6)
    row.spinner.fire()
    glib.elapse()
    try:
        row.load_for_identifier(KEY, 0)
    except AssertionError as e:
        print(f"FAIL(stale-id): a reveal that ran kept its id: {e}")
        return 1
    print("PASS: a cancelled and a spent reveal both leave no id behind")
    return 0


def check_a_load_drops_a_pending_reveal() -> int:
    """A reveal armed for one input must not land on the next one.

    A load binds the row to another input, and the row may be showing that
    one's rate, which needs no arrow at all.
    """
    glib = install_glib()
    page = FakePage(media_path=media_file("clip.gif"), native=9.6)
    install(page)
    row = FakeFpsRow()
    row.load_for_identifier(KEY, 0)

    row.spinner.set_value(6)
    row.spinner.fire()
    page.media_fps = None
    row.load_for_identifier(KEY, 1)
    if glib.armed:
        print(f"FAIL(rebind): {len(glib.armed)} reveal(s) armed for the input "
              f"before survived the load")
        return 1
    glib.elapse()
    if row.revert_button.visible is not False:
        print("FAIL(rebind): the arrow is on screen for an input whose page "
              "carries no rate")
        return 1
    print("PASS: a load drops a reveal armed for the input before it")
    return 0


def check_a_shown_arrow_is_not_re_armed() -> int:
    """An arrow already on screen must not be taken away and delayed again.

    Every step of an edit that starts from a stored rate would otherwise
    restart the wait and hide the arrow it already earned.
    """
    glib = install_glib()
    page = FakePage(media_path=media_file("clip.gif"), media_fps=12, native=9.6)
    install(page)
    row = FakeFpsRow()
    row.load_for_identifier(KEY, 0)
    if row.revert_button.visible is not True:
        print("FAIL(shown): a stored rate must show the arrow on load")
        return 1

    row.spinner.set_value(11)
    row.spinner.fire()
    if row.revert_button.visible is not True:
        print("FAIL(shown): a further edit took the arrow away again")
        return 1
    if glib.armed:
        print(f"FAIL(shown): {len(glib.armed)} reveal(s) armed for an arrow "
              f"that is already on screen")
        return 1
    print("PASS: an arrow already on screen stays through further edits")
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
    rc |= check_the_arrow_waits_for_the_delay()
    rc |= check_the_delay_outlasts_a_burst_of_steps()
    rc |= check_a_quick_pass_reveals_nothing()
    rc |= check_a_hidden_row_reveals_for_its_own_binding()
    rc |= check_a_spent_or_cancelled_reveal_leaves_no_id()
    rc |= check_a_load_drops_a_pending_reveal()
    rc |= check_a_shown_arrow_is_not_re_armed()
    rc |= check_top_of_range_stores_no_rate()
    rc |= check_stored_top_reads_as_no_rate()
    rc |= check_revert_leaves_the_row_wired()
    rc |= check_touchscreen_uses_the_background_seam()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
