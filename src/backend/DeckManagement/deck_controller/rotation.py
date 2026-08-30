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

What a deck controller must redo when its deck is turned.

BetterDeck owns the rotation itself and every mapping that follows from it.
What it cannot own is the controller's own furniture, which is built from the
layout the rotation decides: the input set, the encoded image caches and the
window's key grid. This module is that half, kept out of the controller so
the ordering it depends on reads in one place.

The order matters and it is this. The wrapper is turned first, so every map
below answers for the new orientation. The input set is rebuilt next, because
at 90 and 270 the grid transposes and every identifier and key index the old
set carries names a position the deck no longer has. The window's grid is
rebuilt after that, so it is built from the new inputs. The page loads last,
onto furniture that is already correct.

The rebuild also settles the repaint. A key's present state holds the hash of
what its slot shows, and a fresh key carries none, so the page load below
writes every key even where the composite is unchanged in pixels. That write
is the point: the same picture belongs on a different key once the deck is
turned, and a kept hash would skip exactly the write that moves it.

A turn under a showing screensaver applies all of the above and repaints
nothing. load_page() records the page and returns while the saver owns the
deck, and hide() loads it then. The new layout is in place from this point
on, so the page that hide() loads reaches the turned deck.
"""
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.deck_controller.media_writer import (
    ReleaseStashedInputsMsg,
)
from src.backend import ui_port

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController


def apply_rotation(controller: "DeckController", value: int) -> None:
    """Set rotation and rebuild orientation-owned state on the main loop."""
    controller.deck.set_rotation(value)
    # Rotation in native cache keys prevents stale reads; clear dead old-orientation entries.
    controller.clear_encoded_key_caches()

    # Swap under the page lock so no load observes a partial input set.
    # Call load_page outside because it takes the same lock and marshals plugin work.
    with controller._load_page_lock:
        _swap_input_set(controller)

    # Rebuild the window synchronously from the published inputs before page repaint.
    ui_port.get().on_deck_layout_changed(controller)

    if not controller.get_alive():
        return
    controller.load_page(controller.active_page)


def _swap_input_set(controller: "DeckController") -> None:
    """Publish a complete new input set, cancel retired gestures, and defer release to the media thread.
    The sole writer must serialize release with any tick still rendering retired media."""
    retired = controller.inputs
    for key in retired.get(Input.Key, []):
        key.cancel_gesture()
    for dial in retired.get(Input.Dial, []):
        dial.cancel_gesture()

    # Drop old-grid window markers because quarter-turn coordinates can exceed the new array.
    # The following page load repaints the complete new grid.
    controller.ui_image_changes_while_hidden.clear()

    # init_inputs builds then swaps, so the concurrent media writer sees the
    # old complete set or the new complete one, never a partial one.
    controller.init_inputs()

    if retired:
        controller.media_player.submit_control(ReleaseStashedInputsMsg(retired))
