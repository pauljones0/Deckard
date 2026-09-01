"""Check StorePage and PluginRecommendations use the shared background pool.
Pool work runs off-thread, returns promptly, logs failures, and marshals one callback."""
import threading
import types

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

from loguru import logger as log  # noqa: E402

from src.backend.main_loop import run_in_background  # noqa: E402

WATCHDOG_SECONDS = 30


def pump_main_context(rounds: int = 50) -> None:
    ctx = GLib.MainContext.default()
    for _ in range(rounds):
        while ctx.pending():
            ctx.iteration(False)


def test_background_work_runs_off_caller() -> None:
    """run_in_background must run the work on a pool worker and hand back at
    once, so a caller on the main thread keeps running while the work blocks."""
    started = threading.Event()
    release = threading.Event()
    seen = {}

    def blocking_body() -> str:
        seen["thread"] = threading.current_thread()
        seen["name"] = threading.current_thread().name
        started.set()
        # Hold the worker until the caller has proven it did not block here.
        release.wait(timeout=5.0)
        return "done"

    caller = threading.current_thread()
    try:
        future = run_in_background(blocking_body)

        assert started.wait(timeout=3.0), "the pool worker never started"
        # The caller reached this line while the worker is still blocked, so
        # the submit did not run the body inline.
        assert not release.is_set(), "test wired wrong: release fired early"
        assert seen["thread"] is not caller, (
            "the work ran on the caller thread, not off it -- a synchronous "
            "run freezes the UI, which is what the thread exists to prevent"
        )
        assert seen["thread"] is not threading.main_thread(), (
            "the work ran on the main thread"
        )
        assert seen["name"].startswith("background"), (
            f"the work must run on the shared background pool, whose workers are "
            f"named 'background_*'; ran on {seen['name']!r} instead (a raw thread "
            f"or an inline call)"
        )
    finally:
        release.set()

    assert future.result(timeout=3.0) == "done", "the pool work must complete"
    print("PASS: loader work runs off the caller thread on the background pool")


def test_pool_logs_a_failure() -> None:
    """A raise inside pool work must reach the log, not vanish. A raw one-off
    thread logs nothing; the pool's done-callback logs every failure."""
    records: list[str] = []
    sink_id = log.add(lambda message: records.append(str(message)), level="ERROR")

    def boom() -> None:
        raise RuntimeError("loader work exploded")

    try:
        future = run_in_background(boom)
        caught = future.exception(timeout=3.0)
        assert isinstance(caught, RuntimeError), (
            f"the pool must capture the failure on the future, got {caught!r}"
        )
        # The done-callback logs after the future completes; poll for it.
        logged = fixtures.wait_until(
            lambda: any("background task raised" in r for r in records), timeout=3.0)
        assert logged, (
            f"a failure in pool work must be logged; captured records: {records}"
        )
    finally:
        log.remove(sink_id)
    print("PASS: a failure in pool work is logged, not swallowed")


def test_worker_reaches_main_once() -> None:
    """The worker still marshals to the main loop through GLib.idle_add, and
    the idle callback runs exactly once."""
    ran = {"count": 0}
    done = threading.Event()

    def worker() -> None:
        def on_main() -> bool:
            ran["count"] += 1
            return False  # one-shot; a True return would repeat
        GLib.idle_add(on_main)
        done.set()

    run_in_background(worker)
    assert done.wait(timeout=3.0), "the worker never queued its main-loop touch"
    pump_main_context()
    assert ran["count"] == 1, (
        f"the marshalled callback must run once on the main loop, ran "
        f"{ran['count']} times"
    )
    print("PASS: the pool worker reaches the main loop and the callback runs once")


def test_store_page_load_uses_background_pool() -> None:
    """Bind StorePage methods to a stub and require pool-based loading without GTK."""
    from src.windows.Store.StorePage import StorePage

    ran_on: list[str] = []
    load_done = threading.Event()

    class FakePage:
        ensure_loaded = StorePage.ensure_loaded
        _load_guarded = StorePage._load_guarded

        def __init__(self) -> None:
            self._loaded = False

        def load(self) -> None:
            ran_on.append(threading.current_thread().name)
            load_done.set()

        def show_connection_error(self) -> None:
            pass

    page = FakePage()
    page.ensure_loaded()
    # The submit returned at once, so _loaded is already set here.
    assert page._loaded is True, "ensure_loaded must mark the tab loaded"
    assert load_done.wait(timeout=3.0), "the load never ran"
    assert ran_on and ran_on[0].startswith("background"), (
        f"ensure_loaded must submit the load to the background pool, ran on "
        f"{ran_on!r} instead of a 'background_*' worker"
    )
    print("PASS: StorePage.ensure_loaded submits the load to the pool")


def test_recommendations_retry_uses_the_pool() -> None:
    """The real PluginRecommendations.on_retry_clicked disables the button and
    submits the load to the pool."""
    from src.windows.Onboarding.PluginRecommendations import PluginRecommendations

    sensitivity: list[bool] = []
    ran_on: list[str] = []
    load_done = threading.Event()

    def record_load() -> None:
        ran_on.append(threading.current_thread().name)
        load_done.set()

    fake = types.SimpleNamespace(
        retry_button=types.SimpleNamespace(
            set_sensitive=lambda value: sensitivity.append(value)),
        load=record_load,
    )
    PluginRecommendations.on_retry_clicked(fake, button=None)

    assert sensitivity == [False], (
        "retry must disable its button so a double-click cannot start two "
        f"loaders; got {sensitivity}"
    )
    assert load_done.wait(timeout=3.0), "the retry load never ran"
    assert ran_on and ran_on[0].startswith("background"), (
        f"retry must submit the load to the background pool, ran on {ran_on!r}"
    )
    print("PASS: PluginRecommendations retry submits the load to the pool")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_loader_background_pool")
    test_background_work_runs_off_caller()
    test_pool_logs_a_failure()
    test_worker_reaches_main_once()
    test_store_page_load_uses_background_pool()
    test_recommendations_retry_uses_the_pool()
    print("scenario_loader_background_pool: PASS")


if __name__ == "__main__":
    main()
