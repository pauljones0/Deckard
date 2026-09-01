"""Require FIFO observer batches and asynchronous dispatch entry points."""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading

from fixtures import start_watchdog, wait_until

from src.backend.PluginManager import event_dispatch
from src.backend.PluginManager.EventHolder import EventHolder


def check_batch_runs_in_registration_order() -> None:
    order: list[int] = []

    def make_observer(n):
        def observer(*args, **kwargs):
            order.append(n)
        observer.__name__ = f"observer_{n}"
        return observer

    observers = [make_observer(n) for n in range(10)]
    event_dispatch.dispatch(observers, ("evt",), {}, label="test::FIFO")

    assert wait_until(lambda: len(order) == 10, timeout=5.0), (
        f"not all observers ran (order so far: {order})"
    )
    assert order == list(range(10)), (
        f"batch did not run in registration order: {order} -- a plugin that "
        "connects ordered observers relies on FIFO delivery"
    )
    print("PASS: a batch dispatches its observers in registration (FIFO) order")


def check_dispatch_returns_before_observer_completes() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_observer(*args, **kwargs):
        started.set()
        # Hold the lane for the asynchronous-return assertion, with a bound to
        # prevent a regression from hanging the scenario.
        release.wait(timeout=10)
        finished.set()

    event_dispatch.dispatch([blocking_observer], (), {}, label="test::AsyncReturn")

    # The parked observer proves dispatch returned before completion.
    assert not finished.is_set(), (
        "dispatch() did not return until the observer finished -- the "
        "queue-and-return contract regressed to synchronous dispatch (the "
        "AudioControl PulseEvent hot path must not block on observers)"
    )
    # Prove the observer really is running on the lane, not skipped.
    assert wait_until(started.is_set, timeout=5.0), (
        "the queued observer never started on the dispatch lane"
    )
    assert not finished.is_set(), "observer finished before it was released -- test seam broken"

    release.set()
    assert wait_until(finished.is_set, timeout=5.0), (
        "observer never completed after release -- the lane is broken"
    )
    print("PASS: dispatch() returns before the observer completes (async queue-and-return)")


def check_trigger_event_returns_before_observer() -> None:
    # Check the same contract through plugin-facing EventHolder.trigger_event;
    # an explicit event ID avoids the need for PluginBase.
    holder = EventHolder(plugin_base=None, event_id="test::HolderAsyncReturn")

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    async def blocking_coroutine_observer(*args, **kwargs):
        # An async def is the real ecosystem shape, because every EventHolder
        # observer today is a coroutine. trigger_event must still return at once.
        started.set()
        import asyncio
        # Poll the threading.Event from the observer's own loop, without
        # blocking that loop's thread against the release for the whole time.
        while not release.is_set():
            await asyncio.sleep(0.01)
        finished.set()

    holder.add_listener(blocking_coroutine_observer)
    holder.trigger_event(123)

    assert not finished.is_set(), (
        "trigger_event() blocked until the observer finished -- it must "
        "queue-and-return (see EventHolder.trigger_event / event_dispatch)"
    )
    assert wait_until(started.is_set, timeout=5.0), (
        "trigger_event's observer never started on the dispatch lane"
    )

    release.set()
    assert wait_until(finished.is_set, timeout=5.0), (
        "trigger_event's observer never completed after release"
    )
    print("PASS: EventHolder.trigger_event returns before its observer completes")


def check_async_callable_instance_is_awaited() -> None:
    # Await the result of an async __call__ instance even when the instance is
    # not itself recognized as an async function.
    ran = threading.Event()

    class AsyncCallable:
        async def __call__(self, *args, **kwargs):
            import asyncio
            await asyncio.sleep(0)
            ran.set()

    event_dispatch.dispatch([AsyncCallable()], (), {}, label="test::AsyncInstance")
    assert ran.wait(timeout=5), (
        "an async callable instance's coroutine was discarded unrun -- the "
        "dispatcher only awaited async def functions")
    print("PASS: an async callable instance is awaited, not discarded")


def check_queue_cap_drops_oldest() -> None:
    # While a wedge holds the lane, drop and count oldest batches past the cap.
    real_cap = event_dispatch._QUEUE_MAX
    event_dispatch._QUEUE_MAX = 5
    release = threading.Event()
    started = threading.Event()

    def wedged(*args, **kwargs):
        started.set()
        release.wait(10)

    lane = None
    try:
        # Wedge the lane with the first batch, then flood it well past the cap.
        holder_label = "test::QueueCap"
        event_dispatch.dispatch([wedged], (), {}, label=holder_label)
        assert started.wait(5), "the wedged observer never started"

        for _ in range(50):
            event_dispatch.dispatch([lambda *a, **k: None], (), {}, label=holder_label)

        # Find the lane and assert its queue stayed bounded and it counted drops.
        with event_dispatch._watch_lock:
            lanes = [ln for ln in event_dispatch._lanes if ln.dropped > 0]
        assert lanes, "no lane recorded a drop although the queue was flooded past the cap"
        lane = lanes[0]
        assert len(lane._pending) <= event_dispatch._QUEUE_MAX, (
            f"the queue grew past the cap: {len(lane._pending)} > {event_dispatch._QUEUE_MAX}")
        assert lane.dropped >= 50 - event_dispatch._QUEUE_MAX, (
            f"too few drops counted: {lane.dropped}")
    finally:
        release.set()
        event_dispatch._QUEUE_MAX = real_cap
    print("PASS: a flooded lane drops its oldest batches at the cap and counts them")


def check_observer_repr_and_eq_run_outside_lock() -> None:
    # A plugin observer with a custom __repr__/__eq__ must not have either run
    # under the dispatch watch lock. The observer records the lock state it saw.
    saw_locked = {"repr": False}

    class NosyObserver:
        def __repr__(self):
            saw_locked["repr"] = event_dispatch._watch_lock.locked()
            return "NosyObserver"

        def __call__(self, *args, **kwargs):
            pass

    # A bare instance forces _observer_name to call __repr__; run it directly
    # to prove that call occurs outside the lock.
    name = event_dispatch._observer_name(NosyObserver())
    assert name == "NosyObserver"
    assert saw_locked["repr"] is False, (
        "the observer's __repr__ ran while the watch lock was held")
    print("PASS: a custom __repr__ runs off the watch lock")


def main() -> None:
    start_watchdog(40, label="scenario_event_dispatch_contract")
    check_batch_runs_in_registration_order()
    check_dispatch_returns_before_observer_completes()
    check_trigger_event_returns_before_observer()
    check_async_callable_instance_is_awaited()
    check_queue_cap_drops_oldest()
    check_observer_repr_and_eq_run_outside_lock()
    print("PASS: scenario_event_dispatch_contract")


if __name__ == "__main__":
    main()
