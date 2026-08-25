"""The touchscreen strip band of an extended background uses the
strip_band-owned layout.

An SD+ answers the device-calibrated band. Its strip view is wider than the
key grid, so the extended canvas covers the union and the grid sits at an
offset inside it; the image path and the video-cache path must cut the same
band and the same offset key tiles. Every other touch deck keeps the derived
band, where the layout degenerates to the old grid-width canvas.
"""
import fixtures  # must be first; isolates DATA_PATH before import globals
import globals as gl

from src.backend.DeckManagement.deck_controller.strip_band import (
    band_layout, clamp_box, strip_band_geometry,
)

from PIL import Image
from StreamDeck.Devices.StreamDeckPlus import StreamDeckPlus

from faulty_fake_deck import FaultyFakeDeck

CALIBRATED = (88, 867, -4, 114)
CALIBRATED_CANVAS_W = 828


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


def grid_size(controller) -> tuple[int, int]:
    rows, cols = controller.deck.key_layout()
    key_w, key_h = controller.get_key_image_size()
    sx, sy = controller.key_spacing
    return (key_w * cols + sx * (cols - 1), key_h * rows + sy * (rows - 1))


def check_image_layout(controller) -> None:
    """The image background composes the union canvas, cuts the band from
    its box, and crops the key tiles at the grid offset."""
    from src.backend.DeckManagement.deck_controller.background_media import BackgroundImage

    grid_w, grid_h = grid_size(controller)
    canvas_w, canvas_h, grid_x, band = band_layout(controller, grid_w, grid_h)

    source = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
    background = BackgroundImage(controller, source)
    canvas = background.create_full_deck_sized_image(extend_touchscreen=True)
    assert canvas.size == (canvas_w, canvas_h), (
        f"extended canvas {canvas.size}, expected {(canvas_w, canvas_h)}")
    background.close()

    # Mark the exact band region green and key 0's grid region red on a
    # canvas-sized source, then check the strip is all green and tile 0 all
    # red: band box and grid offset both land where the layout says.
    marked = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
    marked.paste(Image.new("RGB", (band[2] - band[0], band[3] - band[1]), (0, 255, 0)),
                 (band[0], band[1]))
    key_w = controller.get_key_image_size()[0]
    marked.paste(Image.new("RGB", (key_w, key_w), (255, 0, 0)), (grid_x, 0))
    background = BackgroundImage(controller, marked)

    strip = background.get_touchscreen_image()
    strip_w, strip_h = controller.get_touchscreen_image_size()
    assert strip.size == (strip_w, strip_h)
    pixels = strip.convert("RGB")
    for p in [(0, 0), (strip_w - 1, 0), (0, strip_h - 1),
              (strip_w - 1, strip_h - 1), (strip_w // 2, strip_h // 2)]:
        px = pixels.getpixel(p)
        assert px[1] > 200 and px[0] < 55, (
            f"strip must show exactly the marked band; {p} reads {px}")

    tile0 = background.get_tiles(extend_touchscreen=True)[0].convert("RGB")
    for p in [(0, 0), (key_w - 1, 0), (key_w // 2, key_w // 2)]:
        px = tile0.getpixel(p)
        assert px[0] > 200 and px[1] < 55, (
            f"key 0 must crop at the grid offset {grid_x}; {p} reads {px}")
    background.close()


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_strip_band_geometry")

    plus = make_controller("band-plus", plus=True)
    # The calibrated span and xoff are absolute canvas pixels, so the SD+
    # grid must still be the width they were measured at. A key-spacing
    # change moves this width and needs a device recalibration; this pin
    # breaks instead of letting the band drift silently.
    grid_w, grid_h = grid_size(plus)
    assert grid_w == CALIBRATED_CANVAS_W, (
        f"SD+ grid width {grid_w} left the calibrated {CALIBRATED_CANVAS_W}; "
        f"recalibrate PLUS_BAND on the device")
    assert strip_band_geometry(plus, grid_w) == CALIBRATED, (
        f"SD+ must answer the calibrated band, got "
        f"{strip_band_geometry(plus, grid_w)}")

    # The layout covers the union: the SD+ band overhangs the grid on both
    # sides, so the canvas is span-wide and the grid sits inside it.
    canvas_w, canvas_h, grid_x, band = band_layout(plus, grid_w, grid_h)
    gap, span, xoff, band_h = CALIBRATED
    assert canvas_w == span and grid_x > 0, (
        f"band wider than grid must widen the canvas: {canvas_w=} {grid_x=}")
    assert band == (0, grid_h + gap, span, grid_h + gap + band_h), f"band box {band}"
    assert canvas_h == grid_h + gap + band_h

    # A width the calibration does not cover falls back to the derived band.
    fallback = strip_band_geometry(plus, 348)
    assert fallback != CALIBRATED and fallback[1] == 348, (
        f"an uncalibrated width must get the derived band, got {fallback}")
    # clamp_box keeps a crop inside a smaller-than-expected image.
    box = clamp_box(band, 348, 200)
    assert box[0] >= 0 and box[2] <= 348 and box[1] >= 0 and box[3] <= 200, (
        f"clamp_box must stay inside the image, got {box}")

    check_image_layout(plus)

    # The video cache snapshots the same layout.
    from src.backend.DeckManagement.Subclasses.background_video_cache import (
        BackgroundVideoCache,
    )
    video_path = fixtures.make_test_mp4(
        fixtures.DATA_DIR + "/assets/band.mp4")
    cache = BackgroundVideoCache(video_path, deck_controller=plus,
                                 extend_touchscreen=True)
    try:
        assert cache.strip_band_box == band, (
            f"video cache must snapshot the band box, got {cache.strip_band_box}")
        assert cache.grid_x == grid_x, (
            f"video cache must snapshot the grid offset, got {cache.grid_x}")
        assert cache.out_size == (canvas_w, canvas_h), (
            f"video canvas {cache.out_size}, expected {(canvas_w, canvas_h)}")
    finally:
        cache.close()
    fixtures.teardown(plus)

    # A plain touch deck keeps the derived geometry: full-width band, no
    # overhang, grid at the origin.
    plain = make_controller("band-plain", plus=False)
    p_grid_w, p_grid_h = grid_size(plain)
    sy = plain.key_spacing[1]
    strip_w, strip_h = plain.get_touchscreen_image_size()
    derived = (sy, p_grid_w, 0, round(strip_h * p_grid_w / strip_w))
    assert strip_band_geometry(plain, p_grid_w) == derived, (
        f"non-SD+ must keep the derived band, got "
        f"{strip_band_geometry(plain, p_grid_w)}")
    p_canvas_w, _p_canvas_h, p_grid_x, _p_band = band_layout(plain, p_grid_w, p_grid_h)
    assert p_canvas_w == p_grid_w and p_grid_x == 0, (
        "derived band must not widen the canvas or offset the grid")
    check_image_layout(plain)
    fixtures.teardown(plain)

    print("PASS: scenario_strip_band_geometry")


if __name__ == "__main__":
    main()
