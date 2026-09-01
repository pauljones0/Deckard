"""Defer callbacks until App readiness and CLI requests until their deck appears.
Import only globals so every layer can use these process-wide handshakes without cycles."""
from __future__ import annotations

from typing import TYPE_CHECKING

import globals as gl

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


# GIL-atomic list and dict operations synchronize ownership without locks.
# The operation order in each method is part of that synchronization.
class StartupQueue:
    """Apply boot deferral without local state or cached globals slots.
    Each call observes slot rebinding and direct plugin appends."""

    # Leg A. Deliveries that wait on the running App.

    def when_app_ready(self, task: Callable[[], Any]) -> bool:
        """Return whether the caller owns task now; otherwise queue it until gl.app exists.
        Thread-safe ownership requires GIL-atomic append, recheck, and remove in this order."""
        if gl.app is None:
            gl.app_loading_finished_tasks.append(task)
            if gl.app is None:
                return False
            # Recover if gl.app publishes between append and the second check.
            try:
                gl.app_loading_finished_tasks.remove(task)
            except ValueError:
                # The drain took it first and delivers it.
                return False
        return True

    def drain_app_ready(self) -> None:
        """Drain callable tasks by atomic pop on the main thread, including new appends.
        Skip non-callables; exceptions propagate and leave later tasks queued."""
        while gl.app_loading_finished_tasks:
            task = gl.app_loading_finished_tasks.pop(0)
            if callable(task):
                task()

    # Leg B. --change-page requests that wait for their deck.

    def park_page_request(self, serial_number: str, page_name: str) -> None:
        """Park a raw page name until its deck appears; the last write per serial wins.
        Called pre-boot on main; requests live until claimed or process exit."""
        gl.api_page_requests[serial_number] = page_name

    def claim_page_request(self, serial_number: str) -> str | None:
        """Atomically remove this serial's one-shot page request, or return None.
        Callers include main, GTK, rescan, USB, HID, plugin, and action threads."""
        return gl.api_page_requests.pop(serial_number, None)

    # Leg C. --change-state requests that wait for their deck.

    def park_state_request(self, serial_number: str, request: dict[str, Any]) -> None:
        """Park a validated state request until its deck appears; last write per serial wins.
        The CLI parser owns validation and state-number conversion."""
        gl.api_state_requests[serial_number] = request

    def peek_state_request(self, serial_number: str) -> dict[str, Any] | None:
        """Return the parked state request without removal so application failures can retry it.
        Every processing caller must resolve it after success."""
        return gl.api_state_requests.get(serial_number)

    def resolve_state_request(self, serial_number: str) -> None:
        """Idempotently drop a state request after processing succeeds.
        Resolving before processing removes the retry after a failure."""
        gl.api_state_requests.pop(serial_number, None)

    # Legs B and C. The whole parking, for a process that leaves.

    def claim_parked_requests(self) -> tuple[list[tuple[str, str]],
                                             list[tuple[str, dict[str, Any]]]]:
        """Remove all pages then states, preserving insertion order within each kind.
        Use during single-threaded pre-boot handoff to the instance that won the name race."""
        pages = [(serial, gl.api_page_requests.pop(serial))
                 for serial in list(gl.api_page_requests)]
        states = [(serial, gl.api_state_requests.pop(serial))
                  for serial in list(gl.api_state_requests)]
        return pages, states


# Process-wide singleton outside globals to keep the shared namespace small.
_queue = StartupQueue()


def get() -> StartupQueue:
    """The process-wide startup queue. Never None."""
    return _queue
