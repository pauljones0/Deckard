"""Track the media loop's pre-wait capacity rate, not its achieved cadence.

Below 80% warns; idle 2 Hz does not, and cross-thread settings can shift one update a tick.
"""
import statistics
from typing import Callable


class WorkRateMonitor:
    """Maintain a sliding work-rate window and edge-triggered warning.

    Record first; updates publish edges, disables publish False, and empty windows raise.
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

        Enabling resets standing-warning edge state; disabling hides the banner immediately.
        """
        self.warnings_enabled = state
        if state:
            self.published_warning = False
        else:
            self._publish(False)
