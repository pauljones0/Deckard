"""Marshal GLib work and manage background tasks without widget imports.
Backend code imports here; GtkHelper re-exports names, and timeouts are read at call time."""
import functools
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Generic, ParamSpec, TypeVar, TypedDict, cast

from gi.repository import GLib

from loguru import logger as log


# Preserve a decorated function's own parameters and return type, so a
# decorator below hands back a callable with the same signature.
_Params = ParamSpec("_Params")
_Return = TypeVar("_Return")


class _MarshalBox(TypedDict, Generic[_Return], total=False):
    """What one marshalled call hands back across the thread boundary."""

    result: _Return
    exc: BaseException

# How long a worker waits for the main loop to service its marshalled call.
# Module-level (read at call time) so tests can shrink it.
RUN_ON_MAIN_TIMEOUT_S = 30


def run_on_main(func: Callable[_Params, _Return], *args: _Params.args, **kwargs: _Params.kwargs) -> _Return:
    """Run on the GTK main thread and block; execute inline when already there.
    Cancel an unclaimed idle source on timeout so func cannot run after abandonment."""
    if threading.current_thread() is threading.main_thread():
        return func(*args, **kwargs)

    done = threading.Event()
    box: "_MarshalBox[_Return]" = {}
    state_lock = threading.Lock()
    # claimed commits the callback; abandoned cancels an unclaimed callback.
    # The lock makes these states mutually exclusive.
    state = {"claimed": False, "abandoned": False}

    def _cb() -> bool:
        with state_lock:
            if state["abandoned"]:
                # The caller timed out and moved on. Nothing waits for this
                # result, and a run now executes func a second time.
                return GLib.SOURCE_REMOVE
            state["claimed"] = True
        try:
            box["result"] = func(*args, **kwargs)
        except BaseException as e:
            box["exc"] = e
        finally:
            done.set()
        return GLib.SOURCE_REMOVE

    timeout = RUN_ON_MAIN_TIMEOUT_S
    source_id = GLib.idle_add(_cb)
    # Bound the wait. A main loop that stops pumping, during quit for
    # example, parks this worker and every lock it holds.
    if not done.wait(timeout=timeout):
        with state_lock:
            timed_out = not state["claimed"]
            if timed_out:
                state["abandoned"] = True
                # An unclaimed source has not dispatched func and is safe to remove.
                # Removing during dispatch would only flag the source as destroyed.
                GLib.source_remove(source_id)
        if timed_out:
            raise RuntimeError(
                f"main loop did not service run_on_main({getattr(func, '__name__', func)}) "
                f"within {timeout}s"
            )
        # A callback claimed at the deadline is in flight, not abandoned.
        # Wait once more instead of reporting a timeout for a running call.
        if not done.wait(timeout=timeout):
            raise RuntimeError(
                f"run_on_main({getattr(func, '__name__', func)}) started on the main loop "
                f"but did not finish within a further {timeout}s"
            )
    if "exc" in box:
        raise box["exc"]
    # Completion sets exactly one key; the exception branch left result present.
    # cast removes the optional type introduced by dict.get().
    return cast("_Return", box.get("result"))


def on_main(func: Callable[_Params, _Return]) -> Callable[_Params, _Return]:
    """Decorator form of run_on_main."""
    @functools.wraps(func)
    def wrapper(*args: _Params.args, **kwargs: _Params.kwargs) -> _Return:
        return run_on_main(func, *args, **kwargs)
    return wrapper


# Eight non-daemon workers run I/O-bound work; Python CPU work stays GIL-bound.
# App quit uses os._exit, while normal exit wakes idle workers through atexit.
_background_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="background")


def log_future_exception(future: "Future[Any]") -> None:
    """Log exceptions from application and owned-executor futures."""
    try:
        exc = future.exception()
    except Exception:
        return
    if exc is not None:
        log.opt(exception=exc).error("background task raised")


def run_in_background(func: Callable[_Params, _Return], *args: _Params.args, **kwargs: _Params.kwargs) -> "Future[_Return]":
    """Submit func to the background pool and return its Future. A .result()
    call on the GTK thread deadlocks when the work calls an on_main method."""
    future = _background_pool.submit(func, *args, **kwargs)
    future.add_done_callback(log_future_exception)
    return future


def background(func: Callable[_Params, _Return]) -> Callable[_Params, "Future[_Return]"]:
    """Decorator form of run_in_background."""
    @functools.wraps(func)
    def wrapper(*args: _Params.args, **kwargs: _Params.kwargs) -> "Future[_Return]":
        return run_in_background(func, *args, **kwargs)
    return wrapper


def shutdown_background_pool() -> None:
    """Stop the @background pool; call on app quit."""
    _background_pool.shutdown(wait=False, cancel_futures=True)
