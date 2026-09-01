"""Guard Background.tiles, _video_strip, _touchscreen_slice, and _identified_tiles across threads.
Media writes; GTK/load/screensaver swap. Render/video locks stop tears and mid-read release."""

# Run real swap and read paths under contention; no thread may raise or publish inconsistent state.
import os
import threading
import time

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl
from fixtures import make_test_mp4, make_test_png, start_watchdog, teardown, wait_until
from loguru import logger as log

from PIL import Image

from src.backend.DeckManagement import media_loop
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.media_loop import MEDIA_LOOP_FPS

WATCHDOG_SECONDS = 60
STRESS_SECONDS = 1.5

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def check_touchscreen_tick_lock(controller) -> None:
    touch = controller.get_input(Input.Touchscreen("sd-plus"))
    state = touch.get_active_state()

    # Read video and advance its frame deadline under the background-video lock.
    check("touchscreen state exposes tick_background_video",
          hasattr(state, "tick_background_video"))
    if not hasattr(state, "tick_background_video"):
        return

    class _RatedVideo(media_loop.FrameScheduled):
        def _render_rate(self) -> float:
            return float(MEDIA_LOOP_FPS)

    saved = state.background_video
    try:
        video = _RatedVideo()
        state.background_video = video
        first = state.tick_background_video(media_loop.now())
        second = state.tick_background_video(media_loop.now())
        check("first tick renders, immediate second is rate-gated", first and not second,
              f"first={first} second={second}")
        check("the frame deadline advanced under the lock",
              video._frame_deadline is not None)
        state.background_video = None
        check("no background video means no render", state.tick_background_video(media_loop.now()) is False)
    finally:
        state.background_video = saved


def check_background_lock_stress(controller) -> None:
    background = controller.background
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()

    check("Background carries a render-state lock", hasattr(background, "_render_state_lock"))

    image_path = make_test_png(
        os.path.join(gl.DATA_PATH, "assets", "bg.png"), size=(400, 400), color=(20, 80, 200))
    video_path = make_test_mp4(os.path.join(gl.DATA_PATH, "assets", "bg.mp4"))

    background.set_extend_to_touchscreen(True, update=False)

    # update_tiles turns a torn video read into a rate-limited error.
    # Contention must produce neither that log nor a raised error.
    tile_errors: list[str] = []
    sink = log.add(lambda m: tile_errors.append(str(m)), level="ERROR",
                   filter=lambda r: "update background tiles" in r["message"].lower())

    errors: list[str] = []
    stop = threading.Event()

    def swapper() -> None:
        i = 0
        while not stop.is_set():
            try:
                background.set_from_path(image_path if i % 2 else video_path, update=False)
            except Exception as e:  # noqa: BLE001  (any raise is the finding)
                errors.append(f"swapper: {type(e).__name__}: {e}")
                return
            i += 1

    def reader() -> None:
        while not stop.is_set():
            try:
                background.update_tiles()
                ts = background.get_touchscreen_image()
                if ts is not None and not isinstance(ts, Image.Image):
                    errors.append(f"reader: get_touchscreen_image returned {type(ts).__name__}")
                    return
                tiles = background.tiles
                if len(tiles) != key_count:
                    errors.append(f"reader: tiles length {len(tiles)} != key_count {key_count}")
                    return
                background.get_identified_tile(0)
            except Exception as e:  # noqa: BLE001  (any raise is the finding)
                errors.append(f"reader: {type(e).__name__}: {e}")
                return

    threads = [threading.Thread(target=swapper, name="bg-swapper"),
               threading.Thread(target=reader, name="bg-reader-1"),
               threading.Thread(target=reader, name="bg-reader-2")]
    for t in threads:
        t.start()
    time.sleep(STRESS_SECONDS)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    log.remove(sink)

    check("no thread raised swapping the background under read contention",
          not errors, "; ".join(errors[:3]))
    check("update_tiles never tore a source read under contention",
          not tile_errors, "; ".join(tile_errors[:2]))
    check("no stress thread hung", all(not t.is_alive() for t in threads))
    _ = deck  # kept for parity with the other integration scenarios


def main() -> None:
    start_watchdog(WATCHDOG_SECONDS, label="scenario_background_render_lock")
    controller = fixtures.make_headless_controller(serial="bg-lock-1")
    try:
        wait_until(lambda: controller.active_page is not None, timeout=5)
        check_touchscreen_tick_lock(controller)
        check_background_lock_stress(controller)
    finally:
        teardown(controller)

    assert not FAILURES, f"background render-state lock gaps remain: {FAILURES}"
    print("PASS: scenario_background_render_lock")


if __name__ == "__main__":
    main()
