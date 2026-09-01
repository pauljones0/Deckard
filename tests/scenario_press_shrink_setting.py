"""Verify shrink-on-press controls centered press pixels but not cache admission.
The default shrinks; setting changes apply on the next press without a page reload or restart."""
import fixtures  # noqa: F401  (import first: isolated data dir + sys.path)

import threading

from PIL import Image, ImageDraw

import globals as gl
from fixtures import start_watchdog, teardown

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage
from src.backend.DeckManagement.deck_controller import press_look


OPAQUE = (40, 160, 90, 255)
TILE_COLORS = [
    (10, 10, 200, 255),
    (200, 10, 10, 255),
    (10, 200, 10, 255),
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
    assert ran.wait(timeout=15), "fixture sanity: the media player never ran the settle marker"


def _disarm_repaint_retry(controller) -> None:
    """Clear a failed-write repaint that would fire after two seconds.
    Its full-deck repaint would store composites outside this check."""
    controller._full_repaint_pending = False


def _assert_no_repaint(controller) -> None:
    assert not controller._full_repaint_pending, (
        "a device write failed while this file was reading kept composites, which "
        "arms a full repaint of the whole deck; the entries above are not this "
        "module's doing"
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


def _set_key_image(key, source: Image.Image) -> None:
    state = key.get_active_state()
    state.set_image(InputImage(controller_input=key, image=source), update=False)


def _set_shrink(enabled: bool) -> None:
    """Write the setting the way the settings dialog does, and reload nothing.
    What the next composite draws then proves the read happens per press."""
    settings = gl.settings_manager.get_app_settings()
    settings.setdefault("general", {})["shrink-on-press"] = enabled
    gl.settings_manager.save_app_settings(settings)


def _composite(key) -> Image.Image:
    """One fresh composite of this key, as the paint path builds it."""
    return key.get_current_image()


def check_default_shrinks(controller) -> None:
    """Verify an absent setting enables press feedback."""
    assert gl.settings_manager.app().shrink_on_press is True, \
        "the shrink must default to on, or a deck nobody configured changes behaviour"

    key = _key(controller, 0)
    _set_key_image(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    at_rest = _composite(key)
    key.press_state = True
    try:
        pressed = _composite(key)
        assert pressed.tobytes() != at_rest.tobytes(), \
            "with the shrink on, a pressed key must not draw the picture it shows at rest"

        expected = key.shrink_image(at_rest)
        assert pressed.tobytes() == expected.tobytes(), (
            "the pressed picture is not the shrink of the resting one, so the press "
            "branch drew something else"
        )
        expected.close()
        pressed.close()
    finally:
        key.press_state = False
        at_rest.close()

    print("PASS: a key with no setting stored shrinks while it is pressed")


def check_shrink_is_centred(controller) -> None:
    """Verify the opaque region is centered within the transparent press margin.
    Opposite margins may differ by at most one pixel when dimensions are odd."""
    key = _key(controller, 0)
    source = Image.new("RGBA", controller.get_key_image_size(), OPAQUE)
    shrunk = key.shrink_image(source, factor=0.5)
    try:
        width, height = shrunk.size
        box = shrunk.getbbox()
        assert box is not None, "the shrink left nothing opaque to measure"
        left, top, right, bottom = box
        assert abs(left - (width - right)) <= 1, (
            f"the shrunken picture is off centre horizontally: {left} px on the "
            f"left against {width - right} px on the right")
        assert abs(top - (height - bottom)) <= 1, (
            f"the shrunken picture is off centre vertically: {top} px above "
            f"against {height - bottom} px below")
        assert left > 0 and top > 0, "the shrink left no margin at all"
    finally:
        shrunk.close()
        source.close()

    print("PASS: the shrink a pressed key draws is centred on its margin")


def check_shrink_disabled(controller) -> None:
    """Verify disabling shrink makes pressed and resting image bytes equal."""
    key = _key(controller, 1)
    _set_key_image(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[1])

    _set_shrink(False)
    at_rest = _composite(key)
    key.press_state = True
    try:
        pressed = _composite(key)
        assert pressed.tobytes() == at_rest.tobytes(), \
            "with the shrink off, a pressed key must draw the picture it shows at rest"
        pressed.close()

        assert press_look.apply(key, at_rest) is at_rest, (
            "with the shrink off the pressed look must be the resting image itself "
            "and not a copy of it. A copy costs one tile-sized allocation per "
            "composite of a held key, and the caller tells the images it must close "
            "apart by identity"
        )

        # Re-enable without reload; an already-held key has no queued paint,
        # so the device applies the new look on its next press.
        _set_shrink(True)
        pressed_again = _composite(key)
        assert pressed_again.tobytes() != at_rest.tobytes(), (
            "turning the shrink back on reached no composite; the setting is read "
            "once somewhere instead of per press"
        )
        pressed_again.close()
    finally:
        key.press_state = False
        _set_shrink(False)
        at_rest.close()

    print("PASS: the setting stops the shrink, and both directions reach the next press")


def check_pressed_key_skips_composite_cache(controller) -> None:
    """Keep pressed composites out of the covered-key cache when shrink is off.
    Cache admission must follow the press branch, not byte equality with the resting image."""
    _set_shrink(False)
    _disarm_repaint_retry(controller)
    key = _key(controller, 2)
    state = key.get_active_state()
    _set_key_image(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[0])

    key.update()
    assert state.cover_cache._entry is not None, \
        "fixture sanity: the covered key never settled, so this check proves nothing"
    settled = state.cover_cache._entry.image.tobytes()

    key.press_state = True
    try:
        key.update()
        assert state.cover_cache._entry is None, (
            "a pressed key kept a composite. The store is decided by the branch that "
            "draws the pressed look, and it must refuse whether or not that look "
            "differs from the resting one"
        )

        _set_tiles(controller, TILE_COLORS[1])
        key.update()
        assert state.cover_cache._entry is None, \
            "a held key must keep no composite for as long as it is held"
    finally:
        key.press_state = False

    key.update()
    assert state.cover_cache._entry is not None, \
        "the release must let the key settle back into the kept composite"
    assert state.cover_cache._entry.image.tobytes() == settled, \
        "the settled picture must be the resting one"

    _assert_no_repaint(controller)
    print("PASS: a press keeps a covered key out of the cache with the shrink off")


def check_mid_composite_press_skips_cache(controller) -> None:
    """Reject cache storage when a shrink-disabled press starts and ends during composition.
    Both outer reads and the image match rest, so only the executed press branch can reject it."""
    _set_shrink(False)
    _disarm_repaint_retry(controller)
    key = _key(controller, 3)
    state = key.get_active_state()
    _set_key_image(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[2])

    key.update()
    assert state.cover_cache._entry is not None, \
        "fixture sanity: the covered key never settled, so this check proves nothing"

    label_manager = state.label_manager
    original_add = label_manager.add_labels_to_image
    original_unavailable = key.has_unavailable_action
    landed: list[bool] = []

    def add_then_press(image):
        labelled = original_add(image)
        if not landed:
            landed.append(True)
            # The finger lands after the read before the composite.
            key.press_state = True
        return labelled

    def unavailable_then_release() -> bool:
        # This runs one branch after the press look, and before the read that
        # judges the store. The finger comes off in between.
        if landed:
            key.press_state = False
        return original_unavailable()

    state.cover_cache.invalidate()
    label_manager.add_labels_to_image = add_then_press
    key.has_unavailable_action = unavailable_then_release
    try:
        key.update()
    finally:
        del label_manager.add_labels_to_image
        del key.has_unavailable_action
        key.press_state = False
    assert landed, "fixture sanity: the press hook never ran"

    assert state.cover_cache._entry is None, (
        "a press that landed and left inside one composite window was stored. Both "
        "reads say not pressed and the picture gives nothing away with the shrink "
        "off, so the refusal has to come from the press branch itself"
    )

    _assert_no_repaint(controller)
    print("PASS: a press inside one composite window stores nothing with the shrink off")


def check_setting_round_trip() -> None:
    """Verify the choice round-trips through storage and absence reads as enabled.
    Seed the opposite value first so a dropped setter cannot pass unchanged."""
    _set_shrink(True)
    assert gl.settings_manager.app_snapshot().shrink_on_press is True, \
        "fixture sanity: the file does not hold the value this check writes over"

    app = gl.settings_manager.app()
    app.shrink_on_press = False
    app.save()
    assert gl.settings_manager.app_snapshot().shrink_on_press is False, \
        "the choice did not survive the write and a fresh read from disk"

    settings = gl.settings_manager.get_app_settings()
    settings["general"].pop("shrink-on-press")
    gl.settings_manager.save_app_settings(settings)
    assert gl.settings_manager.app_snapshot().shrink_on_press is True, \
        "an absent key must read as the on default rather than as off"

    print("PASS: the setting round-trips through the store and defaults to on")


def main() -> None:
    start_watchdog(60, label="scenario_press_shrink_setting")

    controller = fixtures.make_headless_controller(serial="press-shrink-1", model="xl")
    try:
        _settle(controller)
        check_default_shrinks(controller)
        check_shrink_is_centred(controller)
        check_shrink_disabled(controller)
        check_pressed_key_skips_composite_cache(controller)
        check_mid_composite_press_skips_cache(controller)
        check_setting_round_trip()
        print("PASS: scenario_press_shrink_setting")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
