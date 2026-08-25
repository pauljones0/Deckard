"""The fan-out pool of the store catalog passes.

This module imports stdlib only.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any


class PreparePool:
    """The fan-out pool of the store catalog passes, and its release.

    The pool runs the prepare_* task of every catalog entry. Its size matches
    the store fetch cap, because further workers would only queue on that
    semaphore, and nothing that runs on the pool submits to it, so the pool
    cannot starve itself.

    The pool also owns the app-quit contract. Its workers are not daemons,
    because CPython 3.9 stopped marking the worker threads as daemons, and an
    idle worker parks on the work queue for as long as the pool lives. The
    quit path joins every non-daemon thread with a bound, so a pool that
    nothing releases costs every quit after the first catalog pass, such as
    the first-run prefetch, that bound and the force-quit backstop after it.
    """

    def __init__(self, max_workers: int) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="store-prepare")

        # True once shutdown() ran. The fetch path reads it, so a pass that is
        # in flight ends at its next request rather than on the network.
        self.stopping = False

    def submit(self, func: "Callable[..., Any]", *args: Any) -> "Future[Any]":
        """Queue one prepare task. After shutdown it hands back a cancelled
        future instead of raising, so a pass that starts during the quit
        collects an empty catalog and ends."""
        if self.stopping:
            cancelled: "Future[Any]" = Future()
            cancelled.cancel()
            return cancelled
        return self._pool.submit(func, *args)

    def shutdown(self) -> None:
        """Release the pool, on app quit. cancel_futures drops the entries no
        worker started, and the stopping flag ends a running task at its next
        fetch, so a worker holds one request in flight at most."""
        self.stopping = True
        self._pool.shutdown(wait=False, cancel_futures=True)
