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

A PaintTicket holds every field the write boundary reads. The producer that
rendered a frame fills the page, the generation, the encoded bytes and the
hash of what those bytes show; the writer adds its submit stamp. The media
writer's two image task classes wrap one ticket each, so a paint is one
object from the render that made it to the device write that presents it.

A PresentState is what one target shows and what is on its way to it. Each
key and each touchscreen owns one. The writer's own ordering state, the
submit-seq counter and the high-water mark of executed seqs, is deck-wide
and stays on the writer: a Clear compares its seq against every target's in
one pass, so splitting either per target would break that comparison.

This module imports nothing from its siblings in the deck_controller package
at runtime, so it sits under both the writer and the inputs.
"""
from dataclasses import dataclass, replace

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.media_writer import MediaPlayerThread
    from src.backend.PageManagement.Page import Page


class PaintTarget(Protocol):
    """The input a paint belongs to.

    The writer reaches exactly one thing on it, the present state it stamps
    after a device write that did not raise.
    """

    @property
    def present_state(self) -> "PresentState": ...


class PresentState:
    """What one target shows now, and what is on its way to it.

    The two hashes have two writers. last_presented_hash moves on the media
    thread, right after a device write that did not raise, so it names what
    the device holds. last_enqueued_hash moves on whichever thread rendered
    the paint, at the moment that paint is handed to the writer, so it names
    what is in flight.

    A repaint is skipped only when the new image matches both. Either alone
    can be stale, after a paint the write boundary dropped or an in-flight
    revert, and would wrongly skip the correcting repaint.

    One target owns one of these. The writer's submit-seq counter and its
    high-water mark of executed seqs stay deck-wide on the writer, because a
    Clear judges every target's frames against one seq of its own.

    A subclass binds the target's slot on the device: KeyPresentState a key
    index, TouchscreenPresentState the single strip.
    """

    def __init__(self, target: "PaintTarget") -> None:
        self.target = target
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

        The write boundary calls it right after a device write that did not
        raise, and never at render time. A paint dropped at that boundary must
        not advance this, or the correcting render is hash-skipped and the
        target bleeds forever.
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
        synchronised with it. That edge is known: a paint that loses the race
        to the slot leaves a hash saying it is in flight. The writer's Clear
        and the pending-repaint retry are what recover from it.
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

    def __init__(self, target: "PaintTarget", key_index: int) -> None:
        super().__init__(target)
        self.key_index = key_index

    def _enqueue(self, media_player: "MediaPlayerThread", native_image: bytes,
                 page: "Page | None", config_gen: int | None, img_hash: int) -> None:
        media_player.add_image_task(self.key_index, native_image, page=page,
                                    config_gen=config_gen, controller_key=self.target,
                                    img_hash=img_hash)


class TouchscreenPresentState(PresentState):
    """The present state of the touchscreen, whose slot is the single strip
    every dial, label and background video composites into."""

    def _enqueue(self, media_player: "MediaPlayerThread", native_image: bytes,
                 page: "Page | None", config_gen: int | None, img_hash: int) -> None:
        media_player.add_touchscreen_task(native_image, page=page, config_gen=config_gen,
                                          controller_touchscreen=self.target,
                                          img_hash=img_hash)


@dataclass(frozen=True, slots=True)
class PaintTicket:
    """One paint, from the render that produced it to the device write.

    It is frozen, because a producer and the media thread hold the same
    ticket. Only the writer changes one, and it does so by building a
    successor: the submit stamp under the slot lock, and the payload release
    after the write.
    """

    # The input this paint belongs to, or None for a paint submitted with no
    # input behind it, as the writer scenarios do. The write boundary stamps
    # the target's dedup slot and reads nothing else off it.
    target: "PaintTarget | None"
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
    # The writer's monotonic submit-seq stamp, None until the writer assigns
    # the ticket to a slot.
    submit_seq: int | None = None

    def stamped(self, submit_seq: int) -> "PaintTicket":
        """This ticket with the writer's submit stamp on it. The writer builds
        it inside the slot lock, atomically with the slot assignment."""
        return replace(self, submit_seq=submit_seq)

    def released(self) -> "PaintTicket":
        """This ticket with its encoded bytes dropped.

        The task replaces its own ticket with this right after the write, so a
        frame's bytes are freed as it reaches the device instead of at the end
        of the batch. It matters most for the touchscreen strip, the largest
        single write on the deck and the one native no cache holds. Every
        other field survives, because the writer still reads the submit stamp
        afterwards.
        """
        return replace(self, native_image=b"")
