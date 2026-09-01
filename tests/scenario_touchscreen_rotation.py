"""Verify native strip orientation with a non-symmetric 800 by 100 image and
loss-tolerant corner brightness checks."""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import io

from PIL import Image

from fixtures import FaultyFakeDeck, start_watchdog

from src.backend.DeckManagement.BetterDeck import BetterDeck
from src.backend.DeckManagement.deck_controller.media_writer import encode_native_touchscreen
from src.backend.DeckManagement.deck_controller.native_encode import _encode_strip_native

ROTATIONS = (0, 90, 180, 270)
STRIP_SIZE = (800, 100)
# A short block distinguishes a half turn from a horizontal mirror.
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
    The block follows the strip's aspect, so it is BLOCK_W by BLOCK_H in the device's frame."""
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

        # Where the composite's top-left corner lands on the device; the dark checks below
        # refuse a one-axis mirror, which would land top-right or bottom-left at 180.
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
    """An unturned deck gets the bytes of a plain flatten-and-save, with no resize and no turn.
    A turn or a resize that creeps into the unturned path changes these bytes."""
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


def check_mid_turn_straggler() -> int:
    """A composite shaped for the other orientation encodes to empty bytes.
    Each drop site reads one turn, so a rotation between compose and encode drops the frame."""
    deck = BetterDeck(FaultyFakeDeck(serial_number="rot-straggler", model="plus"))
    touchscreen = _StubTouchScreen(deck)
    tall_size = (STRIP_SIZE[1], STRIP_SIZE[0])

    deck.set_rotation(0)
    tall = Image.new("RGB", tall_size, (0, 0, 0))
    outer = _encode_strip_native(touchscreen, tall)
    inner = encode_native_touchscreen(deck, tall)
    tall.close()
    if outer != b"" or inner != b"":
        print(f"FAIL(straggler): rotation 0 encoded a {tall_size} composite "
              f"to {len(outer)} and {len(inner)} bytes, expected empty at "
              f"both drop sites")
        return 1

    deck.set_rotation(90)
    wide = Image.new("RGB", STRIP_SIZE, (0, 0, 0))
    outer = _encode_strip_native(touchscreen, wide)
    inner = encode_native_touchscreen(deck, wide)
    wide.close()
    if outer != b"" or inner != b"":
        print(f"FAIL(straggler): rotation 90 encoded a {STRIP_SIZE} composite "
              f"to {len(outer)} and {len(inner)} bytes, expected empty at "
              f"both drop sites")
        return 1

    print("PASS: a composite shaped for the other orientation drops as empty "
          "bytes")
    return 0


def main() -> int:
    start_watchdog(60, "touchscreen_rotation")
    fixtures.install_stub_globals()
    # Cover transparent RGBA composites and RGB video frames.
    rc = check_strip_pixels("RGBA")
    rc |= check_strip_pixels("RGB")
    rc |= check_rotation_zero_bytes()
    rc |= check_mid_turn_straggler()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
