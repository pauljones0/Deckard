"""The two device-write task values consumed by the sole media writer."""
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from StreamDeck.Devices import StreamDeck
from loguru import logger as log

from src.backend.DeckManagement.Subclasses.media_pipeline_profiler import media_prof
from src.backend.DeckManagement.deck_controller.paint_protocol import PaintTicket

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController


def _discard_failed_ticket(deck_controller: "DeckController", ticket: PaintTicket,
                           reason: str) -> None:
    ticket.discarded(deck_controller, reason)


@dataclass
class MediaPlayerSetTouchscreenImageTask:
    """One touchscreen write and the paint ticket it completes."""

    deck_controller: "DeckController"
    ticket: PaintTicket
    submit_seq: int | None = None

    def run(self) -> None:
        if not self.deck_controller.deck.is_touch():
            _discard_failed_ticket(
                self.deck_controller, self.ticket, "touchscreen_unavailable")
            return
        ticket = self.ticket
        if not ticket.native_image:
            # Released, so this task already wrote. A second run would put an
            # empty frame on the strip and then record it as presented, which
            # is the bleed the present stamp exists to prevent.
            return
        try:
            ticket.writer_started(self.deck_controller)
            touchscreen_size = self.deck_controller.get_touchscreen_image_size()
            self.deck_controller.deck.set_touchscreen_image(
                ticket.native_image, x_pos=0, y_pos=0,
                width=touchscreen_size[0], height=touchscreen_size[1],
            )
            self.ticket = ticket.presented_by(self.deck_controller)
            self.deck_controller._on_write_result(True)
        except StreamDeck.TransportError as error:
            log.error(f"Failed to set deck touchscreen image. Error: {error}")
            _discard_failed_ticket(self.deck_controller, ticket, "usb_write_failed")
            self.deck_controller._on_write_result(False)


@dataclass
class MediaPlayerSetImageTask:
    """One key write and the paint ticket it completes."""

    deck_controller: "DeckController"
    ticket: PaintTicket
    key_index: int
    submit_seq: int | None = None

    def run(self) -> None:
        ticket = self.ticket
        if not ticket.native_image:
            # Released, so this task already wrote; see the touchscreen twin.
            return
        try:
            ticket.writer_started(self.deck_controller)
            started_at = time.perf_counter() if media_prof else 0.0
            self.deck_controller.deck.set_key_image(self.key_index, ticket.native_image)
            if media_prof:
                media_prof.add("usb_write", time.perf_counter() - started_at)
            self.ticket = ticket.presented_by(self.deck_controller)
            self.deck_controller._on_write_result(True)
        except StreamDeck.TransportError as error:
            log.error(f"Failed to set deck key image. Error: {error}")
            _discard_failed_ticket(self.deck_controller, ticket, "usb_write_failed")
            self.deck_controller._on_write_result(False)
