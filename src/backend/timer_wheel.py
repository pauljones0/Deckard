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
"""
# One Condition-backed heap provides Timer-compatible handles for screensaver, overlay, and key-hold delays.
# Due work uses separate daemon threads, not the scheduler or shared pool, so slow callbacks cannot block timers.
import heapq
import itertools
import threading
import time
from collections.abc import Callable

from loguru import logger as log


def _NOOP() -> None:
    """The callback a cancelled handle carries, so cancel() can drop the real
    closure without leaving _callback None for a probe to trip on."""


class TimerHandle:
    """Returned by TimerWheel.schedule(). Not constructed directly."""

    __slots__ = ("_wheel", "_seq", "_due", "_callback", "_name", "_cancelled", "_fired")

    def __init__(self, wheel: "TimerWheel", seq: int, due: float, callback: Callable[[], object], name: str):
        self._wheel = wheel
        self._seq = seq
        self._due = due
        self._callback = callback
        self._name = name
        self._cancelled = False
        self._fired = False

    def cancel(self) -> None:
        """Stop this timer before it fires. Idempotent, and a no-op after the
        timer fired, the same as threading.Timer.cancel()."""
        self._wheel._cancel(self)

    def is_alive(self) -> bool:
        """True while the timer is pending. The name matches
        threading.Timer.is_alive() for call sites that probe it."""
        return not (self._cancelled or self._fired)


class TimerWheel:
    """Schedule any number of delays behind one daemon thread.
    Any thread can schedule or cancel through the wheel's short-held lock."""

    # Compact only heaps of at least 64 entries with at least half cancelled.
    # Otherwise a long-lived early timer retains later handles and closures.
    _COMPACT_MIN_HEAP = 64

    def __init__(self, name: str = "TimerWheel"):
        self._cond = threading.Condition()
        self._heap: list[tuple[float, int, TimerHandle]] = []
        self._seq_counter = itertools.count()
        # Cancelled handles still in the heap since the last compaction.
        self._cancelled_pending = 0
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def schedule(self, delay_s: float, callback: Callable[[], object], name: str = "TimerWheelJob") -> TimerHandle:
        """Arm a one-shot timer that calls callback() after delay_s seconds
        on its own dispatch thread. Returns a handle with .cancel()."""
        seq = next(self._seq_counter)
        due = time.monotonic() + delay_s
        handle = TimerHandle(self, seq, due, callback, name)
        with self._cond:
            heapq.heappush(self._heap, (due, seq, handle))
            self._cond.notify_all()
        return handle

    def _cancel(self, handle: TimerHandle) -> None:
        with self._cond:
            if handle._fired or handle._cancelled:
                return
            handle._cancelled = True
            # Drop the closure while a cancelled handle remains in the heap.
            # Compaction or front removal later reclaims its small record.
            handle._callback = _NOOP
            # Avoid a scan on each cancel; _run drops front entries.
            # Compact once the cancelled share reaches half of a large heap.
            self._cancelled_pending += 1
            if (len(self._heap) >= self._COMPACT_MIN_HEAP
                    and self._cancelled_pending * 2 >= len(self._heap)):
                self._compact()
            self._cond.notify_all()

    def _compact(self) -> None:
        """Rebuild the heap without its cancelled handles. Caller holds the
        condition lock."""
        self._heap = [item for item in self._heap if not item[2]._cancelled]
        heapq.heapify(self._heap)
        self._cancelled_pending = 0

    def _run(self) -> None:
        with self._cond:
            while True:
                while self._heap and self._heap[0][2]._cancelled:
                    heapq.heappop(self._heap)
                    if self._cancelled_pending > 0:
                        self._cancelled_pending -= 1

                if not self._heap:
                    self._cond.wait()
                    continue

                due, seq, handle = self._heap[0]
                remaining = due - time.monotonic()
                if remaining > 0:
                    self._cond.wait(timeout=remaining)
                    continue

                heapq.heappop(self._heap)
                if handle._cancelled:
                    continue
                handle._fired = True
                self._dispatch(handle)

    @staticmethod
    def _dispatch(handle: TimerHandle) -> None:
        # Run off the scheduler thread. A slow callback must not delay another
        # timer due at about the same time.
        t = threading.Thread(target=TimerWheel._run_callback, args=(handle,), name=handle._name, daemon=True)
        t.start()

    @staticmethod
    def _run_callback(handle: TimerHandle) -> None:
        try:
            handle._callback()
        except Exception:
            log.opt(exception=True).error(f"timer_wheel: callback '{handle._name}' raised")


# One process-wide wheel. The screensaver reset, the overlay hide and the
# hold timer all share this single scheduler thread.
_default_wheel = TimerWheel(name="TimerWheel")


def schedule(delay_s: float, callback: Callable[[], object], name: str = "TimerWheelJob") -> TimerHandle:
    """Arm a one-shot timer on the process-wide wheel. See TimerWheel.schedule."""
    return _default_wheel.schedule(delay_s, callback, name=name)
