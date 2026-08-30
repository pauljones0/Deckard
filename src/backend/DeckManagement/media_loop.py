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
    """Schedules one source's frames on its own timeline.

    due(now) answers one question per loop tick: does this source owe a
    frame now? Between deadlines it answers False, so a 24 fps source on a
    30 Hz loop renders at most 24 times a second instead of on every tick.

    A late tick advances the schedule by whole periods on the original
    timeline, so delays do not accumulate into drift, and the frames a
    stall skipped are dropped rather than replayed as a burst: a picker
    with a complete cache selects frames by elapsed time, so the next
    render already shows the current frame, and a sequential build renders
    at loop rate. A gap longer than RESYNC_GAP_S is an away gap: ticks
    stop while a page is hidden, and on return the schedule re-seeds from
    the gap end, matching the pickers' own timebase-shift handling.
    """

    RESYNC_GAP_S = 1.0

    def __init__(self, rate: float):
        self.set_rate(rate)
        self._next_due: float | None = None

    def set_rate(self, rate: float) -> None:
        """Set the source rate in frames per second, keeping the phase.

        A rate at or above the loop rate makes every tick due, which the
        period arithmetic handles with no special case. A rate of zero or
        below is a caller error the sites guard against, but a degenerate
        value degrades to the loop rate rather than dividing by zero.
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
        # Advance by whole periods past `now`, staying on the original
        # timeline. The count is at least one, so a due tick always moves
        # the deadline forward.
        periods = int(gap / self._period) + 1
        self._next_due += periods * self._period
        return True


class FrameScheduled:
    """Mixin for animated sources the media loop schedules per tick.

    frame_due(now) gates a render pass on the source's own rate, so a
    source below the loop rate pays no composite work on the ticks between
    its frames. The subclass reports its rate through _render_rate(), read
    on every ask so a playback change takes effect at once.
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
