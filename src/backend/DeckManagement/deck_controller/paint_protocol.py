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
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.input_latency import LatencySample
    from src.backend.DeckManagement.deck_controller.media_writer import MediaPlayerThread
    from src.backend.PageManagement.Page import Page


class PresentState:
    """Track one target's presented and enqueued hashes; skip only when both match.

    Render threads update enqueued, the media thread updates presented or resets both, subclasses bind key or strip slots, and deck-wide ordering stays on the writer.
    """

    def __init__(self) -> None:
        self.last_presented_hash: int | None = None
        self.last_enqueued_hash: int | None = None

    def reset(self) -> None:
        """Clear both hashes so the next paint cannot be deduplicated.

        Clear and full repaint need this to prevent identical content from leaving the device blank.
        """
        self.last_presented_hash = None
        self.last_enqueued_hash = None

    def note_presented(self, img_hash: int | None) -> None:
        """Record the hash only after its device write returns.

        Dropped or failed paints must not advance it, or the correcting render is deduplicated.
        """
        self.last_presented_hash = img_hash

    def offer(self, media_player: "MediaPlayerThread", *, page: "Page | None",
              config_gen: int | None, img_hash: int,
              encode: "Callable[[], bytes]", force: bool = False) -> bool:
        """Offer an image unless both hashes match; force bypasses the check, and encoding runs only after acceptance.

        Callers must hold the target paint lock to order stamp and slot; page or generation changes may drop tickets, while the presented hash permits recovery.
        """
        if (not force and img_hash == self.last_presented_hash
                and img_hash == self.last_enqueued_hash):
            return False
        native_image = encode()
        self.last_enqueued_hash = img_hash
        tracker = getattr(getattr(media_player, "deck_controller", None), "input_latency", None)
        latency_sample = tracker.current_sample() if tracker is not None else None
        if latency_sample is not None:
            assert tracker is not None
            tracker.paint_enqueued(latency_sample)
        self._enqueue(media_player, native_image, page, config_gen, img_hash, latency_sample)
        return True

    def _enqueue(self, media_player: "MediaPlayerThread", native_image: bytes,
                 page: "Page | None", config_gen: int | None, img_hash: int,
                 latency_sample: "LatencySample | None") -> None:
        """Hand the encoded bytes to this target's slot on the writer. The two
        subclasses name the slot; nothing reaches this body."""
        raise NotImplementedError

    def _enqueue_ticket(self, submit: "Callable[..., None]", *args: object,
                        native_image: bytes, page: "Page | None",
                        config_gen: int | None, img_hash: int,
                        latency_sample: "LatencySample | None") -> None:
        """Submit either paint target through one latency-aware ticket seam."""
        kwargs = {
            "page": page,
            "config_gen": config_gen,
            "present": self,
            "img_hash": img_hash,
        }
        if latency_sample is not None:
            kwargs["latency_sample"] = latency_sample
        submit(*args, native_image, **kwargs)


class KeyPresentState(PresentState):
    """The present state of one key, whose slot is its key index."""

    def __init__(self, key_index: int) -> None:
        super().__init__()
        self.key_index = key_index

    @override
    def _enqueue(self, media_player: "MediaPlayerThread", native_image: bytes,
                 page: "Page | None", config_gen: int | None, img_hash: int,
                 latency_sample: "LatencySample | None") -> None:
        self._enqueue_ticket(
            media_player.add_image_task, self.key_index, native_image=native_image,
            page=page, config_gen=config_gen, img_hash=img_hash,
            latency_sample=latency_sample,
        )


class TouchscreenPresentState(PresentState):
    """The present state of the touchscreen, whose slot is the single strip
    every dial, label and background video composites into."""

    @override
    def _enqueue(self, media_player: "MediaPlayerThread", native_image: bytes,
                 page: "Page | None", config_gen: int | None, img_hash: int,
                 latency_sample: "LatencySample | None") -> None:
        self._enqueue_ticket(
            media_player.add_touchscreen_task, native_image=native_image,
            page=page, config_gen=config_gen, img_hash=img_hash,
            latency_sample=latency_sample,
        )


@dataclass(frozen=True, slots=True)
class PaintTicket:
    """Carry one immutable paint from rendering to device write.

    The writer releases payload bytes through a successor ticket, so producer and media-thread references never mutate.
    """

    # Present state for this paint, or None for targetless writer scenarios.
    present: "PresentState | None"
    # Active page at render time, or None during boot and teardown; the write
    # boundary uses identity only, so a page-less paint becomes stale.
    page: "Page | None"
    # Generation of the content rendered; the paint is dropped at present if
    # a newer generation superseded it.
    config_gen: int | None
    # Device-ready encoded bytes, dropped by released() once written.
    native_image: bytes
    # Hash of the image these bytes show. run() records it as presented.
    img_hash: int | None
    # The physical input sample that caused this paint, if it has one.
    latency_sample: "LatencySample | None" = None

    def released(self) -> "PaintTicket":
        """Return a successor ticket without encoded bytes; all other fields remain.

        Tasks call this after each write so payload memory, especially the uncached touchscreen strip, is released before batch end; empty means already written.
        """
        return replace(self, native_image=b"")

    def writer_started(self, deck_controller: object) -> None:
        if self.latency_sample is not None:
            tracker = getattr(deck_controller, "input_latency", None)
            if tracker is not None:
                tracker.writer_started(self.latency_sample)

    def usb_presented(self, deck_controller: object) -> None:
        if self.latency_sample is not None:
            tracker = getattr(deck_controller, "input_latency", None)
            if tracker is not None:
                tracker.usb_presented(self.latency_sample)

    def record_drop(self, deck_controller: object, reason: str) -> None:
        if self.latency_sample is not None:
            tracker = getattr(deck_controller, "input_latency", None)
            if tracker is not None:
                tracker.drop(self.latency_sample, reason)

    def discarded(self, deck_controller: object, reason: str) -> None:
        """Record a lost paint frame; a sibling may still complete its token."""
        self.record_drop(deck_controller, reason)

    def presented_by(self, deck_controller: object) -> "PaintTicket":
        """Record a completed device write and release its bytes.

        Only successful writes update the presented hash; otherwise a correcting render could be deduplicated.
        """
        self.usb_presented(deck_controller)
        if self.present is not None:
            self.present.note_presented(self.img_hash)
        return self.released()
