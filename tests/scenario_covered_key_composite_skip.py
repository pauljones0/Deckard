"""Verify pixel-identical composite reuse when a foreground covers its tile."""
import fixtures  # noqa: F401  (import first: isolated data dir + sys.path)

import os
import threading

from PIL import Image, ImageDraw

import globals as gl

from src.backend.DeckManagement import media_loop
from fixtures import start_watchdog, teardown

from src.backend.DeckManagement.ImageHelpers import hides_background
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


def _disarm_repaint_retry(controller) -> None:
    """Disarm delayed full repaints so they cannot change composite counts."""
    controller._full_repaint_pending = False


def _assert_no_repaint(controller) -> None:
    assert not controller._full_repaint_pending, (
        "a device write failed while this file was counting paints, which arms a "
        "full repaint of the whole deck; the counts above are not this module's "
        "doing"
    )


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


class _RecordEnqueued:
    """Record native images handed to the writer and restore its hook."""

    def __init__(self, controller):
        self.controller = controller
        self.natives: list = []
        self._original = controller.media_player.add_image_task

    def __enter__(self) -> "_RecordEnqueued":
        def recording(key_index, native_image, **kwargs):
            self.natives.append((key_index, native_image))
            return self._original(key_index, native_image, **kwargs)

        self.controller.media_player.add_image_task = recording
        return self

    def __exit__(self, *exc) -> None:
        self.controller.media_player.add_image_task = self._original

    def __len__(self) -> int:
        return len(self.natives)


def check_cover_test() -> None:
    """The opacity and geometry test that decides every skip."""
    covers = hides_background
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

    # Wide enough and short by itself, which is what a contain fit of a wide
    # source produces. One dimension short is enough to fail.
    assert not covers(short, 0, 0, tile), (
        "an image that reaches the side edges and stops short of the bottom "
        "leaves a strip of background visible"
    )

    palette = Image.new("P", tile)
    palette.info["transparency"] = 0
    assert not covers(palette, 0, 0, tile), (
        "transparency carried in info, not in a band, cannot be proved opaque "
        "and must read as not covering"
    )

    print("PASS: the foreground cover test answers on geometry and on alpha")


def check_opaque_cover_skips_composite(controller) -> None:
    _disarm_repaint_retry(controller)
    key = _key(controller, 0)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    with _RecordEnqueued(controller) as enqueued, _CompositeCounter(key) as composites:
        key.update()
        assert composites.count == 1, "the first paint must composite"
        assert state.cover_cache._entry is not None, (
            "a full-size opaque foreground must leave a kept composite behind"
        )
        served = state.cover_cache._entry.image.tobytes()
        first_native = enqueued.natives[0][1]

        for color in TILE_COLORS[1:]:
            _set_tiles(controller, color)
            key.update()
        assert composites.count == 1, (
            f"the background moved {len(TILE_COLORS) - 1} times under a foreground that "
            f"hides it, and the key composited {composites.count} times -- the skip did "
            f"not hold"
        )
        _assert_no_repaint(controller)
        assert len(enqueued) == 1, (
            f"a covered key must reach the device once and then hash-skip; the writer "
            f"took {len(enqueued)} paints"
        )

        # Clear the memo so a forced write proves which image reaches the encoder.
        controller.encode_memo.clear()
        key.update(force=True)
        assert len(enqueued) == 2, (
            "a forced repaint of a covered key must reach the writer, as a forced "
            "repaint of any other key does"
        )
        assert composites.count == 1, "a forced repaint must not need a fresh composite"
        assert enqueued.natives[1][1] == first_native, (
            "the reuse path encoded something other than the picture it kept -- with "
            "a cold memo those are the bytes the device receives"
        )

        # Close a skipped image directly to prove the cache owns a separate buffer.
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

    _assert_no_repaint(controller)
    print("PASS: an opaque full-cover foreground composites once, pixel-identical")


def check_alpha_foreground_still_composites(controller) -> None:
    _disarm_repaint_retry(controller)
    key = _key(controller, 1)
    state = key.get_active_state()
    _give_media(key, _alpha_source())

    with _RecordEnqueued(controller) as enqueued, _CompositeCounter(key) as composites:
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
    natives = {native for _index, native in enqueued.natives}
    assert len(natives) > 1, (
        "the background showed through a transparent border, so the device had to "
        "receive more than one distinct frame"
    )

    _assert_no_repaint(controller)
    print("PASS: a foreground with alpha composites per frame as before")


def check_small_media_still_composites(controller) -> None:
    _disarm_repaint_retry(controller)
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

    _assert_no_repaint(controller)
    print("PASS: media smaller than the tile composites per frame")


def check_zero_size_layout(controller) -> None:
    """Retire the cover entry when layout size zero exposes the background."""
    _disarm_repaint_retry(controller)
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

    _assert_no_repaint(controller)
    print("PASS: a layout size of zero retires the cover verdict")


def check_scroll_label_composites(controller) -> None:
    """Keep rolling labels off the cover cache.

    This runs last because scrolling enables concurrent per-tick key work.
    """
    _disarm_repaint_retry(controller)
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

    _assert_no_repaint(controller)
    print("PASS: a rolling label keeps a covered key on the composite")


def check_label_invalidates(controller) -> None:
    _disarm_repaint_retry(controller)
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

    _assert_no_repaint(controller)
    print("PASS: a label over an opaque foreground retires the kept composite, then settles")


def check_press_composites(controller) -> None:
    _disarm_repaint_retry(controller)
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

    _assert_no_repaint(controller)
    print("PASS: a press leaves the skip and the release returns to it")


def check_warning_point_composites(controller) -> None:
    """Keep warning-point keys uncached so the dot clears after repair."""
    _disarm_repaint_retry(controller)
    key = _key(controller, 9)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    with _CompositeCounter(key) as composites:
        key.update()
        assert state.cover_cache._entry is not None, "fixture sanity: the key did not settle"
        healthy = state.cover_cache._entry.image.tobytes()

        # The page's action table decides this; drive it directly, so the
        # check needs no plugin manager.
        key.has_unavailable_action = lambda: True
        key.update()
        assert composites.count == 2, "an unavailable action must reach a fresh composite"
        assert state.cover_cache._entry is None, (
            "a key wearing the warning point must keep no composite -- the dot would "
            "outlive the action that comes back"
        )
        dotted = key.get_current_image()
        assert dotted.tobytes() != healthy, "fixture sanity: the warning point drew nothing"
        dotted.close()

        for color in TILE_COLORS[1:3]:
            _set_tiles(controller, color)
            key.update()
        assert state.cover_cache._entry is None, \
            "the warning point must keep the key off the skip for as long as it shows"

        del key.has_unavailable_action
        key.update()
        assert state.cover_cache._entry is not None, \
            "the action coming back must let the key settle again"
        assert state.cover_cache._entry.image.tobytes() == healthy, \
            "the settled picture must be the one with no dot on it"

    _assert_no_repaint(controller)
    print("PASS: the warning point keeps a covered key compositing until it clears")


def check_release_during_composite(controller) -> None:
    """Do not cache a pressed composite when release lands before its store."""
    _disarm_repaint_retry(controller)
    key = _key(controller, 10)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    key.update()
    assert state.cover_cache._entry is not None, "fixture sanity: the key did not settle"
    unpressed = state.cover_cache._entry.image.tobytes()

    key.press_state = True
    key.update()
    assert state.cover_cache._entry is None, "fixture sanity: a pressed key kept a composite"

    original_shrink = key.shrink_image

    def shrink_then_release(image, factor: float = 0.7):
        shrunk = original_shrink(image, factor)
        # The finger comes off here, between the shrink and the store.
        key.press_state = False
        return shrunk

    key.shrink_image = shrink_then_release
    try:
        key.update()
    finally:
        del key.shrink_image

    assert state.cover_cache._entry is None, (
        "a release inside the composite window stored the pressed picture. Nothing "
        "retires it: press_state is False again and no stamp field moved, so the key "
        "shows itself held down until the next press"
    )

    # And the key still recovers on the next paint.
    key.update()
    assert state.cover_cache._entry is not None, \
        "the released key must settle on the next composite"
    assert state.cover_cache._entry.image.tobytes() == unpressed, \
        "the settled picture must be the unpressed one"

    _assert_no_repaint(controller)
    print("PASS: a release inside the composite window stores nothing")


def check_label_edit_during_composite(controller) -> None:
    """Do not store an old-label composite under a concurrently updated stamp."""
    _disarm_repaint_retry(controller)
    key = _key(controller, 11)
    state = key.get_active_state()
    label_manager = state.label_manager
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    def _label(text: str) -> KeyLabel:
        return KeyLabel(controller_input=key, text=text, font_size=14,
                        color=[255, 255, 255, 255])

    label_manager.set_page_label("center", _label("ONE"), update=False)
    key.update()
    assert state.cover_cache._entry is not None, "fixture sanity: the key did not settle"
    one_bytes = state.cover_cache._entry.image.tobytes()

    original_add = label_manager.add_labels_to_image
    fired: list[bool] = []

    def add_then_edit(image):
        labelled = original_add(image)
        if not fired:
            fired.append(True)
            label_manager.set_page_label("center", _label("TWO"), update=False)
            key.update()
        return labelled

    state.cover_cache.invalidate()
    label_manager.add_labels_to_image = add_then_edit
    try:
        key.update()
    finally:
        del label_manager.add_labels_to_image
    assert fired, "fixture sanity: the hook never ran"

    # Read what settled BEFORE composing anything else. A composite of its own
    # would store a fresh entry over the one under test.
    entry = state.cover_cache._entry
    stored_bytes = None if entry is None else entry.image.tobytes()

    state.cover_cache.invalidate()
    fresh = key.get_current_image()
    two_bytes = fresh.tobytes()
    fresh.close()
    assert two_bytes != one_bytes, "fixture sanity: the label edit changed no pixel"

    assert stored_bytes is None or stored_bytes == two_bytes, (
        f"a label edit inside the composite window settled the cache on the old "
        f"label (stored picture is the old one: {stored_bytes == one_bytes}). The "
        f"stamp already holds the new epoch, so no later read retires it and the key "
        f"serves the old label for as long as the page holds"
    )

    _assert_no_repaint(controller)
    print("PASS: a label edit inside the composite window leaves no stale entry")


def check_press_flip_flop_inside_composite(controller) -> None:
    """Do not cache a press that starts and ends inside one composite."""
    _disarm_repaint_retry(controller)
    key = _key(controller, 14)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    key.update()
    assert state.cover_cache._entry is not None, "fixture sanity: the key did not settle"
    unpressed = state.cover_cache._entry.image.tobytes()

    key.press_state = True
    pressed_image = key.get_current_image()
    pressed = pressed_image.tobytes()
    pressed_image.close()
    key.press_state = False
    assert pressed != unpressed, "fixture sanity: the shrink changed no pixel"

    label_manager = state.label_manager
    original_add = label_manager.add_labels_to_image
    original_shrink = key.shrink_image
    landed: list[bool] = []

    def add_then_press(image):
        labelled = original_add(image)
        if not landed:
            landed.append(True)
            # The finger lands after the read before the composite.
            key.press_state = True
        return labelled

    def shrink_then_release(image, factor: float = 0.7):
        shrunk = original_shrink(image, factor)
        # And comes off before the read after it.
        key.press_state = False
        return shrunk

    state.cover_cache.invalidate()
    label_manager.add_labels_to_image = add_then_press
    key.shrink_image = shrink_then_release
    try:
        key.update()
    finally:
        del label_manager.add_labels_to_image
        del key.shrink_image
    assert landed, "fixture sanity: the press hook never ran"

    entry = state.cover_cache._entry
    stored = None if entry is None else entry.image.tobytes()
    assert stored != pressed, (
        "a press that landed and left inside one composite window stored the "
        "shrunken picture. press_state is False again and no stamp field moved, so "
        "nothing retires it and the key shows itself held down"
    )
    assert stored is None or stored == unpressed, \
        "the store kept a picture that is neither the pressed nor the unpressed one"

    print("PASS: a press inside one composite window stores no shrunken picture")


def check_warning_flip_flop_inside_composite(controller) -> None:
    """Do not cache a warning that starts and ends inside one composite."""
    _disarm_repaint_retry(controller)
    key = _key(controller, 19)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    key.update()
    assert state.cover_cache._entry is not None, "fixture sanity: the key did not settle"
    healthy = state.cover_cache._entry.image.tobytes()

    unavailable = [False]
    key.has_unavailable_action = lambda: unavailable[0]

    label_manager = state.label_manager
    original_add = label_manager.add_labels_to_image
    original_warning = key.add_warning_point
    landed: list[bool] = []

    def add_then_lose_the_action(image):
        labelled = original_add(image)
        if not landed:
            landed.append(True)
            unavailable[0] = True
        return labelled

    def warn_then_recover(image, **kwargs):
        dotted = original_warning(image, **kwargs)
        unavailable[0] = False
        return dotted

    state.cover_cache.invalidate()
    label_manager.add_labels_to_image = add_then_lose_the_action
    key.add_warning_point = warn_then_recover
    try:
        key.update()
    finally:
        del label_manager.add_labels_to_image
        del key.add_warning_point
        del key.has_unavailable_action
    assert landed, "fixture sanity: the hook never ran"

    entry = state.cover_cache._entry
    stored = None if entry is None else entry.image.tobytes()
    assert stored is None or stored == healthy, (
        "an action that went missing and came back inside one composite window "
        "stored the dotted picture. The action is healthy again and no stamp field "
        "moved, so nothing retires the dot"
    )

    print("PASS: a warning point inside one composite window stores no dotted picture")


def check_press_lands_and_stays(controller) -> None:
    """Do not cache a press that starts inside a composite and stays active."""
    _disarm_repaint_retry(controller)
    key = _key(controller, 15)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    key.update()
    assert state.cover_cache._entry is not None, "fixture sanity: the key did not settle"
    unpressed = state.cover_cache._entry.image.tobytes()

    label_manager = state.label_manager
    original_add = label_manager.add_labels_to_image
    landed: list[bool] = []

    def add_then_press(image):
        labelled = original_add(image)
        if not landed:
            landed.append(True)
            key.press_state = True
        return labelled

    state.cover_cache.invalidate()
    label_manager.add_labels_to_image = add_then_press
    try:
        key.update()
    finally:
        del label_manager.add_labels_to_image
    assert landed, "fixture sanity: the press hook never ran"

    entry = state.cover_cache._entry
    assert entry is None or entry.image.tobytes() == unpressed, (
        "a press that landed mid-composite and stayed down left a pressed picture "
        "in the cache, and the release then serves it"
    )

    key.press_state = False
    key.update()
    assert state.cover_cache._entry is not None, "the released key must settle"
    assert state.cover_cache._entry.image.tobytes() == unpressed, \
        "the settled picture after the release is not the unpressed one"

    print("PASS: a press that lands mid-composite and stays leaves nothing wrong")


def check_bare_to_covered_first_store(controller) -> None:
    """Cache the first covering composite after a bare key receives media."""
    _disarm_repaint_retry(controller)
    key = _key(controller, 16)
    state = key.get_active_state()

    for color in TILE_COLORS:
        _set_tiles(controller, color)
        key.update()
    assert state.cover_cache._entry is None, "fixture sanity: a bare key kept a composite"

    _give_media(key, _opaque_source())
    key.update()
    assert state.cover_cache._entry is not None, (
        "the first composite after media landed on a bare key was not kept, so the "
        "bail-out read a verdict that does not belong to this asset"
    )

    print("PASS: the first cacheable composite after bare to covered is kept")


def check_uncovering_then_covering_settles(controller) -> None:
    """A size edit turns a covering foreground bare, then back. The verdict on
    file is one composite stale by design; prove it is exactly one."""
    _disarm_repaint_retry(controller)
    key = _key(controller, 17)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    key.update()
    assert state.cover_cache._entry is not None, "fixture sanity: the key did not settle"

    state.layout_manager.set_page_layout(
        ImageLayout(fill_mode="contain", size=0.5, halign=0, valign=0), update=False)
    key.update()
    assert state.cover_cache._entry is None, "fixture sanity: half-size media still covered"

    state.layout_manager.set_page_layout(
        ImageLayout(fill_mode="cover", size=1.0, halign=0, valign=0), update=False)
    key.update()
    key.update()
    assert state.cover_cache._entry is not None, (
        "a foreground that went from bare to covering never started caching again, "
        "so the bare verdict is never refreshed"
    )

    print("PASS: a foreground that covers again settles within one further composite")


def check_media_removed_releases_entry(controller) -> None:
    """Release a kept composite when its key loses media on either path."""
    _disarm_repaint_retry(controller)
    key = _key(controller, 18)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    key.update()
    assert state.cover_cache._entry is not None, "fixture sanity: the key did not settle"
    state.set_image(None, update=False)
    assert state.cover_cache._entry is None, (
        "the media setter left the kept composite pinned. A bare key over a "
        "background video paints from the frame identity, so no later paint drops it"
    )

    # And the paint path drops one that reached it by any other route.
    _give_media(key, _opaque_source())
    key.update()
    assert state.cover_cache._entry is not None, "fixture sanity: the key did not settle again"
    state.key_image = None  # media gone without the setter, as a wipe does
    for color in TILE_COLORS:
        _set_tiles(controller, color)
        key.update()
    assert state.cover_cache._entry is None, (
        "a key with no media still pins a tile-sized composite after painting; "
        "nothing drops the picture until the state is torn down"
    )

    print("PASS: losing the media releases the kept composite")


def check_release_calls_drop_the_entry(controller) -> None:
    """The memory claim rests on two calls. A state's teardown and its reset
    for a fresh page load each have to release the picture it kept."""
    _disarm_repaint_retry(controller)
    for index, release in ((12, "close_resources"), (13, "clear")):
        key = _key(controller, index)
        state = key.get_active_state()
        _give_media(key, _opaque_source())
        _set_tiles(controller, TILE_COLORS[0])
        key.update()
        assert state.cover_cache._entry is not None, \
            f"fixture sanity: the key did not settle before {release}()"

        # Suppress repaint so this check isolates the explicit release call.
        key.update = lambda force=False: None
        try:
            getattr(state, release)()
        finally:
            del key.update
        assert state.cover_cache._entry is None, (
            f"{release}() left the kept composite behind. Nothing else bounds these "
            f"images, so a page load would retain one per state it ever painted"
        )

    _assert_no_repaint(controller)
    print("PASS: teardown and reset both release the kept composite")


def check_overlay_composites(controller) -> None:
    """An overlay paints over the foreground and replaces the composite, so a
    covered key must drop what it kept the moment one is shown."""
    _disarm_repaint_retry(controller)
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

    _assert_no_repaint(controller)
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
    """Keep covered keys stable across rate-capped GIF background frames."""
    _disarm_repaint_retry(controller)
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
        background._play_start = media_loop.now() - (background._cum_delays[frame_index - 1]
                                                     if frame_index else 0.0) - 0.001
        controller.background.update_tiles()

    try:
        with _CompositeCounter(key) as composites:
            _advance(0)
            key.on_media_player_tick(media_loop.now(), bg_frame_new=True)
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
                key.on_media_player_tick(media_loop.now(), bg_frame_new=True)
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
                key.on_media_player_tick(media_loop.now(), bg_frame_new=True)
            assert composites.count == before + 4, (
                f"a foreground that stopped covering must put every tick back on the "
                f"composite; {composites.count - before} of 4 ran"
            )
            assert state.cover_cache._entry is None, \
                "an uncovered key must hold no kept composite"
    finally:
        controller.background.set_video(None, update=False)

    _assert_no_repaint(controller)
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

    # An XL shape, because each check owns a key and there are more checks than
    # a small deck has keys.
    controller = fixtures.make_headless_controller(serial="cover-skip-1", model="xl")
    try:
        _settle(controller)
        check_opaque_cover_skips_composite(controller)
        check_alpha_foreground_still_composites(controller)
        check_small_media_still_composites(controller)
        check_zero_size_layout(controller)
        check_label_invalidates(controller)
        check_press_composites(controller)
        check_warning_point_composites(controller)
        check_overlay_composites(controller)
        check_release_during_composite(controller)
        check_press_flip_flop_inside_composite(controller)
        check_warning_flip_flop_inside_composite(controller)
        check_press_lands_and_stays(controller)
        check_label_edit_during_composite(controller)
        check_bare_to_covered_first_store(controller)
        check_uncovering_then_covering_settles(controller)
        check_media_removed_releases_entry(controller)
        check_release_calls_drop_the_entry(controller)
        check_capped_gif_background(controller)
        # Last: it leaves a scroll label on the deck, which puts the media
        # loop back on per-tick key work for every check after it.
        check_scroll_label_composites(controller)
        _assert_no_repaint(controller)
        print("PASS: scenario_covered_key_composite_skip")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
