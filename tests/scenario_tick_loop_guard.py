"""
The action tick loop survives a per-input failure and keeps ticking.

tick_actions walks every input on the deck and dispatches one tick each. A
raise on that walk used to leave the while loop, and the thread that carries
it, so one broken input stopped every animated action on the deck for the life
of the process. The guard catches the failure, names the input, rate-limits the
traceback, and walks on.

A plugin's own on_tick() runs on the action pool and never on this thread, so
the failure injected here is the dispatch itself: the active-state read the
loop performs per input.
"""

# The guard sits per input, not around the walk, so a deterministic failure on
# one input cannot starve the inputs that follow it in the walk order.
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time

from loguru import logger

from src.backend.DeckManagement.InputIdentifier import Input

# Under run_all.py's 90 s per-scenario default, so a stuck leg reports itself
# before the harness kills the process with no leg diagnostic.
WATCHDOG_SECONDS = 60
# The thread DeckController gives tick_actions. Failures are injected on that
# thread only, because every other caller of the same method says nothing
# about this loop.
TICK_THREAD = "tick_actions"
# Tick period for the run. The loop re-reads it every walk and floors its wait
# at 0.1s, so this buys about ten walks a second and no faster.
FAST_TICK_DELAY = 0.05
# Substring of the guard's record. The count of records carrying it is what
# the rate limit is measured on.
MARKER = "action tick failed for"
# The guard's summary of what it held back.
SUPPRESSED_MARKER = "earlier repeats were suppressed"
# Shortened window for the legs that wait for a second record. The interval is
# a class attribute for exactly this.
SHORT_INTERVAL_S = 0.4
# Walks to observe before a bounded record count is read. Large enough that a
# per-walk log would be unmistakable against the bound.
OBSERVED_WALKS = 25


class TickCounter:
    """Counts tick-thread dispatches per input, and raises for one of them.

    The patch sits on the controller input, because get_active_state() is what
    the walk calls first and what a broken state table breaks. Other threads
    reach the same method through the media tick and the update path, where a
    raise would say nothing about this loop, so the injection reads the calling
    thread.
    """

    def __init__(self, controller_input, raising: bool):
        self.controller_input = controller_input
        self.raising = raising
        self.lock = threading.Lock()
        self.calls = 0
        self.real = controller_input.get_active_state

    def install(self) -> None:
        counter = self

        def counting_get_active_state(*args, **kwargs):
            if threading.current_thread().name != TICK_THREAD:
                return counter.real(*args, **kwargs)
            with counter.lock:
                counter.calls += 1
            if counter.raising:
                raise RuntimeError("a tick dispatch blew up")
            return counter.real(*args, **kwargs)

        self.controller_input.get_active_state = counting_get_active_state

    def remove(self) -> None:
        del self.controller_input.get_active_state

    def count(self) -> int:
        with self.lock:
            return self.calls


class RaisingScreenSaverView:
    """Forwards to the real screensaver and raises on the tick thread's read.

    The walk reads screen_saver.showing before it touches any input. A raise
    there belongs to no input, so it proves the arm around the walk. Every
    other reader, on every other thread, gets the real object's answer.
    """

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name):
        if name == "showing" and threading.current_thread().name == TICK_THREAD:
            raise RuntimeError("a screensaver read blew up")
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_real"), name, value)


def marker_records(records: list[str], marker: str = MARKER) -> int:
    return sum(marker in record for record in records)


def leg_raising_input_does_not_stop_the_walk(controller, records, raiser, observer) -> None:
    """A failing input costs its own tick, and nothing else on the deck."""
    assert fixtures.wait_until(lambda: raiser.count() >= 3, timeout=20.0), (
        f"the failing input was dispatched only {raiser.count()} times -- the "
        f"walk never reached it, so nothing below measures the guard")
    assert controller.tick_thread.is_alive(), (
        "a raising tick dispatch killed the tick thread -- every animated "
        "action on this deck stops for the life of the process")

    # The observer sits after the raiser in the walk order. A guard around the
    # walk instead of around each input would leave it at zero.
    assert fixtures.wait_until(lambda: observer.count() >= 3, timeout=20.0), (
        f"the input after the failing one was dispatched {observer.count()} "
        f"times while the failing one was dispatched {raiser.count()} times -- "
        f"one broken input starves every input behind it in the walk")

    # The raiser keeps being offered its tick. A guard that dropped the input
    # would leave the deck quiet in a different way.
    before = raiser.count()
    assert fixtures.wait_until(lambda: raiser.count() > before, timeout=20.0), (
        "the failing input stopped being dispatched after its first failure -- "
        "it never recovers when the fault clears")

    assert fixtures.wait_until(lambda: marker_records(records) >= 1, timeout=20.0), (
        "the guard swallowed the failure without a record -- a deck that ticks "
        "on and reports nothing hides the broken action")
    assert any('raise RuntimeError("a tick dispatch blew up")' in record
               for record in records), (
        "the record carries no traceback, so it names no line to fix")
    assert any(str(raiser.controller_input.identifier) in record
               for record in records if MARKER in record), (
        f"the record does not name {raiser.controller_input.identifier}, so a "
        f"reader cannot tell which input failed")
    print("  leg PASS: a raising dispatch leaves the thread alive, keeps the "
          "input in the walk and still ticks the input behind it")


def leg_the_log_is_rate_limited(controller, records, raiser) -> None:
    """A failure on every walk must not write a traceback on every walk."""
    records.clear()
    started = time.monotonic()
    before = raiser.count()
    assert fixtures.wait_until(lambda: raiser.count() >= before + OBSERVED_WALKS,
                               timeout=30.0), (
        f"only {raiser.count() - before} walks failed in the observation "
        f"window -- too few to tell a rate limit from an idle loop")
    elapsed = time.monotonic() - started
    failures = raiser.count() - before
    # One record may land as the window opens, and one per interval after
    # that. The bound comes from the measured elapsed time, so a loaded
    # machine that walks slowly does not turn this into a timing assert.
    allowed = int(elapsed / controller.TICK_ERROR_LOG_INTERVAL_S) + 1
    written = marker_records(records)
    assert written <= allowed, (
        f"the guard wrote {written} records for {failures} failures over "
        f"{elapsed:.1f}s, and the {controller.TICK_ERROR_LOG_INTERVAL_S}s "
        f"window allows at most {allowed} -- a per-walk failure floods "
        f"logs.log and the About-dialog ring")
    assert written < failures, (
        f"the guard wrote one record per failure ({written} of {failures}) -- "
        f"the rate limit does nothing")
    print(f"  leg PASS: {failures} failures over {elapsed:.1f}s wrote "
          f"{written} records, at most {allowed} allowed")


def leg_suppressed_failures_are_reported(controller, records, raiser) -> None:
    """What the limit holds back is counted, and rides on the next record."""
    controller.TICK_ERROR_LOG_INTERVAL_S = SHORT_INTERVAL_S
    records.clear()
    assert fixtures.wait_until(
        lambda: marker_records(records, SUPPRESSED_MARKER) >= 1, timeout=30.0), (
        f"no record reported the failures the limit held back over "
        f"{raiser.count()} dispatches -- the guard turns a flood into silence "
        f"instead of into a summary")
    print("  leg PASS: the suppressed failures ride as a count on a later record")


def leg_the_walk_itself_may_raise(controller, records, raiser, observer) -> None:
    """A failure outside any input costs one walk, and never the thread.

    This leg runs last. It stops every input tick while the injection stands.
    """
    real_screen_saver = controller.screen_saver
    records.clear()
    walked = observer.count()
    controller.screen_saver = RaisingScreenSaverView(real_screen_saver)
    try:
        assert fixtures.wait_until(
            lambda: marker_records(records) >= 1, timeout=30.0), (
            "a screensaver read that raised was never reported -- the walk "
            "carries no arm of its own, so the thread dies on it")
        assert controller.tick_thread.is_alive(), (
            "a raise outside any input killed the tick thread")
        assert any("the input walk" in record for record in records if MARKER in record), (
            "the record blames an input for a failure that belongs to the walk")
        # At most the one walk that was already past the screensaver read when
        # the injection landed. Anything more means the per-input dispatch
        # kept running, and the record above came from somewhere else.
        assert observer.count() <= walked + 1, (
            f"the walk dispatched {observer.count() - walked} inputs while "
            f"every screensaver read raised -- this leg measures the arm "
            f"around the walk, and it never reached it")
    finally:
        controller.screen_saver = real_screen_saver

    # The walk resumes once the fault clears, for the failing input too.
    resumed = observer.count()
    assert fixtures.wait_until(lambda: observer.count() > resumed, timeout=30.0), (
        "the walk never resumed after the failure cleared -- the guard costs "
        "the loop more than the one walk that raised")
    print("  leg PASS: a raise on the walk costs one walk, and the loop resumes")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_tick_loop_guard")
    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(str(message)), level="TRACE")

    controller = fixtures.make_headless_controller(serial="tick-guard-1",
                                                   page_name="TickGuardHome")
    keys = controller.inputs[Input.Key]
    assert len(keys) >= 2, (
        f"fixture sanity: the fake deck exposes {len(keys)} keys, and the legs "
        f"below need a failing input with another input behind it")
    raiser = TickCounter(keys[0], raising=True)
    observer = TickCounter(keys[1], raising=False)
    raiser.install()
    observer.install()
    # Ten walks a second instead of one, so a bounded record count reads
    # against many failures and the run stays short.
    controller.TICK_DELAY = FAST_TICK_DELAY
    try:
        leg_raising_input_does_not_stop_the_walk(controller, records, raiser, observer)
        leg_the_log_is_rate_limited(controller, records, raiser)
        leg_suppressed_failures_are_reported(controller, records, raiser)
        # Last: it stops every input tick while it runs.
        leg_the_walk_itself_may_raise(controller, records, raiser, observer)
    finally:
        raiser.remove()
        observer.remove()
        fixtures.teardown(controller)
        logger.remove(sink_id)

    print("\nALL PASS: scenario_tick_loop_guard")


if __name__ == "__main__":
    main()
