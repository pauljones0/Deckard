"""The media loop's declared rate, its clock, and per-source frame deadlines.

This is a leaf module: it imports only the standard library, so every layer
that needs the loop rate can import it without a cycle. Every follower of
the rate reads MEDIA_LOOP_FPS from here: the writer's loop, the media and
page defaults, the sidebar's fps range, and the scroll cadence. The number
was previously declared independently at each of those sites.

The clock is monotonic. Deadlines, durations, and rate gates must not move
when the wall clock steps, so wall time stays out of scheduling entirely.
The one deliberate exception is the writer's resume-gap check, which keeps
wall time because the monotonic clock stops across a system suspend. Tests
install a controllable clock through install_clock(), which is the
deterministic seam the frame-deadline scenarios run on.
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
    """Schedule one source on its timeline, so a 24 FPS source on a 30 Hz loop renders at most 24 times per second.

    Calls before a deadline return False; late ticks skip whole periods without drift or replay, and gaps over RESYNC_GAP_S reseed at the end.
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
    """Gate animated-source renders on each source's current rate, avoiding composition between frames.

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
