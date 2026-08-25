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

Which way round the touch strip lies, in the frame the user looks at.

Two questions live here, and neither measures anything. How far the band
reaches and where the key grid sits inside the extended canvas belong to
strip_band, which answers them from the device-calibrated geometry. This
module takes that answer whole and turns it.

The first question is which edge the band lies against. strip_band lays the
canvas out the way the device holds it, with the band along the bottom. A
deck the user turned shows that same canvas turned: a half turn puts the
strip above the grid, a quarter turn clockwise puts it to the left, and a
quarter turn the other way to the right. Cutting the band off the wrong edge
is not a wrong pixel here and there; it is a different part of the
wallpaper, so the band stops continuing the picture the keys show.

The second is how the dial slots divide the strip. They divide the long axis
of the strip as the user sees it, which is the horizontal one on an upright
deck and the vertical one on a quarter-turned deck. Which end of that axis
dial 0 takes follows the turn: clockwise brings the end that held dial 0 to
the top, counter-clockwise brings it to the bottom.

Every caller states a rotation once and takes the geometry from here, so the
background crop, the slot the composite draws and the slot a touch lands in
cannot drift apart.
"""
from dataclasses import dataclass

from typing import TYPE_CHECKING, Literal

from src.backend.DeckManagement.deck_controller.strip_band import band_layout

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController

# The edge of the extended canvas the strip lies against, per quarter turn
# the user gave the deck, clockwise. See BetterDeck's rotation notes: at 90
# what sat below the grid comes to lie to its left.
STRIP_SIDES: "dict[int, str]" = {0: "bottom", 90: "left", 180: "top", 270: "right"}

# How the dial slots divide the strip the user sees. "x" spreads them across
# an upright strip, left to right in dial order. "y-down" stacks them down a
# strip standing on its side, dial 0 at the top; "y-up" stacks them the other
# way, dial 0 at the bottom.
SlotOrder = Literal["x", "y-down", "y-up"]


@dataclass(frozen=True)
class StripBand:
    """The canvas an extended background is fitted to, and what sits where in
    it, in the frame the user sees.

    canvas_size is the whole canvas, grid and strip band together.
    key_origin is where the key grid starts inside it. It is not the origin
    when the band takes the top or the left edge, and it is not the origin
    on a deck whose band reaches past the outer key columns either.
    box is the band's own crop box, in the same coordinates.
    """
    canvas_size: "tuple[int, int]"
    key_origin: "tuple[int, int]"
    box: "tuple[int, int, int, int]"


def flat_band(grid_size: "tuple[int, int]") -> StripBand:
    """The canvas of a background that does not reach the strip: the key grid
    alone, at the origin, with no band."""
    return StripBand(grid_size, (0, 0), (0, 0, 0, 0))


def oriented_band(deck_controller: "DeckController", rotation: int,
                  grid_size: "tuple[int, int]") -> StripBand:
    """The extended canvas for a key grid of grid_size with the strip beside
    it, turned into the frame the user sees.

    grid_size is the key grid alone, in canvas pixels, as the user sees it:
    key_layout() already turns with the deck, so the grid stands on its side
    at 90 and 270. strip_band lays the canvas out the way the device holds
    it, and its calibration is stated at the device's own grid width, so the
    grid goes back to the device's frame for that call and the whole layout
    is turned back here afterwards.

    Nothing here re-derives or re-checks the band. strip_band answers a band
    for every width, loudly for one it was not calibrated at, and this turns
    whatever it answered.
    """
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
        # A quarter turn clockwise: (x, y) lands at (canvas_h - y, x), so the
        # bottom edge comes to the left one and the canvas stands on its side.
        return StripBand(
            (canvas_h, canvas_w),
            (canvas_h - grid_h, grid_x),
            (canvas_h - bottom, left, canvas_h - top, right))
    if rotation == 270:
        # A quarter turn the other way: (x, y) lands at (y, canvas_w - x).
        return StripBand(
            (canvas_h, canvas_w),
            (0, canvas_w - grid_x - grid_w),
            (top, canvas_w - right, bottom, canvas_w - left))
    return StripBand((canvas_w, canvas_h), (grid_x, 0), box)


def dial_slot_box(dial_index: int, n_dials: int, strip_size: "tuple[int, int]",
                  order: SlotOrder) -> "tuple[int, int, int, int]":
    """The box dial_index's slot takes in a strip of strip_size, in the
    user's frame. Slots divide the long axis in equal parts and each spans
    the short one."""
    width, height = strip_size
    if order == "x":
        return (int(dial_index * width / n_dials), 0,
                int((dial_index + 1) * width / n_dials), height)
    slot = dial_index if order == "y-down" else n_dials - 1 - dial_index
    return (0, int(slot * height / n_dials),
            width, int((slot + 1) * height / n_dials))


def dial_slot_at(value: "dict[str, int]", n_dials: int,
                 strip_size: "tuple[int, int]", order: SlotOrder) -> int:
    """Which dial's slot a touch at value landed in, or -1 for a touch that
    is not on the strip at all.

    The library clamps nothing and the device can report a position past the
    end, so a position off the strip on either axis answers -1 rather than
    the slot the arithmetic on the other axis would fold it into.
    """
    width, height = strip_size
    if not 0 <= value.get("x", -1) < width or not 0 <= value.get("y", -1) < height:
        return -1
    if order == "x":
        position, extent = value["x"], width
    else:
        position, extent = value["y"], height
    slot = int(position / extent * n_dials)
    return slot if order != "y-up" else n_dials - 1 - slot
