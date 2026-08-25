"""The strip composite reaches the device in the device's orientation.

The keys are turned on the producer side, in _to_rotated_rgb, before the
JPEG encode. The strip is turned in the same place, in _encode_strip_native,
so what the media thread writes is already what the device expects and no
consumer below it holds a second copy of the rule.

The legs encode a strip with a bright block in one corner, decode the bytes
back and ask which of the four corners the block came out in. The block is
shorter than the strip is tall, so a half turn moves it on both axes: a
mirror on one axis alone lands it in a corner the checks call wrong. JPEG is
lossy, so each leg compares corner means with a wide margin instead of exact
pixels.

Deck shape, stated once so a configurable fake deck can adopt it later: an
800 by 100 strip, which is the Stream Deck + shape the fake deck models.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import io

from PIL import Image

from fixtures import FaultyFakeDeck, start_watchdog

from src.backend.DeckManagement.BetterDeck import BetterDeck
from src.backend.DeckManagement.deck_controller.native_encode import _encode_strip_native

ROTATIONS = (0, 90, 180, 270)
STRIP_SIZE = (800, 100)
# The bright block, and the corner box each leg averages over. The box is the
# block, so a correct turn puts the whole block inside exactly one box. The
# block is deliberately not as tall as the strip: a block of full height
# cannot tell a half turn from a left-to-right mirror.
BLOCK_W, BLOCK_H = 100, 40


class _StubController:
    """The one field _encode_strip_native reads off a controller."""

    def __init__(self, deck: BetterDeck) -> None:
        self.deck = deck


class _StubTouchScreen:
    """The one field _encode_strip_native reads off a touchscreen input."""

    def __init__(self, deck: BetterDeck) -> None:
        self.deck_controller = _StubController(deck)


def make_strip(mode: str) -> Image.Image:
    """A black strip with a bright block in its top-left corner."""
    fill = (0, 0, 0, 255) if mode == "RGBA" else (0, 0, 0)
    strip = Image.new(mode, STRIP_SIZE, fill)
    block = Image.new(mode, (BLOCK_W, BLOCK_H),
                      (255, 255, 255, 255) if mode == "RGBA" else (255, 255, 255))
    strip.paste(block, (0, 0))
    block.close()
    return strip


def corner_means(image: Image.Image) -> "dict[str, float]":
    """Mean brightness of each of the four corner boxes of a decoded strip."""
    width, height = image.size
    grey = image.convert("L")
    boxes = {
        "top_left": (0, 0, BLOCK_W, BLOCK_H),
        "top_right": (width - BLOCK_W, 0, width, BLOCK_H),
        "bottom_left": (0, height - BLOCK_H, BLOCK_W, height),
        "bottom_right": (width - BLOCK_W, height - BLOCK_H, width, height),
    }
    means = {}
    for name, box in boxes.items():
        region = grey.crop(box)
        pixels = list(region.getdata())
        means[name] = sum(pixels) / len(pixels)
        region.close()
    grey.close()
    return means


def check_strip_pixels(mode: str) -> int:
    deck = FaultyFakeDeck(serial_number=f"strip-{mode}")
    better = BetterDeck(deck)
    touchscreen = _StubTouchScreen(better)

    for rotation in ROTATIONS:
        better.set_rotation(rotation)
        strip = make_strip(mode)
        native = _encode_strip_native(touchscreen, strip)

        # The caller keeps its image: the window mirrors the same object.
        if strip.size != STRIP_SIZE:
            print(f"FAIL({mode}): rotation {rotation} resized the caller's "
                  f"image to {strip.size}")
            return 1
        if strip.getpixel((10, 10))[:3] != (255, 255, 255):
            print(f"FAIL({mode}): rotation {rotation} edited the caller's "
                  f"image")
            return 1

        with io.BytesIO(native) as buf:
            decoded = Image.open(buf)
            decoded.load()
        if decoded.size != STRIP_SIZE:
            print(f"FAIL({mode}): rotation {rotation} wrote a {decoded.size} "
                  f"strip; the device buffer is {STRIP_SIZE}")
            return 1

        means = corner_means(decoded)
        decoded.close()
        strip.close()

        # At 180 the block belongs in the opposite corner of the device's own
        # strip, moved on both axes, because the deck is upside down under
        # the user's hand. A mirror on one axis alone puts it in the
        # top-right or the bottom-left, which the dark checks below refuse.
        # Everywhere else the composite goes to the device as it was drawn.
        lit = "bottom_right" if rotation == 180 else "top_left"
        if not means[lit] > 200:
            print(f"FAIL({mode}): rotation {rotation}: the block should sit "
                  f"in the {lit} of the written strip, mean {means[lit]:.1f} "
                  f"(corners {means})")
            return 1
        for dark in (name for name in means if name != lit):
            if not means[dark] < 55:
                print(f"FAIL({mode}): rotation {rotation}: the {dark} of the "
                      f"written strip should be black, mean "
                      f"{means[dark]:.1f} (corners {means})")
                return 1

    print(f"PASS: a {mode} strip composite reaches the device turned for the "
          f"deck's orientation")
    return 0


def main() -> int:
    start_watchdog(60, "touchscreen_rotation")
    fixtures.install_stub_globals()
    # Both composite modes: a page with transparency composites RGBA, a
    # background video hands over RGB. The RGB leg is the one that must not
    # close the caller's own image.
    rc = check_strip_pixels("RGBA")
    rc |= check_strip_pixels("RGB")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
