"""Distinguish wedged loader tasks from queued tasks at the batch deadline.
Replace only for running overdue work, preserve the old queue, and report refusals."""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time

from concurrent.futures import ThreadPoolExecutor

from loguru import logger as log

from fixtures import make_headless_controller, start_watchdog, teardown, wait_until
from src.backend.deadline_pool import DeadlinePool

# Short enough to keep the scenario quick, long enough that the whole batch
# reaches the pool before it expires.
DEADLINE = 0.4
# The hang is bounded. A load task that outlives the scenario would keep a
# non-daemon pool thread and stop the interpreter from exiting.
HANG_CAP = 30.0
# The wedge warning carries the running leak count. A leak nobody can see is
# a leak nobody fixes.
LEAK_REPORT = "Leaked threads in this pool so far: {count}."
# The deck wires its own words into the pool's report. A generic report would
# send a user looking at the pool instead of at the plugin that hangs.
STUCK_HINT = "a plugin callback is likely blocked"


def wedged_loader_pool(boot_pool, prefix: str) -> DeadlinePool:
    """Build a one-worker pool so one task runs while all others stay queued.
    Reuse the controller pool's wording so checks inspect user-visible reports."""
    return DeadlinePool(
        max_workers=1,
        thread_name_prefix=prefix,
        replace_on_wedge=True,
        wording=boot_pool.wording,
    )


class LogCapture:
    """Collect log messages that identify wedged, late, and refused inputs."""

    def __init__(self, level: str = "INFO"):
        self._level = level
        self.records: list[str] = []

    def __enter__(self) -> "LogCapture":
        self._handle = log.add(lambda message: self.records.append(str(message)), level=self._level)
        return self

    def __exit__(self, *exc) -> bool:
        log.remove(self._handle)
        return False

    def text(self) -> str:
        return "".join(self.records)

    def listed(self, tail: str) -> str:
        """Return the bracketed input list for a matching report line.
        Input identifiers contain commas, so callers count ``Input(`` entries."""
        head = "Loading inputs ["
        for record in self.records:
            start = record.find(head)
            if start == -1:
                continue
            end = record.find("] " + tail, start)
            if end == -1:
                continue
            return record[start + len(head):end]
        return ""


def submit_order(controller) -> list:
    """The inputs in the order load_all_inputs submits them."""
    return [controller_input for t in controller.inputs for controller_input in controller.inputs[t]]


def quiesce(counter, quiet: float = 0.25, timeout: float = 5.0) -> None:
    """Wait until a counter stays unchanged for the requested quiet period.
    This keeps the fake loader out of the controller's in-flight boot load."""
    deadline = time.monotonic() + timeout
    last = counter()
    steady_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.02)
        now = counter()
        if now != last:
            last = now
            steady_since = time.monotonic()
        elif time.monotonic() - steady_since >= quiet:
            return
    raise AssertionError(f"the controller never went quiet; counter stuck at {counter()}")


class FakeLoader:
    """Stands in for load_input. Records every call, and hangs one input once
    armed."""

    def __init__(self) -> None:
        self.armed = threading.Event()
        self.release = threading.Event()
        self.hang_on: str | None = None
        self.started: list[str] = []
        self.finished: list[str] = []

    def __call__(self, controller_input, page, update=True, *, still_current=None) -> None:
        identifier = str(controller_input.identifier)
        self.started.append(identifier)
        if self.armed.is_set() and identifier == self.hang_on:
            self.release.wait(HANG_CAP)
        self.finished.append(identifier)

    def install(self, controller) -> None:
        controller.load_input = self
        quiesce(lambda: len(self.started))
        self.started.clear()
        self.finished.clear()


def case_wedge_reports_only_started_task() -> None:
    """Replace for one started overdue task without cancelling queued tasks.
    Only the running task is wedged; queued tasks load after it clears."""
    controller = make_headless_controller(serial="load-wedge-1")
    try:
        order = submit_order(controller)
        assert len(order) > 2, f"need several inputs to have a queue, got {len(order)}"
        hung, queued = order[0], order[1:]

        loader = FakeLoader()
        loader.hang_on = str(hung.identifier)
        loader.install(controller)
        loader.armed.set()

        controller.LOAD_INPUTS_TIMEOUT = DEADLINE
        boot_pool = controller.load_executor
        wedged_pool = wedged_loader_pool(boot_pool, "wedge_load")
        controller.load_executor = wedged_pool
        if boot_pool is not None:
            boot_pool.shutdown(wait=False)

        assert wedged_pool.leaked_workers == 0, (
            f"nothing has wedged yet, but the counter reads {wedged_pool.leaked_workers}")

        with LogCapture() as capture:
            began = time.monotonic()
            controller.load_all_inputs(controller.active_page)
            stall = time.monotonic() - began

        # The sole writer waits for one batch deadline, not one deadline per task.
        # Allow a small scheduling margin but not a fraction of the hang duration.
        assert DEADLINE <= stall < 3 * DEADLINE, (
            f"load_all_inputs stalled {stall:.2f}s while loading {len(order)} inputs; "
            f"one batch-absolute {DEADLINE}s deadline is the whole budget, and a "
            f"per-task wait would scale it with the deck")

        stuck_region = capture.listed("did not finish within")
        assert stuck_region, f"the wedge went unlogged. Log was:\n{capture.text()}"
        assert str(hung.identifier) in stuck_region, (
            f"the warning must name the wedged input {hung.identifier}, listed [{stuck_region}]")
        assert stuck_region.count("Input(") == 1, (
            f"only the started-and-overdue task is stuck, but the warning listed "
            f"{stuck_region.count('Input(')} inputs: [{stuck_region}]")

        late_region = capture.listed("had not started within")
        assert late_region.count("Input(") == len(queued), (
            f"every queued task should be reported late, expected {len(queued)}, "
            f"listed [{late_region}]")

        assert wedged_pool.leaked_workers == 1, (
            f"the hung task pins one worker of the abandoned executor, but the "
            f"counter reads {wedged_pool.leaked_workers}")
        assert LEAK_REPORT.format(count=1) in capture.text(), (
            f"the wedge warning must report the leak count. Log was:\n{capture.text()}")
        assert STUCK_HINT in capture.text(), (
            f"the warning must carry the deck's own hint at the cause, so a user "
            f"reads it about their plugin and not about a pool. Log was:\n{capture.text()}")
        assert wedged_pool.replacements == 1, (
            f"the wedged executor must be replaced, and the pool reports "
            f"{wedged_pool.replacements} replacements")
        # What the replacement is for: the next page load must not queue
        # behind the hang, which still holds the abandoned executor's worker.
        probe_ran = threading.Event()
        probe = wedged_pool.submit(probe_ran.set)
        assert probe is not None, "the pool must still take work after the replacement"
        assert probe_ran.wait(5.0), (
            "work submitted after the wedge queued behind the hung input instead "
            "of running on the fresh executor")

        assert loader.finished == [], (
            f"the sole worker is hung, so nothing can have finished, got {loader.finished}")
        assert loader.started == [str(hung.identifier)], (
            f"only the first task can have started on a one-worker pool, got {loader.started}")

        # The queue was abandoned to the old pool, not cancelled. Its worker
        # drains what is left once the hang clears.
        loader.release.set()
        assert wait_until(lambda: len(loader.finished) == len(order), timeout=10.0), (
            f"queued tasks were cancelled as stuck: {len(loader.finished)} of "
            f"{len(order)} inputs ever loaded")
        assert set(loader.finished) == {str(i.identifier) for i in order}, (
            "every input must load, late or not")

        print("PASS: wedge names, counts and replaces for the started task only")
    finally:
        teardown(controller)


def case_healthy_batch_keeps_its_pool() -> None:
    """A batch that finishes inside the deadline replaces nothing and leaks
    nothing."""
    controller = make_headless_controller(serial="load-wedge-ok")
    try:
        order = submit_order(controller)
        loader = FakeLoader()
        loader.install(controller)

        controller.LOAD_INPUTS_TIMEOUT = DEADLINE
        pool = controller.load_executor

        with LogCapture() as capture:
            controller.load_all_inputs(controller.active_page)

        assert controller.load_executor is pool, "a healthy batch must keep its pool"
        assert pool.replacements == 0, (
            f"a healthy batch must keep the pool's executor, but the pool reports "
            f"{pool.replacements} replacements")
        assert pool.leaked_workers == 0, (
            f"nothing wedged, but the counter reads {pool.leaked_workers}")
        assert set(loader.finished) == {str(i.identifier) for i in order}, (
            f"expected every input loaded, got {len(loader.finished)} of {len(order)}")
        assert not capture.listed("did not finish within"), (
            f"a healthy batch must log no wedge. Log was:\n{capture.text()}")
        # Empty report buckets must stay silent.
        assert not capture.listed("had not started within"), (
            f"a healthy batch has no late input, so it must log no late line. "
            f"Log was:\n{capture.text()}")
        assert not capture.listed("never reached the"), (
            f"a healthy batch has no refused input, so it must log no refusal. "
            f"Log was:\n{capture.text()}")

        print("PASS: healthy batch keeps its pool and leaks nothing")
    finally:
        teardown(controller)


class RefusingExecutor:
    """Refuse one executor submission with a selected RuntimeError.
    This distinguishes thread exhaustion from routine pool shutdown."""

    def __init__(self, inner, refuse_on: str, error: RuntimeError) -> None:
        self._inner = inner
        self._refuse_on = refuse_on
        self._error = error

    def submit(self, fn, *args, **kwargs):
        # The task is a partial, and the input it loads is its first bound
        # argument.
        if str(fn.args[0].identifier) == self._refuse_on:
            raise self._error
        return self._inner.submit(fn, *args, **kwargs)

    def shutdown(self, *args, **kwargs) -> None:
        self._inner.shutdown(*args, **kwargs)


def refusing_loader_pool(boot_pool, width: int, refuse_on: str, error: RuntimeError) -> DeadlinePool:
    """A loader pool worded like the deck's, whose executor refuses one
    input."""
    return DeadlinePool(
        max_workers=width,
        thread_name_prefix="refuse_load",
        replace_on_wedge=True,
        wording=boot_pool.wording,
        executor_factory=lambda workers, prefix: RefusingExecutor(
            ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix), refuse_on, error),
    )


def case_exhaustion_refusal_is_reported() -> None:
    """Report thread-exhaustion refusals but keep shutdown refusals silent.
    A refused input never enters the deadline sweep."""
    controller = make_headless_controller(serial="load-wedge-refuse")
    try:
        order = submit_order(controller)
        refused = order[0]
        loader = FakeLoader()
        loader.install(controller)
        controller.LOAD_INPUTS_TIMEOUT = DEADLINE
        real_pool = controller.load_executor
        width = max(8, len(order))

        exhaustion = RuntimeError("can't start new thread")
        exhausted_pool = refusing_loader_pool(real_pool, width, str(refused.identifier), exhaustion)
        closing_pool = refusing_loader_pool(
            real_pool, width, str(refused.identifier),
            RuntimeError("cannot schedule new futures after shutdown"))
        controller.load_executor = exhausted_pool
        with LogCapture() as capture:
            controller.load_all_inputs(controller.active_page)

        dropped = capture.listed("never reached the")
        assert str(refused.identifier) in dropped, (
            f"an input the pool refused must be named. Log was:\n{capture.text()}")
        assert dropped.count("Input(") == 1, f"only one input was refused, listed [{dropped}]"
        assert not capture.listed("did not finish within"), "a refused submit is not a wedge"
        assert not capture.listed("had not started within"), "a refused submit is not a late task"
        assert exhausted_pool.leaked_workers == 0, "a refused submit strands no thread"
        assert set(loader.finished) == {str(i.identifier) for i in order[1:]}, (
            f"every other input must still load, got {len(loader.finished)} of {len(order) - 1}")

        # The same exception type, raised because the deck is closing, is the
        # expected path and must stay quiet.
        loader.finished.clear()
        controller.load_executor = closing_pool
        with LogCapture() as capture:
            controller.load_all_inputs(controller.active_page)

        assert not capture.listed("never reached the"), (
            f"a pool shutting down under close() is routine and must not log. "
            f"Log was:\n{capture.text()}")

        print("PASS: a refused submit is reported, a closing pool is not")
    finally:
        controller.load_executor = real_pool
        exhausted_pool.shutdown()
        closing_pool.shutdown()
        teardown(controller)


def case_stale_generation_loads_nothing() -> None:
    """Require late queued tasks to recheck the page generation before loading.
    A page switch while a wedge holds the old pool must make the drain inert."""
    controller = make_headless_controller(serial="load-wedge-gen")
    try:
        order = submit_order(controller)
        hung = order[0]

        loader = FakeLoader()
        loader.hang_on = str(hung.identifier)
        loader.install(controller)
        loader.armed.set()

        # Count guard entries separately from loads to prove the drain ran but loaded nothing.
        reached: list[str] = []
        guard = controller._load_input_if_current

        def counting_guard(controller_input, page, update=True, gen=None) -> None:
            reached.append(str(controller_input.identifier))
            guard(controller_input, page, update, gen)

        controller._load_input_if_current = counting_guard

        controller.LOAD_INPUTS_TIMEOUT = DEADLINE
        boot_pool = controller.load_executor
        wedged_pool = wedged_loader_pool(boot_pool, "gen_load")
        controller.load_executor = wedged_pool
        if boot_pool is not None:
            boot_pool.shutdown(wait=False)

        # A real page load carries the generation it was issued for. Passing
        # None instead means "always current" and would test nothing.
        generation = controller._page_load_generation
        controller.load_all_inputs(controller.active_page, gen=generation)

        assert wedged_pool.replacements == 1, "the wedge must have replaced the executor"
        assert loader.started == [str(hung.identifier)], (
            f"only the hung task can have started so far, got {loader.started}")

        # Advance the generation under the same lock as load_page.
        # A full load would queue another batch and race the drain under test.
        with controller._page_gen_lock:
            controller._page_load_generation += 1

        loader.release.set()
        assert wait_until(lambda: len(reached) == len(order), timeout=10.0), (
            f"the abandoned pool must still run every queued task: {len(reached)} "
            f"of {len(order)} reached the generation guard")
        assert loader.started == [str(hung.identifier)], (
            f"the drain ran against a superseded generation and must load nothing, "
            f"but these inputs loaded: {loader.started}")

        print("PASS: a page switch during the drain loads nothing")
    finally:
        teardown(controller)


def case_action_pool_survives_loader_deadlines() -> None:
    """Keep the action pool unchanged when one callback wedges.
    Cancelling queued ready callbacks would prevent their tick and update gates from opening."""
    controller = make_headless_controller(serial="action-pool-1")
    hang = threading.Event()
    try:
        pool = controller.action_executor
        assert pool is not None, "fixture sanity: the deck built no action pool"
        started, ran = threading.Event(), []

        def wedge() -> None:
            started.set()
            hang.wait(HANG_CAP)

        assert pool.submit(wedge) is not None, "the action pool must take the callback"
        assert started.wait(5.0), "the wedging callback never started"
        assert pool.submit(lambda: ran.append("queued")) is not None, (
            "the action pool must take a second callback")

        # Wait through multiple loader deadlines while the separate loader pool runs a batch.
        controller.LOAD_INPUTS_TIMEOUT = DEADLINE
        controller.load_all_inputs(controller.active_page)
        time.sleep(DEADLINE * 2)

        assert controller.action_executor is pool, (
            "nothing may swap the deck's action pool out from under a callback")
        assert pool.replacements == 0, (
            f"the action pool must never replace its executor, but it reports "
            f"{pool.replacements} replacements; every replacement abandons the "
            f"callbacks queued on the old one")
        assert pool.leaked_workers == 0, (
            f"the action pool runs no deadline batch, so it counts no wedge, got "
            f"{pool.leaked_workers}")
        assert ran == ["queued"], (
            f"the callback queued behind the wedge must still run, got {ran}")

        print("PASS: the action pool takes the lifecycle only")
    finally:
        hang.set()
        teardown(controller)


def main() -> None:
    start_watchdog(60, label="scenario_load_wedge")
    case_wedge_reports_only_started_task()
    case_healthy_batch_keeps_its_pool()
    case_exhaustion_refusal_is_reported()
    case_stale_generation_loads_nothing()
    case_action_pool_survives_loader_deadlines()
    print("PASS: scenario_load_wedge")


if __name__ == "__main__":
    main()
