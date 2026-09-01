"""Advance InputVideo sequentially until its cache completes.
Wall-clock jumps during a build would decode each skipped intermediate frame."""
import threading

import fixtures
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo


class StubKeyVideoCache:
    """Count calls to the KeyVideoCache surface that InputVideo uses."""

    def __init__(self, n_frames: int):
        self.n_frames = n_frames
        self._complete = False
        self.decode_counts: dict[int, int] = {}
        self.call_log: list[int] = []  # every index actually requested, in order

    def is_cache_complete(self) -> bool:
        return self._complete

    def get_source_fps(self) -> float:
        return getattr(self, "source_fps", None)

    def get_frame(self, n: int):
        n = min(n, self.n_frames - 1)  # KeyVideoCache.get_frame does the same clamp
        self.decode_counts[n] = self.decode_counts.get(n, 0) + 1
        self.call_log.append(n)
        return n  # the frame is its own index, which is enough to assert on


def make_video(n_frames: int, fps: float = 10.0, loop: bool = True) -> InputVideo:
    video = InputVideo.__new__(InputVideo)
    video.fps = fps
    video.loop = loop
    video.natural_speed = False  # key and dial semantics, where fps is the playback rate
    video.active_frame = -1
    video._play_start = None
    video._last_frame_tick = None
    video.video_cache = StubKeyVideoCache(n_frames)
    video._close_lock = threading.Lock()  # __init__ sets this; __new__ bypasses it
    return video


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_keyvideo_build")
    T0 = 1_000_000.0

    # Advance one frame and decode once per build tick despite clock jumps.
    looping_video = make_video(n_frames=5, fps=10.0, loop=True)

    ticks = [T0, T0 + 0.01, T0 + 50.0, T0 + 50.02, T0 + 9000.0]  # erratic, to stress the sequential advance
    expected_sequence = [0, 1, 2, 3, 4]  # sequential, independent of the now argument
    for i, now in enumerate(ticks):
        frame = looping_video.get_next_frame(now=now)
        assert frame == expected_sequence[i], (
            f"building phase must advance sequentially regardless of wall-clock "
            f"jumps: tick {i} expected frame {expected_sequence[i]}, got {frame}"
        )
        assert looping_video.active_frame == expected_sequence[i]

    # Clock jumps must not decode skipped intermediate frame indexes.
    assert len(looping_video.video_cache.call_log) == len(ticks), (
        f"expected exactly {len(ticks)} get_frame calls (one per tick), "
        f"got {len(looping_video.video_cache.call_log)}: {looping_video.video_cache.call_log}"
    )
    assert all(count == 1 for count in looping_video.video_cache.decode_counts.values()), (
        f"each frame index must be requested exactly once, got {looping_video.video_cache.decode_counts}"
    )

    # One more tick wraps, because loop is True and n_frames is 5.
    wrapped = looping_video.get_next_frame(now=T0 + 9000.1)
    assert wrapped == 0 and looping_video.active_frame == 0, f"building-phase loop wrap: expected 0, got {wrapped}"

    # Non-looping build. active_frame may run past n_frames, because the clamp
    # in get_frame handles it, and it must not wrap.
    v_noloop = make_video(n_frames=3, fps=10.0, loop=False)
    for now in (T0, T0 + 1, T0 + 2, T0 + 3):
        v_noloop.get_next_frame(now=now)
    assert v_noloop.active_frame == 3, f"non-loop build must not wrap, got {v_noloop.active_frame}"
    assert v_noloop.video_cache.call_log[-1] == 2, "get_frame must clamp to the last valid index"

    # Flip to complete. Wall-clock picking engages, seeded from the current
    # position, so it continues from the build phase rather than restarting.
    looping_video.video_cache._complete = True
    pre_switch_active_frame = looping_video.active_frame  # 0, from the wrap above
    looping_video.video_cache.call_log.clear()
    looping_video.video_cache.decode_counts.clear()

    t0 = T0 + 20000.0
    first_complete = looping_video.get_next_frame(now=t0)
    # Seed from the next build frame and allow one-frame boundary float error.
    # BackgroundVideo uses the same play-start formula.
    expected_first = (pre_switch_active_frame + 1) % looping_video.video_cache.n_frames
    acceptable = {expected_first, (expected_first - 1) % looping_video.video_cache.n_frames}
    assert first_complete in acceptable, (
        f"wall-clock pick must seed from the build-phase position: "
        f"expected one of {acceptable}, got {first_complete}"
    )

    # After completion, jump seven frames for 0.7 seconds at 10 fps.
    jumped = looping_video.get_next_frame(now=t0 + 0.7)
    # Compute directly from the wall-clock formula rather than re-deriving the
    # frame arithmetic by hand, so frame = int((now - play_start) * fps).
    expected_jumped = int((t0 + 0.7 - looping_video._play_start) * looping_video.fps) % looping_video.video_cache.n_frames
    assert jumped == expected_jumped, f"expected wall-clock jump to frame {expected_jumped}, got {jumped}"
    # It must be a single free lookup, not a walk through intermediates.
    assert len(looping_video.video_cache.call_log) == 2, (
        f"wall-clock phase must do exactly one get_frame per get_next_frame call, "
        f"got {looping_video.video_cache.call_log}"
    )

    # Gap clamp once complete. A tick gap over 1 s, from a page-away resume,
    # shifts the timebase instead of fast-forwarding, mirroring BackgroundVideo.
    last_tick_before = looping_video._last_frame_tick
    play_start_before = looping_video._play_start
    GAP = 5.0
    looping_video.get_next_frame(now=last_tick_before + GAP)
    expected_play_start = play_start_before + (GAP - 1.0 / looping_video.fps)
    assert abs(looping_video._play_start - expected_play_start) < 1e-9, (
        f"gap clamp did not shift _play_start as expected: "
        f"{looping_video._play_start} != {expected_play_start}"
    )

    # Natural speed uses source fps; configured fps only quantizes render picks.
    natural_speed_video = make_video(n_frames=100, fps=5.0, loop=True)  # cap=5
    natural_speed_video.natural_speed = True
    natural_speed_video.video_cache.source_fps = 20.0  # native speed, 4x the cap
    natural_speed_video.video_cache._complete = True

    natural_speed_start = T0 + 40000.0
    natural_speed_video.get_next_frame(now=natural_speed_start)
    base = natural_speed_video._play_start
    # The position advances at the source fps, so after 1 s it must be about 20
    # frames on, not 5, which is what fps-as-speed would give.
    f_1s = natural_speed_video.get_next_frame(now=natural_speed_start + 1.0)
    assert f_1s == int(int((natural_speed_start + 1.0 - base) * 5.0) / 5.0 * 20.0) % 100, (
        f"natural-speed pick mismatch: got {f_1s}"
    )
    assert f_1s >= 15, (
        f"natural_speed must advance at source fps (~20 frames/s), got {f_1s} after 1s"
    )
    # Within one cap window, 0.2 s at cap 5, the pick must not advance.
    window_start_frame = natural_speed_video.get_next_frame(now=natural_speed_start + 2.00)
    same_window_frame = natural_speed_video.get_next_frame(now=natural_speed_start + 2.19)
    assert window_start_frame == same_window_frame, (
        f"picks within one 1/cap window must be identical (render cap), got {window_start_frame} then {same_window_frame}"
    )
    # The next window advances by source_fps over cap frames, which is 4.
    next_window_frame = natural_speed_video.get_next_frame(now=natural_speed_start + 2.21)
    assert next_window_frame != window_start_frame, "the next cap window must advance the pick"

    print("PASS: scenario_keyvideo_build")


if __name__ == "__main__":
    main()
