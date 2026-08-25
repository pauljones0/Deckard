"""A key whose foreground hides its whole tile must stop compositing per frame.

The composite of such a key is the same picture whatever the background does,
so the paint path keeps it and reuses it. The pixels must not move: every
check that asserts a skip also asserts that a fresh composite over the very
background the skip ignored returns the same bytes.

The other half is the refusals. A foreground with any transparency, a
foreground smaller than the tile, a press, a new label and a key that plays
its own video each have to composite exactly as before, because each of them
either lets the background through or changes what the key shows per frame.

Nothing here sleeps. The background is advanced by hand, and the GIF timeline
is rebased onto a chosen frame, so a composite count is a function of the
calls this file makes and of nothing else.
"""
import fixtures  # noqa: F401  (import first: isolated data dir + sys.path)

import os
import threading
import time

from PIL import Image, ImageDraw

import globals as gl
import src.backend.DeckManagement.deck_controller.label_engine as label_engine
from fixtures import start_watchdog, teardown

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout
from src.backend.DeckManagement.deck_controller.gif_pipeline import GifBackground


OPAQUE = (40, 160, 90, 255)
TILE_COLORS = [
    (10, 10, 200, 255),
    (200, 10, 10, 255),
    (10, 200, 10, 255),
    (200, 200, 10, 255),
    (10, 200, 200, 255),
]


# --- fixtures -------------------------------------------------------------

def _settle(controller) -> None:
    """Wait out the page load, which rebuilds every state's managers on
    worker threads. Media staged before that lands is discarded."""
    assert fixtures.wait_until(lambda: controller.active_page is not None, timeout=15), \
        "fixture sanity: no page loaded"

    def _background_load_done() -> bool:
        future = getattr(controller, "_bg_future", None)
        return future is None or future.done()

    assert fixtures.wait_until(_background_load_done, timeout=15), \
        "fixture sanity: the page's background load never finished"
    assert fixtures.wait_until(
        lambda: not controller.media_player.tasks and not controller.media_player.image_tasks,
        timeout=15), \
        "fixture sanity: the media player never drained its page-load tasks"
    # Empty queues mean dequeued, not done. A marker on the same queue runs
    # after everything queued ahead of it.
    ran = threading.Event()
    controller.media_player.add_task(ran.set)
    assert ran.wait(timeout=15), \
        "fixture sanity: the media player never ran the settle marker"


def _key(controller, index: int):
    keys = sorted(controller.inputs[Input.Key], key=lambda k: k.index)
    assert index < len(keys), f"fixture sanity: the fake deck has only {len(keys)} keys"
    return keys[index]


def _set_tiles(controller, color) -> None:
    """Publish one flat background frame. The media thread does not touch the
    tiles while no background video is installed, so this is the only writer."""
    tile = Image.new("RGBA", controller.get_key_image_size(), color)
    controller.background.tiles = [tile.copy() for _ in range(controller.deck.key_count())]
    controller.background._identified_tiles = None


def _opaque_source(size=(144, 144)) -> Image.Image:
    """A foreground with no transparent pixel anywhere. The pattern is
    asymmetric, so a composite that lost it would not read as a flat fill."""
    image = Image.new("RGBA", size, OPAQUE)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, size[0] // 3, size[1]], fill=(220, 60, 30, 255))
    draw.ellipse([size[0] // 2, 10, size[0] - 10, size[1] // 2], fill=(20, 30, 200, 255))
    return image


def _alpha_source(size=(144, 144)) -> Image.Image:
    """The same picture with a transparent border, which the background shows
    through."""
    image = _opaque_source(size)
    border = Image.new("RGBA", size, (0, 0, 0, 0))
    inset = 12
    border.paste(image.crop((inset, inset, size[0] - inset, size[1] - inset)), (inset, inset))
    return border


def _give_media(key, source: Image.Image) -> None:
    state = key.get_active_state()
    state.set_image(InputImage(controller_input=key, image=source), update=False)


class _CompositeCounter:
    """Counts the composites one key really runs. It wraps the instance, so
    the count covers every caller of the composite and not only update()."""

    def __init__(self, key):
        self.key = key
        self.count = 0
        self._original = key.get_current_image

    def __enter__(self) -> "_CompositeCounter":
        def counted():
            self.count += 1
            return self._original()

        self.key.get_current_image = counted
        return self

    def __exit__(self, *exc) -> None:
        del self.key.get_current_image


def _record_enqueued(controller) -> list:
    """Every native the paint path hands the writer, in call order."""
    enqueued: list = []
    original = controller.media_player.add_image_task

    def recording(key_index, native_image, **kwargs):
        enqueued.append((key_index, native_image))
        return original(key_index, native_image, **kwargs)

    controller.media_player.add_image_task = recording
    return enqueued


# --- checks ---------------------------------------------------------------

def check_cover_test() -> None:
    """The opacity and geometry test that decides every skip."""
    covers = label_engine._foreground_hides_background
    tile = (72, 72)

    opaque_rgb = Image.new("RGB", tile, (10, 20, 30))
    assert covers(opaque_rgb, 0, 0, tile), "an exact-size image with no alpha channel covers"

    opaque_rgba = Image.new("RGBA", tile, (10, 20, 30, 255))
    assert covers(opaque_rgba, 0, 0, tile), "an exact-size RGBA image at alpha 255 covers"

    oversize = Image.new("RGBA", (90, 90), (10, 20, 30, 255))
    assert covers(oversize, -9, -9, tile), "an oversize image cropped by the paste covers"

    translucent = Image.new("RGBA", tile, (10, 20, 30, 255))
    translucent.putpixel((36, 36), (10, 20, 30, 254))
    assert not covers(translucent, 0, 0, tile), (
        "one pixel below alpha 255 must fail the test -- the composite still "
        "depends on the background there"
    )

    inset = Image.new("RGBA", (60, 60), (10, 20, 30, 255))
    assert not covers(inset, 6, 6, tile), "an image smaller than the tile leaves a margin"

    # Big enough, and still off the top left corner.
    assert not covers(oversize, 6, 6, tile), (
        "a paste that starts inside the tile leaves the top and left edges bare, "
        "however large the image is"
    )

    # Flush at the top left, and short of the bottom right.
    assert not covers(inset, 0, 0, tile), (
        "an image flush at the origin still has to reach the far edges"
    )

    short = Image.new("RGBA", (72, 60), (10, 20, 30, 255))
    assert not covers(short, 0, 6, tile), "an image that misses two edges does not cover"

    palette = Image.new("P", tile)
    palette.info["transparency"] = 0
    assert not covers(palette, 0, 0, tile), (
        "transparency carried in info, not in a band, cannot be proved opaque "
        "and must read as not covering"
    )

    print("PASS: the foreground cover test answers on geometry and on alpha")


def check_opaque_cover_skips_composite(controller) -> None:
    key = _key(controller, 0)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    enqueued = _record_enqueued(controller)
    with _CompositeCounter(key) as composites:
        key.update()
        assert composites.count == 1, "the first paint must composite"
        assert state.cover_cache._entry is not None, (
            "a full-size opaque foreground must leave a kept composite behind"
        )
        served = state.cover_cache._entry.image.tobytes()

        for color in TILE_COLORS[1:]:
            _set_tiles(controller, color)
            key.update()
        assert composites.count == 1, (
            f"the background moved {len(TILE_COLORS) - 1} times under a foreground that "
            f"hides it, and the key composited {composites.count} times -- the skip did "
            f"not hold"
        )
        assert len(enqueued) == 1, (
            f"a covered key must reach the device once and then hash-skip; the writer "
            f"took {len(enqueued)} paints"
        )

        # force must still reach the device, hashes or no hashes.
        key.update(force=True)
        assert len(enqueued) == 2, (
            "a forced repaint of a covered key must reach the writer, as a forced "
            "repaint of any other key does"
        )
        assert composites.count == 1, "a forced repaint must not need a fresh composite"

        # The paint path closes a composite whose offer it hash-skipped, so
        # the cache has to hold a buffer of its own. The close is driven here
        # rather than waited for, so the order is this file's and not the
        # writer thread's.
        state.cover_cache.invalidate()
        fresh = key.get_current_image()
        assert composites.count == 2, "fixture sanity: the invalidated key must recomposite"
        fresh.close()  # exactly what update() does with a hash-skipped paint
        key.update()
        assert composites.count == 2, "the recomposited key must settle back into the skip"
        assert state.cover_cache._entry.image.tobytes() == served, (
            "the kept composite must survive the close the paint path performs on the "
            "image it was handed"
        )

    # Golden compare. A fresh composite over each of those backgrounds has to
    # return the very bytes the skip served, or the skip changed the picture.
    for color in TILE_COLORS:
        _set_tiles(controller, color)
        state.cover_cache.invalidate()
        fresh = key.get_current_image()
        assert fresh.tobytes() == served, (
            f"the kept composite differs from a fresh composite over background {color} "
            f"-- the covered fast path is not pixel-identical to the path it replaces"
        )
        fresh.close()

    print("PASS: an opaque full-cover foreground composites once, pixel-identical")


def check_alpha_foreground_still_composites(controller) -> None:
    key = _key(controller, 1)
    state = key.get_active_state()
    _give_media(key, _alpha_source())

    enqueued = _record_enqueued(controller)
    with _CompositeCounter(key) as composites:
        for color in TILE_COLORS:
            _set_tiles(controller, color)
            key.update()

    assert composites.count == len(TILE_COLORS), (
        f"a foreground with a transparent border must composite every frame; it "
        f"composited {composites.count} of {len(TILE_COLORS)}"
    )
    assert state.cover_cache._entry is None, (
        "a foreground that lets the background through must keep no composite"
    )
    natives = {native for _index, native in enqueued}
    assert len(natives) > 1, (
        "the background showed through a transparent border, so the device had to "
        "receive more than one distinct frame"
    )

    print("PASS: a foreground with alpha composites per frame as before")


def check_small_media_still_composites(controller) -> None:
    key = _key(controller, 2)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    state.layout_manager.set_page_layout(
        ImageLayout(fill_mode="contain", size=0.5, halign=0, valign=0), update=False)

    with _CompositeCounter(key) as composites:
        for color in TILE_COLORS:
            _set_tiles(controller, color)
            key.update()

    assert composites.count == len(TILE_COLORS), (
        f"media at half the tile leaves a margin the background fills, so it must "
        f"composite every frame; it composited {composites.count} of {len(TILE_COLORS)}"
    )
    assert state.cover_cache._entry is None, \
        "media smaller than the tile must keep no composite"

    print("PASS: media smaller than the tile composites per frame")


def check_zero_size_layout(controller) -> None:
    """A layout size of zero draws no foreground at all. The composite is then
    the bare background, which moves every frame, and the entry left over from
    when the same asset did cover must not claim it."""
    key = _key(controller, 6)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    with _CompositeCounter(key) as composites:
        key.update()
        assert state.cover_cache._entry is not None, "fixture sanity: the key did not settle"

        state.layout_manager.set_page_layout(
            ImageLayout(fill_mode="cover", size=0.0, halign=0, valign=0), update=False)
        for color in TILE_COLORS[1:]:
            _set_tiles(controller, color)
            key.update()
        assert composites.count == len(TILE_COLORS), (
            f"a size of zero paints no foreground, so every frame is the bare "
            f"background and has to composite; {composites.count} composites ran for "
            f"{len(TILE_COLORS)} frames"
        )
        assert state.cover_cache._entry is None, \
            "a key that paints no foreground must keep no composite"

    print("PASS: a layout size of zero retires the cover verdict")


def check_scroll_label_composites(controller) -> None:
    """A rolling label redraws at its own cadence over the very foreground
    that hides the background, so such a key must never settle.

    This runs last. A scroll label anywhere on the deck puts the media loop
    back on per-tick key work, and that loop then composites alongside this
    file. Counts here are therefore lower bounds, and the kept composite,
    which only this file's paints can create, carries the contract.
    """
    key = _key(controller, 7)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])
    state.label_manager.set_page_label(
        "center", KeyLabel(controller_input=key, text="a label far wider than this key",
                           font_size=20, color=[255, 255, 255, 255]), update=False)
    assert state.label_manager.get_has_scroll_labels(), (
        "fixture sanity: the label did not overflow the key, so nothing scrolls"
    )

    with _CompositeCounter(key) as composites:
        for color in TILE_COLORS:
            _set_tiles(controller, color)
            key.update()
            assert state.cover_cache._entry is None, (
                "a rolling label must leave no kept composite to freeze on -- the "
                "label moves under a foreground that does hide the background"
            )

    assert composites.count >= len(TILE_COLORS), (
        f"a key carrying a rolling label must composite on every paint; it "
        f"composited {composites.count} times over {len(TILE_COLORS)} paints"
    )

    print("PASS: a rolling label keeps a covered key on the composite")


def check_label_invalidates(controller) -> None:
    key = _key(controller, 3)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    with _CompositeCounter(key) as composites:
        key.update()
        _set_tiles(controller, TILE_COLORS[1])
        key.update()
        assert composites.count == 1, "fixture sanity: the key did not settle into the skip"
        unlabelled = state.cover_cache._entry.image.tobytes()

        state.label_manager.set_page_label(
            "center", KeyLabel(controller_input=key, text="LIVE", font_size=14,
                               color=[255, 255, 255, 255]), update=False)
        key.update()
        assert composites.count == 2, (
            "a label added over an opaque foreground must retire the kept composite "
            "and force one fresh composite"
        )
        entry = state.cover_cache._entry
        assert entry is not None, (
            "a label draws over the media and hides nothing more of the background, "
            "so the key must settle back into the skip"
        )
        labelled = entry.image.tobytes()
        assert labelled != unlabelled, "the fresh composite must carry the label"

        for color in TILE_COLORS[2:]:
            _set_tiles(controller, color)
            key.update()
        assert composites.count == 2, (
            f"the labelled composite hides the background just as the bare one did, so "
            f"it must settle again; it composited {composites.count} times"
        )

    print("PASS: a label over an opaque foreground retires the kept composite, then settles")


def check_press_composites(controller) -> None:
    key = _key(controller, 4)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    with _CompositeCounter(key) as composites:
        key.update()
        assert state.cover_cache._entry is not None, "fixture sanity: the key did not settle"

        key.press_state = True
        key.update()
        assert composites.count == 2, "a press must composite, because the shrink exposes the background"
        assert state.cover_cache._entry is None, "a pressed key must keep no composite"

        _set_tiles(controller, TILE_COLORS[1])
        key.update()
        assert composites.count == 3, "a pressed key must keep compositing per frame"

        key.press_state = False
        key.update()
        assert composites.count == 4, "the release must composite"
        assert state.cover_cache._entry is not None, "the released key must settle back into the skip"

        _set_tiles(controller, TILE_COLORS[2])
        key.update()
        assert composites.count == 4, "the released key must skip again"

    print("PASS: a press leaves the skip and the release returns to it")


def check_overlay_composites(controller) -> None:
    """An overlay paints over the foreground and replaces the composite, so a
    covered key must drop what it kept the moment one is shown."""
    key = _key(controller, 8)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    with _CompositeCounter(key) as composites:
        key.update()
        assert state.cover_cache._entry is not None, "fixture sanity: the key did not settle"
        covered = state.cover_cache._entry.image.tobytes()

        overlay = Image.new("RGBA", (48, 48), (255, 0, 255, 255))
        state.show_overlay(overlay)  # shows the overlay through update()
        assert composites.count == 2, "an overlay must reach a fresh composite"
        assert state.cover_cache._entry is None, \
            "a key showing an overlay must keep no composite"
        shown = key.get_current_image()
        assert shown.tobytes() != covered, "fixture sanity: the overlay changed no pixel"
        shown.close()

        state.hide_overlay()
        assert state.cover_cache._entry is not None, \
            "hiding the overlay must let the key settle back into the skip"
        assert state.cover_cache._entry.image.tobytes() == covered, \
            "the key must return to exactly the picture it showed before the overlay"

    print("PASS: an overlay drops the kept composite and hiding it restores the skip")


def _make_gif(path: str, size=(64, 64), n_frames: int = 4) -> str:
    frames = []
    for i in range(n_frames):
        frame = Image.new("RGBA", size, (0, 0, 0, 255))
        draw = ImageDraw.Draw(frame)
        x0 = 4 + i * 10
        draw.ellipse([x0, 12, x0 + 26, 40], fill=(220, 40, 60, 255))
        frames.append(frame)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(path, format="GIF", save_all=True, append_images=frames[1:],
                   duration=100, loop=0, disposal=2)
    return path


def check_capped_gif_background(controller) -> None:
    """The rate cap decides how often a GIF background advances; the cover
    verdict decides whether a key redraws when it does. The two are
    independent, and a covered key must redraw on neither."""
    gif_path = _make_gif(os.path.join(gl.DATA_PATH, "media", "cover_bg.gif"))
    # A rate under the media loop's own is what a capped page carries.
    background = GifBackground(controller, gif_path, loop=True, fps=10)
    # page None keeps the media thread's tick off this video, so the frame the
    # background lands on is this file's decision alone.
    background.page = None
    controller.background.set_video(background, update=False)

    key = _key(controller, 5)
    state = key.get_active_state()
    _give_media(key, _opaque_source())

    def _advance(frame_index: int) -> None:
        background._last_frame_tick = None
        background._play_start = time.time() - (background._cum_delays[frame_index - 1]
                                                if frame_index else 0.0) - 0.001
        controller.background.update_tiles()

    try:
        with _CompositeCounter(key) as composites:
            _advance(0)
            key.on_media_player_tick()
            assert composites.count == 1, "fixture sanity: the first tick must composite"
            assert state.cover_cache._entry is not None, (
                "fixture sanity: the key over the GIF background did not settle into the skip"
            )

            frames = len(background.frames)
            assert frames > 1, "fixture sanity: the GIF decoded to a single frame"
            seen = set()
            for step in range(12):
                _advance(step % frames)
                seen.add(background.active_frame)
                key.on_media_player_tick()
            assert len(seen) > 1, (
                "fixture sanity: the GIF background never advanced, so the check proved nothing"
            )
            assert composites.count == 1, (
                f"the GIF background advanced through {len(seen)} frames under a covered key "
                f"and the key composited {composites.count} times"
            )

            # Un-cover it. Every later tick has to composite again.
            _give_media(key, _alpha_source())
            before = composites.count
            for step in range(4):
                _advance(step % frames)
                key.on_media_player_tick()
            assert composites.count == before + 4, (
                f"a foreground that stopped covering must put every tick back on the "
                f"composite; {composites.count - before} of 4 ran"
            )
            assert state.cover_cache._entry is None, \
                "an uncovered key must hold no kept composite"
    finally:
        controller.background.set_video(None, update=False)

    print("PASS: a capped GIF background under a covered key drives no composite, "
          "and un-covering restores them")


def main() -> None:
    start_watchdog(60, label="scenario_covered_key_composite_skip")
    check_cover_test()

    # Rolling labels on, so the scroll check has something to scroll. Every
    # other label here fits its key, and a label that fits never scrolls.
    fixtures._install_integration_globals()
    app_settings = gl.settings_manager.get_app_settings()
    app_settings.setdefault("general", {})["rolling-labels"] = True
    gl.settings_manager.save_app_settings(app_settings)

    controller = fixtures.make_headless_controller(serial="cover-skip-1", model="original")
    try:
        _settle(controller)
        check_opaque_cover_skips_composite(controller)
        check_alpha_foreground_still_composites(controller)
        check_small_media_still_composites(controller)
        check_zero_size_layout(controller)
        check_label_invalidates(controller)
        check_press_composites(controller)
        check_overlay_composites(controller)
        check_capped_gif_background(controller)
        # Last: it leaves a scroll label on the deck, which puts the media
        # loop back on per-tick key work for every check after it.
        check_scroll_label_composites(controller)
        print("PASS: scenario_covered_key_composite_skip")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
