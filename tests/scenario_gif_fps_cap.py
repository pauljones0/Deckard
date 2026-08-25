"""A GIF key must honour the page's frame-rate cap inside the GIF pipeline.

fps caps how often the picked frame advances. It never becomes the playback
rate: the GIF's own delay timeline still says where the wall clock lands, and
the cap only coarsens how finely that position is read, which drops frames. A
cap at the media loop's ceiling must leave the picks exactly as they were
before caps existed, which is what every page that carries no fps key loads
under.

The ordering of that arithmetic is pinned here, because getting it wrong is
silent and severe. A grid laid on raw elapsed time and then folded through the
loop modulo samples only the positions one cap period generates in the loop:
those walk backwards through the animation, and collapse onto a single frame
whenever the animation length divides the cap period, which stops the GIF
dead.

The clock is driven through get_next_frame(now=...), so nothing here sleeps.
"""
import os

import fixtures  # noqa: F401  (import first: isolated data dir + sys.path)

from PIL import Image, ImageDraw

import globals as gl
from src.backend.DeckManagement.DeckController import KeyGIF


class _StubDeckController:
    """Exactly what KeyGIF.__init__ reads, as in scenario_gif_delays."""

    def get_key_image_size(self) -> tuple[int, int]:
        return (72, 72)

    def get_display_saturation(self) -> float:
        return 1.0


class _StubControllerKey:
    def __init__(self):
        self.deck_controller = _StubDeckController()


def _make_gif(path: str, durations_ms: list[int], size=(64, 64)) -> str:
    """An animated GIF with one explicit duration per frame.

    The frames are visually distinct, so PIL never merges them at save time.
    They keep a transparent margin, so the rendered-alpha verdict holds the
    GIF on the retained frame list and no tile-cache encode runs here.
    """
    frames = []
    for i in range(len(durations_ms)):
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        x0 = 2 + i * 3
        draw.ellipse([x0, 10, x0 + 20, 34], fill=(220, 30, 30, 255))
        frames.append(frame)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations_ms, loop=0, disposal=2,
    )
    return path


def _decode(name: str, durations_ms: list[int], fps: int = 30,
            loop: bool = True) -> KeyGIF:
    path = _make_gif(os.path.join(gl.DATA_PATH, "media", name), durations_ms)
    return KeyGIF(controller_key=_StubControllerKey(), gif_path=path,
                  fps=fps, loop=loop)


def _walk(gif: KeyGIF, t0: float, step: float, count: int) -> list[int]:
    """The picked index at every step, for count steps from t0. The steps stay
    well under the one-second away-gap threshold, so the gap clamp never
    fires and the walk reads the plain timeline."""
    picks = []
    for i in range(count):
        gif.get_next_frame(now=t0 + i * step)
        picks.append(gif.active_frame)
    return picks


def _advances(picks: list[int]) -> int:
    return sum(1 for before, after in zip(picks, picks[1:]) if before != after)


def _illegal_backward_steps(picks: list[int]) -> list[tuple[int, int]]:
    """Every step that goes backwards through the animation for a reason
    other than the loop wrapping.

    Correct playback sweeps the picked frames upward and then wraps once per
    pass, from the highest frame it shows to the lowest. Any other decrease
    means the sampled position itself moved backwards, which is what a grid
    folded through the loop modulo does.
    """
    lowest, highest = min(picks), max(picks)
    return [(before, after) for before, after in zip(picks, picks[1:])
            if after < before and not (before == highest and after == lowest)]


T0 = 1_000_000.0  # arbitrary wall-clock base, far from 0 to catch base-0 bugs


def check_cap_limits_advances() -> int:
    """A cap below the loop ceiling must throttle the frame advance.

    Ten 100 ms frames run at 10 fps natively. Under a cap of 5 no more than
    five of them may reach the key each second.
    """
    capped = _decode("fps_cap_capped.gif", [100] * 10, fps=5)
    native = _decode("fps_cap_native.gif", [100] * 10, fps=30)
    try:
        capped_picks = _walk(capped, T0, 0.01, 200)   # two whole loops
        native_picks = _walk(native, T0, 0.01, 200)

        if len(set(native_picks)) != 10:
            print(f"FAIL(setup): the uncapped GIF showed "
                  f"{len(set(native_picks))} of its 10 frames")
            return 1
        if len(set(capped_picks)) != 5:
            print(f"FAIL(cap): a cap of 5 on a 10 fps GIF let "
                  f"{len(set(capped_picks))} distinct frames through per loop, "
                  f"not 5 -- fps does not reach the GIF timeline")
            return 1
        if _advances(capped_picks) > 10:
            print(f"FAIL(cap): {_advances(capped_picks)} frame advances in two "
                  f"seconds under a cap of 5 -- the cap must allow at most 10")
            return 1
        if _advances(capped_picks) < 8:
            print(f"FAIL(cap): only {_advances(capped_picks)} advances in two "
                  f"seconds under a cap of 5 -- the GIF has nearly stopped")
            return 1
        print("PASS: a cap of 5 lets 5 of a 10 fps GIF's frames through per second")
        return 0
    finally:
        capped.close()
        native.close()


def check_ceiling_cap_changes_nothing() -> int:
    """A cap at the loop ceiling must leave the timeline untouched.

    Ten 20 ms frames run at 50 fps, a rate whose frame edges do not land on
    the ceiling's own 1/30 s grid. Quantizing at the ceiling would therefore
    drop frames from a GIF that carries no cap at all.
    """
    gif = _decode("fps_cap_fast.gif", [20] * 10, fps=30)
    try:
        picks = _walk(gif, T0, 0.002, 100)  # one whole 0.2 s loop
        if sorted(set(picks)) != list(range(10)):
            print(f"FAIL(ceiling): a 50 fps GIF with no cap showed only "
                  f"{sorted(set(picks))} -- the ceiling cap must not quantize "
                  f"the timeline of an uncapped GIF")
            return 1
        print("PASS: a cap at the loop ceiling leaves an uncapped GIF's picks alone")
        return 0
    finally:
        gif.close()


def check_cap_never_freezes_the_animation() -> int:
    """The lowest cap on a common GIF must still animate.

    Ten 100 ms frames is a one-second loop, the shape a stock GIF most often
    has, and 1 is the bottom of the sidebar's range. A cap period as long as
    the loop puts every sample on the same position: with the grid laid on
    raw elapsed time that position is the start of the loop, and the GIF
    stands on frame 0 for as long as it is shown.
    """
    gif = _decode("fps_cap_freeze.gif", [100] * 10, fps=1)
    try:
        picks = _walk(gif, T0, 0.05, 1200)  # a full minute of playback
        if _advances(picks) == 0:
            print(f"FAIL(freeze): a cap of 1 on a one-second GIF froze it on "
                  f"frame {picks[0]} for a whole minute -- the cap must read "
                  f"every pass at least twice, so the GIF runs slowly instead "
                  f"of stopping")
            return 1
        if len(set(picks)) < 2:
            print(f"FAIL(freeze): a cap of 1 showed only frame "
                  f"{picks[0]} across a minute")
            return 1
        if _illegal_backward_steps(picks):
            print(f"FAIL(freeze): the lowest cap walked backwards through the "
                  f"animation: {_illegal_backward_steps(picks)[:4]}")
            return 1
        print("PASS: the lowest cap slows a one-second GIF instead of freezing it")
        return 0
    finally:
        gif.close()


def check_position_does_not_depend_on_the_pass() -> int:
    """The same position in the animation must pick the same frame, whichever
    pass of the loop it falls in.

    This is the ordering rule itself. A grid on raw elapsed time folded
    through the modulo rotates the sampled positions from pass to pass, so
    one moment in the animation shows a different frame each time round.
    """
    cases = [
        ("fps_cap_phase_a.gif", [100] * 5, 3, 0.35),
        ("fps_cap_phase_b.gif", [100] * 7, 3, 0.45),
    ]
    for name, delays, cap, phase in cases:
        gif = _decode(name, delays, fps=cap)
        try:
            total = sum(delays) / 1000.0
            # Fix the start of playback first. The first pick is what sets it,
            # so sampling straight at the position under test would put that
            # position at zero and prove nothing.
            gif.get_next_frame(now=T0)
            # One sample per pass, at the same position each time. The step
            # stays under the one-second away-gap threshold.
            picks = []
            for p in range(10):
                gif.get_next_frame(now=T0 + p * total + phase)
                picks.append(gif.active_frame)
            if len(set(picks)) != 1:
                print(f"FAIL(phase): {name} at cap {cap} showed frames "
                      f"{picks} at the same position {phase}s into the "
                      f"animation on ten successive passes -- the sampled "
                      f"position must not rotate with the pass")
                return 1
            if picks[0] == 0:
                print(f"FAIL(phase): {name} at cap {cap} sat on frame 0 at "
                      f"{phase}s in, so the check proves nothing")
                return 1
        finally:
            gif.close()
    print("PASS: one position in the animation picks one frame, pass after pass")
    return 0


def check_no_backward_steps() -> int:
    """Across several shapes and caps, playback must never step backwards
    except at the loop wrap."""
    cases = [
        ("fps_cap_fwd_a.gif", [100] * 10, 5),
        ("fps_cap_fwd_b.gif", [100] * 5, 3),
        ("fps_cap_fwd_c.gif", [150] * 7, 4),
        ("fps_cap_fwd_d.gif", [100] * 10, 1),
        ("fps_cap_fwd_e.gif", [40, 200, 60, 300, 100], 6),
    ]
    for name, delays, cap in cases:
        gif = _decode(name, delays, fps=cap)
        try:
            picks = _walk(gif, T0, 0.01, 500)  # five seconds
            bad = _illegal_backward_steps(picks)
            if bad:
                print(f"FAIL(order): {name} at cap {cap} stepped backwards "
                      f"through the animation {len(bad)} times, e.g. "
                      f"{bad[:4]} -- only the loop wrap may go back")
                return 1
        finally:
            gif.close()
    print("PASS: no shape steps backwards through its animation under a cap")
    return 0


def check_non_loop_reaches_the_last_frame() -> int:
    """A capped GIF that does not loop must still settle on its last frame.

    That frame is what the key shows from then on, so the cap must not leave
    playback resting in the middle of the animation. The last frame here is
    far shorter than one cap period, so reading the position after the end
    clamp instead of before it lands short of it.
    """
    delays = [300, 300, 300, 40]
    gif = _decode("fps_cap_noloop.gif", delays, fps=3, loop=False)
    try:
        gif.get_next_frame(now=T0)
        step = 0.5
        while step <= 5.0:
            gif.get_next_frame(now=T0 + step)
            step += 0.5
        last = len(delays) - 1
        if gif.active_frame != last:
            print(f"FAIL(noloop): a capped GIF that does not loop rested on "
                  f"frame {gif.active_frame} of {last} -- the key would show "
                  f"the middle of the animation for ever")
            return 1
        print("PASS: a capped GIF that does not loop settles on its last frame")
        return 0
    finally:
        gif.close()


def check_degenerate_cap() -> int:
    """A zero or negative cap must not divide by zero.

    Zero reads as no cap, which is what a page carrying no fps key loads
    under. A negative one clamps to the one-per-second floor, which InputVideo
    does with the same value, and then the two-reads-per-pass floor lifts it
    to whatever keeps the animation moving.
    """
    gif = _decode("fps_cap_zero.gif", [100] * 10, fps=30)
    try:
        for bad in (0, -5):
            gif.set_playback(fps=bad, loop=True)
            gif._play_start = None
            gif._last_frame_tick = None
            picks = _walk(gif, T0, 0.01, 100)
            if bad == 0 and len(set(picks)) < 9:
                print(f"FAIL(zero): fps=0 left the GIF on {len(set(picks))} "
                      f"frames -- zero must read as no cap")
                return 1
            if _advances(picks) == 0:
                print(f"FAIL(degenerate): fps={bad} froze the GIF on frame "
                      f"{picks[0]}")
                return 1
            if _illegal_backward_steps(picks):
                print(f"FAIL(degenerate): fps={bad} stepped backwards through "
                      f"the animation")
                return 1
        print("PASS: a zero or negative cap raises nothing and keeps the GIF moving")
        return 0
    finally:
        gif.close()


def check_live_cap_change() -> int:
    """set_playback must retune a GIF that is already playing.

    Without it a sidebar edit waits for a page reload.
    """
    gif = _decode("fps_cap_live.gif", [100] * 10, fps=2)
    try:
        before = _walk(gif, T0, 0.01, 100)
        if len(set(before)) != 2:
            print(f"FAIL(live): a cap of 2 let {len(set(before))} frames "
                  f"through in one second, not 2")
            return 1

        gif.set_playback(fps=10, loop=True)
        after = _walk(gif, T0 + 1.0, 0.01, 100)
        if len(set(after)) <= len(set(before)):
            print(f"FAIL(live): raising the cap from 2 to 10 left the GIF on "
                  f"{len(set(after))} frames per second against "
                  f"{len(set(before))} before -- set_playback does not reach "
                  f"the timeline")
            return 1
        if gif.loop is not True:
            print("FAIL(live): set_playback dropped the loop flag")
            return 1
        print("PASS: set_playback retunes a playing GIF without a page reload")
        return 0
    finally:
        gif.close()


def check_native_rate() -> int:
    """native_fps must report the rate the GIF plays at with no cap."""
    even = _decode("fps_cap_native_even.gif", [100] * 10)
    uneven = _decode("fps_cap_native_uneven.gif", [100, 300])
    try:
        rate = even.native_fps()
        if rate is None or abs(rate - 10.0) > 0.01:
            print(f"FAIL(native): ten 100 ms frames run at 10 fps, reported {rate}")
            return 1
        # Two frames across 0.4 s average 5 fps. An irregular GIF has no single
        # rate, so the row shows the average of the whole loop.
        rate = uneven.native_fps()
        if rate is None or abs(rate - 5.0) > 0.01:
            print(f"FAIL(native): a 100 ms plus 300 ms GIF averages 5 fps, "
                  f"reported {rate}")
            return 1
        even.close()
        if even.native_fps() is not None:
            print("FAIL(native): a closed GIF still reports a rate, from a "
                  "timeline it no longer holds")
            return 1

        # Frames but no time: a timeline of zero-length windows. Dividing by
        # it raises, so the guard must answer None instead.
        degenerate = KeyGIF.__new__(KeyGIF)
        degenerate._cum_delays = [0.0, 0.0, 0.0]
        degenerate._total_delay = 0.0
        try:
            rate = degenerate.native_fps()
        except ZeroDivisionError:
            print("FAIL(native): a timeline of zero-length windows divided by "
                  "zero instead of reporting no usable rate")
            return 1
        if rate is not None:
            print(f"FAIL(native): a timeline of zero-length windows reported "
                  f"{rate!r}, not None")
            return 1
        print("PASS: native_fps reports the uncapped rate, and None with no timeline")
        return 0
    finally:
        even.close()
        uneven.close()


def check_constructor_takes_the_cap() -> int:
    """The page's fps must reach the object the page load builds."""
    gif = _decode("fps_cap_ctor.gif", [100] * 4, fps=7)
    try:
        if gif.fps != 7:
            print(f"FAIL(ctor): KeyGIF built with fps=7 holds {gif.fps} -- the "
                  f"page dict value never reaches the pipeline")
            return 1
        print("PASS: the page's fps reaches the KeyGIF the page load builds")
        return 0
    finally:
        gif.close()


def main() -> int:
    # KeyGIF reads performance.cache-videos at construction, so the stub tier
    # has to exist for the setting to be readable. Every GIF here carries
    # alpha and stays on the retained frame list either way, and the picked
    # index this scenario asserts on is the same on both routes.
    fixtures.install_stub_globals({"performance": {"cache-videos": True}})
    fixtures.start_watchdog(60, label="scenario_gif_fps_cap")
    rc = check_cap_limits_advances()
    rc |= check_ceiling_cap_changes_nothing()
    rc |= check_cap_never_freezes_the_animation()
    rc |= check_position_does_not_depend_on_the_pass()
    rc |= check_no_backward_steps()
    rc |= check_non_loop_reaches_the_last_frame()
    rc |= check_degenerate_cap()
    rc |= check_live_cap_change()
    rc |= check_native_rate()
    rc |= check_constructor_takes_the_cap()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
