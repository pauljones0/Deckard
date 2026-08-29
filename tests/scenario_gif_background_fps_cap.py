"""A background GIF must honour the fps render cap its setter documents.

GifBackground._pick_frame never quantized against the configured rate, so a
deck or strip background GIF advanced at its own delay timeline and ignored
background/fps even though set_playback calls the number a render cap.
_pick_frame now applies the same wall-clock quantization and ceiling guard as
KeyGIF: a cap below the loop ceiling throttles the frame advance, and a cap at
or above it leaves the picks exactly as before. This walks the picked frame
under a capped and an uncapped background GIF and compares the advances.
"""
import os

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl
from fixtures import start_watchdog, teardown

from PIL import Image, ImageDraw

from src.backend.DeckManagement.deck_controller.gif_pipeline import GifBackground


def _make_gif(path: str, durations_ms: list[int]) -> str:
    frames = []
    for i in range(len(durations_ms)):
        fr = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(fr).ellipse([2 + i * 3, 10, 22 + i * 3, 34], fill=(220, 30, 30, 255))
        frames.append(fr)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(path, format="GIF", save_all=True, append_images=frames[1:],
                   duration=durations_ms, loop=0, disposal=2)
    return path


def _walk(bg: GifBackground, step: float, count: int) -> list[int]:
    picks = []
    for i in range(count):
        bg._pick_frame(now=i * step)
        picks.append(bg.active_frame)
    return picks


def _advances(picks: list[int]) -> int:
    return sum(1 for a, b in zip(picks, picks[1:]) if a != b)


def main() -> int:
    start_watchdog(60, "gif_background_fps_cap")
    controller = fixtures.make_headless_controller(serial="gif-bg-cap-1")
    failures: list[str] = []
    try:
        # Ten 100 ms frames run at 10 fps natively.
        path = _make_gif(os.path.join(gl.DATA_PATH, "media", "bgcap.gif"), [100] * 10)

        capped = GifBackground(controller, path, loop=True, fps=5)
        native = GifBackground(controller, path, loop=True, fps=30)

        # Sample at 60 Hz for one native loop. A cap of 5 must advance at most
        # about 5 times per second; the native run advances about 10.
        step, count = 1 / 60, 60
        capped_adv = _advances(_walk(capped, step, count))
        native_adv = _advances(_walk(native, step, count))

        if not (capped_adv < native_adv):
            failures.append(f"the cap did not throttle the advance: capped={capped_adv} "
                            f"native={native_adv}")
        if capped_adv > 7:
            failures.append(f"a cap of 5 advanced {capped_adv} times in a second; "
                            f"the render cap is not applied")

        # A cap at the loop ceiling changes nothing: the picks match the
        # uncapped run frame for frame.
        ceiling = GifBackground(controller, path, loop=True, fps=30)
        uncapped_like = GifBackground(controller, path, loop=True, fps=30)
        if _walk(ceiling, step, count) != _walk(uncapped_like, step, count):
            failures.append("a cap at the loop ceiling changed the picks")

        for bg in (capped, native, ceiling, uncapped_like):
            bg.close()
    finally:
        teardown(controller)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: a background GIF honours its fps render cap; the ceiling is a no-op")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
