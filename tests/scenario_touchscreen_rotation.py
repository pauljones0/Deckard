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

The composite is drawn in the frame the user sees, so on a quarter-turned
deck it is the transpose of the device's buffer and the turn expands it back
into that buffer. A last leg pins the unturned bytes against a fixture the
scenario builds itself, so the turn cannot creep into that path.

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


def make_strip(mode: str, size: "tuple[int, int]") -> Image.Image:
    """A black strip of size with a bright block in its top-left corner.

    The block keeps the strip's own aspect, so it comes out BLOCK_W by
    BLOCK_H in the device's frame whichever way the strip was turned."""
    fill = (0, 0, 0, 255) if mode == "RGBA" else (0, 0, 0)
    strip = Image.new(mode, size, fill)
    block_size = (BLOCK_W, BLOCK_H) if size[0] >= size[1] else (BLOCK_H, BLOCK_W)
    block = Image.new(mode, block_size,
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
    deck = FaultyFakeDeck(serial_number=f"strip-{mode}", model="plus")
    better = BetterDeck(deck)
    touchscreen = _StubTouchScreen(better)

    for rotation in ROTATIONS:
        better.set_rotation(rotation)
        logical_size = better.logical_touchscreen_size()
        expected_logical = (STRIP_SIZE if rotation in (0, 180)
                            else (STRIP_SIZE[1], STRIP_SIZE[0]))
        if logical_size != expected_logical:
            print(f"FAIL({mode}): rotation {rotation}: the composers draw at "
                  f"{logical_size}, expected {expected_logical}")
            return 1
        strip = make_strip(mode, logical_size)
        native = _encode_strip_native(touchscreen, strip)

        # The caller keeps its image: the window mirrors the same object.
        if strip.size != logical_size:
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

        # Where the block ends up on the device, read off a turned deck. The
        # composite's own top-left corner is the one the user sees at the top
        # left, and it comes to lie wherever the turn carries it.
        #
        # 0: nowhere, so the top-left.
        # 180: the opposite corner, moved on both axes, because the deck is
        #      upside down under the user's hand. A mirror on one axis alone
        #      puts it in the top-right or the bottom-left, which the dark
        #      checks below refuse.
        # 90: the deck was turned a quarter turn clockwise, so the composite
        #     is turned back counter-clockwise and the top-left corner swings
        #     down to the bottom-left of the device's own strip.
        # 270: the same turn the other way, so it swings up to the top-right.
        lit = {0: "top_left", 90: "bottom_left", 180: "bottom_right",
               270: "top_right"}[rotation]
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


def check_rotation_zero_bytes() -> int:
    """An unturned deck gets exactly the bytes it got before the strip
    learned to transpose.

    The fixture is built here, from the steps the encode ran before this
    scenario's subject changed: flatten an RGBA composite onto black, no
    resize because the composite is already the device's size, no turn, and
    the same JPEG settings. A turn or a resize that creeps into the
    unturned path changes these bytes.
    """
    deck = FaultyFakeDeck(serial_number="strip-identity", model="plus")
    better = BetterDeck(deck)
    better.set_rotation(0)
    touchscreen = _StubTouchScreen(better)

    strip = make_strip("RGBA", STRIP_SIZE)
    native = _encode_strip_native(touchscreen, strip)

    flattened = Image.new("RGB", strip.size, (0, 0, 0))
    flattened.paste(strip, (0, 0), strip)
    with io.BytesIO() as buf:
        flattened.save(buf, "JPEG", quality=90, subsampling=0)
        expected = buf.getvalue()
    flattened.close()
    strip.close()

    if native != expected:
        print(f"FAIL(identity): rotation 0 wrote {len(native)} bytes, the "
              f"unturned pipeline writes {len(expected)}")
        return 1

    print("PASS: an unturned deck gets byte-identical strip output")
    return 0


def main() -> int:
    start_watchdog(60, "touchscreen_rotation")
    fixtures.install_stub_globals()
    # Both composite modes: a page with transparency composites RGBA, a
    # background video hands over RGB. The RGB leg is the one that must not
    # close the caller's own image.
    rc = check_strip_pixels("RGBA")
    rc |= check_strip_pixels("RGB")
    rc |= check_rotation_zero_bytes()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
