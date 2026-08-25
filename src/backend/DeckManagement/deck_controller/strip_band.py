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
sits on the key-grid canvas, and how big it is. One module owns this, so the
image path and the video-cache path cut the same band.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController

# Device-calibrated SD+ band, in key-grid canvas pixels at the calibrated
# key spacing: (gap, span, xoff, band_height). gap is the bezel below the
# bottom key row. The strip shows a span-wide view of the canvas, offset
# xoff from centered on the key grid, band_height tall. The strip is
# anamorphic against the keys, so band_height is measured on the device,
# not derived from the strip's own 800:100 aspect.
PLUS_BAND = (40, 516, 0, 72)


def strip_band_geometry(deck_controller: "DeckController", canvas_width: int) -> tuple[int, int, int, int]:
    """(gap, span, xoff, band_height) for deck_controller's strip band.

    An SD+ answers the device-calibrated PLUS_BAND, valid at its calibrated
    key spacing. Every other touch deck keeps the derived geometry: one
    spacing_y gap, the full canvas width, and the height that width maps to
    at the strip's aspect.
    """
    if deck_controller.is_plus:
        return PLUS_BAND
    strip_width, strip_height = deck_controller.get_touchscreen_image_size()
    return (deck_controller.key_spacing[1], canvas_width, 0,
            round(strip_height * canvas_width / strip_width))
