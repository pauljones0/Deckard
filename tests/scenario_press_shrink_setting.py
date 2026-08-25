"""The shrink a pressed key draws is a setting. The store refusal is not.

A held key draws its picture smaller, centred on a transparent margin, so the
background shows at the edges. general.shrink-on-press turns that off, for a
page whose keys carry one picture between them and whose seams the margin
breaks. The default keeps the shrink, so a deck that nothing configures
presses as it always has.

What the setting must not reach is the covered-key cache. A press keeps a
composite out of that cache whatever the picture ends up looking like, because
the branch that draws a gated look is the branch that decides the store. This
file drives both halves: the pixels, from real composites of a real key, and
the kept entry, from the cache the render path stores into.

Nothing here sleeps or reloads a page. The setting is written the way the
settings dialog writes it, and the next composite reads it, which is what
proves a change needs no restart.
"""
import fixtures  # noqa: F401  (import first: isolated data dir + sys.path)

import threading

from PIL import Image, ImageDraw

import globals as gl
from fixtures import start_watchdog, teardown

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage


OPAQUE = (40, 160, 90, 255)
TILE_COLORS = [
    (10, 10, 200, 255),
    (200, 10, 10, 255),
    (10, 200, 10, 255),
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
    assert ran.wait(timeout=15), "fixture sanity: the media player never ran the settle marker"


def _disarm_repaint_retry(controller) -> None:
    """Clear the armed full repaint. A failed device write arms one, and the
    media loop fires it two seconds later: it repaints the whole deck, which
    stores a kept composite this file did not ask for."""
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


def _give_media(key, source: Image.Image) -> None:
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


# --- checks ---------------------------------------------------------------

def check_default_shrinks(controller) -> None:
    """The setting absent means the press feedback the app has always given."""
    assert gl.settings_manager.app().shrink_on_press is True, \
        "the shrink must default to on, or a deck nobody configured changes behaviour"

    key = _key(controller, 0)
    _give_media(key, _opaque_source())
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


def check_setting_stops_the_shrink(controller) -> None:
    """The setting off means a pressed key draws exactly what it drew at rest.

    Byte equality is the whole claim of the feature, so the check is on the
    bytes and not on the size of anything.
    """
    key = _key(controller, 1)
    _give_media(key, _opaque_source())
    _set_tiles(controller, TILE_COLORS[1])

    _set_shrink(False)
    at_rest = _composite(key)
    key.press_state = True
    try:
        pressed = _composite(key)
        assert pressed.tobytes() == at_rest.tobytes(), \
            "with the shrink off, a pressed key must draw the picture it shows at rest"
        pressed.close()

        # Back on, with no page reload and no restart, on the same held key.
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


def check_press_keeps_no_composite(controller) -> None:
    """A press stores nothing even when it draws nothing.

    The covered-key cache keeps one composite per key state and reuses it
    while its inputs hold. A pressed look must never be the picture it keeps,
    and with the shrink off that look is byte-identical to the resting one,
    which is exactly the case a store decided by comparing pictures would get
    wrong.
    """
    _set_shrink(False)
    _disarm_repaint_retry(controller)
    key = _key(controller, 2)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
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


def check_press_inside_composite_stores_nothing(controller) -> None:
    """A press that lands and leaves inside one composite window, shrink off.

    This is the case the store cannot reason its way out of. The read before
    the composite says not pressed, the read after says not pressed again, and
    with the shrink off the picture in between carries no evidence either: it
    is the resting picture, byte for byte. Only the branch that ran while the
    key was held knows, so that branch records the refusal itself.
    """
    _set_shrink(False)
    _disarm_repaint_retry(controller)
    key = _key(controller, 3)
    state = key.get_active_state()
    _give_media(key, _opaque_source())
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
    """The choice survives the settings store, and its absence reads as on.

    The stored value is put back to on first, and by the raw path. A check
    that writes the value the file already holds proves nothing about the
    write: a setter that dropped it on the floor would read back the same.
    """
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
        check_setting_stops_the_shrink(controller)
        check_press_keeps_no_composite(controller)
        check_press_inside_composite_stores_nothing(controller)
        check_setting_round_trip()
        print("PASS: scenario_press_shrink_setting")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
