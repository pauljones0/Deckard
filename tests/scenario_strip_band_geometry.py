"""Use strip_band layout for extended touchscreen backgrounds.

SD+ uses calibrated union geometry; other touch decks use the derived grid width.
"""
import fixtures  # must be first; isolates DATA_PATH before import globals
import globals as gl

from src.backend.DeckManagement.deck_controller.strip_band import (
    band_layout, clamp_box, strip_band_geometry,
)
from src.backend.DeckManagement.strip_geometry import oriented_band

from PIL import Image
from StreamDeck.Devices.StreamDeckPlus import StreamDeckPlus

from faulty_fake_deck import FaultyFakeDeck

CALIBRATED = (88, 867, -4, 114)
CALIBRATED_CANVAS_W = 828


class FakePlus(FaultyFakeDeck, StreamDeckPlus):
    """A FaultyFakeDeck that also has StreamDeckPlus type identity.

    __class__ reassignment avoids the StreamDeckPlus constructor and transport.
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

    # Mark the band green and key 0 red to verify band and grid offsets.
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
    # Calibration requires the measured SD+ grid width.
    # Any spacing change requires device recalibration instead of silent drift.
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

    # The rotation layer turns this layout and owns nothing of it. At
    # rotation 0 it must hand the measured answer straight back, offset and
    # box included, so an unturned deck composes exactly what strip_band
    # laid out. This asserts through band_layout rather than restating its
    # numbers, so a recalibration moves both sides at once.
    unturned = oriented_band(plus, 0, (grid_w, grid_h))
    assert unturned.canvas_size == (canvas_w, canvas_h), unturned
    assert unturned.key_origin == (grid_x, 0), unturned
    assert unturned.box == band, unturned

    # Every quarter turn is a rigid turn of that same layout: the canvas
    # transposes, the grid keeps its size in the user's frame, and the band
    # keeps its own, so no rotation may quietly resize or re-derive it.
    for rotation in (90, 180, 270):
        swapped = rotation in (90, 270)
        # key_layout() turns with the deck, so the caller's grid is the one
        # the user sees: the transpose of the device's at the quarter turns.
        turned = oriented_band(plus, rotation,
                               (grid_h, grid_w) if swapped else (grid_w, grid_h))
        assert turned.canvas_size == ((canvas_h, canvas_w) if swapped
                                      else (canvas_w, canvas_h)), (rotation, turned)
        box_w, box_h = band[2] - band[0], band[3] - band[1]
        t_w, t_h = turned.box[2] - turned.box[0], turned.box[3] - turned.box[1]
        assert (t_w, t_h) == ((box_h, box_w) if swapped else (box_w, box_h)), (
            rotation, turned)
        # The band lies on the edge the user sees it against: left at 90,
        # top at 180, right at 270.
        edge = {90: turned.box[0] == 0, 180: turned.box[1] == 0,
                270: turned.box[2] == turned.canvas_size[0]}[rotation]
        assert edge, f"rotation {rotation} put the band at {turned.box}"
        # The grid origin is pinned exactly, expressed through band_layout's
        # own outputs so a recalibration moves both sides. A fit-on-canvas
        # bound alone leaves slack that lets a dropped grid_x overhang pass,
        # which misaligns every key crop along the strip's long axis.
        expected_origin = {
            90: (canvas_h - grid_h, grid_x),
            180: (canvas_w - grid_x - grid_w, canvas_h - grid_h),
            270: (0, canvas_w - grid_x - grid_w),
        }[rotation]
        assert turned.key_origin == expected_origin, (
            f"rotation {rotation}: key_origin {turned.key_origin}, "
            f"expected {expected_origin}")

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
        assert cache.band is not None and cache.band.box == band, (
            f"video cache must snapshot the band box, got {cache.band}")
        assert cache.band.key_origin == (grid_x, 0), (
            f"video cache must snapshot the grid origin, got {cache.band.key_origin}")
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
