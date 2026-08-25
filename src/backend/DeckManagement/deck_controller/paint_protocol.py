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

The paint protocol: the value a producer hands the media thread for one
paint, and the state that judges the next one.

A PaintTicket holds what the render decided: the present state the paint
belongs to, the page and generation it was rendered for, the encoded bytes
and the hash of what those bytes show. The media writer's two image task
classes wrap one ticket each, so a paint is one object from the render that
made it to the device write that presents it. The writer's own bookkeeping,
the submit stamp and the ordering it feeds, stays on the task and off the
ticket.

A PresentState is what one target shows and what is on its way to it. Each
key and each touchscreen owns one, and a ticket carries the one its paint
belongs to. The writer's own ordering state, the submit-seq counter and the
high-water mark of executed seqs, is deck-wide and stays on the writer: a
Clear compares its seq against every target's in one pass, so splitting
either per target would break that comparison.

Nothing here holds the input back. The input owns its present state, the
present state owns two hashes and an index, and a ticket is dropped after
its write, so an input set the screensaver retires falls by reference count
and never waits for the cycle collector.

This module imports nothing from its siblings in the deck_controller package
at runtime, so it sits under both the writer and the inputs.
"""
from dataclasses import dataclass, replace

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.media_writer import MediaPlayerThread
    from src.backend.PageManagement.Page import Page


class PresentState:
    """What one target shows now, and what is on its way to it.

    The two hashes have three writers. last_presented_hash moves on the
    media thread, once the device call for a paint has returned, so it names
    what the device holds. last_enqueued_hash moves on whichever thread
    rendered the paint, at the moment that paint is handed to the writer, so
    it names what is in flight. reset() clears both, from a Clear on the
    media thread and from the full repaint that same thread fires.

    A repaint is skipped only when the new image matches both. Either alone
    can be stale, after a paint the write boundary dropped or an in-flight
    revert, and would wrongly skip the correcting repaint.

    One target owns one of these. The writer's submit-seq counter and its
    high-water mark of executed seqs stay deck-wide on the writer, because a
    Clear judges every target's frames against one seq of its own.

    A subclass binds the target's slot on the device: KeyPresentState a key
    index, TouchscreenPresentState the single strip.
    """

    def __init__(self) -> None:
        self.last_presented_hash: int | None = None
        self.last_enqueued_hash: int | None = None

    def reset(self) -> None:
        """Forget both hashes, so the next paint reaches the device whatever
        it shows. A Clear and a full repaint both need it: without it a
        repaint of visually identical content matches the hash cached before
        the clear, is skipped, and the device stays on the blank."""
        self.last_presented_hash = None
        self.last_enqueued_hash = None

    def note_presented(self, img_hash: int | None) -> None:
        """Record that img_hash is on the device now.

        The write boundary calls it once the device call for that paint has
        returned, and never at render time. A paint the boundary dropped, and
        a write that raised, never reach it. Either would advance this past
        what the device holds, so the correcting render is hash-skipped and
        the target bleeds forever.
        """
        self.last_presented_hash = img_hash

    def offer(self, media_player: "MediaPlayerThread", *, page: "Page | None",
              config_gen: int | None, img_hash: int,
              encode: "Callable[[], bytes]", force: bool = False) -> bool:
        """Offer a rendered image to the device, and report whether it was
        enqueued.

        The caller has an image and its hash. This decides whether that image
        is worth a device write, encodes it if it is, records it as in flight
        and hands it to the writer's slot for this target.

        An image is skipped when its hash matches both what the device shows
        and what is already on its way there. Either alone can be stale, after
        a paint the write boundary dropped or an in-flight revert, and would
        wrongly skip the correcting repaint. force runs the paint through
        whatever the hashes say.

        encode runs only for a paint that is not skipped, which is what keeps
        an unchanged composite off the JPEG encoder. page and config_gen are
        the pair the caller captured before it rendered, so a page switch
        mid-render invalidates this paint at the write boundary.

        The enqueued-hash stamp lands before the slot assignment and is not
        synchronised with it here. The producer is what orders the two: every
        caller offers while holding the paint lock of the input that owns this
        target, so two paints of one target reach the slot in the order they
        were composed, and no paint can stamp a hash the slot does not hold. A
        producer that offers outside that lock reopens that race, where a paint
        that loses to the slot leaves a hash saying it is in flight.

        The stamp still runs ahead of the device write, which the lock does not
        change. A paint stamped here is dropped later at the write boundary when
        _is_current judges its page or generation stale, or when a Clear wipes
        it, and note_presented never runs for it. last_enqueued_hash then names
        bytes the device never got. The dual-hash skip above is what recovers:
        the correcting repaint still differs from last_presented_hash, which
        names what the device actually holds, so the two-way test fails and the
        repaint is offered rather than skipped as a repeat.
        """
        if (not force and img_hash == self.last_presented_hash
                and img_hash == self.last_enqueued_hash):
            return False
        native_image = encode()
        self.last_enqueued_hash = img_hash
        self._enqueue(media_player, native_image, page, config_gen, img_hash)
        return True

    def _enqueue(self, media_player: "MediaPlayerThread", native_image: bytes,
                 page: "Page | None", config_gen: int | None, img_hash: int) -> None:
        """Hand the encoded bytes to this target's slot on the writer. The two
        subclasses name the slot; nothing reaches this body."""
        raise NotImplementedError


class KeyPresentState(PresentState):
    """The present state of one key, whose slot is its key index."""

    def __init__(self, key_index: int) -> None:
        super().__init__()
        self.key_index = key_index

    def _enqueue(self, media_player: "MediaPlayerThread", native_image: bytes,
                 page: "Page | None", config_gen: int | None, img_hash: int) -> None:
        media_player.add_image_task(self.key_index, native_image, page=page,
                                    config_gen=config_gen, present=self,
                                    img_hash=img_hash)


class TouchscreenPresentState(PresentState):
    """The present state of the touchscreen, whose slot is the single strip
    every dial, label and background video composites into."""

    def _enqueue(self, media_player: "MediaPlayerThread", native_image: bytes,
                 page: "Page | None", config_gen: int | None, img_hash: int) -> None:
        media_player.add_touchscreen_task(native_image, page=page, config_gen=config_gen,
                                          present=self, img_hash=img_hash)


@dataclass(frozen=True, slots=True)
class PaintTicket:
    """One paint, from the render that produced it to the device write.

    It is frozen, because a producer and the media thread hold the same
    ticket. The one change the writer makes is the payload release after the
    write, and it builds a successor for that rather than mutating this one.
    """

    # The present state this paint belongs to, or None for a paint submitted
    # with no target behind it, as the writer scenarios do. The write boundary
    # stamps it and reads nothing else off it.
    present: "PresentState | None"
    # None when the deck has no active page, at boot or during teardown. The
    # write boundary only identity-compares it against active_page and never
    # dereferences it, so a page-less paint is judged stale, not crashed on.
    page: "Page | None"
    # Generation of the content rendered; the paint is dropped at present if
    # a newer generation superseded it.
    config_gen: int | None
    # Device-ready encoded bytes, dropped by released() once written.
    native_image: bytes
    # Hash of the image these bytes show. run() records it as presented.
    img_hash: int | None

    def released(self) -> "PaintTicket":
        """This ticket with its encoded bytes dropped.

        The task replaces its own ticket with this right after the write, so a
        frame's bytes are freed as it reaches the device instead of at the end
        of the batch. It matters most for the touchscreen strip, the largest
        single write on the deck and the one native no cache holds. Every
        other field survives, so an empty payload means exactly one thing:
        this paint was already written.
        """
        return replace(self, native_image=b"")
