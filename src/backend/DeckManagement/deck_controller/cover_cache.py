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
two branches of the composite that draw a gated look, the press shrink and the
warning point, hand back NO_STORE beside the picture they made. Their gates are
therefore not judged twice and guessed at, but recorded once by the code that
acted on them.

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


#: A pre-read that refuses the store outright. precheck() answers it for a
#: composite that can never be kept, so the caller pays one attribute read and
#: no stamp, and the composite itself assigns it over its own pre-read the
#: moment it draws something a kept picture must not carry. The False alone
#: refuses, and the empty stamp refuses again, because no stamp a real
#: pre-read produces can equal it.
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


def _gates_clear(key: "ControllerKey", state: "ControllerKeyState") -> bool:
    """Whether nothing about this key stops its composite from being a still
    picture of a static foreground.

    An overlay or a video paints over that foreground, a rolling label redraws
    every tick, a press shrinks the composite onto a transparent margin the
    background then shows through, and the warning point marks a key whose
    action is about to be replaced. Each of the four is read on both sides of
    a composite, before it and after it, so one definition serves both and
    they cannot disagree.
    """
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


def precheck(key: "ControllerKey", state: "ControllerKeyState") -> "tuple[bool, tuple[object, ...]]":
    """What a composite about to run must be judged against when it ends.

    It answers NO_STORE without reading anything else for the two shapes this
    module can never help, so neither pays for it per composite: a key with no
    static media, which is every bare key and every key playing a video, and a
    key whose foreground was already resized and found not to cover.

    The second test reads the verdict of the previous composite, so a
    foreground that goes from bare to covering without its asset changing,
    which a size or fill-mode edit does, settles one composite later than it
    otherwise would: this one is judged on the old verdict and not kept, the
    next builds and keeps. New media has no entry to read, so it settles at
    once. The trade buys a page of transparent icons its full speed back.
    """
    image = state.key_image
    if image is None or state.layout_manager.foreground_proved_bare(image):
        return NO_STORE
    return (_gates_clear(key, state), _stamp(state))


def _covering_foreground(key: "ControllerKey", state: "ControllerKeyState") -> "tuple[object, ...] | None":
    """The foreground entry this state's composite stands on, when that
    composite owes nothing to the background behind it, else None.

    The entry is asked for first, because it is one attribute read and it
    refuses every key that carries no covering static media, which is most of
    them. It also stands in for the media test: an entry is only ever stored
    against a foreground asset, so a state whose media went away, or was
    replaced, fails the identity check.
    """
    foreground = state.layout_manager.get_covering_foreground()
    if foreground is None or foreground[0] is not state.key_image:
        # No entry, an entry whose paste did not cover, or an entry left by
        # some other asset. None of the three proves this composite.
        return None
    if not _gates_clear(key, state):
        return None
    return foreground


class CoveredComposite:
    """The kept composite of one key state, or nothing.

    One state owns one of these, so a key with several states holds one
    picture per state it has painted while covered, not one per key. A page of
    32 keys whose five states have all been visited therefore retains 32 times
    5 tile-sized images, about 5.9 MB at the XL tile size. Each is no larger
    than the resized foreground the same state's layout manager already keeps.

    These images sit outside the process byte budget, which enrols the encode
    memo and the native tile cache and nothing else, and outside the sweep of
    clear_encoded_key_caches(). What bounds them instead is the state objects
    themselves: a page load builds new ones, and close_resources() and clear()
    release what the old ones held.
    """

    def __init__(self) -> None:
        self._entry: "_Covered | None" = None

    def invalidate(self) -> None:
        """Drop the kept composite.

        Four callers release an image, and the memory claim above rests on
        them: the state's teardown, its reset for a fresh page load, its media
        setters, and present() when it finds a key with no static media. A
        reuse that no longer holds drops the entry too, but a key that stops
        qualifying may also stop reaching reuse(), which is why the setters
        and present() do not leave it to that.

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
                 image: Image.Image, pre: "tuple[bool, tuple[object, ...]]") -> Image.Image:
        """Keep image as this state's composite when nothing moved across the
        composite that produced it, and return image either way.

        pre is what precheck() read before that composite started, unless the
        composite replaced it with NO_STORE because it drew a gated look. That
        replacement is what makes the gate claim hold: a press or a warning
        point that arrives and leaves inside one composite window is invisible
        to a read taken before it and to a read taken after it, and only the
        branch that drew the shrink or the dot ever saw it.

        Three things must hold together: pre still permits a store, so nothing
        the picture carries forbids it; the foreground it built covers the tile
        and the gates are clear now; and the stamp is where it was, so no
        label, layout or colour edit landed in between. Any one of them failing
        costs one uncacheable composite, which is the cheap half of the trade.

        The stored stamp is the one read before the composite, which the
        equality test has just proved equal to the one after it.

        The copy exists because the caller owns what it passes in and closes
        it once the paint is judged.
        """
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
    """Paint a covered key from its kept composite, and report whether that
    happened. False means the caller composites as it always did.

    The offer is the one the full paint path makes, with the kept hash in
    place of a fresh one, so the write boundary, the encode memo and the
    hash de-dup all see exactly what they would have seen. The bytes the
    encode produces are the kept picture's own, so a memo that evicted between
    two paints re-encodes what the key really shows.
    """
    state = key.get_active_state()
    if state.key_image is None:
        # A key with no static media can hold no picture of its own. This is
        # also where a key that lost its media releases the one it kept: the
        # test costs one attribute read on every bare key's paint, and it is
        # the reason this call is not guarded at its site.
        state.cover_cache.invalidate()
        return False
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


def remember(key: "ControllerKey", state: "ControllerKeyState", image: Image.Image,
             pre: "tuple[bool, tuple[object, ...]]") -> Image.Image:
    """Offer a finished composite to this state's cache and return it
    unchanged, so a caller can hand its result straight through."""
    return state.cover_cache.remember(key, state, image, pre)
