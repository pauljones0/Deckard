"""Provide the shared loop rate, monotonic clock, and per-source frame deadlines.

All consumers use MEDIA_LOOP_FPS; tests can replace the clock, but resume-gap checks use wall time.
"""
import time
from collections.abc import Callable

MEDIA_LOOP_FPS = 30

_clock: Callable[[], float] = time.monotonic


def now() -> float:
    """The scheduling clock: monotonic seconds, or the installed fake."""
    return _clock()


def install_clock(clock: Callable[[], float] | None) -> None:
    """Install a replacement clock, or restore monotonic with None."""
    global _clock
    _clock = time.monotonic if clock is None else clock


class FrameDeadline:
    """Schedule 24 FPS on 30 Hz at most 24 times each second; early calls return False.

    Late ticks skip frames without drift or replay; gaps over RESYNC_GAP_S reseed.
    """

    RESYNC_GAP_S = 1.0

    def __init__(self, rate: float):
        self.set_rate(rate)
        self._next_due: float | None = None

    def set_rate(self, rate: float) -> None:
        """Set source FPS without resetting phase.

        Nonpositive rates use loop rate; rates at or above loop rate make every tick due.
        """
        if rate <= 0:
            rate = MEDIA_LOOP_FPS
        self._period = 1.0 / rate

    def due(self, now: float) -> bool:
        """Report whether a frame is owed at `now`, and advance if so."""
        if self._next_due is None:
            # First ask renders at once and seeds the timeline.
            self._next_due = now + self._period
            return True
        if now < self._next_due:
            return False
        gap = now - self._next_due
        if gap > self.RESYNC_GAP_S:
            self._next_due = now + self._period
            return True
        # Advance by at least one whole period on the original timeline, always
        # moving the deadline past `now`.
        periods = int(gap / self._period) + 1
        self._next_due += periods * self._period
        return True


class FrameScheduled:
    """Gate animated renders at the source rate to avoid between-frame composition.

    Read _render_rate() each tick so playback-rate changes apply immediately.
    """

    _frame_deadline: FrameDeadline | None = None

    def _render_rate(self) -> float:
        raise NotImplementedError

    def frame_due(self, now: float) -> bool:
        deadline = self._frame_deadline
        if deadline is None:
            deadline = FrameDeadline(self._render_rate())
            self._frame_deadline = deadline
        else:
            deadline.set_rate(self._render_rate())
        return deadline.due(now)
