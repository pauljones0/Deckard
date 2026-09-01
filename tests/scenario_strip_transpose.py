"""Verify the strip composers over a real DeckController on a Stream Deck + at every rotation:
canvas sizes, dial slots and their order, touch to slot, turned key gaps, video keep-check."""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import os

import globals as gl

from fixtures import make_headless_controller, start_watchdog, teardown

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.deck_controller import background_media
from src.backend.DeckManagement.deck_controller.viewport import DEFAULT_VIEW

ROTATIONS = (0, 90, 180, 270)
DEVICE_STRIP = (800, 100)
N_DIALS = 4


def logical_strip(rotation: int) -> "tuple[int, int]":
    return DEVICE_STRIP if rotation in (0, 180) else (DEVICE_STRIP[1], DEVICE_STRIP[0])


def touchscreen_of(controller):
    return controller.get_input(Input.Touchscreen("sd-plus"))


def turn_to(controller, rotation: int) -> bool:
    """Turn the deck and wait for the page load the turn ends in."""
    controller.set_rotation(rotation)
    return controller._input_load_done.wait(10.0)


def check_composite_size(controller) -> int:
    """(a) Every strip canvas takes the logical size."""
    for rotation in ROTATIONS:
        if not turn_to(controller, rotation):
            print(f"FAIL(a): rotation {rotation}: the input load did not finish")
            return 1
        expected = logical_strip(rotation)
        touchscreen = touchscreen_of(controller)
        if touchscreen is None:
            print("FAIL(a): the deck reports no touchscreen input")
            return 1
        if tuple(controller.get_touchscreen_image_size()) != expected:
            print(f"FAIL(a): rotation {rotation}: the composers draw at "
                  f"{controller.get_touchscreen_image_size()}, expected {expected}")
            return 1
        # The controller and the wrapper must agree, or one size composes and another turns.
        if tuple(controller.deck.logical_touchscreen_size()) != expected:
            print(f"FAIL(a): rotation {rotation}: the deck answers "
                  f"{controller.deck.logical_touchscreen_size()} while the "
                  f"controller hands out {expected}")
            return 1
        for name, size in (("empty", touchscreen.generate_empty_image().size),
                           ("video frame", touchscreen.get_image_size()),
                           ("composite", touchscreen.get_current_image().size)):
            if tuple(size) != expected:
                print(f"FAIL(a): rotation {rotation}: the strip {name} is "
                      f"{tuple(size)}, expected {expected}")
                return 1

    print("PASS: every strip canvas takes the size the user sees")
    return 0


def check_slot_rects(controller) -> int:
    """(b) The slots divide the axis the user sees, in the user's dial order."""
    for rotation in ROTATIONS:
        if not turn_to(controller, rotation):
            print(f"FAIL(b): rotation {rotation}: the input load did not finish")
            return 1
        touchscreen = touchscreen_of(controller)
        width, height = logical_strip(rotation)
        boxes = [touchscreen.get_dial_image_area(Input.Dial(str(i)))
                 for i in range(N_DIALS)]

        vertical = rotation in (90, 270)
        # Each slot spans the short axis and takes its share of the long one.
        for index, (start_x, start_y, end_x, end_y) in enumerate(boxes):
            span = (end_y - start_y) if vertical else (end_x - start_x)
            across = (end_x - start_x) if vertical else (end_y - start_y)
            long_axis = height if vertical else width
            short_axis = width if vertical else height
            if span != long_axis // N_DIALS or across != short_axis:
                print(f"FAIL(b): rotation {rotation}: slot {index} is "
                      f"{(start_x, start_y, end_x, end_y)} on a "
                      f"{(width, height)} strip")
                return 1

        # Together the slots cover the strip once, with no gap and no overlap.
        edges = sorted((box[1], box[3]) if vertical else (box[0], box[2])
                       for box in boxes)
        if edges[0][0] != 0 or edges[-1][1] != (height if vertical else width):
            print(f"FAIL(b): rotation {rotation}: the slots cover {edges}, "
                  f"not the whole strip")
            return 1
        for lower, upper in zip(edges, edges[1:]):
            if lower[1] != upper[0]:
                print(f"FAIL(b): rotation {rotation}: the slots leave a gap or "
                      f"overlap at {lower} and {upper}")
                return 1

        # Dial 0 keeps the knob at the end the strip starts at: the top at 90, the bottom at 270.
        if vertical:
            first_top = boxes[0][1] == 0
            if first_top != (rotation == 90):
                print(f"FAIL(b): rotation {rotation}: dial 0 took the slot at "
                      f"{boxes[0]}; it belongs at the "
                      f"{'top' if rotation == 90 else 'bottom'}")
                return 1
        elif boxes[0][0] != 0:
            print(f"FAIL(b): rotation {rotation}: dial 0 took the slot at "
                  f"{boxes[0]}, not the left end")
            return 1

        # An empty dial image fills exactly one slot.
        empty = touchscreen.get_empty_dial_image().size
        first = boxes[0]
        if tuple(empty) != (first[2] - first[0], first[3] - first[1]):
            print(f"FAIL(b): rotation {rotation}: an empty dial image is "
                  f"{empty}, its slot is {first}")
            return 1

    print("PASS: the dial slots divide the axis the user sees, dial 0 at the "
          "end the turn brought it to")
    return 0


def check_touch_to_slot(controller) -> int:
    """(c) A touch in the user's frame reaches the slot it landed in."""
    for rotation in ROTATIONS:
        if not turn_to(controller, rotation):
            print(f"FAIL(c): rotation {rotation}: the input load did not finish")
            return 1
        touchscreen = touchscreen_of(controller)
        for index in range(N_DIALS):
            start_x, start_y, end_x, end_y = touchscreen.get_dial_image_area(
                Input.Dial(str(index)))
            centre = {"x": (start_x + end_x) // 2, "y": (start_y + end_y) // 2}
            dial = touchscreen.get_dial_for_touch(centre)
            reached = None if dial is None else dial.identifier.json_identifier
            if reached != str(index):
                print(f"FAIL(c): rotation {rotation}: a touch at {centre}, in "
                      f"slot {index}, reached dial {reached}")
                return 1

        width, height = logical_strip(rotation)
        for off_strip in ({"x": width, "y": 0}, {"x": 0, "y": height},
                          {"x": -1, "y": 0}):
            if touchscreen.get_dial_for_touch(off_strip) is not None:
                print(f"FAIL(c): rotation {rotation}: a touch at {off_strip}, "
                      f"off a {(width, height)} strip, reached a dial")
                return 1

    print("PASS: a touch reaches the slot it landed in, and nothing off the "
          "strip reaches a dial")
    return 0


def check_turned_grid_spacing(controller) -> int:
    """(d) The turned grid swaps the asymmetric SD+ key gaps with the layout."""
    if not turn_to(controller, 0):
        print("FAIL(d): rotation 0: the input load did not finish")
        return 1
    flat_spacing = controller.logical_key_spacing()
    flat_rows, flat_cols = controller.deck.key_layout()
    key_w, key_h = controller.get_key_image_size()
    flat_grid = (key_w * flat_cols + flat_spacing[0] * (flat_cols - 1),
                 key_h * flat_rows + flat_spacing[1] * (flat_rows - 1))

    if not turn_to(controller, 90):
        print("FAIL(d): rotation 90: the input load did not finish")
        return 1
    turned_spacing = controller.logical_key_spacing()
    if turned_spacing != (flat_spacing[1], flat_spacing[0]):
        print(f"FAIL(d): rotation 90 answers spacing {turned_spacing}; the "
              f"gaps must turn with the layout, expected "
              f"{(flat_spacing[1], flat_spacing[0])}")
        return 1
    rows, cols = controller.deck.key_layout()
    turned_grid = (key_w * cols + turned_spacing[0] * (cols - 1),
                   key_h * rows + turned_spacing[1] * (rows - 1))
    if turned_grid != (flat_grid[1], flat_grid[0]):
        print(f"FAIL(d): the turned grid is {turned_grid}, not the transpose "
              f"of the flat {flat_grid}; a grid built from unturned gaps "
              f"misplaces every crop on the asymmetric SD+")
        return 1

    print("PASS: the turned grid swaps the asymmetric key gaps with the layout")
    return 0


def check_rotation_change_rebuilds_video(controller) -> int:
    """(e) The prebuild keep-check refuses a provider built for another turn.
    The deck turns directly over its own Background; set_rotation's reload swaps the provider."""
    background = background_media.Background(controller)
    background.extend_to_touchscreen = True
    path = os.path.join(gl.DATA_PATH, "keepcheck.mp4")
    with open(path, "wb") as handle:
        handle.write(b"placeholder")

    class _BuiltProvider:
        """The fields the keep-check reads, as a provider built at rotation 0 carries them."""
        video_path = path
        extend_touchscreen = True
        rotation = 0
        view = DEFAULT_VIEW
        saturation = controller.get_display_saturation()

    class _StubVideo:
        """Records construction instead of opening the placeholder with cv2."""
        def __init__(self, deck_controller, video_path, loop=True, fps=30,
                     extend_touchscreen=False, view=DEFAULT_VIEW):
            self.video_path = video_path

    real_video_cls = background_media.BackgroundVideo
    try:
        controller.deck.set_rotation(0)
        background.video = _BuiltProvider()
        kind, _payload = background.prebuild_from_path(path)
        if kind != "keep":
            print(f"FAIL(e): the same rotation answered {kind}, expected keep")
            return 1

        controller.deck.set_rotation(90)
        background_media.BackgroundVideo = _StubVideo
        kind, payload = background.prebuild_from_path(path)
        if kind != "video" or not isinstance(payload, _StubVideo):
            print(f"FAIL(e): after a turn the keep-check answered {kind}; a "
                  f"kept provider carries the old canvas and band side")
            return 1
    finally:
        background_media.BackgroundVideo = real_video_cls
        background.video = None
        controller.deck.set_rotation(0)

    print("PASS: the keep-check rebuilds a background video after a turn")
    return 0


def main() -> int:
    start_watchdog(180, "strip_transpose")
    controller = make_headless_controller(serial="strip-transpose",
                                          model="plus")
    try:
        rc = check_composite_size(controller)
        rc |= check_slot_rects(controller)
        rc |= check_touch_to_slot(controller)
        rc |= check_turned_grid_spacing(controller)
        rc |= check_rotation_change_rebuilds_video(controller)
    finally:
        teardown(controller)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
