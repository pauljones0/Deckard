"""Hold newest unobserved event per key until registration or launch deadline; live dispatch wins.
Relaunch drops data; the rpyc service thread re-reads listeners, queues only, and does no GTK."""

import threading
from collections.abc import Callable

from loguru import logger as log

from src.backend import timer_wheel

# Starts at backend launch and does not move when later events arrive.
# Covers child launch and rpyc handshake, but bounds failure delay.
HOLD_BOUND_S = 2.0


class BackendEventHold:
    """The hold of one plugin. PluginBase owns one and drives its window."""

    def __init__(self, label: str = "", bound_s: float = HOLD_BOUND_S):
        self.label = label
        self.bound_s = bound_s
        # One lock covers launch, arbitrary plugin, rpyc, and timer-wheel threads.
        self._lock = threading.Lock()
        self._armed = False
        self._generation = 0
        self._held: "dict[str, tuple[int, Callable[[], None]]]" = {}
        self._timer: "timer_wheel.TimerHandle | None" = None

    def arm(self) -> None:
        """Open the window. A backend launch calls this before it spawns."""
        with self._lock:
            self._generation += 1
            generation = self._generation
            dropped = len(self._held)
            self._held.clear()
            self._armed = True
            previous, self._timer = self._timer, timer_wheel.schedule(
                self.bound_s, lambda: self._expire(generation),
                name=f"backend_event_hold:{self.label}")
        if previous is not None:
            previous.cancel()
        if dropped:
            log.warning(f"{self.label}: dropped {dropped} event(s) held for the previous "
                        f"backend connection, because the backend was launched again")

    def release(self) -> None:
        """Close the window and deliver held events before the plugin hook.
        register_backend calls this after the connection becomes live."""
        with self._lock:
            if not self._armed:
                return
            generation = self._generation
            pending = [deliver for stamp, deliver in self._held.values() if stamp == generation]
            self._held.clear()
            self._armed = False
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        for deliver in pending:
            self._deliver(deliver)

    def cancel(self) -> None:
        """Close the window and drop held events without delivery.
        Teardown uses this instead of retaining events until the deadline."""
        with self._lock:
            if not self._armed:
                return
            dropped = len(self._held)
            self._held.clear()
            self._armed = False
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        if dropped:
            log.debug(f"{self.label}: dropped {dropped} held event(s); the backend stopped "
                      f"before it registered")

    def submit(self, key: str, deliver: "Callable[[], None]") -> bool:
        """Replace the event for one holder key while the window is open.
        Return True when held for release, or False when the caller must deliver now."""
        with self._lock:
            if not self._armed:
                return False
            self._held[key] = (self._generation, deliver)
            return True

    def drop(self, key: str) -> None:
        """Drop the older held value before a live dispatch for this key."""
        with self._lock:
            self._held.pop(key, None)

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed

    def _expire(self, generation: int) -> None:
        with self._lock:
            if not self._armed or generation != self._generation:
                # A running timer cannot be un-fired after release, cancel, or relaunch.
                # The armed and generation check makes that expiry harmless.
                return
            dropped = len(self._held)
            self._held.clear()
            self._armed = False
            self._timer = None
        if dropped:
            log.warning(f"{self.label}: dropped {dropped} event(s) fired while the backend "
                        f"connected, because it did not register within {self.bound_s:g}s")

    def _deliver(self, callback: "Callable[[], None]") -> None:
        try:
            callback()
        except Exception:
            log.opt(exception=True).error(f"{self.label}: a held event could not be delivered")
