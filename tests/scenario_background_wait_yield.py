"""A pending page background must leave the writer idle and controls live."""

import fixtures  # must be first, to isolate DATA_PATH before any src import

import threading
import time

import globals as gl

from fixtures import make_headless_controller, raw_deck, teardown, wait_until
from src.backend import ui_port
from src.backend.DeckManagement.deck_controller.page_completion import PageLoadCompletion


BACKGROUND_BLOCK_S = 5.0
IDLE_SAMPLE_S = 0.3


def check_pending_background_leaves_writer_idle() -> None:
    controller = make_headless_controller(serial="page-background-callback")
    started = threading.Event()
    release = threading.Event()
    real_load_background = controller.load_background

    def blocked_load_background(*args, **kwargs) -> None:
        started.set()
        assert release.wait(BACKGROUND_BLOCK_S), "scenario cleanup did not release the background load"
        real_load_background(*args, **kwargs)

    controller.load_background = blocked_load_background
    try:
        page_path = fixtures.seed_page("BackgroundCallback")
        page = gl.page_manager.get_page(page_path, controller)
        assert page is not None, "fixture page did not build"
        controller.load_page(page)
        assert started.wait(3.0), "the background decode did not start"
        assert controller._input_load_done.wait(3.0), "the input rebuild did not finish"

        ticks_before = controller.media_player.media_ticks
        time.sleep(IDLE_SAMPLE_S)
        pending_ticks = controller.media_player.media_ticks - ticks_before
        assert pending_ticks <= 2, (
            f"a pending background kept the writer active ({pending_ticks} ticks in {IDLE_SAMPLE_S}s)"
        )

        before = raw_deck(controller).current_seq()
        tick_before = controller.media_player.media_ticks
        controller.set_brightness(42)
        assert wait_until(
            lambda: (
                (brightness := raw_deck(controller).last_op_for("brightness")) is not None
                and brightness[1] > before
            ),
            timeout=1.0,
            interval=0.005,
        ), "a control submitted during background decode did not execute"
        assert controller.media_player.media_ticks <= tick_before + 1, (
            "the control did not drain on the next writer tick"
        )
    finally:
        release.set()
        del controller.load_background
        teardown(controller)

    print("  leg PASS: pending background leaves writer idle and controls live")


def check_superseded_completion_cannot_paint() -> None:
    controller = make_headless_controller(serial="page-background-supersession")
    old_started = threading.Event()
    old_release = threading.Event()
    handoff = threading.Event()
    writer_blocked = threading.Event()
    writer_release = threading.Event()
    real_load_background = controller.load_background
    real_add_task = controller.media_player.add_task
    real_update_all_inputs = controller.update_all_inputs
    rendered_gens: list[tuple[int | None, int]] = []

    def blocked_old_background(page, *args, **kwargs) -> None:
        if page.get_name() == "CallbackOld":
            old_started.set()
            assert old_release.wait(BACKGROUND_BLOCK_S), "scenario did not release the old background"
        real_load_background(page, *args, **kwargs)

    def recording_update_all_inputs(*, gen=None) -> None:
        rendered_gens.append((gen, controller._page_load_generation))
        real_update_all_inputs(gen=gen)

    def block_writer() -> None:
        writer_blocked.set()
        assert writer_release.wait(BACKGROUND_BLOCK_S), "scenario did not release the writer"

    def recording_add_task(method, *args, **kwargs) -> None:
        if getattr(method, "__self__", None) is old_completion:
            handoff.set()
        real_add_task(method, *args, **kwargs)

    controller.load_background = blocked_old_background
    controller.update_all_inputs = recording_update_all_inputs
    try:
        old_path = fixtures.seed_page("CallbackOld")
        new_path = fixtures.seed_page("CallbackNew")
        old_page = gl.page_manager.get_page(old_path, controller)
        new_page = gl.page_manager.get_page(new_path, controller)
        assert old_page is not None and new_page is not None, "fixture pages did not build"
        controller.load_page(old_page)
        old_gen = controller._page_load_generation
        old_completion = controller._page_completion
        assert old_completion is not None, "the old page completion was not created"
        assert old_started.wait(3.0), "the old background decode did not start"
        assert controller._input_load_done.wait(3.0), "the old input rebuild did not finish"
        controller.media_player.add_task(block_writer)
        assert writer_blocked.wait(3.0), "the writer blocker did not start"
        controller.media_player.add_task = recording_add_task
        old_release.set()
        assert handoff.wait(3.0), "the old completion did not attempt its writer handoff"
        controller.load_page(new_page)
        new_gen = controller._page_load_generation
        assert new_gen > old_gen, "the rapid switch did not advance the generation"
        writer_release.set()
        assert wait_until(lambda: any(gen == new_gen for gen, _ in rendered_gens), timeout=5.0), (
            "the new page did not render"
        )
        assert not any(gen == old_gen and current > old_gen for gen, current in rendered_gens), (
            f"the superseded generation {old_gen} rendered after generation {new_gen}: {rendered_gens}"
        )
    finally:
        old_release.set()
        writer_release.set()
        controller.media_player.add_task = real_add_task
        controller.update_all_inputs = real_update_all_inputs
        del controller.load_background
        teardown(controller)

    print("  leg PASS: superseded completion cannot paint")


def check_close_does_not_wait_for_background() -> None:
    controller = make_headless_controller(serial="page-background-close")
    started = threading.Event()
    release = threading.Event()
    load_inside_switch = threading.Event()
    release_switch = threading.Event()
    real_update_all_inputs = controller.update_all_inputs
    real_load_screensaver = controller.load_screensaver
    post_close_renders: list[int | None] = []

    def blocked_load_background(*args, **kwargs) -> None:
        started.set()
        assert release.wait(BACKGROUND_BLOCK_S), "scenario did not release the background"

    def blocked_load_screensaver(*args, **kwargs) -> None:
        real_load_screensaver(*args, **kwargs)
        load_inside_switch.set()
        assert release_switch.wait(BACKGROUND_BLOCK_S), "scenario did not release the page switch"

    def recording_update_all_inputs(*, gen=None) -> None:
        if controller._closing:
            post_close_renders.append(gen)
        real_update_all_inputs(gen=gen)

    controller.load_background = blocked_load_background
    controller.load_screensaver = blocked_load_screensaver
    controller.update_all_inputs = recording_update_all_inputs
    try:
        page_path = fixtures.seed_page("CallbackClose")
        page = gl.page_manager.get_page(page_path, controller)
        assert page is not None, "fixture page did not build"
        loader = threading.Thread(target=controller.load_page, args=(page,), name="racing-page-load")
        loader.start()
        assert load_inside_switch.wait(3.0), "load_page did not pass its inner close check"
        assert started.wait(3.0), "the background decode did not start"
        closer = threading.Thread(target=controller.close, args=(True,), name="racing-close")
        closer.start()
        time.sleep(0.05)
        assert closer.is_alive(), "close did not serialize with the active page switch"
        began = time.monotonic()
        release_switch.set()
        loader.join(timeout=3.0)
        closer.join(timeout=3.0)
        elapsed = time.monotonic() - began
        assert not loader.is_alive() and not closer.is_alive(), "the close/load race did not finish"
        assert elapsed < 1.0, f"close waited {elapsed:.3f}s for a blocked background"
        assert controller._page_completion is None, "close retained page-completion state"

        release.set()
        assert wait_until(
            lambda: controller._bg_future is not None and controller._bg_future.done(),
            timeout=3.0,
        ), "the released background did not finish"
        assert not post_close_renders, f"background completion rendered after close: {post_close_renders}"
    finally:
        release.set()
        release_switch.set()
        controller.load_screensaver = real_load_screensaver
        controller.update_all_inputs = real_update_all_inputs
        del controller.load_background
        teardown(controller)

    print("  leg PASS: close does not wait for background completion")


def check_background_timeout_completes_current_page() -> None:
    controller = make_headless_controller(serial="page-background-timeout")
    started = threading.Event()
    release = threading.Event()
    inputs_started = threading.Event()
    release_inputs = threading.Event()
    real_update_all_inputs = controller.update_all_inputs
    real_load_all_inputs = controller.load_all_inputs
    rendered_gens: list[int | None] = []
    original_timeout = PageLoadCompletion.BACKGROUND_TIMEOUT_S
    real_timed_out = PageLoadCompletion._background_timed_out
    timed_out_at: list[float] = []

    class RecordingPort(ui_port.UIPort):
        def __init__(self) -> None:
            self.writer_pages: list[object] = []

        def on_page_changed(self, controller) -> None:
            if threading.current_thread() is controller.media_player:
                self.writer_pages.append(controller.active_page)

    recording_port = RecordingPort()

    def blocked_load_background(*args, **kwargs) -> None:
        started.set()
        assert release.wait(BACKGROUND_BLOCK_S), "scenario did not release the background"

    def recording_update_all_inputs(*, gen=None) -> None:
        rendered_gens.append(gen)
        real_update_all_inputs(gen=gen)

    def blocked_load_all_inputs(*args, **kwargs) -> None:
        inputs_started.set()
        assert release_inputs.wait(BACKGROUND_BLOCK_S), "scenario did not release input loading"
        real_load_all_inputs(*args, **kwargs)

    def recording_timeout(completion) -> None:
        timed_out_at.append(time.monotonic())
        real_timed_out(completion)

    PageLoadCompletion.BACKGROUND_TIMEOUT_S = 0.05
    PageLoadCompletion._background_timed_out = recording_timeout
    ui_port.install(recording_port)
    controller.load_background = blocked_load_background
    controller.load_all_inputs = blocked_load_all_inputs
    controller.update_all_inputs = recording_update_all_inputs
    try:
        page_path = fixtures.seed_page("CallbackTimeout")
        page = gl.page_manager.get_page(page_path, controller)
        assert page is not None, "fixture page did not build"
        controller.load_page(page)
        gen = controller._page_load_generation
        assert started.wait(3.0), "the background decode did not start"
        assert inputs_started.wait(3.0), "input loading did not start"
        time.sleep(0.1)
        assert gen not in rendered_gens, "the background timeout started before input loading finished"
        release_inputs.set()
        assert controller._input_load_done.wait(3.0), "input loading did not publish completion"
        inputs_done_at = time.monotonic()
        assert wait_until(lambda: gen in rendered_gens, timeout=2.0), (
            "the timeout did not release the current page completion"
        )
        assert timed_out_at[0] - inputs_done_at >= 0.04, "timeout allowance started before input completion"
        assert rendered_gens.count(gen) == 1, f"timeout rendered generation {gen} more than once"
        assert wait_until(lambda: page in recording_port.writer_pages, timeout=2.0), (
            "completion did not publish its writer-side UI notification"
        )
        release.set()
        assert wait_until(lambda: controller._bg_future is not None and controller._bg_future.done(), timeout=3.0)
        time.sleep(0.05)
        assert rendered_gens.count(gen) == 1, "late background completion rendered a second time"
    finally:
        PageLoadCompletion.BACKGROUND_TIMEOUT_S = original_timeout
        PageLoadCompletion._background_timed_out = real_timed_out
        ui_port.install(None)
        release.set()
        release_inputs.set()
        controller.load_all_inputs = real_load_all_inputs
        controller.update_all_inputs = real_update_all_inputs
        del controller.load_background
        teardown(controller)

    print("  leg PASS: background timeout completes the current page once")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_background_wait_yield")
    check_pending_background_leaves_writer_idle()
    check_superseded_completion_cannot_paint()
    check_close_does_not_wait_for_background()
    check_background_timeout_completes_current_page()
    print("PASS: scenario_background_wait_yield")


if __name__ == "__main__":
    main()
