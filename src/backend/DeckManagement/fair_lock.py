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
"""
import threading
import time
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from types import TracebackType

# FIFO transport mutex that bounds a reader behind queued device I/O chunks.
# It is non-reentrant; a holder that reacquires queues behind itself and deadlocks.


class FairLock:
    """Ticket mutex that grants ownership in acquisition order.
    Supports blocking, non-blocking, timed, and context-manager use."""

    __slots__ = ("_cond", "_next_ticket", "_serving", "_abandoned")

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        # acquire() hands out tickets from _next_ticket and serves them in
        # order. _serving != _next_ticket means a thread owns the lock.
        self._next_ticket = 0
        self._serving = 0
        # Tickets whose waiter timed out. release() steps over them, so the
        # queue never stalls behind a ticket with no waiter.
        self._abandoned: set[int] = set()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        with self._cond:
            if not blocking:
                # Do not take a ticket that can go unclaimed. Succeed only
                # when no thread owns or queues for the lock.
                if self._serving != self._next_ticket:
                    return False
                self._next_ticket += 1
                return True

            ticket = self._next_ticket
            self._next_ticket += 1

            if timeout is None or timeout < 0:
                while self._serving != ticket:
                    self._cond.wait()
                return True

            deadline = time.monotonic() + timeout
            while self._serving != ticket:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)

            if self._serving == ticket:
                # Served at or before the deadline. The caller owns the lock.
                return True
            self._abandoned.add(ticket)
            return False

    def release(self) -> None:
        with self._cond:
            if self._serving == self._next_ticket:
                raise RuntimeError("release unlocked FairLock")
            self._advance_locked()

    def locked(self) -> bool:
        with self._cond:
            return self._serving != self._next_ticket

    def _advance_locked(self) -> None:
        # Caller holds self._cond.
        self._serving += 1
        while self._serving in self._abandoned:
            self._abandoned.discard(self._serving)
            self._serving += 1
        # Wake all because notify can select a waiter whose ticket is not next.
        # Each waiter rechecks its ticket, so no wakeup is lost.
        self._cond.notify_all()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
                 traceback: "TracebackType | None") -> Literal[False]:
        self.release()
        return False
