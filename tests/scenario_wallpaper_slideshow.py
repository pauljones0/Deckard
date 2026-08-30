"""Verify order, timing, settings round-trip, indexed rendering, and page-stop behavior.
Keep single-image loading and video/slideshow exclusivity."""
import fixtures  # noqa: F401  (must be first: rewrites argv to a temp data dir)

import os  # noqa: E402
import random  # noqa: E402

import globals as gl  # noqa: E402

from src.backend.DeckManagement.deck_controller.slideshow import (  # noqa: E402
    IN_ORDER,
    SHUFFLE,
    Slideshow,
)

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


# The model, driven with controlled times

def check_single_image_never_advances() -> None:
    """One image is a static background. The interval means nothing without a
    second image to move to, so a one-element list never rotates."""
    show = Slideshow(["only.png"], interval=1, order=IN_ORDER)
    show.seed(0.0)
    check("single-image current path", show.current_path() == "only.png")
    check("single-image index stays 0", show.index == 0)
    check("single-image is never due", show.due(1000.0) is False)
    check("single-image never advances", show.maybe_advance(1000.0) is None)
    check("single-image still on its image after a tick", show.current_path() == "only.png")


def check_index_advances_and_wraps() -> None:
    """In-order walks the list and wraps at the end. The interval gates each
    step, and the current path always matches the current index."""
    paths = ["a.png", "b.png", "c.png"]
    show = Slideshow(paths, interval=10, order=IN_ORDER)
    show.seed(0.0)

    check("starts on the first image", show.current_path() == "a.png" and show.index == 0)
    check("not due before the interval", show.maybe_advance(9.9) is None)
    check("still the first image", show.current_path() == "a.png")

    check("advances at the interval", show.maybe_advance(10.0) == "b.png")
    check("index moved to 1", show.index == 1)
    check("path matches index after step 1", show.current_path() == paths[show.index])

    check("not due again before the next interval", show.maybe_advance(15.0) is None)
    check("advances to the third", show.maybe_advance(20.0) == "c.png" and show.index == 2)

    # The wrap: past the end returns to the first image.
    check("wraps to the first image", show.maybe_advance(30.0) == "a.png")
    check("index wrapped to 0", show.index == 0)


def check_zero_interval_holds() -> None:
    """A non-positive interval holds the first image rather than flickering."""
    show = Slideshow(["a.png", "b.png"], interval=0, order=IN_ORDER)
    show.seed(0.0)
    check("zero interval is never due", show.due(1_000_000.0) is False)
    check("zero interval never advances", show.maybe_advance(1_000_000.0) is None)


def check_shuffle_is_a_permutation_without_repeats() -> None:
    """Shuffle walks a permutation of every image, and a wrap reshuffles
    without replaying the image the last cycle ended on."""
    paths = ["a.png", "b.png", "c.png", "d.png"]
    show = Slideshow(paths, interval=5, order=SHUFFLE, rng=random.Random(7))

    seen = [show.index]
    for step in range(len(paths) - 1):
        show.advance(now=float(step + 1))
        seen.append(show.index)
    check("one cycle visits every image once", sorted(seen) == [0, 1, 2, 3], f"saw {seen}")

    # Seed 4 makes a naive two-image reshuffle repeat the prior cycle's last image.
    # The no-repeat guard must reorder that wrap.
    two = Slideshow(["a.png", "b.png"], interval=5, order=SHUFFLE, rng=random.Random(4))
    walk = [two.index]
    for step in range(2):
        two.advance(now=float(step + 1))
        walk.append(two.index)
    check("a two-image shuffle wrap does not repeat the last image (seed 4)",
          walk[1] != walk[2], f"walk {walk}")


# The render path, through a real headless controller

def _png(name: str, color) -> str:
    return fixtures.make_test_png(os.path.join(gl.DATA_PATH, "assets", name), color=color)


def _mp4(name: str) -> str:
    return fixtures.make_test_mp4(os.path.join(gl.DATA_PATH, "assets", name))


def check_list_round_trips_through_the_settings_seam(serial: str) -> None:
    """The list, the interval and the order persist and read back through the
    deck-settings store, never a bare file write."""
    p1, p2 = _png("rt1.png", (10, 20, 30)), _png("rt2.png", (30, 20, 10))
    settings = gl.settings_manager.deck(serial)
    settings.set("background", "media-paths", [p1, p2])
    settings.set("background", "slideshow-interval", 7)
    settings.set("background", "slideshow-order", "shuffle")
    settings.save()

    reread = gl.settings_manager.deck(serial).section("background")
    check("the list round-trips", reread["media-paths"] == [p1, p2], f"got {reread['media-paths']}")
    check("the interval round-trips", reread["slideshow-interval"] == 7)
    check("the order round-trips", reread["slideshow-order"] == "shuffle")
    # Sparse: only the keys set were written, and the store filled the rest.
    on_disk = gl.settings_manager.get_deck_settings(serial)["background"]
    check("only chosen keys were persisted",
          set(on_disk) == {"media-paths", "slideshow-interval", "slideshow-order"},
          f"persisted {sorted(on_disk)}")


def _load_deck_background(controller, serial: str, background: dict) -> None:
    gl.settings_manager.save_deck_settings(serial, {"background": background})
    controller.load_background(controller.active_page, update=False)


def check_render_path_gets_the_current_index(controller, serial: str) -> None:
    """Let the media thread advance and wrap the render image on a short interval.
    The pure model checks exact index and interval arithmetic separately."""
    p1 = _png("idx1.png", (200, 10, 10))
    p2 = _png("idx2.png", (10, 200, 10))
    _load_deck_background(controller, serial, {
        "enable": True, "media-paths": [p1, p2], "slideshow-interval": 0.3,
    })

    background = controller.background
    check("a two-image list installs a slideshow", background.slideshow is not None)
    if background.slideshow is None:
        return
    check("no video runs beside the slideshow", background.video is None)
    check("the first frame is on the render path",
          background.image is not None and background.image.path == p1)

    # The media thread ticks the rotation on its own clock. Watch the render
    # image walk to the second still and then wrap back to the first.
    advanced = fixtures.wait_until(
        lambda: background.image is not None and background.image.path == p2, timeout=10)
    check("the render path advances to the second image", advanced,
          f"path {getattr(background.image, 'path', None)}")
    wrapped = fixtures.wait_until(
        lambda: background.image is not None and background.image.path == p1, timeout=10)
    check("the render path wraps back to the first image", wrapped)


def check_page_switch_cancels_the_rotation(controller, serial: str) -> None:
    """A background reload onto a page without a slideshow drops the rotation.
    Nothing advances afterward, and no rotation is left armed."""
    p1 = _png("sw1.png", (5, 5, 200))
    p2 = _png("sw2.png", (200, 5, 5))
    _load_deck_background(controller, serial, {
        "enable": True, "media-paths": [p1, p2], "slideshow-interval": 10,
    })
    check("the slideshow is armed before the switch", controller.background.slideshow is not None)

    # Reload with the background turned off: the equivalent of switching to a
    # page that shows no background.
    _load_deck_background(controller, serial, {"enable": False})
    check("the switch cancelled the rotation", controller.background.slideshow is None)
    check("no rotation is left armed", controller.background.slideshow is None)
    check("a tick after the switch swaps nothing",
          controller.background.slideshow_tick(now=1_000_000.0) is False)


def check_single_image_background_still_loads(controller, serial: str) -> None:
    """A background of one media-path, the shape from before the slideshow,
    loads as a single still and arms no rotation."""
    p1 = _png("single.png", (120, 120, 10))
    _load_deck_background(controller, serial, {"enable": True, "media-path": p1})
    check("a single media-path loads an image",
          controller.background.image is not None and controller.background.image.path == p1)
    check("a single image arms no slideshow", controller.background.slideshow is None)
    check("a single image runs no video", controller.background.video is None)


def check_video_and_slideshow_are_exclusive(controller, serial: str) -> None:
    """A video and a slideshow never both show. Setting one clears the other,
    and a config carrying both resolves to the slideshow."""
    p1 = _png("ex1.png", (0, 100, 100))
    p2 = _png("ex2.png", (100, 0, 100))
    video = _mp4("ex.mp4")

    # A video, then a slideshow: the slideshow wins and the video is gone.
    _load_deck_background(controller, serial, {"enable": True, "media-path": video})
    check("the video background loaded", controller.background.video is not None)
    controller.background.set_slideshow([p1, p2], interval=10, update=False)
    check("the slideshow cleared the video", controller.background.video is None)
    check("the slideshow is now set", controller.background.slideshow is not None)

    # A slideshow, then a video: the video wins and the rotation is dropped.
    controller.background.set_from_path(video, update=False, loop=False, fps=30)
    check("a video after a slideshow cleared the rotation", controller.background.slideshow is None)
    check("the video is set", controller.background.video is not None)

    # A config that carries both a video media-path and an image list: the
    # slideshow wins.
    _load_deck_background(controller, serial, {
        "enable": True, "media-path": video, "media-paths": [p1, p2], "slideshow-interval": 10,
    })
    check("a config with both resolves to the slideshow",
          controller.background.slideshow is not None and controller.background.video is None)


def check_single_image_set_clears_the_rotation(controller, serial: str) -> None:
    """Setting a single image over a running slideshow ends the rotation, so a
    stale show cannot keep swapping its next image over the new single one."""
    p1 = _png("f2a.png", (30, 30, 200))
    p2 = _png("f2b.png", (200, 30, 30))
    single = _png("f2single.png", (30, 200, 30))
    controller.background.set_slideshow([p1, p2], interval=10, update=False)
    check("the slideshow is armed before the single-image set",
          controller.background.slideshow is not None)
    controller.background.set_from_path(single, update=False, loop=False, fps=30)
    check("a single-image set clears the slideshow", controller.background.slideshow is None)
    check("the single image is on the render path",
          controller.background.image is not None and controller.background.image.path == single)


def check_moved_page_does_not_advance(controller, serial: str) -> None:
    """Refuse rotation after active_page changes but before background reload.
    This closes the worker-reload race with an old slideshow still installed."""
    p1 = _png("mv1.png", (10, 90, 10))
    p2 = _png("mv2.png", (90, 10, 10))
    _load_deck_background(controller, serial, {
        "enable": True, "media-paths": [p1, p2], "slideshow-interval": 10,
    })
    show = controller.background.slideshow
    check("the rotation records the page it loaded for",
          show is not None and show.page is controller.active_page)
    if show is None:
        return

    original_page = controller.active_page
    before_index = show.index
    before_path = controller.background.image.path
    # Flip active_page while the old rotation remains installed.
    # An elapsed interval ensures only the page guard stops the advance.
    controller.active_page = object()
    try:
        show.seed(0.0)
        swapped = controller.background.slideshow_tick(now=1_000_000.0)
        check("a tick with a moved active_page does not swap", swapped is False)
        check("the index did not advance", controller.background.slideshow.index == before_index)
        check("the render image did not change",
              controller.background.image is not None and controller.background.image.path == before_path)
    finally:
        controller.active_page = original_page


def check_corrupt_frame_is_discarded_cleanly(controller) -> None:
    """A frame that exists but does not decode is discarded, returning False,
    rather than raising into the media loop's per-tick guard."""
    corrupt = os.path.join(gl.DATA_PATH, "assets", "corrupt.png")
    with open(corrupt, "wb") as handle:
        handle.write(b"this is not a decodable image\x00\x01\x02\x03")
    try:
        result = controller.background._install_slideshow_frame(corrupt, update=False, keep=True)
        check("a corrupt frame is discarded, not raised", result is False)
    except Exception as exc:  # noqa: BLE001  (any raise is the finding)
        check("a corrupt frame is discarded, not raised", False, f"raised {type(exc).__name__}")


def main() -> None:
    fixtures.start_watchdog(90, label="scenario_wallpaper_slideshow")

    check_single_image_never_advances()
    check_index_advances_and_wraps()
    check_zero_interval_holds()
    check_shuffle_is_a_permutation_without_repeats()

    serial = "slideshow-1"
    controller = fixtures.make_headless_controller(serial=serial)
    try:
        fixtures.wait_until(lambda: controller.active_page is not None, timeout=5)
        check_list_round_trips_through_the_settings_seam(serial)
        check_render_path_gets_the_current_index(controller, serial)
        check_page_switch_cancels_the_rotation(controller, serial)
        check_single_image_background_still_loads(controller, serial)
        check_video_and_slideshow_are_exclusive(controller, serial)
        check_single_image_set_clears_the_rotation(controller, serial)
        check_moved_page_does_not_advance(controller, serial)
        check_corrupt_frame_is_discarded_cleanly(controller)
    finally:
        fixtures.teardown(controller)

    assert not FAILURES, f"wallpaper slideshow gaps remain: {FAILURES}"
    print("PASS: scenario_wallpaper_slideshow")


if __name__ == "__main__":
    main()
