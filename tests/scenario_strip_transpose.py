"""The touch strip is composed in the frame the user sees.

The wrapper answers the geometry (scenario_betterdeck_rotation pins those
answers); this drives the composers that read it, over a real DeckController
on a Stream Deck + shape:

  (a) the strip composite, the empty strip and the frame size a plugin video
      is cached at all take the logical size, which is the transpose of the
      device's buffer at the quarter turns;
  (b) the dial slots divide the axis the user sees, cover the strip exactly
      once, and dial 0 sits at the end the turn brought it to: the top at 90
      and the bottom at 270;
  (c) a touch reaches the slot it landed in, and a touch off the strip
      reaches no slot;
  (d) an extended background cuts its band from the edge of the wallpaper the
      user sees beside the strip, which is below the key grid on an unturned
      deck, above it at 180, and to one side at the quarter turns. The keys
      keep their own part of the wallpaper when the band takes the top or the
      left edge;
  (e) the same of the video cache, which crops its own canvas and captures
      the rotation for itself, so a capture taken at the wrong moment shows
      up there and nowhere else.

That the turn hands the measured layout back unchanged at rotation 0, and
turns it rigidly at the rest, is pinned in scenario_strip_band_geometry,
whose fixture reads as a real SD+ and so carries a band that overhangs its
key grid. This deck's band does not, so the grid offset carries no signal
here.

Legs (d) and (e) use a wallpaper with one gradient down and another across,
so the band's own mean says which edge it came off. The alternative,
comparing crop boxes, would restate the arithmetic under test.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import os

from PIL import Image

import globals as gl

from fixtures import make_headless_controller, start_watchdog, teardown

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.background_video_cache import BackgroundVideoCache
from src.backend.DeckManagement.deck_controller.background_media import BackgroundImage

ROTATIONS = (0, 90, 180, 270)
DEVICE_STRIP = (800, 100)
N_DIALS = 4
# Which edge of the wallpaper the strip lies against, in the user's view.
# Hold a Stream Deck + and turn it clockwise: the strip that sat below the key
# grid comes to lie to the left. Turn it the other way and it lies to the
# right; turn it over and it lies above.
BAND_SIDES = {0: "bottom", 90: "left", 180: "top", 270: "right"}


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
        # The controller hands out the size the wrapper answers. Two sources
        # that drift apart would compose one size and turn another.
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

        # Which end holds dial 0. The dial index map is one for one at both
        # quarter turns, so dial 0 stays the knob at the end of the device the
        # strip starts at. The clockwise turn brings that end to the top; the
        # turn the other way brings it to the bottom.
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


def gradient_wallpaper(size: int = 1200) -> Image.Image:
    """A wallpaper that grows brighter to the right in red and downwards in
    green, so a crop's mean says which edge it came off."""
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        green = y * 255 // (size - 1)
        for x in range(size):
            pixels[x, y] = (x * 255 // (size - 1), green, 0)
    return image


def channel_stats(image: Image.Image, channel: int) -> "tuple[float, int, int]":
    """(mean, lowest, highest) of one channel of image."""
    band = image.convert("RGB").getchannel(channel)
    data = list(band.getdata())
    band.close()
    return sum(data) / len(data), min(data), max(data)


def check_band_edge(controller) -> int:
    """(d) The band comes off the edge the user sees beside the strip."""
    background = controller.background
    background.extend_to_touchscreen = True

    for rotation in ROTATIONS:
        if not turn_to(controller, rotation):
            print(f"FAIL(d): rotation {rotation}: the input load did not finish")
            return 1
        source = gradient_wallpaper()
        image = BackgroundImage(controller, source)
        canvas = image.create_full_deck_sized_image(extend_touchscreen=True)
        strip = image.get_touchscreen_image()
        tiles = image.get_tiles(extend_touchscreen=True)

        if tuple(strip.size) != logical_strip(rotation):
            print(f"FAIL(d): rotation {rotation}: the band came out "
                  f"{strip.size}, the strip is {logical_strip(rotation)}")
            return 1

        # Red grows to the right, green downwards, so the band's mean on the
        # axis it lies on sits past the canvas mean, on the side it came off.
        side = BAND_SIDES[rotation]
        channel = 1 if side in ("top", "bottom") else 0
        band_mean, band_low, band_high = channel_stats(strip, channel)
        canvas_mean = channel_stats(canvas, channel)[0]
        brighter = side in ("bottom", "right")
        if (band_mean > canvas_mean) != brighter:
            print(f"FAIL(d): rotation {rotation}: the band's mean on channel "
                  f"{channel} is {band_mean:.1f} against a canvas mean of "
                  f"{canvas_mean:.1f}; the strip lies on the {side} of the "
                  f"grid, so the band comes off that edge")
            return 1

        # A band on the top or the left edge moves the key grid, and a crop
        # that ignores that shows the first key a piece of the band.
        # The band is the darkest part of the canvas on this channel there, so
        # the first key must start past its far edge, bezel gap included.
        if side in ("top", "left"):
            key_low = channel_stats(tiles[0], channel)[1]
            if key_low <= band_high:
                print(f"FAIL(d): rotation {rotation}: the first key runs from "
                      f"{key_low} on channel {channel} while the band reaches "
                      f"{band_high}; the key crops did not move with the band")
                return 1
        elif band_low <= channel_stats(tiles[0], channel)[2]:
            print(f"FAIL(d): rotation {rotation}: the band starts at "
                  f"{band_low} on channel {channel}, at or below the first "
                  f"key's {channel_stats(tiles[0], channel)[2]}; the key "
                  f"crops reach into the band")
            return 1

        for tile in tiles:
            tile.close()
        strip.close()
        canvas.close()
        image.close()
        source.close()

    print("PASS: the extended background cuts its band from the edge the user "
          "sees beside the strip")
    return 0


def check_video_band_edge(controller) -> int:
    """(d) again, over the video cache, which crops its own canvas.

    The image path and this one share the band arithmetic but capture the
    rotation separately, so a capture taken at the wrong moment shows up
    here alone. The canvas is fed in directly, because what is under test is
    the crop and not the decoder.
    """
    import numpy as np

    background = controller.background
    background.extend_to_touchscreen = True
    video_path = fixtures.make_test_mp4(
        os.path.join(gl.DATA_PATH, "strip_band_source.mp4"))

    for rotation in ROTATIONS:
        if not turn_to(controller, rotation):
            print(f"FAIL(e): rotation {rotation}: the input load did not finish")
            return 1
        cache = BackgroundVideoCache(video_path, deck_controller=controller,
                                     extend_touchscreen=True)
        try:
            source = gradient_wallpaper()
            canvas = source.resize(cache.out_size, Image.Resampling.LANCZOS)
            # _payload_from_bgr converts back to RGB, so hand it the channels
            # the decoder would.
            entries = cache._payload_from_bgr(np.array(canvas)[:, :, ::-1].copy())
            strip = entries[-1]

            if tuple(strip.size) != logical_strip(rotation):
                print(f"FAIL(e): rotation {rotation}: the video band came out "
                      f"{strip.size}, the strip is {logical_strip(rotation)}")
                return 1
            side = BAND_SIDES[rotation]
            channel = 1 if side in ("top", "bottom") else 0
            band_mean, band_low, band_high = channel_stats(strip, channel)
            canvas_mean = channel_stats(canvas, channel)[0]
            if (band_mean > canvas_mean) != (side in ("bottom", "right")):
                print(f"FAIL(e): rotation {rotation}: the video band's mean on "
                      f"channel {channel} is {band_mean:.1f} against a canvas "
                      f"mean of {canvas_mean:.1f}; the strip lies on the "
                      f"{side} of the grid")
                return 1
            if side in ("top", "left"):
                key_low = channel_stats(entries[0], channel)[1]
                if key_low <= band_high:
                    print(f"FAIL(e): rotation {rotation}: the first key runs "
                          f"from {key_low} on channel {channel} while the band "
                          f"reaches {band_high}; the video key crops did not "
                          f"move with the band")
                    return 1
            elif band_low <= channel_stats(entries[0], channel)[2]:
                print(f"FAIL(e): rotation {rotation}: the video band starts at "
                      f"{band_low} on channel {channel}, at or below the first "
                      f"key's {channel_stats(entries[0], channel)[2]}")
                return 1

            for entry in entries:
                entry.close()
            canvas.close()
            source.close()
        finally:
            cache.close()

    print("PASS: the video cache cuts its band from the same edge the image "
          "path does")
    return 0


def main() -> int:
    start_watchdog(180, "strip_transpose")
    controller = make_headless_controller(serial="strip-transpose",
                                          model="plus")
    try:
        rc = check_composite_size(controller)
        rc |= check_slot_rects(controller)
        rc |= check_touch_to_slot(controller)
        rc |= check_band_edge(controller)
        rc |= check_video_band_edge(controller)
    finally:
        teardown(controller)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
