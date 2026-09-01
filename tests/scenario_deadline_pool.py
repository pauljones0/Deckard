"""Exercise deadlines, replacement budgets, non-cancelling pools, and shutdown.
Each case drives DeadlinePool directly without a controller or page.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time

from concurrent.futures import ThreadPoolExecutor

from loguru import logger as log

from fixtures import start_watchdog
from src.backend.deadline_pool import BatchOutcome, BatchWording, DeadlinePool

# Short enough to keep the scenario quick, long enough that every task in a
# batch reaches the pool before it expires.
DEADLINE = 0.4
# The hang is bounded. A task that outlives the scenario would keep a
# non-daemon executor thread and stop the interpreter from exiting.
HANG_CAP = 10.0
# How long a case waits for work that must run promptly.
PROMPT = 5.0

# Words no other module uses, so a report line proves the pool took them from
# its owner instead of falling back to its own defaults.
WORDING = BatchWording(
    subject="Loading things",
    pool_name="test pool",
    stuck_hint="a task is wedged",
)


class LogCapture:
    """Capture loguru output because task names in deadline reports are under test."""

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

    def line(self, tail: str) -> str:
        """The captured line that holds tail, or "" when none does."""
        for record in self.records:
            if tail in record:
                return record
        return ""


class Task:
    """Record one batch task's progress.
    A hanging task waits for release or HANG_CAP, whichever comes first.
    """

    def __init__(self, name: str, hang: bool = False, raises: "Exception | None" = None):
        self.name = name
        self.started = threading.Event()
        self.finished = threading.Event()
        self.release = threading.Event()
        self._hang = hang
        self._raises = raises

    def run(self) -> None:
        self.started.set()
        if self._hang:
            self.release.wait(HANG_CAP)
        if self._raises is not None:
            raise self._raises
        self.finished.set()


def batch(*tasks: Task) -> list:
    """The (name, callable) pairs run_batch takes, in submit order."""
    return [(task.name, task.run) for task in tasks]


class RefusingExecutor:
    """Raise a specified RuntimeError on one submit to distinguish refusal causes."""

    def __init__(self, inner, refuse_at: int, error: RuntimeError) -> None:
        self._inner = inner
        self._refuse_at = refuse_at
        self._error = error
        self.calls = 0

    def submit(self, fn, *args, **kwargs):
        self.calls += 1
        if self.calls == self._refuse_at:
            raise self._error
        return self._inner.submit(fn, *args, **kwargs)

    def shutdown(self, *args, **kwargs) -> None:
        self._inner.shutdown(*args, **kwargs)


class CountingExecutor:
    """Signal when the whole batch is submitted so closure can precede its sweep."""

    def __init__(self, inner, expect: int) -> None:
        self._inner = inner
        self._expect = expect
        self.calls = 0
        self.submitted = threading.Event()

    def submit(self, fn, *args, **kwargs):
        future = self._inner.submit(fn, *args, **kwargs)
        self.calls += 1
        if self.calls >= self._expect:
            self.submitted.set()
        return future

    def shutdown(self, *args, **kwargs) -> None:
        self._inner.shutdown(*args, **kwargs)


def one_worker_pool(prefix: str, replace_on_wedge: bool) -> DeadlinePool:
    """A pool with a single worker, so exactly one task of a batch starts and
    the rest stay queued behind it."""
    return DeadlinePool(
        max_workers=1,
        thread_name_prefix=prefix,
        replace_on_wedge=replace_on_wedge,
        wording=WORDING,
    )


def case_classifies_stuck_and_late_tasks() -> None:
    """Require an overdue started task to be stuck and queued tasks to be late.
    The late tasks must run after the wedge clears.
    """
    pool = one_worker_pool("dp_wedge", replace_on_wedge=True)
    hung, first, second = Task("hung", hang=True), Task("first"), Task("second")
    # A third queued task, so a per-task wait would overrun the ceiling below
    # by a margin no scheduling noise can close.
    third = Task("third")
    try:
        assert pool.leaked_workers == 0, (
            f"nothing has wedged yet, but the counter reads {pool.leaked_workers}")

        with LogCapture() as capture:
            began = time.monotonic()
            outcome = pool.run_batch(batch(hung, first, second, third), deadline=DEADLINE)
            waited = time.monotonic() - began

        # Bound the whole batch to a small multiple of one deadline; a per-task
        # wait would stall the media thread once for each overdue task.
        assert DEADLINE <= waited < 3 * DEADLINE, (
            f"run_batch held its caller {waited:.2f}s for a 4-task batch; one "
            f"batch-absolute {DEADLINE}s deadline is the whole budget, and a "
            f"per-task wait would scale it with the batch")

        assert outcome.stuck == ("hung",), (
            f"only the started-and-overdue task is stuck, got {outcome.stuck}")
        assert outcome.late == ("first", "second", "third"), (
            f"every queued task is late, in submit order, got {outcome.late}")
        assert outcome.refused == () and outcome.failed == (), (
            f"nothing was refused and nothing raised, got {outcome.refused} and {outcome.failed}")
        assert pool.leaked_workers == 1, (
            f"the hung task pins one worker of the abandoned executor, but the "
            f"counter reads {pool.leaked_workers}")

        warning = capture.line("did not finish within")
        assert "hung" in warning and "first" not in warning, (
            f"the warning must name the stuck task and no other, got: {warning}")
        assert "Leaked threads in this pool so far: 1." in warning, (
            f"the warning must carry the running leak count, got: {warning}")
        assert WORDING.stuck_hint in warning and WORDING.pool_name in warning, (
            f"the report must use the owner's wording, got: {warning}")
        late_line = capture.line("had not started within")
        assert "first" in late_line and "second" in late_line and "hung" not in late_line, (
            f"the late line must name the queued tasks and no other, got: {late_line}")
        assert WORDING.subject in late_line, (
            f"the report must use the owner's subject, got: {late_line}")

        assert outcome.replaced is True, "a wedge on a replacing pool must replace the executor"
        assert pool.replacements == 1, (
            f"the wedged executor must be replaced exactly once, got {pool.replacements}")
        assert not any(task.started.is_set() for task in (first, second, third)), (
            "the sole worker is hung, so no queued task can have started")

        # The queue went to the abandoned executor, and nothing cancelled it.
        hung.release.set()
        assert all(task.finished.wait(PROMPT) for task in (first, second, third)), (
            f"the queued tasks were cancelled instead of drained: "
            f"{[task.name for task in (first, second, third) if not task.finished.is_set()]} never ran")

        print("PASS: the deadline tells a stuck task from a late one")
    finally:
        hung.release.set()
        pool.shutdown()


def case_replacement_accepts_work_immediately() -> None:
    """Require work after a wedge to run on the replacement executor at once.
    A second wedge must accumulate replacement and leaked-worker counts.
    """
    pool = one_worker_pool("dp_fresh", replace_on_wedge=True)
    hung, hung_again, probe = Task("hung", hang=True), Task("hung-again", hang=True), Task("probe")
    try:
        outcome = pool.run_batch(batch(hung), deadline=DEADLINE)
        assert outcome.replaced is True, "the wedge must have replaced the executor"

        future = pool.submit(probe.run)
        assert future is not None, "a pool that replaced its executor must still take work"
        assert probe.finished.wait(PROMPT), (
            "work submitted after the wedge queued behind the hung task instead of "
            "running on the fresh executor")
        assert not hung.finished.is_set(), (
            "the hung task is still meant to be hanging; this case proves nothing if it returned")

        outcome = pool.run_batch(batch(hung_again), deadline=DEADLINE)
        assert outcome.replaced is True, "the second wedge must replace the executor again"
        assert pool.leaked_workers == 2, (
            f"two wedges strand two workers, and the count carries both, got "
            f"{pool.leaked_workers}")
        assert pool.replacements == 2, (
            f"two wedges cost two executors, got {pool.replacements} replacements")

        print("PASS: the replacement takes work at once, and the counts accumulate")
    finally:
        hung.release.set()
        hung_again.release.set()
        pool.shutdown()


def case_replacement_budget_quarantines_pool() -> None:
    """Require replacement to stop at its budget and quarantine the pool.
    Quarantine keeps the stuck task on the current executor and reports it.
    """
    budget = 3
    pool = DeadlinePool(
        max_workers=1,
        thread_name_prefix="dp_budget",
        replace_on_wedge=True,
        max_replacements=budget,
        wording=WORDING,
    )
    hung: list[Task] = []
    try:
        # Wedge it exactly budget times; each replaces and strands one worker.
        for i in range(budget):
            task = Task(f"hung-{i}", hang=True)
            hung.append(task)
            outcome = pool.run_batch(batch(task), deadline=DEADLINE)
            assert outcome.replaced is True, f"wedge {i} within budget must replace"
        assert pool.replacements == budget, (
            f"the pool must replace exactly {budget} times, got {pool.replacements}")
        assert pool.leaked_workers == budget, (
            f"each replacement strands one worker, got {pool.leaked_workers}")
        assert not pool.is_quarantined, "the pool must not quarantine until the budget is spent"

        # One more wedge is past the budget: no replacement, quarantine set,
        # and the leaked-worker count does not grow.
        over = Task("hung-over", hang=True)
        hung.append(over)
        with LogCapture() as capture:
            outcome = pool.run_batch(batch(over), deadline=DEADLINE)
        assert outcome.replaced is False, "a wedge past the budget must not replace"
        assert pool.is_quarantined, "the pool must quarantine once the budget is spent"
        # Replacements bound abandoned executors to the budget; leaked_workers
        # also counts the over-budget task pinned to the current executor.
        assert pool.replacements == budget, (
            f"a quarantined pool abandons no more executors, got {pool.replacements}")
        assert pool.leaked_workers == budget + 1, (
            f"the over-budget stuck task is still counted, got {pool.leaked_workers}")
        warning = capture.line("did not finish within")
        assert "stops" in warning and "give up on it" in warning, (
            f"the report must name the quarantine, got: {warning}")

        print("PASS: the replacement budget quarantines the pool and bounds the leak")
    finally:
        for task in hung:
            task.release.set()
        pool.shutdown()


def case_nonreplacing_pool_keeps_queue() -> None:
    """Require a non-replacing pool to keep its executor and queued tasks.
    Cancelling a ready callback would leave the action gates closed.
    """
    pool = one_worker_pool("dp_keep", replace_on_wedge=False)
    hung, queued = Task("hung", hang=True), Task("queued")
    try:
        with LogCapture() as capture:
            outcome = pool.run_batch(batch(hung, queued), deadline=DEADLINE)

        assert outcome.stuck == ("hung",), f"the started-and-overdue task is stuck, got {outcome.stuck}"
        assert outcome.replaced is False, "a pool that does not replace must report no replacement"
        assert pool.replacements == 0, (
            f"a pool that does not replace must keep its executor, got {pool.replacements} replacements")
        assert pool.leaked_workers == 1, (
            f"the wedge is counted whether or not the pool replaces, got {pool.leaked_workers}")
        warning = capture.line("did not finish within")
        assert "keeps the stuck task(s)" in warning, (
            f"the warning must say the queue stays behind the wedge, got: {warning}")

        hung.release.set()
        assert queued.finished.wait(PROMPT), (
            "the queued task was cancelled by the wedge; nothing may cancel it")

        print("PASS: a pool that does not replace cancels nothing")
    finally:
        hung.release.set()
        pool.shutdown()


def case_shutdown_pool_refuses_quietly() -> None:
    """Require calls during close to return None or an empty outcome without raising."""
    pool = one_worker_pool("dp_closed", replace_on_wedge=True)
    late_submit, in_batch = Task("late-submit"), Task("in-batch")

    pool.shutdown()
    assert pool.is_shutdown, "shutdown() must say so"

    try:
        future = pool.submit(late_submit.run)
    except Exception as error:
        raise AssertionError(
            f"a submit onto a shut-down pool must not raise at the caller, got {error!r}") from error
    assert future is None, f"a shut-down pool must hand back None, got {future!r}"
    assert not late_submit.started.is_set(), "a shut-down pool must not run the work"

    with LogCapture() as capture:
        try:
            outcome = pool.run_batch(batch(in_batch), deadline=DEADLINE)
        except Exception as error:
            raise AssertionError(
                f"a batch on a shut-down pool must not raise at the caller, got {error!r}") from error
    assert outcome == BatchOutcome(), (
        f"a batch on a shut-down pool has nothing to report, got {outcome}")
    assert not capture.text(), (
        f"a pool shutting down under the owner is routine and must stay quiet. "
        f"Log was:\n{capture.text()}")
    assert not in_batch.started.is_set(), "a shut-down pool must not run the batch"

    print("PASS: a shut-down pool refuses without raising")


def case_close_cancels_queued_tasks() -> None:
    """Treat queued-task cancellation during a close as quiet teardown.
    A wedge in the closed pool must not create a replacement executor.
    """
    executors: list[CountingExecutor] = []

    def counting_factory(workers: int, prefix: str) -> CountingExecutor:
        executor = CountingExecutor(
            ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix), 3)
        executors.append(executor)
        return executor

    pool = DeadlinePool(
        max_workers=1,
        thread_name_prefix="dp_close_mid",
        replace_on_wedge=True,
        wording=WORDING,
        executor_factory=counting_factory,
    )
    hung, first, second = Task("hung", hang=True), Task("first"), Task("second")
    # The batch blocks its caller for the deadline, so it needs a thread of
    # its own for the close to land inside that window.
    result: list = []

    def run_current_batch() -> None:
        try:
            result.append(pool.run_batch(batch(hung, first, second), deadline=DEADLINE))
        except BaseException as error:  # noqa: BLE001  the assertion below reports it
            result.append(error)

    batch_thread = threading.Thread(target=run_current_batch, name="dp_close_mid_batch")
    try:
        batch_thread.start()
        assert executors[0].submitted.wait(PROMPT), (
            f"the batch never reached the executor: {executors[0].calls} of 3 submitted")
        assert hung.started.wait(PROMPT), "the first task never started"

        # The owner closes while the sweep is still waiting out the deadline.
        pool.shutdown(cancel_futures=True)

        batch_thread.join(PROMPT)
        assert not batch_thread.is_alive(), "run_batch never returned after the close"
        outcome = result[0]
        assert isinstance(outcome, BatchOutcome), (
            f"a close during the sweep must not raise at the caller, got {outcome!r}")
        assert outcome.stuck == ("hung",), (
            f"the started-and-overdue task is stuck whatever the close does, got {outcome.stuck}")
        assert outcome.late == () and outcome.failed == () and outcome.refused == (), (
            f"a cancelled task is neither late, failed nor refused, got {outcome.late}, "
            f"{outcome.failed} and {outcome.refused}")
        assert not first.started.is_set() and not second.started.is_set(), (
            "cancel_futures took the queued tasks, so neither may have run")
        assert outcome.replaced is False and pool.replacements == 0, (
            f"a wedge on a closed pool must not replace anything, got "
            f"{pool.replacements} replacements")
        assert len(executors) == 1, (
            f"the closed pool built {len(executors)} executors; every one past the "
            f"first is litter that no owner shuts down")

        print("PASS: a close during the sweep cancels the queue quietly")
    finally:
        hung.release.set()
        pool.shutdown()
        batch_thread.join(PROMPT)


def case_live_pool_reports_refusal() -> None:
    """Report thread-exhaustion refusal but keep shutdown refusal quiet.
    A refused task never reaches the queue or deadline sweep.
    """
    exhaustion = RuntimeError("can't start new thread")
    exhausted = DeadlinePool(
        max_workers=4,
        thread_name_prefix="dp_refuse",
        wording=WORDING,
        executor_factory=lambda workers, prefix: RefusingExecutor(
            ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix), 2, exhaustion),
    )
    closing = DeadlinePool(
        max_workers=4,
        thread_name_prefix="dp_closing",
        wording=WORDING,
        executor_factory=lambda workers, prefix: RefusingExecutor(
            ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix), 2,
            RuntimeError("cannot schedule new futures after shutdown")),
    )
    try:
        first, refused, third = Task("first"), Task("refused"), Task("third")
        with LogCapture() as capture:
            outcome = exhausted.run_batch(batch(first, refused, third), deadline=DEADLINE)

        assert outcome.refused == ("refused",), (
            f"the task the executor would not take must be named, got {outcome.refused}")
        assert outcome.refusal is exhaustion, (
            f"the outcome must carry the error that says why, got {outcome.refusal!r}")
        assert outcome.stuck == () and outcome.late == (), (
            f"a refused task is neither stuck nor late, got {outcome.stuck} and {outcome.late}")
        assert exhausted.leaked_workers == 0, "a refused submit strands no thread"
        report = capture.line("never reached the")
        assert "refused" in report and WORDING.pool_name in report, (
            f"the refusal must be named in the report, got: {report}")
        assert not refused.started.is_set(), "the refused task must not have run"
        assert first.finished.wait(PROMPT) and third.finished.wait(PROMPT), (
            "one refusal must not stop the rest of the batch")

        quiet_first, quiet_second, quiet_third = Task("q1"), Task("q2"), Task("q3")
        with LogCapture() as capture:
            outcome = closing.run_batch(batch(quiet_first, quiet_second, quiet_third), deadline=DEADLINE)

        assert outcome == BatchOutcome(), (
            f"an executor that is shutting down is routine, with nothing to report, got {outcome}")
        assert not capture.text(), f"a shutting-down executor must stay quiet. Log was:\n{capture.text()}"

        print("PASS: a live pool that refuses is reported, a closing one is not")
    finally:
        exhausted.shutdown()
        closing.shutdown()


def case_task_failure_allows_sweep() -> None:
    """Require a task exception not to stop classification of the rest of its batch."""
    pool = one_worker_pool("dp_raise", replace_on_wedge=False)
    raiser = Task("raiser", raises=RuntimeError("the task itself failed"))
    hung, queued = Task("hung", hang=True), Task("queued")
    try:
        with LogCapture("ERROR") as capture:
            outcome = pool.run_batch(batch(raiser, hung, queued), deadline=DEADLINE)

        assert outcome.failed == ("raiser",), (
            f"the raising task must be named as failed, got {outcome.failed}")
        assert "the task itself failed" in capture.text(), (
            f"the task's own error must reach the log. Log was:\n{capture.text()}")
        # One worker: it ran the raiser, took the hang next, and never got to
        # the third task.
        assert outcome.stuck == ("hung",), (
            f"the sweep must carry on past the raiser and classify the rest, got {outcome.stuck}")
        assert outcome.late == ("queued",), (
            f"the queued task is late, not lost, got {outcome.late}")

        print("PASS: a task that raises does not stop the sweep")
    finally:
        hung.release.set()
        pool.shutdown()


def main() -> None:
    start_watchdog(60, label="scenario_deadline_pool")
    case_classifies_stuck_and_late_tasks()
    case_replacement_accepts_work_immediately()
    case_replacement_budget_quarantines_pool()
    case_nonreplacing_pool_keeps_queue()
    case_shutdown_pool_refuses_quietly()
    case_close_cancels_queued_tasks()
    case_live_pool_reports_refusal()
    case_task_failure_allows_sweep()
    print("PASS: scenario_deadline_pool")


if __name__ == "__main__":
    main()
