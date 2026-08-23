"""A bounded worker pool that can put a deadline on a batch of tasks.

A DeadlinePool owns one executor and adds the three things a plain
ThreadPoolExecutor leaves to every caller that must survive a task which
never returns:

- run_batch() waits out one deadline for the whole batch and then tells a
  task that started and did not return, which holds a worker, from one that
  is only queued behind it. The two need opposite handling and the futures
  alone look the same.
- a wedge costs the pool its executor, on request. The old executor is
  abandoned with its queue intact, so the late tasks still run, and a fresh
  executor takes the next submit.
- submit() returns None once the pool is shut down. An owner tears down
  while its callers still submit, and that race is routine, not an error.

The owner keeps the policy: how wide the pool is, how long a batch may take,
and what a stuck task means for its own work. The pool keeps the mechanism.

Close order. The owner shuts the pool down first and drops its reference
second. Between those two steps a caller can still reach the pool, and it
gets None from submit() and an empty outcome from run_batch(), never an
exception.

Scope. This serves the pool family: the per-deck loader pool, which takes
the deadline and the replacement, and the per-deck action pool, which takes
the lifecycle only. The store-prepare pool and the process background pool
are the next candidates and keep their own executors for now.

Two thread users stay out of this on purpose:

- the per-holder event dispatch lanes. One thread per lane is what proves a
  wedged observer isolates itself, and a shared pool of any width brings
  back the exhaustion cliff that the lane design rejected.
- the timer wheel. It fires each timer on its own thread so a slow timer
  never contends the background pool, which is the same reasoning read from
  the other side.

Both may borrow the thread-naming convention here and nothing else.

This module imports the standard library and loguru only, so it imports
before the widget stack and before globals.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import CancelledError, Executor, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import ParamSpec, TypeVar

from loguru import logger as log

# Preserve a submitted callable's own parameters and return type, so submit()
# hands back a Future of what the callable returns.
_Params = ParamSpec("_Params")
_Return = TypeVar("_Return")

# Builds the workers. It takes the width and the thread-name prefix, so a
# replacement rebuilds what the constructor built, from one definition. An
# owner that needs another executor kind passes its own.
ExecutorFactory = Callable[[int, str], Executor]


def _new_thread_pool(max_workers: int, thread_name_prefix: str) -> Executor:
    return ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)


@dataclass(frozen=True)
class BatchWording:
    """The words a batch report borrows from the owner of the pool.

    A user reads the report about the work, not about the pool, so the owner
    names the work. This changes the text of the three report lines and
    nothing the pool does.
    """

    # Subject of every report line, such as "Loading inputs".
    subject: str = "Tasks"
    # What the pool is called in prose, such as "loader pool of deck AB1".
    pool_name: str = "pool"
    # The likely cause, named in the wedge warning to point the reader at it.
    stuck_hint: str = "a task is blocked"


@dataclass(frozen=True)
class BatchOutcome:
    """What one deadline batch left behind. Each tuple holds task names.

    stuck: started before the deadline and had not returned at it. Each one
    holds a worker until it returns, if it ever does.
    late: still queued at the deadline. Nothing cancels these and they run
    on the executor the batch used.
    refused: never reached the queue, because a live pool would not take
    them. refusal carries the error that says why.
    failed: ran and raised. The pool logs each one with its traceback.
    replaced: the wedge cost the pool its executor.
    """

    stuck: tuple[str, ...] = ()
    late: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    refusal: RuntimeError | None = None
    replaced: bool = False


class DeadlinePool:
    """A bounded pool of worker threads, with the batch deadline optional.

    A pool that never calls run_batch() is a lifecycle wrapper: construct,
    submit, shut down. It cancels nothing of its own accord, which is what an
    owner needs whose queued task must run exactly once or leave its work
    half done.
    """

    def __init__(
        self,
        *,
        max_workers: int,
        thread_name_prefix: str,
        replace_on_wedge: bool = False,
        wording: BatchWording | None = None,
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._replace_on_wedge = replace_on_wedge
        self._wording = BatchWording() if wording is None else wording
        self._new_executor: ExecutorFactory = _new_thread_pool if executor_factory is None else executor_factory
        # Held across every submit, every swap and the shutdown flag, so a
        # submit and a replacement never interleave. It is not reentrant: a
        # task, or a factory, that calls back into its own pool on the thread
        # that holds it would deadlock.
        self._lock = threading.Lock()
        self._shutdown = False
        self._leaked_workers = 0
        self._replacements = 0
        self._executor: Executor = self._new_executor(max_workers, thread_name_prefix)

    @property
    def leaked_workers(self) -> int:
        """Workers pinned by a task that was still running at a deadline,
        summed over every executor this pool abandoned. An upper bound: a
        task that merely ran long frees its worker later, and nothing here
        tells that from a call that never returns."""
        return self._leaked_workers

    @property
    def replacements(self) -> int:
        """How often a wedge cost this pool its executor."""
        return self._replacements

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown

    @property
    def wording(self) -> BatchWording:
        """The words this pool reports a batch in. Frozen, so a reader cannot
        change what the owner set."""
        return self._wording

    def submit(
        self,
        fn: Callable[_Params, _Return],
        /,
        *args: _Params.args,
        **kwargs: _Params.kwargs,
    ) -> Future[_Return] | None:
        """Queue fn and hand back its Future, or None when the pool is shut
        down. Safe from any thread.

        A RuntimeError still leaves here when a live pool refuses the work.
        "can't start new thread" is that case: the process is out of threads
        and the task never reached the queue. A caller that treats it like a
        shutdown loses the work without a word.
        """
        # The lock covers the read of the executor and the call on it as one
        # step. A replacement that landed between the two would leave this
        # submit on the executor the swap then shuts down, and that
        # RuntimeError reads exactly like a shut-down pool: a live pool would
        # drop the work and say nothing. The swap takes the same lock.
        with self._lock:
            try:
                return self._executor.submit(fn, *args, **kwargs)
            except RuntimeError as error:
                # Both cases arrive as RuntimeError and the text is what tells
                # them apart. The flag alone does not cover an executor that
                # the owner shut down through another reference.
                if self._shutdown or "shutdown" in str(error):
                    return None
                raise

    def run_batch(
        self,
        tasks: Iterable[tuple[str, Callable[[], object]]],
        *,
        deadline: float,
    ) -> BatchOutcome:
        """Submit every (name, task) pair, wait out one deadline for the
        whole batch, report what missed it, and return the outcome.

        The deadline covers the batch and not the task: it starts once the
        last task is submitted and every wait shares what is left of it. A
        batch that submits nothing waits for nothing.

        This blocks the caller for the deadline at most. It is the caller's
        thread that the deadline protects, so run this on the thread that
        must not wedge.
        """
        pending: list[tuple[str, Future[object]]] = []
        refused: list[str] = []
        refusal: RuntimeError | None = None
        for name, task in tasks:
            try:
                future = self.submit(task)
            except RuntimeError as error:
                # The sweep below neither waits for this task nor names it,
                # so without this the work would vanish without a trace.
                refused.append(name)
                refusal = error
                continue
            if future is None:
                continue  # shutting down; the owner is tearing the pool down
            pending.append((name, future))

        expiry = time.monotonic() + deadline
        stuck: list[str] = []
        late: list[str] = []
        failed: list[str] = []
        for name, future in pending:
            try:
                # exception() and not result(): result() re-raises what the
                # task raised, and a task that raises TimeoutError would then
                # read as a task that missed the deadline. This way the only
                # TimeoutError is the wait's own.
                raised = future.exception(timeout=max(0.0, expiry - time.monotonic()))
            except FutureTimeoutError:
                # Overdue covers two states that need opposite handling. A
                # started task that has not returned holds a worker and wedges
                # the pool; one still queued behind it is only late. The
                # executor tracks that under the future's own lock, and marks
                # a future running before it calls the task. A task that
                # returned just after the timeout reads as late, which is what
                # it is.
                (stuck if future.running() else late).append(name)
                continue
            except CancelledError:
                # Only a shutdown(cancel_futures=True) cancels here, which
                # says the owner is tearing down. Nothing to report.
                continue
            if raised is not None:
                failed.append(name)
                log.opt(exception=raised).error(f"{self._wording.subject} {name} raised while running")

        replaced = self._report(deadline, stuck, late, refused, refusal)
        return BatchOutcome(
            stuck=tuple(stuck),
            late=tuple(late),
            refused=tuple(refused),
            failed=tuple(failed),
            refusal=refusal,
            replaced=replaced,
        )

    def shutdown(self, wait: bool = False, *, cancel_futures: bool = False) -> None:
        """Stop taking work. Every later submit() returns None.

        wait defaults to False, where the stdlib waits. This pool exists for
        work that can block without end, and a wait on it is the hang the
        deadline avoids everywhere else. Pass wait=True only where the tasks
        are known to return.
        """
        with self._lock:
            self._shutdown = True
            executor = self._executor
        executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def _report(
        self,
        deadline: float,
        stuck: list[str],
        late: list[str],
        refused: list[str],
        refusal: RuntimeError | None,
    ) -> bool:
        """Log what missed the deadline and replace the executor for a wedge.
        Returns True when the executor was replaced."""
        words = self._wording
        if refused:
            log.error(
                f"{words.subject} [{', '.join(refused)}] never reached the "
                f"{words.pool_name}, so they stay as they were: {refusal!r}. The "
                f"pool refused the work, which usually means the process is out "
                f"of threads.")
        if late:
            log.info(
                f"{words.subject} [{', '.join(late)}] had not started within "
                f"{deadline}s. They keep their place in the queue and run late.")
        if not stuck:
            return False
        replaced = self._wedge(len(stuck))
        head = (
            f"{words.subject} [{', '.join(stuck)}] did not finish within "
            f"{deadline}s")
        tail = f"Leaked threads in this pool so far: {self._leaked_workers}."
        if self._shutdown:
            # A wedge at teardown asks nothing of the reader: the owner is
            # dropping this pool, a close with cancel_futures took the queue,
            # and a fresh executor would be litter nobody shuts down. So it
            # reports as news and not as a warning.
            log.info(
                f"{head}; the {words.pool_name} is shut down, so each stuck "
                f"task holds its thread until it returns and the close took "
                f"whatever was still queued. {tail}")
            return False
        aftermath = (
            f"Replacing the {words.pool_name}, so the stuck task(s) leak their "
            f"executor's thread(s) once instead of wedging every later batch "
            f"behind them."
            if replaced else
            f"The {words.pool_name} keeps the stuck task(s), so later work "
            f"queues behind them."
        )
        log.warning(
            f"{head}; continuing without them ({words.stuck_hint}). "
            f"{aftermath} {tail}")
        return replaced

    def _wedge(self, stuck_count: int) -> bool:
        """Count the workers a wedge pinned and, on a replacing pool, swap in
        a fresh executor. Returns True when it replaced.

        One hold of the lock covers the count and the swap. The count needs
        it because two batches on one pool otherwise lose an increment
        between the read and the write, and the swap needs it because a
        submit must not land on the executor this abandons. Everything else a
        batch tracks is its own.

        Build first and publish second, so no submit finds the pool without
        an executor. The replacement takes the width and the name the
        constructor took, so it is the same pool again. The old executor is
        never waited on and never cancelled: a stuck task may never return,
        and cancelling would take the queued tasks with it. Nothing
        re-submits those, so they would stay unrun for good. The abandoned
        executor keeps them, and its free workers drain the queue before it
        exits.

        A shut-down pool replaces nothing. Its owner is dropping it, and a
        fresh executor there is litter that nobody ever shuts down.
        """
        old: Executor | None = None
        with self._lock:
            self._leaked_workers += stuck_count
            if self._replace_on_wedge and not self._shutdown:
                old = self._executor
                self._executor = self._new_executor(self._max_workers, self._thread_name_prefix)
                self._replacements += 1
        if old is None:
            return False
        old.shutdown(wait=False)
        return True
