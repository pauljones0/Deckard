"""Run deadline batches that distinguish running wedges from queued late tasks.
Pools can replace a wedged executor while its queue drains and return None after shutdown."""
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

# Replace at most five executors because each wedge can pin non-daemon workers.
# After the limit, quarantine the current executor to bound further leaks.
DEFAULT_MAX_REPLACEMENTS = 5

# Build replacements with the original width and thread prefix.
# Owners can inject another executor kind.
ExecutorFactory = Callable[[int, str], Executor]


def _new_thread_pool(max_workers: int, thread_name_prefix: str) -> Executor:
    return ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)


@dataclass(frozen=True)
class BatchWording:
    """Owner-provided report wording that does not change pool behavior."""

    # Subject of every report line, such as "Loading inputs".
    subject: str = "Tasks"
    # What the pool is called in prose, such as "loader pool of deck AB1".
    pool_name: str = "pool"
    # The likely cause, named in the wedge warning to point the reader at it.
    stuck_hint: str = "a task is blocked"


@dataclass(frozen=True)
class BatchOutcome:
    """Name stuck running, late queued, refused, and failed tasks from one batch.
    refusal gives the rejection; replaced says a wedge cost the executor."""

    stuck: tuple[str, ...] = ()
    late: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    refusal: RuntimeError | None = None
    replaced: bool = False


class DeadlinePool:
    """Bound worker threads with an optional batch deadline.
    Without run_batch, preserve queued exactly-once work until explicit shutdown."""

    def __init__(
        self,
        *,
        max_workers: int,
        thread_name_prefix: str,
        replace_on_wedge: bool = False,
        max_replacements: int = DEFAULT_MAX_REPLACEMENTS,
        wording: BatchWording | None = None,
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._replace_on_wedge = replace_on_wedge
        self._max_replacements = max_replacements
        self._wording = BatchWording() if wording is None else wording
        self._new_executor: ExecutorFactory = _new_thread_pool if executor_factory is None else executor_factory
        # Serialize submits, swaps, and shutdown; the lock is not reentrant.
        # A task or factory that calls its own pool while holding it deadlocks.
        self._lock = threading.Lock()
        self._shutdown = False
        self._leaked_workers = 0
        self._replacements = 0
        self._quarantined = False
        self._executor: Executor = self._new_executor(max_workers, thread_name_prefix)

    @property
    def leaked_workers(self) -> int:
        """Return workers pinned at deadlines across abandoned executors.
        This upper bound includes long tasks that later release their workers."""
        return self._leaked_workers

    @property
    def replacements(self) -> int:
        """How often a wedge cost this pool its executor."""
        return self._replacements

    @property
    def is_quarantined(self) -> bool:
        """Return whether the replacement budget is spent and swaps have stopped.
        Current work still runs, but owners must stop feeding a wedged quarantined pool."""
        with self._lock:
            return self._quarantined

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
        """Queue fn from any thread, returning its Future or None after shutdown.
        Propagate live-pool RuntimeError because refused work never reached the queue."""
        # Keep executor selection and submission atomic against replacement.
        # Otherwise a live pool can silently submit to the executor being shut down.
        with self._lock:
            try:
                return self._executor.submit(fn, *args, **kwargs)
            except RuntimeError as error:
                # RuntimeError text distinguishes shutdown from live refusal.
                # The flag misses executors shut down through another reference.
                if self._shutdown or "shutdown" in str(error):
                    return None
                raise

    def run_batch(
        self,
        tasks: Iterable[tuple[str, Callable[[], object]]],
        *,
        deadline: float,
    ) -> BatchOutcome:
        """Submit named tasks and wait at most one shared deadline from the final submission.
        Run on the caller thread that must not wedge; an empty batch waits for nothing."""
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
                # exception() keeps a task's TimeoutError distinct from the wait timeout.
                raised = future.exception(timeout=max(0.0, expiry - time.monotonic()))
            except FutureTimeoutError:
                # A running overdue task wedges a worker; a queued one is only late.
                # Future state is locked and becomes running before task entry.
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
        """Stop taking work so later submissions return None.
        Default to no wait; use wait=True only when every task is known to return."""
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
            # During teardown, report rather than replace a pool nobody will own.
            # cancel_futures has already taken its queue.
            log.info(
                f"{head}; the {words.pool_name} is shut down, so each stuck "
                f"task holds its thread until it returns and the close took "
                f"whatever was still queued. {tail}")
            return False
        if replaced:
            aftermath = (
                f"Replacing the {words.pool_name}, so the stuck task(s) leak their "
                f"executor's thread(s) once instead of wedging every later batch "
                f"behind them.")
        elif self.is_quarantined:
            aftermath = (
                f"The {words.pool_name} has replaced its executor "
                f"{self._max_replacements} times and now stops, so it leaks no "
                f"more threads. Later work queues behind the stuck task(s), and "
                f"this pool needs its owner to give up on it.")
        else:
            aftermath = (
                f"The {words.pool_name} keeps the stuck task(s), so later work "
                f"queues behind them.")
        log.warning(
            f"{head}; continuing without them ({words.stuck_hint}). "
            f"{aftermath} {tail}")
        return replaced

    def _wedge(self, stuck_count: int) -> bool:
        """Atomically count pinned workers and optionally publish a fresh executor.
        Never replace after shutdown or cancel the old queue; its free workers must drain it."""
        old: Executor | None = None
        with self._lock:
            self._leaked_workers += stuck_count
            # Replace only within budget, then quarantine the current executor.
            # This bounds workers leaked behind tasks that never return.
            if (self._replace_on_wedge and not self._shutdown
                    and self._replacements < self._max_replacements):
                old = self._executor
                self._executor = self._new_executor(self._max_workers, self._thread_name_prefix)
                self._replacements += 1
            elif self._replace_on_wedge and not self._shutdown:
                self._quarantined = True
        if old is None:
            return False
        old.shutdown(wait=False)
        return True
