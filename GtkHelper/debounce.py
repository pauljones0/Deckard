"""Trailing debounce for expensive GTK handlers.
The Settings font rows otherwise start overlapping full-page reloads for each rapid change."""
import threading
from typing import Callable, Generic, Optional, Protocol, TypeVar, cast

# The handle a scheduler hands back from schedule and takes in cancel. Each
# scheduler defines its own; the debouncer never looks inside one.
HandleT = TypeVar("HandleT", default=int)


# Inject the timer source so tests can drive coalescing without a GTK loop.
# Production uses GLib source removal and timeout scheduling.
class Scheduler(Protocol[HandleT]):
    """The two operations that a trailing debounce needs from a timer."""

    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> HandleT:
        """Arm a one-shot timer and return a handle for cancel."""
        ...

    def cancel(self, handle: HandleT) -> None:
        """Disarm a timer that has not fired yet."""
        ...


class GLibScheduler:
    """Scheduler backed by the GTK main loop.
    Method-local imports keep a module import from loading GTK.
    """

    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> int:
        from gi.repository import GLib
        return GLib.timeout_add(delay_ms, self._fire, callback)

    def cancel(self, handle: int) -> None:
        from gi.repository import GLib
        GLib.source_remove(handle)

    @staticmethod
    def _fire(callback: Callable[[], None]) -> bool:
        from gi.repository import GLib
        callback()
        return GLib.SOURCE_REMOVE


class TrailingDebouncer(Generic[HandleT]):
    """Coalesce a burst of triggers into one trailing callback.
    Each trigger rearms the timer, so the callback runs delay_ms after the final trigger.
    """

    def __init__(self, delay_ms: int, callback: Callable[[], None],
                 scheduler: "Optional[Scheduler[HandleT]]" = None):
        self.delay_ms = delay_ms
        self.callback = callback
        # Without a scheduler, HandleT defaults to the int that GLibScheduler returns.
        # The cast states the binding that the checker cannot infer from an absent argument.
        self.scheduler: "Scheduler[HandleT]" = scheduler or cast("Scheduler[HandleT]", GLibScheduler())
        # Each trigger must reach exactly one callback per burst.
        # No equality check, dirty flag, or early return can drop it.
        self._pending: Optional[HandleT] = None
        # trigger() and the callback run on the scheduler thread, which is GTK's main thread in production.
        # The pending handle therefore needs no lock.
        self._owner_thread: Optional[int] = None

    def trigger(self) -> None:
        """Request a callback delay_ms from now, and replace a pending one."""
        # Enforce the one-thread contract because _pending has no lock.
        # A second thread can double-fire or remove a dead source id.
        ident = threading.get_ident()
        if self._owner_thread is None:
            self._owner_thread = ident
        elif ident != self._owner_thread:
            raise RuntimeError(
                "TrailingDebouncer.trigger() called from a second thread -- "
                "this class is single-thread by contract (class docstring)")
        if self._pending is not None:
            self.scheduler.cancel(self._pending)
            self._pending = None
        self._pending = self.scheduler.schedule(self.delay_ms, self._fire)

    def _fire(self) -> None:
        # Clear before the call, so a trigger from inside the callback arms a
        # new timer instead of cancelling a source that GLib already removed.
        self._pending = None
        self.callback()
