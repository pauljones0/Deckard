"""Verify an extended background cuts its band from the edge the user sees beside the strip, over
the image path and the video cache at every rotation; a two-axis gradient tells the edges apart."""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import os

from PIL import Image

import globals as gl

from fixtures import make_headless_controller, start_watchdog, teardown

from src.backend.DeckManagement.Subclasses.background_video_cache import BackgroundVideoCache
from src.backend.DeckManagement.deck_controller.background_media import BackgroundImage

ROTATIONS = (0, 90, 180, 270)
DEVICE_STRIP = (800, 100)
# The edge the strip lies against in the user's view, per clockwise quarter turn.
BAND_SIDES = {0: "bottom", 90: "left", 180: "top", 270: "right"}


def logical_strip(rotation: int) -> "tuple[int, int]":
    return DEVICE_STRIP if rotation in (0, 180) else (DEVICE_STRIP[1], DEVICE_STRIP[0])


def turn_to(controller, rotation: int) -> bool:
    """Turn the deck and wait for the page load the turn ends in."""
    controller.set_rotation(rotation)
    return controller._input_load_done.wait(10.0)


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
    """(a) The band comes off the edge the user sees beside the strip."""
    background = controller.background
    background.extend_to_touchscreen = True

    for rotation in ROTATIONS:
        if not turn_to(controller, rotation):
            print(f"FAIL(a): rotation {rotation}: the input load did not finish")
            return 1
        source = gradient_wallpaper()
        image = BackgroundImage(controller, source)
        canvas = image.create_full_deck_sized_image(extend_touchscreen=True)
        strip = image.get_touchscreen_image()
        tiles = image.get_tiles(extend_touchscreen=True)

        if tuple(strip.size) != logical_strip(rotation):
            print(f"FAIL(a): rotation {rotation}: the band came out "
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
            print(f"FAIL(a): rotation {rotation}: the band's mean on channel "
                  f"{channel} is {band_mean:.1f} against a canvas mean of "
                  f"{canvas_mean:.1f}; the strip lies on the {side} of the "
                  f"grid, so the band comes off that edge")
            return 1

        # A top or left band moves the key grid; the first key must start past the band's far
        # edge, bezel gap included, or the crop shows a piece of the band.
        if side in ("top", "left"):
            key_low = channel_stats(tiles[0], channel)[1]
            if key_low <= band_high:
                print(f"FAIL(a): rotation {rotation}: the first key runs from "
                      f"{key_low} on channel {channel} while the band reaches "
                      f"{band_high}; the key crops did not move with the band")
                return 1
        elif band_low <= channel_stats(tiles[0], channel)[2]:
            print(f"FAIL(a): rotation {rotation}: the band starts at "
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
    """(b) The same over the video cache, which captures the rotation for itself.
    The canvas is fed in directly; the crop is under test, not the decoder."""
    import numpy as np

    background = controller.background
    background.extend_to_touchscreen = True
    video_path = fixtures.make_test_mp4(
        os.path.join(gl.DATA_PATH, "strip_band_source.mp4"))

    for rotation in ROTATIONS:
        if not turn_to(controller, rotation):
            print(f"FAIL(b): rotation {rotation}: the input load did not finish")
            return 1
        cache = BackgroundVideoCache(video_path, deck_controller=controller,
                                     extend_touchscreen=True)
        try:
            source = gradient_wallpaper()
            canvas = source.resize(cache.out_size, Image.Resampling.LANCZOS)
            # _payload_from_bgr converts back to RGB, so hand it the channels the decoder would.
            entries = cache._payload_from_bgr(np.array(canvas)[:, :, ::-1].copy())
            strip = entries[-1]

            if tuple(strip.size) != logical_strip(rotation):
                print(f"FAIL(b): rotation {rotation}: the video band came out "
                      f"{strip.size}, the strip is {logical_strip(rotation)}")
                return 1
            side = BAND_SIDES[rotation]
            channel = 1 if side in ("top", "bottom") else 0
            band_mean, band_low, band_high = channel_stats(strip, channel)
            canvas_mean = channel_stats(canvas, channel)[0]
            if (band_mean > canvas_mean) != (side in ("bottom", "right")):
                print(f"FAIL(b): rotation {rotation}: the video band's mean on "
                      f"channel {channel} is {band_mean:.1f} against a canvas "
                      f"mean of {canvas_mean:.1f}; the strip lies on the "
                      f"{side} of the grid")
                return 1
            if side in ("top", "left"):
                key_low = channel_stats(entries[0], channel)[1]
                if key_low <= band_high:
                    print(f"FAIL(b): rotation {rotation}: the first key runs "
                          f"from {key_low} on channel {channel} while the band "
                          f"reaches {band_high}; the video key crops did not "
                          f"move with the band")
                    return 1
            elif band_low <= channel_stats(entries[0], channel)[2]:
                print(f"FAIL(b): rotation {rotation}: the video band starts at "
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
    start_watchdog(180, "strip_band_edges")
    controller = make_headless_controller(serial="strip-band-edges",
                                          model="plus")
    try:
        rc = check_band_edge(controller)
        rc |= check_video_band_edge(controller)
    finally:
        teardown(controller)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
