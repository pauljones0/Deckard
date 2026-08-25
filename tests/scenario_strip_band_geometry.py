"""The touchscreen strip band of an extended background uses the
strip_band-owned geometry.

An SD+ answers the device-calibrated band (the strip is anamorphic against
the keys, so the height is measured, not derived); every other touch deck
keeps the derived band. The image path and the video-cache path must cut the
same band.
"""
import fixtures  # must be first; isolates DATA_PATH before import globals
import globals as gl

from src.backend.DeckManagement.deck_controller.strip_band import band_box, strip_band_geometry

from PIL import Image
from StreamDeck.Devices.StreamDeckPlus import StreamDeckPlus

from faulty_fake_deck import FaultyFakeDeck

CALIBRATED = (40, 516, 0, 72)


class FakePlus(FaultyFakeDeck, StreamDeckPlus):
    """A FaultyFakeDeck whose type also reads as a StreamDeckPlus.

    Never instantiated: instances get here by __class__ reassignment, so no
    StreamDeckPlus constructor or transport runs.
    """


def make_controller(serial: str, plus: bool):
    fixtures._install_integration_globals()
    fixtures.seed_page("Main")
    from src.backend.DeckManagement.DeckController import DeckController

    deck = FaultyFakeDeck(serial_number=serial, deck_type="Fake Deck",
                          model="plus")
    if plus:
        deck.__class__ = FakePlus
    controller = DeckController(gl.deck_manager, deck)
    gl.deck_manager.deck_controller.append(controller)
    return controller


def check_image_band(controller, gap: int, span: int, xoff: int, band_h: int) -> None:
    """The image background composes the canvas with the band and cuts the
    strip's view out of it."""
    from src.backend.DeckManagement.deck_controller.background_media import BackgroundImage

    rows, cols = controller.deck.key_layout()
    key_w, key_h = controller.get_key_image_size()
    sx, sy = controller.key_spacing
    grid_w = key_w * cols + sx * (cols - 1)
    grid_h = key_h * rows + sy * (rows - 1)

    source = Image.new("RGB", (grid_w, grid_h + gap + band_h), (0, 0, 0))
    background = BackgroundImage(controller, source)
    canvas = background.create_full_deck_sized_image(extend_touchscreen=True)
    assert canvas.size == (grid_w, grid_h + gap + band_h), (
        f"extended canvas {canvas.size}, expected "
        f"{(grid_w, grid_h + gap + band_h)}")

    # Mark the exact band region on a fresh canvas-sized source, then check
    # the strip image is that region and nothing else.
    marked = Image.new("RGB", canvas.size, (0, 0, 0))
    left = (canvas.width - span) // 2 + xoff
    band = Image.new("RGB", (span, band_h), (0, 255, 0))
    marked.paste(band, (left, canvas.height - band_h))
    background_marked = BackgroundImage(controller, marked)
    strip = background_marked.get_touchscreen_image()
    strip_w, strip_h = controller.get_touchscreen_image_size()
    assert strip.size == (strip_w, strip_h)
    pixels = strip.convert("RGB")
    corners = [pixels.getpixel(p) for p in
               [(0, 0), (strip_w - 1, 0), (0, strip_h - 1),
                (strip_w - 1, strip_h - 1), (strip_w // 2, strip_h // 2)]]
    for px in corners:
        assert px[1] > 200 and px[0] < 55, (
            f"strip must show exactly the marked band; corner/center reads {px}"
        )
    background.close()
    background_marked.close()


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_strip_band_geometry")

    plus = make_controller("band-plus", plus=True)
    # The calibrated span and xoff are absolute canvas pixels, so the SD+
    # canvas must still be the width they were measured at. A key-spacing
    # change moves this width and needs a device recalibration; this pin
    # breaks instead of letting the band drift silently.
    key_w = plus.get_key_image_size()[0]
    cols = plus.deck.key_layout()[1]
    plus_canvas_w = key_w * cols + plus.key_spacing[0] * (cols - 1)
    assert plus_canvas_w == 540, (
        f"SD+ canvas width {plus_canvas_w} left the calibrated 540; "
        f"recalibrate PLUS_BAND on the device")
    assert strip_band_geometry(plus, 540) == CALIBRATED, (
        f"SD+ must answer the calibrated band, got "
        f"{strip_band_geometry(plus, 540)}")
    # A width the calibration does not cover falls back to the derived band.
    fallback = strip_band_geometry(plus, 348)
    assert fallback != CALIBRATED and fallback[1] == 348, (
        f"an uncalibrated width must get the derived band, got {fallback}")
    # The crop box clamps inside a canvas smaller than the band expects.
    box = band_box(348, 200, CALIBRATED)
    assert box[0] >= 0 and box[2] <= 348 and box[1] >= 0 and box[3] <= 200, (
        f"band_box must stay inside the canvas, got {box}")
    check_image_band(plus, *CALIBRATED)

    # The video cache snapshots the same band.
    from src.backend.DeckManagement.Subclasses.background_video_cache import (
        BackgroundVideoCache,
    )
    video_path = fixtures.make_test_mp4(
        fixtures.DATA_DIR + "/assets/band.mp4")
    cache = BackgroundVideoCache(video_path, deck_controller=plus,
                                 extend_touchscreen=True)
    try:
        assert cache.strip_band == CALIBRATED, (
            f"video cache must snapshot the calibrated band, got {cache.strip_band}")
        gap, _span, _xoff, band_h = CALIBRATED
        rows, cols = plus.deck.key_layout()
        key_w, key_h = plus.get_key_image_size()
        sx, sy = plus.key_spacing
        expected_h = key_h * rows + sy * (rows - 1) + gap + band_h
        assert cache.out_size[1] == expected_h, (
            f"video canvas height {cache.out_size[1]}, expected {expected_h}")
    finally:
        cache.close()
    fixtures.teardown(plus)

    # A plain touch deck keeps the derived geometry.
    plain = make_controller("band-plain", plus=False)
    sy = plain.key_spacing[1]
    strip_w, strip_h = plain.get_touchscreen_image_size()
    derived = (sy, 540, 0, round(strip_h * 540 / strip_w))
    assert strip_band_geometry(plain, 540) == derived, (
        f"non-SD+ must keep the derived band, got "
        f"{strip_band_geometry(plain, 540)}")
    plain_canvas_w = (plain.get_key_image_size()[0] * plain.deck.key_layout()[1]
                      + plain.key_spacing[0] * (plain.deck.key_layout()[1] - 1))
    check_image_band(plain, *strip_band_geometry(plain, plain_canvas_w))
    fixtures.teardown(plain)

    print("PASS: scenario_strip_band_geometry")


if __name__ == "__main__":
    main()
