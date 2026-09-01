"""Verify GIF FPS visibility, explicit-rate-only revert, native-rate restore, and signal continuity.
Delayed reveal re-reads hidden bindings; completion and cancellation clear its source ID."""

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
    """Hold row reveal timeouts until a check advances the main loop.
    Reject unarmed source removal because GLib warns and leaves the intended timer active."""

    SOURCE_REMOVE = False
    SOURCE_CONTINUE = True

    def __init__(self) -> None:
        self.armed: dict[int, tuple[int, object]] = {}
        self.fires = 0
        self.idles: list[object] = []
        self._next_source_id = 100

    def timeout_add(self, delay_ms, callback, *args):
        source_id = self._next_source_id
        self._next_source_id += 1
        self.armed[source_id] = (delay_ms, callback)
        return source_id

    def idle_add(self, callback, *args):
        """Run the callback immediately and retain it for inspection.
        This supports any borrowed image-row path that submits work through idle_add."""
        source_id = self._next_source_id
        self._next_source_id += 1
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


def create_media_file(name: str) -> str:
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
        self._next_handler_id = 1
        self._value = 0.0

    def connect(self, signal, handler):
        hid = self._next_handler_id
        self._next_handler_id += 1
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


class FakeRevertButton:
    """The revert arrow, which build() starts hidden."""

    def __init__(self) -> None:
        self.visible = False

    def set_visible(self, value):
        self.visible = value

    def get_visible(self):
        return self.visible


class FakePage:
    """Provide the page seam that the row reads and writes.
    None means that no rate key exists, which is the required revert state."""

    def __init__(self, media_path=None, background_image=None,
                 media_fps=None, background_fps=None, native_fps=None) -> None:
        self.media_path = media_path
        self.background_image = background_image
        self.media_fps = media_fps
        self.background_fps = background_fps
        self.native_fps = native_fps
        self.writes: list[tuple] = []
        self.native_reads = 0

    def get_media_path(self, identifier, state):
        return self.media_path

    def get_background_image(self, identifier, state):
        return self.background_image

    def has_media_fps_override(self, identifier, state):
        return self.media_fps is not None

    def get_media_fps(self, identifier, state):
        return 30 if self.media_fps is None else self.media_fps

    def get_media_native_fps(self, identifier, state):
        self.native_reads += 1
        return self.native_fps

    def set_media_fps(self, identifier, state, fps, update=True):
        self.media_fps = fps
        self.writes.append(("media", fps))

    def has_background_fps_override(self, identifier, state):
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
    _update_revert_visibility = VideoFpsRow._update_revert_visibility
    _uses_media_fps = VideoFpsRow._uses_media_fps
    _write_fps = VideoFpsRow._write_fps
    _stored_fps = VideoFpsRow._stored_fps
    _has_override = VideoFpsRow._has_override
    _displayed_fps = VideoFpsRow._displayed_fps

    def __init__(self) -> None:
        self.spinner = FakeSpinner()
        self.revert_button = FakeRevertButton()
        self.active_identifier = None
        self.active_state = None
        self._change_handler_id = None
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


def install_page_context(page) -> None:
    window = FakeMainWindow(page)
    services.require_main_window = lambda: window
    # The loaders reach the window through gl.app, and the change handlers
    # through services. Both must answer with the same page.
    gl.app = type("FakeApp", (), {"main_win": window})()


def check_gif_media_shows_the_row() -> int:
    """Show the video row for a key GIF but not a dial GIF.
    Dials cannot load GIF media, so a rate would edit media that never reaches the dial."""
    cases = [
        (KEY, "clip.gif", True, "a GIF on a key"),
        (KEY, "clip.mp4", True, "an mp4 on a key"),
        (KEY, "still.png", False, "a still image on a key"),
        (KEY, None, False, "no media at all on a key"),
        (DIAL, "clip.gif", False, "a GIF on a dial"),
        (DIAL, "clip.mp4", True, "an mp4 on a dial"),
    ]
    for identifier, name, expected, description in cases:
        page = FakePage(media_path=create_media_file(name) if name else None)
        install_page_context(page)
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


def check_revert_hidden_without_override() -> int:
    """The arrow means there is something to revert to."""
    page = FakePage(media_path=create_media_file("clip.gif"), native_fps=9.6)
    install_page_context(page)
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
    for native_fps, expected in ((87.5, 30), (0.4, 1), (None, 30)):
        page = FakePage(media_path=create_media_file("clip.gif"), native_fps=native_fps)
        install_page_context(page)
        row = FakeFpsRow()
        row.load_for_identifier(KEY, 0)
        if row.spinner.get_value() != expected:
            print(f"FAIL(clamp): media running at {native_fps!r} fps put "
                  f"{row.spinner.get_value()!r} in the row; the range is "
                  f"{FakeFpsRow.MIN_FPS} to {FakeFpsRow.MAX_FPS} and an "
                  f"unknown rate falls back to {expected}")
            return 1
    print("PASS: a media rate outside the spinner's range is clamped")
    return 0


def check_change_then_revert_round_trip() -> int:
    """An edit stores a rate; a revert clears it and shows the media's own."""
    glib = install_glib()
    page = FakePage(media_path=create_media_file("clip.gif"), native_fps=9.6)
    install_page_context(page)
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
    print("PASS: an edit stores a rate and a revert clears it back to native FPS")
    return 0


def check_arrow_waits_for_reveal_delay() -> int:
    """Delay the arrow until the edited rate settles.
    Immediate visibility shifts the spinner sideways within the same linked-box frame."""
    glib = install_glib()
    page = FakePage(media_path=create_media_file("clip.gif"), native_fps=9.6)
    install_page_context(page)
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


def check_reveal_delay_exceeds_step_burst() -> int:
    """Keep the reveal delay between 150 and 500 ms.
    It must outlast 50 ms repeats and a 150 ms pass, but stay tied to the edit."""
    if not 150 <= VideoFpsRow.REVEAL_DELAY_MS <= 500:
        print(f"FAIL(delay-value): the reveal delay is "
              f"{VideoFpsRow.REVEAL_DELAY_MS} ms; under 150 ms it fires inside "
              f"a burst of spinner steps and flashes the arrow, over 500 ms it "
              f"stops reading as the answer to the edit")
        return 1
    print(f"PASS: the reveal delay of {VideoFpsRow.REVEAL_DELAY_MS} ms outlasts "
          f"a burst of spinner steps")
    return 0


def check_ceiling_round_trip_cancels_reveal() -> int:
    """Show no arrow when a rate returns to the ceiling within the delay.
    This prevents a flash at the start of a downward range pass."""
    glib = install_glib()
    page = FakePage(media_path=create_media_file("clip.gif"), native_fps=9.6)
    install_page_context(page)
    row = FakeFpsRow()
    row.load_for_identifier(KEY, 0)

    for value in (29, 28, 27, 28, 29, FakeFpsRow.MAX_FPS):
        row.spinner.set_value(value)
        # Each step cancels the prior reveal.
        # A stale ID warns and leaves the current reveal active.
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


def prepare_hidden_row_after_edit(rate_survives):
    """Arm a reveal, then hide the row without rebinding, as for an input with no video.
    rate_survives selects whether the retained binding still has a rate at reveal time."""
    glib = install_glib()
    page = FakePage(media_path=create_media_file("clip.gif"), native_fps=9.6)
    install_page_context(page)
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


def check_hidden_row_reveal_uses_current_binding() -> int:
    """Read the page when a delayed reveal runs instead of trusting the edit that armed it.
    A hidden row keeps its prior binding, whose rate can disappear before the callback."""
    glib, row = prepare_hidden_row_after_edit(rate_survives=False)
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

    glib, row = prepare_hidden_row_after_edit(rate_survives=True)
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


def check_finished_reveal_clears_source_id() -> int:
    """Clear the source ID after both reveal completion and cancellation.
    A retained stale ID makes the next cancel warn while leaving its intended reveal active."""
    glib = install_glib()
    page = FakePage(media_path=create_media_file("clip.gif"), native_fps=9.6)
    install_page_context(page)
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
    page = FakePage(media_path=create_media_file("clip.gif"), native_fps=9.6)
    install_page_context(page)
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


def check_load_cancels_pending_reveal() -> int:
    """Cancel a reveal when loading another input.
    The new binding can have no explicit rate and therefore require no arrow."""
    glib = install_glib()
    page = FakePage(media_path=create_media_file("clip.gif"), native_fps=9.6)
    install_page_context(page)
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


def check_visible_arrow_skips_rearm() -> int:
    """Keep an already visible arrow through further edits.
    Restarting the delay would hide an arrow earned by the stored rate."""
    glib = install_glib()
    page = FakePage(media_path=create_media_file("clip.gif"), media_fps=12, native_fps=9.6)
    install_page_context(page)
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


def check_max_fps_clears_override() -> int:
    """Clear the rate key when the spinner reaches its ceiling.
    The ceiling limits nothing, so storing it would offer a revert for an ineffective override."""
    page = FakePage(media_path=create_media_file("clip.gif"), media_fps=8, native_fps=9.6)
    install_page_context(page)
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


def check_stored_max_fps_reads_as_no_override() -> int:
    """A page written before this rule can carry the ceiling. It limits
    nothing, so the row must treat it as no rate at all."""
    page = FakePage(media_path=create_media_file("clip.gif"),
                    media_fps=FakeFpsRow.MAX_FPS, native_fps=12.0)
    install_page_context(page)
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
    """Reconnect the spinner after a revert writes its value.
    Leaving the handler disconnected silently drops every later edit."""
    page = FakePage(media_path=create_media_file("clip.gif"), media_fps=20, native_fps=10.0)
    install_page_context(page)
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
    page = FakePage(background_image=create_media_file("wall.mp4"), background_fps=15)
    install_page_context(page)
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
    rc |= check_revert_hidden_without_override()
    rc |= check_native_rate_is_clamped_into_range()
    rc |= check_change_then_revert_round_trip()
    rc |= check_arrow_waits_for_reveal_delay()
    rc |= check_reveal_delay_exceeds_step_burst()
    rc |= check_ceiling_round_trip_cancels_reveal()
    rc |= check_hidden_row_reveal_uses_current_binding()
    rc |= check_finished_reveal_clears_source_id()
    rc |= check_load_cancels_pending_reveal()
    rc |= check_visible_arrow_skips_rearm()
    rc |= check_max_fps_clears_override()
    rc |= check_stored_max_fps_reads_as_no_override()
    rc |= check_revert_leaves_the_row_wired()
    rc |= check_touchscreen_uses_the_background_seam()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
