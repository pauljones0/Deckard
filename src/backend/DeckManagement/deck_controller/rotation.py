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
    """Turn the deck to value degrees and rebuild what the old orientation
    owned. DeckController.set_rotation is the only caller, and it runs on the
    main loop."""
    controller.deck.set_rotation(value)
    # Both native cache keys hold the rotation, so nothing stale can be
    # served. This clear is memory hygiene, because every entry encoded for
    # the old rotation is dead as soon as the rotation changes.
    controller.clear_encoded_key_caches()

    # Under the page lock, so a page load cannot interleave with the swap and
    # load a page into a set that is half replaced. The page load below runs
    # outside it: load_page takes the same lock itself, and its plugin-facing
    # tail marshals onto the main loop.
    with controller._load_page_lock:
        _swap_input_set(controller)

    # The window rebuilds its key grid for the new geometry, from the input
    # set the swap just published. This is synchronous on the main loop,
    # where the only caller runs, so the load below repaints into the new
    # grid and not the transposed old one.
    ui_port.get().on_deck_layout_changed(controller)

    if not controller.get_alive():
        return
    controller.load_page(controller.active_page)


def _swap_input_set(controller: "DeckController") -> None:
    """Publish a fresh input set for the new layout and retire the old one.

    This is the pattern ScreenSaver.show() uses to confiscate an input set,
    for the same reasons. A gesture in flight dies with the retired input:
    the physical release lands on the replacement, so a retired key's hold
    timer would otherwise fire into its pinned down-time snapshot after the
    finger left, and pin that page's action objects for good. The touchscreen
    keeps no gesture state, because its events arrive pre-classified and
    single-shot.

    The retired set is released on the media thread as a control message, and
    never closed here. A tick that began just before the swap still renders
    against the retired objects, through the key images and videos they hold,
    so the sole writer is what serializes the release against that render. A
    control message carries no page affinity, so the page load that follows
    cannot drop it.
    """
    retired = controller.inputs
    for key in retired.get(Input.Key, []):
        key.cancel_gesture()
    for dial in retired.get(Input.Dial, []):
        dial.cancel_gesture()

    # init_inputs builds then swaps, so the concurrent media writer sees the
    # old complete set or the new complete one, never a partial one.
    controller.init_inputs()

    if retired:
        controller.media_player.submit_control(ReleaseStashedInputsMsg(retired))
