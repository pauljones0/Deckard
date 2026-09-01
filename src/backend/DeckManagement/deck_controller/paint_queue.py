"""Typed queue operations owned by one media writer."""
from dataclasses import replace
from typing import TYPE_CHECKING
from collections.abc import Callable

from src.backend.DeckManagement.deck_controller.media_tasks import (
    MediaPlayerSetImageTask,
    MediaPlayerSetTouchscreenImageTask,
)

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.media_writer import MediaPlayerThread


PaintTask = MediaPlayerSetImageTask | MediaPlayerSetTouchscreenImageTask


class PaintQueue:
    """Keep every latest-wins and terminal-removal decision beside the writer."""

    def __init__(self, writer: "MediaPlayerThread") -> None:
        self._writer = writer

    @staticmethod
    def _inherit_sample(task: PaintTask, replaced: PaintTask | None) -> None:
        # A superseding frame inherits a displaced sample only when it has none;
        # its own sample retires the old one, and _slot_lock makes swaps atomic.
        if (replaced is not None
                and task.ticket.latency_sample is None
                and replaced.ticket.latency_sample is not None):
            task.ticket = replace(
                task.ticket, latency_sample=replaced.ticket.latency_sample)
            replaced.ticket = replace(replaced.ticket, latency_sample=None)

    def replace_image(self, key_index: int, task: MediaPlayerSetImageTask,
                      allocate_seq: Callable[[], int]) -> None:
        with self._writer._slot_lock:
            replaced = self._writer.image_tasks.get(key_index)
            self._inherit_sample(task, replaced)
            task.submit_seq = allocate_seq()
            self._writer.image_tasks[key_index] = task
        if replaced is not None:
            replaced.ticket.discarded(
                self._writer.deck_controller, "writer_slot_superseded")

    def replace_touchscreen(self, task: MediaPlayerSetTouchscreenImageTask,
                            allocate_seq: Callable[[], int]) -> None:
        with self._writer._slot_lock:
            replaced = self._writer.touchscreen_task
            self._inherit_sample(task, replaced)
            task.submit_seq = allocate_seq()
            self._writer.touchscreen_task = task
        if replaced is not None:
            replaced.ticket.discarded(
                self._writer.deck_controller, "writer_slot_superseded")

    def take_touchscreen(self) -> MediaPlayerSetTouchscreenImageTask | None:
        with self._writer._slot_lock:
            task, self._writer.touchscreen_task = self._writer.touchscreen_task, None
            return task

    def discard_all(self, reason: str) -> None:
        with self._writer._slot_lock:
            tasks: list[PaintTask] = list(self._writer.image_tasks.values())
            if self._writer.touchscreen_task is not None:
                tasks.append(self._writer.touchscreen_task)
            self._writer.image_tasks.clear()
            self._writer.touchscreen_task = None
        for task in tasks:
            task.ticket.discarded(self._writer.deck_controller, reason)

    def discard_before_clear(self, seq: int) -> None:
        with self._writer._slot_lock:
            removed: list[PaintTask] = []
            for key in list(self._writer.image_tasks):
                task = self._writer.image_tasks.get(key)
                if task is not None and task.submit_seq is not None and task.submit_seq < seq:
                    del self._writer.image_tasks[key]
                    removed.append(task)
            touch_task = self._writer.touchscreen_task
            if (touch_task is not None and touch_task.submit_seq is not None
                    and touch_task.submit_seq < seq):
                self._writer.touchscreen_task = None
                removed.append(touch_task)
        for task in removed:
            task.ticket.discarded(self._writer.deck_controller, "clear_preceding_paint")

    def defer_rate_limited_touchscreen(self, task: MediaPlayerSetTouchscreenImageTask) -> None:
        with self._writer._slot_lock:
            replacement = self._writer.touchscreen_task
            if replacement is None:
                self._writer.touchscreen_task = task
                return
        task.ticket.discarded(
            self._writer.deck_controller, "touchscreen_rate_cap_superseded")

    def account_writer_tick_exception(self, tasks: list[PaintTask]) -> None:
        """Name every popped paint the current writer tick cannot finish."""
        for task in tasks:
            task.ticket.discarded(self._writer.deck_controller, "writer_tick_exception")
