"""The writer's per-tick metric is a work-rate, and the low-FPS warning keys
on capacity, not on the achieved cadence. loop_metrics.py owns the full
semantics; these checks pin them.

  (1) The recorded value is the inverse of the tick's work duration, taken
      before the scheduled wait: a live idle writer with slow work records
      rates far above its 2 Hz cadence.
  (2) A fast workload at a slow scheduled cadence never reveals the
      warning; a loop whose work alone runs below the warn fraction of the
      target rate reveals it once, and recovery hides it once.
  (3) The writer exposes no fps-named per-tick metric an operator could
      tune from.
  (4) The settings toggle hides the banner at once on disable, pushes
      nothing on enable, and a disabled monitor pushes nothing at all.

The warning checks drive the writer's WorkRateMonitor in the exact
record-then-update sequence the loop runs, so the banner pushes go through
the writer's real publish path into a recording UI port. The work-rate
check runs the real writer thread over the stub controller.
"""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import time

from src.backend import ui_port
from src.backend.DeckManagement.deck_controller.media_writer import (
    MediaPlayerThread,
)

# The target rate is 30, so the warning threshold sits at 24. These two
# rates land clearly on either side of it.
FAST_RATE = 500.0   # 2ms of work per tick
LATE_RATE = 20.0    # 50ms of work per tick

# Injected work duration for the live-thread check, and the bounds the
# recorded rates must land in. The idle cadence waits toward 2 Hz, so a
# value recorded after the wait could never exceed ~2.2; the injected 20ms
# of work bounds a before-the-wait value at 50. The lower bound leaves room
# for scheduler noise stretching the measured work duration.
INJECTED_WORK_S = 0.02
WORK_RATE_MIN = 3.0
WORK_RATE_MAX = 55.0


class RecordingPort(ui_port.UIPort):
    """Captures every banner push the writer makes."""

    def __init__(self):
        self.states: list[bool] = []

    def set_low_fps_warning(self, controller, shown: bool) -> None:
        self.states.append(shown)


def make_writer(warnings_enabled: bool = True) -> MediaPlayerThread:
    # A real writer over the stub controller, not started; the checks drive
    # the metric path directly. The toggle goes through
    # set_show_fps_warnings, the same facade the settings window calls; no
    # port is installed yet, so the disable push lands in the null port.
    _controller, media_player, _deck_manager = fixtures.make_stub_controller()
    media_player.set_show_fps_warnings(warnings_enabled)
    return media_player


def tick(writer: MediaPlayerThread, rate: float, n: int = 1) -> None:
    """Run the loop's per-tick metric sequence n times at a fixed work-rate."""
    for _ in range(n):
        writer.metrics.record(rate)
        writer.metrics.update_warning()


def check_recorded_value_is_work_rate() -> None:
    controller, media_player, _deck_manager = fixtures.make_stub_controller()
    media_player.set_show_fps_warnings(False)

    # Inject work into every tick through a call the tick body makes before
    # the wait. The idle target stays 2 Hz because nothing animates.
    real_needs = media_player._needs_key_ticks

    def slow_needs():
        time.sleep(INJECTED_WORK_S)
        return real_needs()

    media_player._needs_key_ticks = slow_needs
    media_player.start()
    try:
        deadline = time.monotonic() + 10.0
        while (len(media_player.metrics.work_rates) < 3
               and time.monotonic() < deadline):
            time.sleep(0.05)
        rates = list(media_player.metrics.work_rates)
        assert len(rates) >= 3, "the live writer recorded no work-rates"
        for rate in rates[:3]:
            assert WORK_RATE_MIN <= rate <= WORK_RATE_MAX, (
                f"a live idle tick recorded {rate:.2f}: a value at the ~2 Hz "
                f"cadence means the record happens after the scheduled wait "
                f"and measures achieved rate; a value near {FAST_RATE} means "
                f"the injected work went unmeasured")
    finally:
        media_player.stop()
        fixtures.teardown(controller)
    print("PASS: the recorded value is the pre-wait work-rate")


def check_metric_naming() -> None:
    writer = make_writer()
    for fps_name in ("append_fps", "get_median_fps"):
        assert not hasattr(writer, fps_name), (
            f"the writer exposes {fps_name}(), which reads as an achieved"
            f" FPS figure; the per-tick metric must carry work-rate naming")
    assert not isinstance(getattr(writer, "fps", None), list), (
        "the writer keeps its per-tick samples in a list named fps")
    assert hasattr(writer.metrics, "record"), "the work-rate monitor is missing"
    assert hasattr(writer.metrics, "median_work_rate"), (
        "median_work_rate() is missing from the monitor")
    print("PASS: the per-tick metric carries work-rate naming only")


def check_fast_work_slow_cadence_stays_quiet() -> None:
    writer = make_writer()
    port = RecordingPort()
    ui_port.install(port)
    try:
        # Two full windows of fast work. The achieved cadence plays no part
        # in the recorded value, so nothing here can look slow.
        tick(writer, FAST_RATE, n=writer.FPS * 4)
        assert port.states == [], (
            f"a fast workload pushed banner states {port.states}; an idle "
            f"2 Hz cadence must not read as a low-FPS condition")
    finally:
        ui_port.install(None)
    print("PASS: fast work at a slow cadence stays quiet")


def check_late_loop_warns_once_and_recovers_once() -> None:
    writer = make_writer()
    port = RecordingPort()
    ui_port.install(port)
    try:
        # The very first late sample makes the window median 20, so the
        # warning shows at once and shows exactly once.
        tick(writer, LATE_RATE, n=writer.FPS * 2)
        assert port.states == [True], (
            f"a late loop pushed {port.states}, expected one True: the "
            f"warning must show once and not repeat while the state holds")

        # A full window of fast samples evicts every late one, so the median
        # recovers and the banner hides, again exactly once.
        tick(writer, FAST_RATE, n=writer.FPS * 2)
        assert port.states == [True, False], (
            f"recovery pushed {port.states}, expected [True, False]")
    finally:
        ui_port.install(None)
    print("PASS: a late loop warns once and recovery hides once")


def check_settings_toggle_contract() -> None:
    writer = make_writer()
    port = RecordingPort()
    ui_port.install(port)
    try:
        # Disable hides the banner at once, whatever it showed before. This
        # is the facade the settings window calls.
        writer.set_show_fps_warnings(False)
        assert port.states == [False], (
            f"disable pushed {port.states}, expected an immediate [False]")

        # A disabled monitor pushes nothing, however late the loop runs.
        tick(writer, LATE_RATE, n=writer.FPS * 2)
        assert port.states == [False], (
            f"a disabled monitor pushed: {port.states}")

        # Enable pushes nothing by itself; the still-late window shows the
        # warning on the next update, so a standing condition survives an
        # off/on cycle.
        writer.set_show_fps_warnings(True)
        assert port.states == [False], (
            f"enable pushed {port.states} on its own, expected no new push")
        tick(writer, LATE_RATE)
        assert port.states == [False, True], (
            f"a standing late condition pushed {port.states} after re-enable,"
            f" expected [False, True]")
    finally:
        ui_port.install(None)
    print("PASS: the settings toggle keeps its push contract")


def check_window_stays_bounded() -> None:
    writer = make_writer(warnings_enabled=False)
    tick(writer, FAST_RATE, n=writer.FPS * 10)
    assert len(writer.metrics.work_rates) == writer.metrics.window_size, (
        f"the sample window holds {len(writer.metrics.work_rates)} entries, "
        f"expected exactly {writer.metrics.window_size}")
    print("PASS: the sample window stays bounded")


fixtures.start_watchdog(90, "fps metric semantics")
check_recorded_value_is_work_rate()
check_metric_naming()
check_fast_work_slow_cadence_stays_quiet()
check_late_loop_warns_once_and_recovers_once()
check_settings_toggle_contract()
check_window_stays_bounded()
print("SCENARIO PASS")
