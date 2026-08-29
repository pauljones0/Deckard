"""Callback-driven completion for one page generation."""

import threading
from concurrent.futures import Future
from typing import TYPE_CHECKING

from loguru import logger as log

from src.backend import timer_wheel, ui_port

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController


class PageLoadCompletion:
    """Join input and background readiness without occupying a worker."""

    BACKGROUND_TIMEOUT_S = 10.0

    def __init__(self, controller: "DeckController", gen: int, *, wait_for_inputs: bool) -> None:
        self._controller = controller
        self._gen = gen
        self._lock = threading.Lock()
        self._inputs_ready = not wait_for_inputs
        self._background_ready = False
        self._queued = False
        self._cancelled = False
        self._background_future: Future[None] | None = None
        self._timeout: timer_wheel.TimerHandle | None = None

    def watch_background(self, future: Future[None] | None) -> None:
        """Attach one future callback and one cancellable timeout."""
        queue_finish = False
        with self._lock:
            if self._cancelled:
                return
            self._background_future = future
            if future is None:
                self._background_ready = True
                queue_finish = self._mark_queued_if_ready_locked()
            else:
                self._start_timeout_if_needed_locked()
        if queue_finish:
            self._queue_finish()
        if future is None:
            return
        future.add_done_callback(self._background_finished)

    def inputs_finished(self) -> None:
        """Publish the FIFO input-load marker from the writer."""
        with self._lock:
            if self._cancelled:
                return
            self._inputs_ready = True
            self._start_timeout_if_needed_locked()
            queue_finish = self._mark_queued_if_ready_locked()
        if queue_finish:
            self._queue_finish()

    def cancel(self) -> None:
        """Prevent future callbacks and queued completion from painting."""
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            timeout = self._timeout
            self._timeout = None
            future = self._background_future
        if timeout is not None:
            timeout.cancel()
        if future is not None:
            future.cancel()

    def _background_finished(self, future: Future[None]) -> None:
        with self._lock:
            if self._cancelled or self._background_ready:
                return
        try:
            future.result()
        except Exception:
            log.warning("Background not ready before update_all_inputs; painting anyway")
        with self._lock:
            if self._cancelled or self._background_ready:
                return
            self._background_ready = True
            self._cancel_timeout_locked()
            queue_finish = self._mark_queued_if_ready_locked()
        if queue_finish:
            self._queue_finish()

    def _background_timed_out(self) -> None:
        with self._lock:
            if self._cancelled or self._background_ready:
                return
            log.warning("Background not ready before update_all_inputs; painting anyway")
            self._background_ready = True
            self._timeout = None
            queue_finish = self._mark_queued_if_ready_locked()
        if queue_finish:
            self._queue_finish()

    def _cancel_timeout_locked(self) -> None:
        timeout = self._timeout
        self._timeout = None
        if timeout is not None:
            timeout.cancel()

    def _start_timeout_if_needed_locked(self) -> None:
        if self._cancelled or self._background_ready or not self._inputs_ready:
            return
        if self._timeout is None:
            self._timeout = timer_wheel.schedule(
                self.BACKGROUND_TIMEOUT_S,
                self._background_timed_out,
                name="PageBackgroundTimeout",
            )

    def _mark_queued_if_ready_locked(self) -> bool:
        if self._cancelled or self._queued:
            return False
        if not (self._inputs_ready and self._background_ready):
            return False
        self._queued = True
        self._cancel_timeout_locked()
        return True

    def _queue_finish(self) -> None:
        controller = self._controller
        with controller._load_page_lock:
            with self._lock:
                if self._cancelled:
                    return
            if controller._closing or not controller._page_is_current(self._gen):
                return
            controller.media_player.add_task(self.finish)

    def finish(self) -> None:
        """Paint one current page from the sole writer."""
        controller = self._controller
        with controller._load_page_lock:
            with self._lock:
                if self._cancelled:
                    return
            if controller._closing or not controller._page_is_current(self._gen):
                return
            controller.update_all_inputs(gen=self._gen)
            if controller._closing or not controller._page_is_current(self._gen):
                return
            ui_port.get().on_page_changed(controller)
