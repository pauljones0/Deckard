"""Use named deck workers while all eight shared application workers are busy.
Cancel queued superseded work and reject submissions after deck close."""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import threading

import globals as gl

from fixtures import make_headless_controller, teardown
from src.backend import main_loop


def _saturate_shared_pool(release: threading.Event) -> list:
    """Fill every worker of the application pool with a blocked task."""
    holds = []
    worker_barrier = threading.Barrier(9)

    def hold() -> None:
        worker_barrier.wait(timeout=10)
        release.wait(timeout=30)

    for _ in range(8):
        holds.append(main_loop.run_in_background(hold))
    worker_barrier.wait(timeout=10)
    return holds


def check_decode_ignores_pool_saturation() -> None:
    release = threading.Event()
    controller = make_headless_controller(serial="decode-isolation")
    try:
        decoded = threading.Event()
        seen_thread: list[str] = []
        real_load_background = controller.load_background

        def recording_load_background(*args, **kwargs) -> None:
            seen_thread.append(threading.current_thread().name)
            decoded.set()
            real_load_background(*args, **kwargs)

        controller.load_background = recording_load_background
        _saturate_shared_pool(release)

        page_path = fixtures.seed_page("DecodeIsolation")
        page = gl.page_manager.get_page(page_path, controller)
        assert page is not None, "fixture page did not build"
        controller.load_page(page)

        assert decoded.wait(3.0), (
            "the background decode never started while the application "
            "pool was saturated; the dedicated worker is not isolating it")
        assert seen_thread and seen_thread[0].startswith("bg-decode-"), (
            f"the decode ran on {seen_thread[:1]}, not on the deck's own "
            f"bg-decode worker")
    finally:
        release.set()
        teardown(controller)
    print("PASS: a saturated application pool cannot delay the decode")


def check_superseding_load_cancels_queued_decode() -> None:
    controller = make_headless_controller(serial="decode-cancel")
    try:
        decoded_pages: list[str] = []
        real_load_background = controller.load_background

        def recording_load_background(page, *args, **kwargs) -> None:
            decoded_pages.append(page.get_name())
            real_load_background(page, *args, **kwargs)

        controller.load_background = recording_load_background

        # Occupy both workers so superseding load_page cancels a queued decode
        # before it starts.
        worker_barrier = threading.Barrier(3)
        release = threading.Event()
        for _ in range(2):
            controller._bg_decode_pool.submit(
                lambda: (worker_barrier.wait(timeout=10), release.wait(10)))
        worker_barrier.wait(timeout=10)

        superseded_page = gl.page_manager.get_page(fixtures.seed_page("SupersededA"), controller)
        winning_page = gl.page_manager.get_page(fixtures.seed_page("WinnerB"), controller)
        assert superseded_page is not None and winning_page is not None, "fixture pages did not build"
        controller.load_page(superseded_page)
        superseded = controller._bg_future
        controller.load_page(winning_page)
        release.set()
        assert fixtures.wait_until(lambda: "WinnerB" in decoded_pages, timeout=3.0), (
            "the winning page's decode never ran")
        assert superseded is not None and superseded.cancelled(), (
            "load_page did not cancel the superseded page's queued decode")
        assert "SupersededA" not in decoded_pages, (
            f"the superseded page still decoded: {decoded_pages}")
    finally:
        teardown(controller)
    print("PASS: a superseding load cancels the queued decode")


def check_close_shuts_the_executor_down() -> None:
    controller = make_headless_controller(serial="decode-close")
    pool = controller._bg_decode_pool
    teardown(controller)
    try:
        pool.submit(lambda: None)
    except RuntimeError:
        print("PASS: close shuts the decode executor down")
        return
    raise AssertionError(
        "a submission after close was accepted; the executor is unowned")


fixtures.start_watchdog(90, "render decode isolation")
check_decode_ignores_pool_saturation()
check_superseding_load_cancels_queued_decode()
check_close_shuts_the_executor_down()
print("SCENARIO PASS")
