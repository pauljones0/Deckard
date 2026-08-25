"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

The touchscreen strip band of an extended background: where the strip's view
sits relative to the key grid, and how big it is. One module owns this, so
the image, video and GIF paths all cut the same band. band_layout() is the
full extended-canvas model; on the SD+ the strip's view is WIDER than the
key grid, so the canvas covers the union of both and the grid sits at an
offset inside it.
"""
from loguru import logger as log

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController

# Device-calibrated SD+ band, in key-grid canvas pixels: (gap, span, xoff,
# band_height). gap is the bezel below the bottom key row. The strip shows a
# span-wide view of the canvas, offset xoff from centered on the key grid,
# band_height tall. span and xoff were measured with the strip's own touch
# sensor as a ruler (a dial-nulled marker aligned under a straightedge at
# each lit key edge); gap, band_height and the key spacing were tuned
# visually on a color-coded card. The strip shows slightly more than the
# grid's width, and its vertical scale is a few percent taller than the
# square-pixel derivation, so band_height is measured, not derived.
PLUS_BAND = (88, 867, -4, 114)

# The canvas width the SD+ band was calibrated at: 4 keys of 120 at the
# calibrated 116-pixel horizontal gap. PLUS_BAND's span and xoff are absolute
# canvas pixels, so they hold only at this width; strip_band_geometry checks
# it, and a key-spacing change without a recalibration falls back loudly to
# the derived band instead of silently cutting the wrong slice.
PLUS_CANVAS_WIDTH = 828


def strip_band_geometry(deck_controller: "DeckController", canvas_width: int) -> tuple[int, int, int, int]:
    """(gap, span, xoff, band_height) for deck_controller's strip band.

    An SD+ answers the device-calibrated PLUS_BAND, valid at its calibrated
    key spacing. Every other touch deck keeps the derived geometry: one
    spacing_y gap, the full canvas width, and the height that width maps to
    at the strip's aspect. A canvas width the calibration does not cover
    (a spacing change, or a dead deck's fallback key size) gets the derived
    band, behind a warning.
    """
    if getattr(deck_controller, "is_plus", False):
        if canvas_width == PLUS_CANVAS_WIDTH:
            return PLUS_BAND
        log.warning(
            f"SD+ canvas width {canvas_width} does not match the calibrated "
            f"{PLUS_CANVAS_WIDTH}; using the derived strip band. Recalibrate "
            f"PLUS_BAND after a key-spacing change."
        )
    strip_width, strip_height = deck_controller.get_touchscreen_image_size()
    return (deck_controller.key_spacing[1], canvas_width, 0,
            round(strip_height * canvas_width / strip_width))


def band_layout(deck_controller: "DeckController", grid_w: int, grid_h: int) -> tuple[int, int, int, tuple[int, int, int, int]]:
    """The extended canvas around a grid_w by grid_h key grid:
    (canvas_w, canvas_h, grid_x, band_crop_box).

    The canvas covers the union of the key grid and the strip's view. When
    the band is wider than the grid (the SD+ strip shows content beyond the
    outer key columns), the canvas grows by the overhang and the grid sits
    at grid_x inside it; key crops must add that offset. The band crop box
    is in canvas coordinates and in bounds by construction. A deck whose
    band is the derived full-grid one gets grid_x 0 and the old geometry
    unchanged.
    """
    gap, span, xoff, band_h = strip_band_geometry(deck_controller, grid_w)
    band_left = (grid_w - span) // 2 + xoff
    left_overhang = max(0, -band_left)
    right_overhang = max(0, band_left + span - grid_w)
    canvas_w = grid_w + left_overhang + right_overhang
    canvas_h = grid_h + gap + band_h
    band_x = left_overhang + band_left
    return canvas_w, canvas_h, left_overhang, (
        band_x, grid_h + gap, band_x + span, canvas_h)


def clamp_box(box: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    """box intersected with a width-by-height image, preserving size where it
    fits. Defends a crop against an image smaller than the layout expects (a
    dead deck's fallback key size); PIL would otherwise pad the out-of-bounds
    region with black.
    """
    left, top, right, bottom = box
    w = min(right - left, width)
    h = min(bottom - top, height)
    left = max(0, min(left, width - w))
    top = max(0, min(top, height - h))
    return (left, top, left + w, top + h)
