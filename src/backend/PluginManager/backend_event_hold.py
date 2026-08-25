"""A bounded hold for the events a plugin fires while its backend connects.

A backend launch is asynchronous: the child has to start python, connect back
over rpyc and call register_backend. A plugin that fires an event in that
window fires it into nothing, because the actions that listen for it are
often not loaded yet, and an event with no observer is dropped where it is
raised. A device the backend reports at startup therefore never reaches the
keys that show it, until something makes the plugin fire again.

The hold keeps such an event for a short window and delivers it when the
backend registers. It is not a replay buffer:

- Bounded. The window opens at the launch and its deadline never moves, so a
  later event does not extend it. What the window still holds when it closes
  is dropped, with one log line.
- Coalesced. One entry per holder and event id, and the newest wins. A chatty
  event source cannot grow the hold. Two holders that share one event id keep
  an entry each, which EventHolder allows.
- Superseded by a live dispatch. An event that goes out while the window is
  open drops what the window holds for the same holder and event id, so the
  release cannot land an older value behind a newer one the observers already
  saw.
- Generation guarded. Every entry carries the generation of the launch that
  took it. A relaunch opens a new window and drops what the previous one
  held, so a reconnect starts a plugin's event stream instead of replaying
  the events of the connection that failed.

Delivery re-reads the observers, so an action that subscribes inside the
window still receives the event. It runs on the thread that closes the
window, which is the rpyc service thread of register_backend, and it only
queues onto the dispatch lanes, so it blocks that thread on nothing and
touches no GTK.

Two limits are deliberate. The window is anchored to the backend connecting
and not to an observer arriving, so an action whose on_ready runs after
register_backend still misses the event; the delivery logs that it reached
nobody rather than pretending otherwise. And coalescing keeps one value per
holder and event id, so a source that reports several distinct items under
one event id inside the window delivers the last of them alone.
"""

import threading
from collections.abc import Callable

from loguru import logger as log

from src.backend import timer_wheel

# Long enough to cover the launch of a python child and its rpyc handshake,
# short enough that a backend which never registers costs a bounded delay on
# the events of one plugin.
HOLD_BOUND_S = 2.0


class BackendEventHold:
    """The hold of one plugin. PluginBase owns one and drives its window."""

    def __init__(self, label: str = "", bound_s: float = HOLD_BOUND_S):
        self.label = label
        self.bound_s = bound_s
        # One lock over every field below. The window opens on the launching
        # thread, takes events from arbitrary plugin threads, closes on an
        # rpyc service thread and expires on the timer wheel's thread.
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
        """Close the window and deliver what it holds.

        register_backend calls this once the backend connection is live and
        before it runs the plugin's own hook, so an event the hook fires goes
        out the normal way.
        """
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
        """Close the window and drop what it holds, with no delivery.

        A teardown calls this. Without it the events of a plugin whose backend
        went away would stay held until the deadline, for no one.
        """
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
        """Offer one event to the hold.

        True means the hold keeps it and release() will deliver it. False
        means the window is shut and the caller delivers now. An entry
        replaces the one that key held already. The key names one holder and
        one event id, so two holders that share an event id keep an entry
        each.
        """
        with self._lock:
            if not self._armed:
                return False
            self._held[key] = (self._generation, deliver)
            return True

    def drop(self, key: str) -> None:
        """Forget what this key holds.

        A caller that dispatches an event live calls this for the same key
        first. Without it the release would hand the observers a value they
        already moved past, because the held entry is older than the one that
        just went out.
        """
        with self._lock:
            self._held.pop(key, None)

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed

    def _expire(self, generation: int) -> None:
        with self._lock:
            if not self._armed or generation != self._generation:
                # A release, a cancel or a later launch closed this window
                # already. cancel() on the handle cannot un-fire a timer that
                # is running, so this check is what makes the expiry harmless.
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
