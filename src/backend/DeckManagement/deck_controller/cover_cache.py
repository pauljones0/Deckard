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

The covered-key fast path: what a key paints while its own foreground hides
the tile behind it.

A full-size opaque icon over an animated background is a common page shape,
and it is the worst one for the render loop. The background moves, so the key
composites every frame; the icon hides every pixel the background changed, so
every one of those composites returns the same picture, and the hash de-dup
throws it away after the work is done. This module keeps that picture instead,
and the next frame reuses it.

Two things decide whether the last composite may be reused. The first is the
foreground entry, which LayoutManager publishes through
get_covering_foreground() only when the paste it made hides the whole
background. Its layout key pins the asset, the backing image, the alignment,
the composed size and the tile geometry, so holding that entry and comparing
it by identity stands for all of them. The second is a stamp of what the entry
cannot be trusted for: the label epoch, which moves whenever a label changes;
the composed layout, which a layout edit moves with no composite to re-key the
entry behind it; and the composed background colour, which the foreground
provably hides and which is stamped anyway so a change to the colour layer
cannot outlive that proof.

Everything else is a gate rather than a stamp field, which means a key in that
condition composites as it always did and caches nothing: a press, a warning
point, an overlay, a video, a rolling label, or no static media at all. Each
of those either animates or is short-lived, so caching it buys nothing, and
keeping them out of the stamp keeps the reuse decision to two comparisons.

The known edge is a state change that lands between a composite and the store
that keeps it, which stamps a picture with a state it does not show. It is
bounded: the next change to any stamp field drops the entry, the offer path
still judges every frame against what the device holds, and the fields left in
the stamp move on edit paths only, never per frame. The gates take the press
and the warning point, the two that a device event can flip under a running
composite, out of that window entirely.

The reuse path is a hash lookup and a device offer, so a covered key costs the
same as a passthrough key over a video background: no composite, no encode and
no write while the picture holds.
"""
from copy import copy

from PIL import Image

from src.backend.DeckManagement.Subclasses.media_pipeline_profiler import media_prof
from src.backend.DeckManagement.deck_controller.native_encode import _encode_key_native

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerKey
    from src.backend.DeckManagement.deck_controller.input_state_classes import ControllerKeyState
    from src.backend.PageManagement.Page import Page


class _Covered:
    """One kept composite, with the two values that say it is still current
    and the hash the paint path judges it by."""

    __slots__ = ("stamp", "foreground", "image", "_img_hash")

    def __init__(self, stamp: "tuple[object, ...]", foreground: "tuple[object, ...]",
                 image: Image.Image) -> None:
        self.stamp = stamp
        self.foreground = foreground
        self.image = image
        self._img_hash: int | None = None

    def img_hash(self) -> int:
        """The hash the paint path would have computed for this composite.

        It is computed on the first reuse and kept, so the second frame pays
        one serialization and every frame after it pays none. Two threads that
        race here compute the same value, so the store needs no lock.
        """
        img_hash = self._img_hash
        if img_hash is None:
            img_hash = hash(self.image.tobytes())
            self._img_hash = img_hash
        return img_hash


def _covering_foreground(key: "ControllerKey", state: "ControllerKeyState") -> "tuple[object, ...] | None":
    """The foreground entry this state's composite stands on, when that
    composite owes nothing to the background behind it, else None.

    The entry is asked for first, because it is one attribute read and it
    refuses every key that carries no covering static media, which is most of
    them. It also stands in for the media test: an entry is only ever stored
    against a foreground asset, so a state whose media went away, or was
    replaced, fails the identity check.

    The gates after it are the reasons a covering foreground still does not
    make a still picture. An overlay or a video paints over it, a rolling
    label redraws every tick, a press shrinks the composite onto a
    transparent margin the background then shows through, and the warning
    point marks a key whose action is about to be replaced.
    """
    foreground = state.layout_manager.get_covering_foreground()
    if foreground is None or foreground[0] is not state.key_image:
        # No entry, an entry whose paste did not cover, or an entry left by
        # some other asset. None of the three proves this composite.
        return None
    if state.key_video is not None or state._overlay is not None:
        return None
    if key.is_pressed():
        return None
    if key.has_unavailable_action() and not key.deck_controller.screen_saver.showing:
        return None
    if state.label_manager.get_has_scroll_labels():
        return None
    return foreground


def _stamp(state: "ControllerKeyState") -> "tuple[object, ...]":
    """What the composite depends on that the foreground entry cannot be
    trusted to describe. See the module docstring for why it is these three.

    The composed layout is here, and not left to the entry that already
    carries it, because the entry is re-keyed by a composite and a reuse runs
    instead of one. A layout edit alone would otherwise reach no composite,
    leave the entry as it was, and freeze the key at the old size.
    """
    layout = state.layout_manager.get_composed_layout()
    return (state.label_manager.get_label_epoch(),
            tuple(state.background_manager.get_composed_color()),
            (layout.fill_mode, layout.halign, layout.valign, layout.size))


class CoveredComposite:
    """The kept composite of one key state, or nothing.

    One state owns one of these. It holds at most one tile-sized image, which
    is no more than the resized foreground the layout manager already keeps
    for the same state, and it drops that image as soon as a reuse check
    fails.
    """

    def __init__(self) -> None:
        self._entry: "_Covered | None" = None

    def invalidate(self) -> None:
        """Drop the kept composite. The state's own teardown and its reset for
        a fresh page load both call it. Nothing else has to: a reuse that no
        longer holds drops the entry itself.

        The image is released by reference count and never closed here. A
        thread that took this entry out of reuse() may still be encoding it,
        and a close under that read raises out of the media thread.
        """
        self._entry = None

    def reuse(self, key: "ControllerKey", state: "ControllerKeyState") -> "_Covered | None":
        """The kept composite when it is still what this key paints, else
        None. A miss drops the entry, so a key that stops qualifying stops
        holding an image."""
        entry = self._entry
        if entry is None:
            return None
        if _covering_foreground(key, state) is not entry.foreground:
            self.invalidate()
            return None
        if _stamp(state) != entry.stamp:
            self.invalidate()
            return None
        return entry

    def remember(self, key: "ControllerKey", state: "ControllerKeyState",
                 image: Image.Image) -> Image.Image:
        """Keep image as this state's composite when the foreground hides the
        background, and return image either way.

        The copy exists because the caller owns what it passes in and closes
        it once the paint is judged.
        """
        foreground = _covering_foreground(key, state)
        if foreground is None:
            self._entry = None
            return image
        self._entry = _Covered(_stamp(state), foreground, image.copy())
        return image


def present(key: "ControllerKey", page: "Page | None", config_gen: "int | None",
            force: bool) -> bool:
    """Paint a covered key from its kept composite, and report whether that
    happened. False means the caller composites as it always did.

    The offer is the one the full paint path makes, with the kept hash in
    place of a fresh one, so the write boundary, the encode memo and the
    hash de-dup all see exactly what they would have seen.
    """
    state = key.get_active_state()
    entry = state.cover_cache.reuse(key, state)
    if entry is None:
        return False

    if media_prof:
        media_prof.count("cover_skip")

    img_hash = entry.img_hash()
    # is_visual short-circuits the offer as in ControllerKey.update(),
    # equivalently.
    if key.deck_controller.is_visual() and not key.present_state.offer(
            key.deck_controller.media_player, page=page, config_gen=config_gen,
            img_hash=img_hash, force=force,
            encode=lambda: _encode_key_native(key, entry.image, img_hash)):
        if media_prof:
            media_prof.count("hash_skip")
        return True

    # The kept composite stays this cache's own, so the preview gets a copy.
    key.set_ui_key_image(copy(entry.image))
    return True


def remember(key: "ControllerKey", state: "ControllerKeyState",
             image: Image.Image) -> Image.Image:
    """Offer a finished composite to this state's cache and return it
    unchanged, so a caller can hand its result straight through."""
    return state.cover_cache.remember(key, state, image)
