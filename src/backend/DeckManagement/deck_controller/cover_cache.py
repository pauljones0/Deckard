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

Two things decide whether the kept composite may be reused. The first is the
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
of those either animates or is short-lived, so caching it buys nothing.

Reading those inputs after the composite is what makes an entry stale, and the
window is real. Three threads composite the same key state: the media thread
on its tick, the GTK main thread when the key grid pushes a preview, and a
plugin worker whenever an action sets a label or media and calls update().
A label epoch therefore moves under a running composite as a matter of course,
not as a rarity, and a press or an action swap flips a gate from the input
callback in the same way. Judging a finished picture by what its inputs read
at the end stores a picture of one state under the stamp of another, and
nothing later retires it: the reuse path re-keys nothing, so a stamp that
already matches keeps matching for as long as the page holds.

So the inputs are read before the composite starts, and precheck() is that
read. A composite is kept only when that read permitted it, the foreground
entry it built covers the tile, and the stamp is unmoved when it ends. An edit
that lands mid-composite makes one composite uncacheable rather than one entry
wrong. The stored stamp is the one read before, which the equality test proves
equal to the one after. LabelManager.get_composed_labels() keeps the same
discipline for the same reason: it reads the epoch before it composes, and
publishes the pair.

A read before and a read after are still not enough on their own, because a
gate can arrive and leave between them and neither sees it, while the picture
in the middle carries what it drew. So the rule underneath all of this is that
the read which decides the picture is the read which decides the store: the
two branches of the composite that act on a gate, the press look and the
warning point, hand back NO_STORE beside the picture they made. Their gates
are therefore not judged twice and guessed at, but recorded once by the code
that acted on them.

The press look is a setting, and the refusal is not. A general setting turns
the shrink off, and a press then composites the picture the key already shows,
byte for byte. The gate stays unconditional through that: no kept composite
may be stamped while a key is held, whether or not the press drew anything.
Reading a picture to decide is exactly what this module refuses to do, and a
press that draws nothing leaves no picture to read anyway.

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


#: Refuse retention without computing a stamp before or during a gated composite.
#: False rejects directly, and the empty stamp cannot match a real precheck stamp.
NO_STORE: "tuple[bool, tuple[object, ...]]" = (False, ())


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
        """Return the paint-path hash, computing it only on first reuse.
        Racing threads compute the same value, so storage needs no lock."""
        img_hash = self._img_hash
        if img_hash is None:
            img_hash = hash(self.image.tobytes())
            self._img_hash = img_hash
        return img_hash


def _gates_clear(key: "ControllerKey", state: "ControllerKeyState") -> bool:
    """Reject video, overlay, press, visible action warning, or scrolling-label composites.
    Press always rejects retention, even when shrink-on-press is disabled."""
    if state.key_video is not None or state._overlay is not None:
        return False
    if key.is_pressed():
        return False
    if key.has_unavailable_action() and not key.deck_controller.screen_saver.showing:
        return False
    if state.label_manager.get_has_scroll_labels():
        return False
    return True


def _stamp(state: "ControllerKeyState") -> "tuple[object, ...]":
    """Return label, background-color, and layout state not proven by foreground identity.
    Stamp layout because reuse bypasses the composite that would re-key its entry."""
    layout = state.layout_manager.get_composed_layout()
    return (state.label_manager.get_label_epoch(),
            tuple(state.background_manager.get_composed_color()),
            (layout.fill_mode, layout.halign, layout.valign, layout.size))


def precheck(key: "ControllerKey", state: "ControllerKeyState") -> "tuple[bool, tuple[object, ...]]":
    """Return the gate verdict and stamp that a completed composite must match.
    Bare or proven-uncovered media rejects early; layout-only coverage changes settle one composite later."""
    image = state.key_image
    if image is None or state.layout_manager.foreground_proved_bare(image):
        return NO_STORE
    return (_gates_clear(key, state), _stamp(state))


def _covering_foreground(key: "ControllerKey", state: "ControllerKeyState") -> "tuple[object, ...] | None":
    """Return this state's covering foreground entry when all gates remain clear.
    Foreground identity also rejects removed or replaced media."""
    foreground = state.layout_manager.get_covering_foreground()
    if foreground is None or foreground[0] is not state.key_image:
        # No entry, an entry whose paste did not cover, or an entry left by
        # some other asset. None of the three proves this composite.
        return None
    if not _gates_clear(key, state):
        return None
    return foreground


class CoveredComposite:
    """Hold at most one retained composite per key state outside the image byte budget.
    State teardown bounds lifetime, and each image is no larger than its resized foreground."""

    def __init__(self) -> None:
        self._entry: "_Covered | None" = None

    def invalidate(self) -> None:
        """Drop the retained composite during teardown, reset, media change, detach, or bare presentation.
        Release by reference only because a concurrent reuse can still encode the image."""
        self._entry = None

    def reuse(self, key: "ControllerKey", state: "ControllerKeyState") -> "_Covered | None":
        """Return the current retained composite, or drop a stale entry and return None."""
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
                 image: Image.Image, pre: "tuple[bool, tuple[object, ...]]") -> Image.Image:
        """Retain a copy only when precheck allowed it, foreground covers, and the stamp is unchanged.
        NO_STORE captures transient gates seen only during composition; return the caller-owned image."""
        gates_were_clear, pre_stamp = pre
        if not gates_were_clear:
            self._entry = None
            return image
        foreground = _covering_foreground(key, state)
        if foreground is None or _stamp(state) != pre_stamp:
            self._entry = None
            return image
        self._entry = _Covered(pre_stamp, foreground, image.copy())
        return image


def present(key: "ControllerKey", page: "Page | None", config_gen: "int | None",
            force: bool) -> bool:
    """Offer a retained covered-key composite through the normal paint boundary.
    Return false for normal composition; cache eviction re-encodes the retained image."""
    state = key.get_active_state()
    if state.key_image is None:
        # A bare key cannot retain a composite; invalidate media removed since the last paint.
        state.cover_cache.invalidate()
        return False
    entry = state.cover_cache.reuse(key, state)
    if entry is None:
        return False

    if media_prof:
        media_prof.count("cover_skip")

    img_hash = entry.img_hash()
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


def remember(key: "ControllerKey", state: "ControllerKeyState", image: Image.Image,
             pre: "tuple[bool, tuple[object, ...]]") -> Image.Image:
    """Offer a finished composite to this state's cache and return it
    unchanged, so a caller can hand its result straight through."""
    return state.cover_cache.remember(key, state, image, pre)
