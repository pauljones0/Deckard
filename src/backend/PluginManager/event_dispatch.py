"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

Observer dispatch for EventHolder and for the AssetManager plugin-settings
Observer, with one lane per event source.

A new asyncio event loop per trigger, and the default executor it creates,
churn file descriptors and threads. AudioControl fires its PulseEvent holder
tens of times per second during a volume change, which makes that churn visible
in telemetry. See docs/archive/memory-footprint-plan.md. Instead, a trigger hands its
batch of observers to a background thread that keeps one loop alive across
events. Lane below states what a lane isolates, and shutdown() states what quit
does to a queue.

Ordering. Batches run FIFO inside a lane. The observers of a batch run in
registration order, one at a time, each in its own try and except, so one
failing observer never stops the rest. The observers of one holder share its
lane, so one that blocks still delays that event source's other observers. A
split of a batch across lanes would lose the registration-order FIFO that
plugins depend on, which tests/scenario_event_dispatch_contract.py pins.
Nothing is ordered across lanes, because dispatch is a queue-and-return from
arbitrary plugin threads.

trigger_event() and notify() return as soon as the batch is queued, before the
observers run. That already holds for the call site that matters.
PulseEvent.trigger_event() runs synchronously inside the dispatch loop of
pulse.event_listen(), and nothing reads a return value or waits for the
observers.

Known limitation. A lane's loop identity changes across an idle reap, so an
observer that captured its running loop for a later call_soon_threadsafe holds
a closed one. No installed plugin does that, and AudioControl is the only
producer of async observers.
"""
import asyncio
import inspect
import threading
import time
from collections import deque
from typing import Any, Callable, Iterable, TypedDict, cast
from weakref import WeakSet

from loguru import logger as log

# Each lane runner reuses one thread-local asyncio loop.
# Runner exit closes the loop, so an idle lane holds no epoll descriptor.
_thread_state = threading.local()


def _get_loop() -> asyncio.AbstractEventLoop:
    loop: "asyncio.AbstractEventLoop | None" = getattr(_thread_state, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        # Retrieve observer task exceptions through the application handler.
        # Lazy import avoids a module import-order requirement.
        from src.backend.log_hooks import asyncio_exception_handler
        loop.set_exception_handler(asyncio_exception_handler)
        _thread_state.loop = loop
    return loop


def _close_thread_loop() -> None:
    """Close the calling runner's loop and reclaim its epoll descriptor.
    Every runner exit path calls this."""
    loop = getattr(_thread_state, "loop", None)
    _thread_state.loop = None
    if loop is None:
        return
    try:
        asyncio.set_event_loop(None)
        if not loop.is_closed():
            loop.close()
    except Exception:
        log.opt(exception=True).warning("failed to close an event dispatch lane's loop")


# Report a wedged observer's lane, duration, and queued work.
# Other lanes continue while the blocked event source stalls.
_WEDGE_WARN_S = 10.0
_WEDGE_REWARN_S = 30.0
_MONITOR_INTERVAL_S = 5.0
_BACKLOG_WARN_THRESHOLD = 100

# Cap queued batches and drop the oldest first; any burst can reach this limit.
# The limit exceeds the warning threshold and bounds objects retained by a stalled lane.
_QUEUE_MAX = 1000

# Reap an idle runner and its loop after this interval.
# The next dispatch starts a new runner; active lanes do not reach the timeout.
_IDLE_REAP_S = 60.0

_watch_lock = threading.Lock()
# Hold lanes weakly so the monitor does not retain their owners.
# Lock mutation and snapshots; WeakSet tolerates GC removal but not concurrent additions.
_lanes: "WeakSet[Lane]" = WeakSet()
# The app-wide count of queued batches over every lane. Each lane keeps its own
# count. This one serves diagnostics, and every dispatch decision is per lane.
_backlog = 0
_monitor_started = False
_shutdown = False


def _observer_name(observer: object) -> str:
    # Use cheap names before guarded repr, which can run plugin code or raise.
    # Callers name observers outside _watch_lock.
    name = getattr(observer, "__qualname__", None)
    if name is None:
        name = getattr(observer, "__name__", None)
    if name is None:
        try:
            name = repr(observer)
        except Exception:
            name = "<unrepresentable observer>"
    return cast(str, name)


def _ensure_monitor() -> None:
    global _monitor_started
    # A stale fast-path read costs one extra lock acquisition.
    # The locked check still starts only one monitor thread.
    if _monitor_started:
        return
    with _watch_lock:
        # A fresh, declared read: the outer check narrowed the module flag,
        # and narrowing cannot see another thread's write before the lock.
        started: bool = _monitor_started
        if started:
            return
        _monitor_started = True
    threading.Thread(target=_monitor_loop, name="event_dispatch_watchdog",
                     daemon=True).start()


def _monitor_tick() -> None:
    # End the snapshot frame before sleep so strong lane references die.
    # Otherwise a dead holder and its queued graph live one more interval.
    with _watch_lock:
        lanes = list(_lanes)
    for lane in lanes:
        lane._check_wedge()


def _monitor_loop() -> None:
    while True:
        time.sleep(_MONITOR_INTERVAL_S)
        if _shutdown:
            return
        try:
            _monitor_tick()
        except Exception:
            # Keep the monitor alive after lane failures or pre-3.14 GC removal races
            # outside the lock. A failed tick costs one interval, not later reports.
            log.opt(exception=True).error("event dispatch watchdog tick failed")


class _CurrentObserver(TypedDict):
    """The observer this lane runs now, in Lane.current, if there is one."""
    name: str | None
    label: str | None
    started: float
    next_warn: float


class DispatchShutdown(RuntimeError):
    """Signal dispatch attempts after shutdown while retaining RuntimeError compatibility.
    Plugin entry points catch only this subtype, not thread-creation failures."""


class Lane:
    """Serialize a dispatch queue on at most one isolated runner thread.
    Spawn on first dispatch and reap the runner after an empty _IDLE_REAP_S interval."""

    def __init__(self, label: str | None = None):
        self.label = label
        self._cond = threading.Condition()
        self._pending: deque[Any] = deque()
        self._runner: threading.Thread | None = None
        # _watch_lock guards this watchdog state across short observer updates.
        # The TypedDict preserves slot types while the runtime value stays a dict.
        self.current: _CurrentObserver = {"name": None, "label": None, "started": 0.0, "next_warn": 0.0}
        self.backlog = 0
        self.backlog_warned = False
        # Lifetime count of batches dropped at the cap.
        # _account_dropped reports each producer-side update immediately.
        self.dropped = 0
        with _watch_lock:
            _lanes.add(self)

    @property
    def name(self) -> str:
        return self.label or "default"

    def dispatch(self, observers: Iterable[Callable[..., Any]], args: tuple[Any, ...], kwargs: dict[str, Any],
                 label: str | None = None) -> None:
        """Queue observers onto this lane and return."""
        global _backlog
        observers = list(observers)
        if not observers:
            return
        if _shutdown:
            # Reject before accounting so the batch adds no backlog count.
            raise DispatchShutdown("event dispatch is shut down")
        _ensure_monitor()
        with _watch_lock:
            _backlog += 1
            self.backlog += 1
            backlog = self.backlog
            if backlog >= _BACKLOG_WARN_THRESHOLD and not self.backlog_warned:
                self.backlog_warned = True
                warn_backlog = True
            else:
                if backlog < _BACKLOG_WARN_THRESHOLD // 2:
                    self.backlog_warned = False
                warn_backlog = False
            stuck_name = self.current["name"]
        if warn_backlog:
            log.error(
                f"event dispatch lane {self.name} backlog reached {backlog} "
                f"queued batch(es) -- this lane is stalled"
                + (f" inside observer {stuck_name}" if stuck_name else "")
            )
        try:
            self._enqueue((observers, label, args, kwargs))
        except BaseException:
            # The batch never runs, so the backlog count must not leak.
            with _watch_lock:
                _backlog -= 1
                self.backlog -= 1
            raise

    def _enqueue(self, batch: tuple[Any, ...]) -> None:
        dropped = 0
        with self._cond:
            if _shutdown:
                # Recheck under the lane lock, but shutdown can race after this point.
                # A newly created runner then sees _shutdown and abandons its queue.
                raise DispatchShutdown("event dispatch is shut down")
            self._pending.append(batch)
            # Drop oldest batches until the queue is within its cap.
            # This keeps fresh state and releases objects retained by a stalled lane.
            while len(self._pending) > _QUEUE_MAX:
                self._pending.popleft()
                dropped += 1
            if self._runner is not None:
                self._cond.notify()
            else:
                try:
                    self._spawn_locked()
                except BaseException:
                    self._pending.pop()
                    raise
        if dropped:
            self._account_dropped(dropped)

    def _account_dropped(self, dropped: int) -> None:
        """Retire backlog counts for batches that cannot reach _run_batch.
        Call without self._cond held and record each drop."""
        global _backlog
        with _watch_lock:
            _backlog -= dropped
            self.backlog -= dropped
            self.dropped += dropped
            total = self.dropped
        log.error(
            f"event dispatch lane {self.name} dropped {dropped} queued "
            f"batch(es) at the {_QUEUE_MAX}-batch cap ({total} dropped in "
            f"all); a wedged observer is shedding events")

    def _spawn_locked(self) -> None:
        """Start this lane's runner. The caller holds self._cond."""
        runner = threading.Thread(target=self._run, name=f"event_dispatch:{self.name}",
                                  daemon=True)
        self._runner = runner
        try:
            runner.start()
        except BaseException:
            # Clear a booked runner when its thread fails to start.
            # Otherwise later dispatches notify a nonexistent thread.
            self._runner = None
            raise

    def _run(self) -> None:
        try:
            while True:
                with self._cond:
                    idle_deadline = time.monotonic() + _IDLE_REAP_S
                    while not self._pending:
                        remaining = idle_deadline - time.monotonic()
                        if _shutdown or remaining <= 0:
                            # Clear _runner under the lock that guards queue emptiness.
                            # Earlier appends prevent exit; later appends start a new runner.
                            self._runner = None
                            return
                        self._cond.wait(remaining)
                    if _shutdown:
                        # Abandon queued observers after decks and log sinks close.
                        # Clear _runner with the same discipline as idle reaping.
                        self._runner = None
                        return
                    batch = self._pending.popleft()
                try:
                    self._run_batch(*batch)
                except Exception:
                    # Log batch setup failures outside observer handling.
                    # Keep the runner available for later batches.
                    log.opt(exception=True).error("event dispatch batch failed before observer dispatch")
        finally:
            self._retire()

    def _retire(self) -> None:
        # Keep _runner None whenever no thread services the lane.
        # The identity guard preserves a replacement spawned after idle reaping.
        try:
            with self._cond:
                if self._runner is threading.current_thread():
                    self._runner = None
                    if self._pending and not _shutdown:
                        # Replace a runner that exits through BaseException.
                        # This prevents queued work from becoming stranded.
                        self._spawn_locked()
        except Exception:
            log.opt(exception=True).error(
                f"event dispatch lane {self.name} could not be retired cleanly")
        _close_thread_loop()

    def _run_batch(self, observers: list[Callable[..., Any]], label: str | None,
                   args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        global _backlog
        try:
            # Keep loop creation and its lazy import inside the try because either can raise.
            # The finally owns this batch's backlog decrement.
            loop = _get_loop()
            asyncio.set_event_loop(loop)
            for observer in observers:
                # Compute the display name off the lock: it may run a plugin's
                # __repr__, which must not execute under _watch_lock.
                observer_name = _observer_name(observer)
                with _watch_lock:
                    self.current["name"] = observer_name
                    self.current["label"] = label
                    self.current["started"] = time.monotonic()
                    self.current["next_warn"] = _WEDGE_WARN_S
                try:
                    # Invoke once and await any returned awaitable.
                    # This includes async callable instances and decorated wrappers.
                    result = observer(*args, **kwargs)
                    if inspect.isawaitable(result):
                        loop.run_until_complete(result)
                except Exception:
                    name = getattr(observer, "__name__", repr(observer))
                    where = f" in {label}" if label else ""
                    # Attach sys.exc_info() so observer failures retain full tracebacks.
                    log.opt(exception=True).error(f"Callback {name}{where} could not be called")
        finally:
            with _watch_lock:
                self.current["name"] = None
                self.backlog -= 1
                _backlog -= 1

    def _check_wedge(self) -> None:
        with _watch_lock:
            name = self.current["name"]
            label = self.current["label"]
            started = self.current["started"]
            next_warn = self.current["next_warn"]
            backlog = self.backlog
        if name is None:
            return
        stuck_for = time.monotonic() - started
        if stuck_for < next_warn:
            return
        with _watch_lock:
            if self.current["name"] is not name or self.current["started"] != started:
                # Do not move the re-warn clock if the observer changed.
                return
            self.current["next_warn"] = stuck_for + _WEDGE_REWARN_S
        # Print the batch label only when it adds to the lane's own name. For
        # an EventHolder's lane the two hold the same event id.
        where = f" in {label}" if label and label != self.label else ""
        log.error(
            f"event dispatch lane {self.name} wedged for {stuck_for:.0f}s "
            f"inside observer {name}{where} -- events on this lane are "
            f"stalled behind it ({backlog} batch(es) queued); other lanes "
            f"are unaffected"
        )


# Callers without their own lane share this default lane.
_default_lane = Lane()


def dispatch(observers: Iterable[Callable[..., Any]], args: tuple[Any, ...], kwargs: dict[str, Any], label: str | None = None) -> None:
    """Queue observers on the shared default lane and return.
    Blocking observers stall all callers on this lane; the watchdog names them."""
    _default_lane.dispatch(observers, args, kwargs, label=label)


def shutdown() -> None:
    """Reject later dispatch, abandon queued batches, and wake lane runners.
    Do not interrupt or join running daemon batches, so a wedged lane cannot delay exit."""
    global _shutdown
    _shutdown = True
    with _watch_lock:
        lanes = list(_lanes)
    for lane in lanes:
        with lane._cond:
            lane._cond.notify_all()
