"""Work-rate window and low-FPS warning state for the media loop.

This module is the authoritative statement of the metric's semantics. The
loop records the inverse of each tick's work duration, taken before the
scheduled wait. That value is a work-rate: a capacity ceiling that says how
fast the loop could run if it never waited. It is not the achieved loop
rate. The idle cadence parks the achieved rate near 2 Hz on purpose, and
the media-prof report logs the achieved rate under its own name, loop_fps,
when profiling is enabled. Nothing in this module carries an fps-style name
for the recorded value, so an operator cannot tune from a false FPS
reading.

The warning keys on capacity. A median work-rate below the warn fraction of
the target rate means the loop cannot hold that rate even with a zero wait.
A short work duration yields a high work-rate no matter how long the
scheduled wait is, so an idle deck never trips the warning.

Threading: the media thread records and updates, the settings window
toggles the enable flag, and a torn read of the flag only shifts one
update by a tick.
"""
import statistics
from typing import Callable


class WorkRateMonitor:
    """Sliding work-rate window plus edge-triggered low-FPS warning state.

    record() must run before update_warning() on every tick, which is the
    order the loop guarantees; update_warning() on an empty window raises.
    The publish callable receives the new warning state only on a state
    change, so a holding state never re-sends its banner.
    """

    # The warning shows once the median work-rate falls below this fraction
    # of the target rate.
    WARN_FRACTION = 0.8

    def __init__(self, target_fps: int, warnings_enabled: bool,
                 publish: Callable[[bool], None]) -> None:
        # The target rate is a snapshot: the writer assigns its FPS once in
        # __init__ and never rewrites it.
        self.target_fps = target_fps
        self.window_size = target_fps * 2
        self.warnings_enabled = warnings_enabled
        self._publish = publish
        self.work_rates: list[float] = []
        self.published_warning = False

    def record(self, rate: float) -> None:
        self.work_rates.append(rate)
        if len(self.work_rates) > self.window_size:
            self.work_rates.pop(0)

    def median_work_rate(self) -> float:
        return statistics.median(self.work_rates)

    def update_warning(self) -> None:
        if not self.warnings_enabled:
            return

        show_warning = self.median_work_rate() < self.target_fps * self.WARN_FRACTION
        if self.published_warning == show_warning:
            return
        self.published_warning = show_warning

        self._publish(show_warning)

    def set_enabled(self, state: bool) -> None:
        """Enable or disable the warning, mirroring the settings toggle.

        Enabling resets the edge state so the next update can show a
        still-standing warning. Disabling hides the banner at once.
        """
        self.warnings_enabled = state
        if state:
            self.published_warning = False
        else:
            self._publish(False)
