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

# SD+ calibrated band in key-grid pixels: gap, span, center offset, and height.
# Span, offset, gap, and height are measured because the strip exceeds derived grid geometry.
PLUS_BAND = (88, 867, -4, 114)

# PLUS_BAND uses absolute pixels valid only at this calibrated four-key width.
# A width mismatch warns and falls back to derived geometry.
PLUS_CANVAS_WIDTH = 828


def strip_band_geometry(deck_controller: "DeckController", canvas_width: int) -> tuple[int, int, int, int]:
    """Return strip gap, span, offset, and height for the canvas.
    Use SD+ calibration only at its exact width; otherwise warn and derive geometry."""
    if getattr(deck_controller, "is_plus", False):
        if canvas_width == PLUS_CANVAS_WIDTH:
            return PLUS_BAND
        log.warning(
            f"SD+ canvas width {canvas_width} does not match the calibrated "
            f"{PLUS_CANVAS_WIDTH}; using the derived strip band. Recalibrate "
            f"PLUS_BAND after a key-spacing change."
        )
    # The device's own strip size, not the logical one: the band is laid out
    # the way the device holds the deck, and the caller turns the finished
    # layout into the frame the user sees.
    strip_width, strip_height = deck_controller.device_touchscreen_image_size()
    return (deck_controller.key_spacing[1], canvas_width, 0,
            round(strip_height * canvas_width / strip_width))


def band_layout(deck_controller: "DeckController", grid_w: int, grid_h: int) -> tuple[int, int, int, tuple[int, int, int, int]]:
    """Return extended canvas size, grid offset, and in-bounds strip crop box.
    The canvas covers grid and band union; key crops must include any left overhang."""
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
    """Clamp a crop box into the image while preserving its size where possible.
    This prevents PIL black padding when fallback images are smaller than layout."""
    left, top, right, bottom = box
    w = min(right - left, width)
    h = min(bottom - top, height)
    left = max(0, min(left, width - w))
    top = max(0, min(top, height - h))
    return (left, top, left + w, top + h)
