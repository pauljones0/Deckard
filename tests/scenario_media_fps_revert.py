"""Reverting the media frame rate must clear the page's fps key.

An explicit rate lives under media/fps and reaches playing media at once. A
revert removes the key instead of storing the current default, so the page
reads as one the rate never reached, and a default that later changes still
reaches it. The sidebar then shows the rate the media itself runs at, which
the pipeline that decoded it reports.
"""

# Timers stay disarmed, so every write here is one a check asks for by name.
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import copy
import json
import threading

from PIL import Image

from fixtures import make_headless_controller, start_watchdog, teardown

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo
from src.backend.PageManagement import page_flush

IDENT = Input.Key("0x0")

# The number a page with no fps key loads under: the media loop's own tick
# ceiling, which caps nothing.
UNCAPPED = 30


class NoTimers:
    """A timer source that arms nothing, so a line in the test fixes the
    moment a page reaches its file."""

    def schedule(self, delay_s, callback):
        return object()

    def cancel(self, handle):
        pass


def fresh_flush() -> None:
    """A flush seam that writes only when told, installed process-wide."""
    page_flush._flush = page_flush.PageFlush(scheduler=NoTimers())


class FakeVideo:
    """Playing media, as the page seam sees it.

    It records every cap the seam pushes, and reports a native rate. It is
    well-behaved for the controller's live media loop, which composites
    active-state videos while the scenario runs.
    """

    def __init__(self, native=None):
        self.loop = True
        self.calls = []
        self._native = native
        self._frame = Image.new("RGBA", (72, 72), (0, 0, 0, 255))

    def set_playback(self, fps=None, loop=None):
        self.calls.append(fps)

    def native_fps(self):
        return self._native

    def get_raw_image(self):
        return self._frame

    def get_next_frame(self, *a, **k):
        return self._frame

    def close(self):
        pass


class LegacyVideo:
    """Media that answers no native rate at all, as a plugin's own object
    can. The page must read past it instead of raising."""

    def __init__(self):
        self.loop = True
        self._frame = Image.new("RGBA", (72, 72), (0, 0, 0, 255))

    def set_playback(self, fps=None, loop=None):
        pass

    def get_raw_image(self):
        return self._frame

    def get_next_frame(self, *a, **k):
        return self._frame

    def close(self):
        pass


class StubKeyVideoCache:
    """The KeyVideoCache surface InputVideo.native_fps reads."""

    def __init__(self, source_fps):
        self.n_frames = 10
        self._source_fps = source_fps

    def get_source_fps(self):
        return self._source_fps


def section(content: dict, name: str) -> dict:
    """One section of key 0x0 state 0, or {} while the page carries none. A
    seeded page starts with no key entry at all, which is the state a revert
    must leave the file in."""
    state = content.get("keys", {}).get("0x0", {}).get("states", {}).get("0", {})
    return state.get(name, {})


def media_dict(page) -> dict:
    """The media section the page holds for key 0x0 state 0, in memory."""
    return section(page.dict, "media")


def media_on_disk(page) -> dict:
    """The media section the page's file holds, after a write it asks for."""
    page_flush.get().flush_path(page.json_path)
    with open(page.json_path) as f:
        content = json.load(f)
    return section(content, "media")


def attach(controller, video):
    """Put video on key 0x0 state 0, where the page seam looks for it."""
    controller.get_input(IDENT).states[0].key_video = video
    return video


def check_absent_key_reads_uncapped(page) -> int:
    if page.has_media_fps(IDENT, 0):
        print("FAIL(default): a freshly seeded page already claims an explicit "
              "media frame rate")
        return 1
    if page.get_media_fps(IDENT, 0) != UNCAPPED:
        print(f"FAIL(default): a page with no fps key must read as uncapped "
              f"({UNCAPPED}), got {page.get_media_fps(IDENT, 0)}")
        return 1
    print("PASS: a page with no fps key reads as uncapped")
    return 0


def check_set_then_revert_round_trip(page) -> int:
    """A rate persists; a revert removes the key from memory and from the
    file, and leaves the rest of the media section exactly as it was."""
    # Give the section real siblings first. Without them a revert that wiped
    # the whole media section would look identical to one that removed the
    # single key, and the check below would prove nothing.
    page.set_media_path(IDENT, 0, "/nonexistent/clip.gif", update=False)
    page.set_media_size(IDENT, 0, 0.75, update=False)
    page.set_media_halign(IDENT, 0, -0.5, update=False)
    before = copy.deepcopy(media_dict(page))
    if set(before) < {"path", "size", "halign"}:
        print(f"FAIL(setup): the media section holds {sorted(before)}, so a "
              f"revert that destroys siblings would go unnoticed")
        return 1

    page.set_media_fps(IDENT, 0, 12, update=False)
    if not page.has_media_fps(IDENT, 0) or page.get_media_fps(IDENT, 0) != 12:
        print(f"FAIL(set): 12 did not stick: has="
              f"{page.has_media_fps(IDENT, 0)} get={page.get_media_fps(IDENT, 0)}")
        return 1
    if media_dict(page).get("fps") != 12:
        print(f"FAIL(set): the page holds {media_dict(page).get('fps')!r} "
              f"under media/fps, not 12")
        return 1
    if media_on_disk(page).get("fps") != 12:
        print(f"FAIL(set): the file holds "
              f"{media_on_disk(page).get('fps')!r} under media/fps, not 12")
        return 1

    page.set_media_fps(IDENT, 0, None, update=False)
    if page.has_media_fps(IDENT, 0):
        print("FAIL(revert): the page still claims an explicit frame rate")
        return 1
    if page.get_media_fps(IDENT, 0) != UNCAPPED:
        print(f"FAIL(revert): a reverted page must read as uncapped "
              f"({UNCAPPED}), got {page.get_media_fps(IDENT, 0)}")
        return 1
    if "fps" in media_dict(page):
        print(f"FAIL(revert): the page kept media/fps="
              f"{media_dict(page)['fps']!r} -- a revert must REMOVE the key, "
              f"not store the current default, or a later default change "
              f"never reaches this page")
        return 1
    on_disk = media_on_disk(page)
    if "fps" in on_disk:
        print(f"FAIL(revert): the file kept media/fps={on_disk['fps']!r} after "
              f"a revert")
        return 1

    after = {k: v for k, v in media_dict(page).items() if k != "fps"}
    expected = {k: v for k, v in before.items() if k != "fps"}
    if after != expected:
        print(f"FAIL(revert): the revert changed the rest of the media "
              f"section: {expected} became {after} -- clearing one key must "
              f"leave every sibling in place")
        return 1
    if {k: v for k, v in on_disk.items() if k != "fps"} != expected:
        print(f"FAIL(revert): the file's media section became "
              f"{on_disk} against an expected {expected}")
        return 1
    print("PASS: a rate round-trips through the file, and a revert removes "
          "only its own key")
    return 0


def check_revert_reaches_playing_media(controller) -> int:
    """A cap and a revert must both reach media already on the deck, so
    neither waits for a page reload."""
    page = controller.active_page
    video = attach(controller, FakeVideo())

    page.set_media_fps(IDENT, 0, 12, update=False)
    if video.calls != [12]:
        print(f"FAIL(live): setting 12 pushed {video.calls} into playing media")
        return 1

    page.set_media_fps(IDENT, 0, None, update=False)
    if video.calls != [12, UNCAPPED]:
        print(f"FAIL(live): a revert pushed {video.calls} into playing media -- "
              f"it must push the value a fresh page load would give it "
              f"({UNCAPPED}), or the media keeps the dropped cap until a reload")
        return 1
    print("PASS: a cap and a revert both reach media already on the deck")
    return 0


def check_native_rate_read_back(controller) -> int:
    """The rate the sidebar shows after a revert comes from the pipeline."""
    page = controller.active_page

    attach(controller, FakeVideo(native=12.5))
    rate = page.get_media_native_fps(IDENT, 0)
    if rate != 12.5:
        print(f"FAIL(native): the page reported {rate!r} for media running at "
              f"12.5 fps")
        return 1

    for unusable in (None, 0, 0.0):
        attach(controller, FakeVideo(native=unusable))
        rate = page.get_media_native_fps(IDENT, 0)
        if rate is not None:
            print(f"FAIL(native): media reporting {unusable!r} gave the page "
                  f"{rate!r}, not None")
            return 1

    attach(controller, LegacyVideo())
    rate = page.get_media_native_fps(IDENT, 0)
    if rate is not None:
        print(f"FAIL(native): media with no native rate at all gave the page "
              f"{rate!r}, not None")
        return 1

    attach(controller, None)
    rate = page.get_media_native_fps(IDENT, 0)
    if rate is not None:
        print(f"FAIL(native): a state holding no media gave the page {rate!r}, "
              f"not None")
        return 1
    print("PASS: the page reports the pipeline's rate, and None when there is none")
    return 0


def check_video_side_unchanged() -> int:
    """A video's own frame-rate behaviour must not move.

    native_fps reads the container's rate through the tile cache, and
    set_playback still rebases the timebase when fps is the playback rate.
    """
    video = InputVideo.__new__(InputVideo)
    video.fps = 10
    video.loop = True
    video.natural_speed = False  # key and dial semantics: fps IS the rate
    video.active_frame = 4
    video._play_start = 1_000_000.0
    video._last_frame_tick = None
    video._close_lock = threading.Lock()
    video.video_cache = StubKeyVideoCache(15.0)

    if video.native_fps() != 15.0:
        print(f"FAIL(video): a 15 fps source reported {video.native_fps()!r}")
        return 1

    before = video._play_start
    video.set_playback(fps=20, loop=True)
    if video.fps != 20 or video._play_start == before:
        print(f"FAIL(video): set_playback no longer rebases the timebase when "
              f"fps is the playback rate: fps={video.fps} "
              f"play_start moved={video._play_start != before}")
        return 1

    video.video_cache = StubKeyVideoCache(None)
    if video.native_fps() is not None:
        print("FAIL(video): a container with no usable rate must report None")
        return 1

    video.video_cache = None
    if video.native_fps() is not None:
        print("FAIL(video): a released reader must report None, not a rate it "
              "can no longer read")
        return 1
    print("PASS: a video's frame-rate behaviour is unchanged")
    return 0


def check_clear_is_safe_on_odd_pages(page) -> int:
    """A revert on a page that never carried the key must change nothing."""
    # Set and clear once, so the branch below exists whatever ran before.
    page.set_media_fps(IDENT, 0, 15, update=False)
    page.set_media_fps(IDENT, 0, None, update=False)
    states = page.dict["keys"]["0x0"]["states"]

    # A second revert, on a media section that exists and carries siblings but
    # no rate of its own. Nothing is there to remove, so the setter must stop
    # before the removal rather than reach for a key that is not there.
    siblings = dict(media_dict(page))
    try:
        page.set_media_fps(IDENT, 0, None, update=False)
    except Exception as e:
        print(f"FAIL(odd): reverting a rate the page never carried raised "
              f"{e!r}")
        return 1
    if dict(media_dict(page)) != siblings:
        print(f"FAIL(odd): reverting a rate the page never carried changed the "
              f"media section: {siblings} became {dict(media_dict(page))}")
        return 1

    page.set_media_fps(IDENT, 7, None, update=False)
    if "7" in states:
        print("FAIL(odd): reverting a state the page does not carry created "
              "the branch")
        return 1

    # A media section that is not a mapping, as a hand-edited page can hold.
    # The text carries the key's own name, because a walk with no type check
    # finds "fps" inside the string, reads that as the key being present, and
    # reaches a delete the string cannot take.
    saved = states["0"].get("media")
    states["0"]["media"] = "fps was here"
    try:
        page.set_media_fps(IDENT, 0, None, update=False)
    except Exception as e:
        print(f"FAIL(odd): reverting past a malformed media section raised "
              f"{e!r} -- the walk must stop at a parent that is not a mapping")
        return 1
    if states["0"]["media"] != "fps was here":
        print("FAIL(odd): reverting past a malformed media section rewrote it")
        return 1
    if saved is None:
        del states["0"]["media"]
    else:
        states["0"]["media"] = saved
    print("PASS: a revert on a page that never carried the key changes nothing")
    return 0


def check_background_rate_reverts_too(page) -> int:
    """The same row edits a touchscreen background, and its revert must clear
    that key the same way."""
    page.set_background_fps(IDENT, 0, 10, update=False)
    if not page.has_background_fps(IDENT, 0) or page.get_background_fps(IDENT, 0) != 10:
        print("FAIL(background): 10 did not stick")
        return 1

    page.set_background_fps(IDENT, 0, None, update=False)
    if page.has_background_fps(IDENT, 0):
        print("FAIL(background): the page still claims an explicit frame rate")
        return 1
    if page.get_background_fps(IDENT, 0) != UNCAPPED:
        print(f"FAIL(background): a reverted background must read as uncapped "
              f"({UNCAPPED}), got {page.get_background_fps(IDENT, 0)}")
        return 1
    background = section(page.dict, "background")
    if "fps" in background:
        print(f"FAIL(background): the page kept background/fps="
              f"{background['fps']!r} after a revert")
        return 1
    print("PASS: a background rate reverts the same way")
    return 0


def main() -> int:
    start_watchdog(60, "media_fps_revert")
    fresh_flush()
    controller = make_headless_controller(serial="fps-revert",
                                          page_name="FpsRevert")
    try:
        page = controller.active_page
        rc = check_absent_key_reads_uncapped(page)
        rc |= check_set_then_revert_round_trip(page)
        rc |= check_revert_reaches_playing_media(controller)
        rc |= check_native_rate_read_back(controller)
        rc |= check_video_side_unchanged()
        rc |= check_clear_is_safe_on_odd_pages(page)
        rc |= check_background_rate_reverts_too(page)
    finally:
        teardown(controller)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
