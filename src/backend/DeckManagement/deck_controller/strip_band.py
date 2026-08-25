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
image, video and GIF paths all cut the same band.
"""
from loguru import logger as log

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController

# Device-calibrated SD+ band, in key-grid canvas pixels: (gap, span, xoff,
# band_height). gap is the bezel below the bottom key row. The strip shows a
# span-wide view of the canvas, offset xoff from centered on the key grid,
# band_height tall. The strip is anamorphic against the keys, so band_height
# is measured on the device, not derived from the strip's own 800:100 aspect.
PLUS_BAND = (40, 516, 0, 72)

# The canvas width the SD+ band was calibrated at: 4 keys of 120 at the
# calibrated 20-pixel horizontal gap. PLUS_BAND's span and xoff are absolute
# canvas pixels, so they hold only at this width; strip_band_geometry checks
# it, and a key-spacing change without a recalibration falls back loudly to
# the derived band instead of silently cutting the wrong slice.
PLUS_CANVAS_WIDTH = 540


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


def band_box(canvas_width: int, canvas_height: int,
             band: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """The band's crop box on the extended canvas, clamped inside it.

    The clamp keeps the crop in bounds when the canvas is smaller than the
    band expects (a dead deck's fallback key size); PIL would otherwise pad
    the out-of-bounds region with black instead of raising.
    """
    _gap, span, xoff, band_height = band
    span = min(span, canvas_width)
    band_height = min(band_height, canvas_height)
    left = (canvas_width - span) // 2 + xoff
    left = max(0, min(left, canvas_width - span))
    return (left, canvas_height - band_height, left + span, canvas_height)
