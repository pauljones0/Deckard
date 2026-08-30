"""Check source-rate deadlines, drift, stall recovery, and away-gap resync.

With a fake clock, stalls up to RESYNC_GAP_S catch up once; longer gaps resync.
"""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import time

from src.backend.DeckManagement import media_loop
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.media_loop import FrameDeadline, MEDIA_LOOP_FPS

TICK = 1.0 / MEDIA_LOOP_FPS


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def run_loop(deadline: FrameDeadline, clock: FakeClock, seconds: float) -> int:
    """Advance the fake clock in loop ticks and count due passes."""
    due = 0
    for _ in range(round(seconds / TICK)):
        clock.advance(TICK)
        if deadline.due(media_loop.now()):
            due += 1
    return due


def check_source_rate_fidelity() -> None:
    clock = FakeClock()
    media_loop.install_clock(clock)
    try:
        for rate, budget in ((24.0, 24), (20.0, 20), (12.0, 12), (30.0, 30), (60.0, 30)):
            deadline = FrameDeadline(rate)
            count = run_loop(deadline, clock, seconds=1.0)
            assert count <= budget + 1, (
                f"a {rate} fps source rendered {count} times in one second "
                f"on a {MEDIA_LOOP_FPS} Hz loop, budget {budget}")
            assert count >= budget - 1, (
                f"a {rate} fps source rendered only {count} times in one "
                f"second, expected about {budget}")
    finally:
        media_loop.install_clock(None)
    print("PASS: each source renders at its own rate on the shared loop")


def check_no_drift_over_a_minute() -> None:
    clock = FakeClock()
    media_loop.install_clock(clock)
    try:
        deadline = FrameDeadline(24.0)
        count = run_loop(deadline, clock, seconds=60.0)
        assert abs(count - 24 * 60) <= 1, (
            f"a 24 fps source rendered {count} frames in a simulated minute, "
            f"expected {24 * 60} within one frame; the schedule drifts")
    finally:
        media_loop.install_clock(None)
    print("PASS: the schedule holds rate over a minute with no drift")


def check_catch_up_without_burst() -> None:
    clock = FakeClock()
    media_loop.install_clock(clock)
    try:
        deadline = FrameDeadline(24.0)
        run_loop(deadline, clock, seconds=0.5)
        # A 0.2 s stall below RESYNC_GAP_S owes one render, not a missed-frame burst.
        clock.advance(0.2)
        assert deadline.due(media_loop.now()), (
            "the first tick after a stall must render")
        clock.advance(TICK / 10)
        assert not deadline.due(media_loop.now()), (
            "the tick right after a stall recovery rendered again: the "
            "deadline replays missed frames as a burst instead of dropping "
            "them")
        # The long-run average still holds after the stall.
        count = run_loop(deadline, clock, seconds=2.0)
        assert abs(count - 48) <= 2, (
            f"after a stall, 2 simulated seconds rendered {count} frames, "
            f"expected about 48; catch-up bends the average")
    finally:
        media_loop.install_clock(None)
    print("PASS: a stalled loop catches up without a frame burst")


def check_away_gap_resync() -> None:
    clock = FakeClock()
    media_loop.install_clock(clock)
    try:
        deadline = FrameDeadline(12.0)
        run_loop(deadline, clock, seconds=0.5)
        # A 10 s gap above RESYNC_GAP_S renders once, then re-seeds from return.
        clock.advance(10.0)
        assert deadline.due(media_loop.now()), (
            "the first tick after an away gap must render")
        clock.advance(1.0 / 12.0 / 2)
        assert not deadline.due(media_loop.now()), (
            "half a period after the resync the source rendered again, so "
            "the schedule kept the pre-gap timeline instead of re-seeding")
        clock.advance(1.0 / 12.0)
        assert deadline.due(media_loop.now()), (
            "one full period after the resync the source must render")
    finally:
        media_loop.install_clock(None)
    print("PASS: an away gap re-seeds the schedule at the gap end")


def check_source_render_rates() -> None:
    from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo
    from src.backend.DeckManagement.deck_controller.gif_pipeline import (
        GifBackground,
        KeyGIF,
    )

    # Bare instances isolate the fields used by each render-rate formula.
    video = InputVideo.__new__(InputVideo)
    video.video_cache = None
    video.natural_speed = False
    video.fps = 24
    assert video._render_rate() == 24.0, "a 24 fps page cap must render at 24"

    class _BuildingCache:
        def is_cache_complete(self) -> bool:
            return False

    video.video_cache = _BuildingCache()
    assert video._render_rate() == MEDIA_LOOP_FPS, (
        "a building cache advances one frame per render pass, so the build "
        "must run at loop rate, whatever the cap says")
    video.video_cache = None
    video.fps = 0
    assert video._render_rate() == MEDIA_LOOP_FPS, "a 0 cap plays at loop rate"
    video.natural_speed = True
    video.fps = MEDIA_LOOP_FPS
    video.native_fps = lambda: 24.0
    assert video._render_rate() == 24.0, (
        "natural speed must follow the 24 fps source under a loop-rate cap")
    video.fps = 12
    assert video._render_rate() == 12.0, (
        "a 12 fps cap must bound a faster natural source")

    gif = GifBackground.__new__(GifBackground)
    gif.fps = MEDIA_LOOP_FPS
    gif._fastest_frame_rate = 20.0
    gif._total_delay = 1.0
    assert gif._render_rate() == 20.0, (
        "a GIF whose shortest delay is 50ms must render at 20")

    key_gif = KeyGIF.__new__(KeyGIF)
    key_gif.fps = MEDIA_LOOP_FPS
    key_gif._fastest_frame_rate = float("inf")
    key_gif._total_delay = 0.0
    assert key_gif._render_rate() == MEDIA_LOOP_FPS, (
        "before the timeline is known, the cap alone paces a key GIF")
    key_gif._fastest_frame_rate = 20.0
    key_gif._total_delay = 1.0
    assert key_gif._render_rate() == 20.0, (
        "an adopted 50ms-delay timeline must pace a key GIF at 20")
    print("PASS: the per-class render rates follow source, cap, and loop")


class TickingClock(FakeClock):
    """A fake clock that creeps forward a microsecond per read, so code that
    divides by a measured duration never sees an exact zero."""

    def __call__(self) -> float:
        self.t += 1e-6
        return self.t


class FakeVideo(media_loop.FrameScheduled):
    """The minimum surface the writer tick reads off a background video."""

    def __init__(self, page, rate: float):
        self.page = page
        self.fps = MEDIA_LOOP_FPS
        self._rate = rate

    def _render_rate(self) -> float:
        return self._rate


def check_writer_paces_background_to_source_rate() -> None:
    controller, media_player, _deck_manager = fixtures.make_stub_controller(n_keys=1)
    clock = TickingClock()
    media_loop.install_clock(clock)
    try:
        background = controller.background
        background.video = FakeVideo(page=controller.active_page, rate=24.0)
        renders = {"n": 0}
        background.update_tiles = lambda: renders.__setitem__("n", renders["n"] + 1)
        background.get_touchscreen_image = lambda: None
        # The unit-tier stub inputs carry no media tick; the render pass the
        # background frame triggers must still be able to visit them.
        for key in controller.inputs.get(Input.Key, []):
            key.on_media_player_tick = lambda now, bg_frame_new: None

        seconds = 5
        for _ in range(MEDIA_LOOP_FPS * seconds):
            clock.advance(TICK)
            # A set wake event makes the inter-tick wait return at once, so
            # the check spends no real time.
            media_player._wake_event.set()
            assert media_player._run_one_tick(), "the tick asked to stop"

        per_second = renders["n"] / seconds
        assert per_second <= 24.5, (
            f"a 24 fps source rendered {per_second:.1f} times per simulated "
            f"second on the {MEDIA_LOOP_FPS} Hz loop; the deadline must cap "
            f"it at the source rate")
        assert per_second >= 22.0, (
            f"a 24 fps source rendered only {per_second:.1f} times per "
            f"simulated second; the deadline starves the source")
    finally:
        media_loop.install_clock(None)
        fixtures.teardown(controller)
    print("PASS: the writer paces the background video to its source rate")


def check_clock_seam() -> None:
    clock = FakeClock()
    media_loop.install_clock(clock)
    try:
        assert media_loop.now() == clock.t, "the installed clock is not read"
        clock.advance(5.0)
        assert media_loop.now() == clock.t, "the installed clock is stale"
    finally:
        media_loop.install_clock(None)
    before = time.monotonic()
    value = media_loop.now()
    after = time.monotonic()
    assert before <= value <= after, (
        "install_clock(None) did not restore the monotonic clock")
    print("PASS: the clock seam installs and restores")


fixtures.start_watchdog(90, "frame deadlines")
check_source_rate_fidelity()
check_no_drift_over_a_minute()
check_catch_up_without_burst()
check_away_gap_resync()
check_source_render_rates()
check_writer_paces_background_to_source_rate()
check_clock_seam()
print("SCENARIO PASS")
