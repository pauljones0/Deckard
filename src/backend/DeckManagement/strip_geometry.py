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

The strip band layout and the dial slots, turned into the frame the user sees.
strip_band measures the band in the device's frame; every caller turns it here, so none drifts.
"""
from dataclasses import dataclass

from typing import TYPE_CHECKING, Literal

from src.backend.DeckManagement.deck_controller.strip_band import band_layout

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController

# The canvas edge the strip lies against, per clockwise quarter turn of the deck.
STRIP_SIDES: "dict[int, str]" = {0: "bottom", 90: "left", 180: "top", 270: "right"}

# How the dial slots divide the strip: "x" across it in dial order, "y-down" stacked with
# dial 0 at the top, "y-up" with dial 0 at the bottom.
SlotOrder = Literal["x", "y-down", "y-up"]


@dataclass(frozen=True)
class StripBand:
    """The extended canvas in the frame the user sees: size, key grid origin, and band crop box.
    The grid starts off the corner when the band overhangs it or takes the top or the left edge."""
    canvas_size: "tuple[int, int]"
    key_origin: "tuple[int, int]"
    box: "tuple[int, int, int, int]"


def flat_band(grid_size: "tuple[int, int]") -> StripBand:
    """The canvas of a background that does not reach the strip: the key grid alone, no band."""
    return StripBand(grid_size, (0, 0), (0, 0, 0, 0))


def oriented_band(deck_controller: "DeckController", rotation: int,
                  grid_size: "tuple[int, int]") -> StripBand:
    """The extended canvas for a turned grid_size, in the frame the user sees.
    strip_band is calibrated at the device's grid width, so the grid turns back for that call."""
    turned = rotation in (90, 270)
    grid_w, grid_h = (grid_size[1], grid_size[0]) if turned else grid_size
    canvas_w, canvas_h, grid_x, box = band_layout(deck_controller, grid_w, grid_h)
    left, top, right, bottom = box

    if rotation == 180:
        # A half turn mirrors both axes, and the band comes to the top.
        return StripBand(
            (canvas_w, canvas_h),
            (canvas_w - grid_x - grid_w, canvas_h - grid_h),
            (canvas_w - right, canvas_h - bottom, canvas_w - left, canvas_h - top))
    if rotation == 90:
        # A quarter turn clockwise: (x, y) lands at (canvas_h - y, x); the band comes to the left.
        return StripBand(
            (canvas_h, canvas_w),
            (canvas_h - grid_h, grid_x),
            (canvas_h - bottom, left, canvas_h - top, right))
    if rotation == 270:
        # The other quarter turn: (x, y) lands at (y, canvas_w - x); the band comes to the right.
        return StripBand(
            (canvas_h, canvas_w),
            (0, canvas_w - grid_x - grid_w),
            (top, canvas_w - right, bottom, canvas_w - left))
    return StripBand((canvas_w, canvas_h), (grid_x, 0), box)


def dial_slot_box(dial_index: int, n_dials: int, strip_size: "tuple[int, int]",
                  order: SlotOrder) -> "tuple[int, int, int, int]":
    """The box of dial_index's slot in a strip of strip_size, in the user's frame.
    Slots divide the long axis in equal parts and each spans the short one."""
    width, height = strip_size
    if order == "x":
        return (int(dial_index * width / n_dials), 0,
                int((dial_index + 1) * width / n_dials), height)
    slot = dial_index if order == "y-down" else n_dials - 1 - dial_index
    return (0, int(slot * height / n_dials),
            width, int((slot + 1) * height / n_dials))


def dial_slot_at(value: "dict[str, int]", n_dials: int,
                 strip_size: "tuple[int, int]", order: SlotOrder) -> int:
    """The dial whose slot a touch at value landed in, or -1 off the strip.
    The device can report a position past the end, so either axis off the strip answers -1."""
    width, height = strip_size
    if not 0 <= value.get("x", -1) < width or not 0 <= value.get("y", -1) < height:
        return -1
    if order == "x":
        position, extent = value["x"], width
    else:
        position, extent = value["y"], height
    slot = int(position / extent * n_dials)
    return slot if order != "y-up" else n_dials - 1 - slot
