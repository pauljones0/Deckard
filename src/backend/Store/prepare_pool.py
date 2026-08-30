"""Standard-library fan-out pool for store catalog passes."""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any


class PreparePool:
    """Run catalog preparation at the fetch limit and support bounded app shutdown."""

    def __init__(self, max_workers: int) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="store-prepare")

        # Stop an in-flight pass before its next request after shutdown.
        self.stopping = False

    def submit(self, func: "Callable[..., Any]", *args: Any) -> "Future[Any]":
        """Queue a task, or return a cancelled future after shutdown."""
        if self.stopping:
            cancelled: "Future[Any]" = Future()
            cancelled.cancel()
            return cancelled
        return self._pool.submit(func, *args)

    def shutdown(self) -> None:
        """Stop new fetches and cancel preparation tasks that have not started."""
        self.stopping = True
        self._pool.shutdown(wait=False, cancel_futures=True)
