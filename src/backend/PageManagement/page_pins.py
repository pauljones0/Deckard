"""Pin cached Page objects beyond the cache-visible active_page and screensaver-pending page so eviction cannot duplicate live actions.
Identity counts support renames and replacements; holding() and bracket() protect work, while each deck has one reserve_fetch() slot."""
from __future__ import annotations

import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import globals as gl

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.PageManagement.Page import Page


@contextmanager
def holding(page: "Page | None") -> Iterator["Page | None"]:
    """Hold the named page during the block and release it even on exceptions.
    Missing managers and None pages are allowed."""
    # Input work can outlive a page switch, and overlapping tick and key brackets
    # require counts; structural release prevents exceptions from pinning forever.
    manager = gl.page_manager
    pins = manager.pins if manager is not None else None
    if pins is not None:
        pins.pin(page)
    try:
        yield page
    finally:
        if pins is not None:
            pins.unpin(page)


class PagePins:
    """Track holders invisible to eviction under the subsystem's innermost lock."""

    # Cache or deck-load locks precede this lock, and nothing calls out under it.
    # It is re-entrant because reservation methods call the count methods.

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Weak identity counts avoid retaining pages after all real holders vanish;
        # Page has no __eq__, so each key is the acquired object itself.
        self._counts: "weakref.WeakKeyDictionary[Page, int]" = weakref.WeakKeyDictionary()
        # Keep one weak page reservation per deck so the next fetch can retire it
        # without retaining an evicted or deleted page until then.
        self._reservations: dict["DeckController", "weakref.ref[Page]"] = {}

    def pin(self, page: "Page | None") -> "Page | None":
        """Add a holder to page and return it, so the caller can bracket the
        work with the page the acquire named."""
        if page is None:
            return None
        with self._lock:
            self._counts[page] = self._counts.get(page, 0) + 1
        return page

    def unpin(self, page: "Page | None") -> None:
        """Drop a holder and forget the page at zero.
        An unmatched release is a no-op so it cannot make a future holder appear released."""
        if page is None:
            return
        with self._lock:
            remaining = self._counts.get(page, 0) - 1
            if remaining > 0:
                self._counts[page] = remaining
            else:
                self._counts.pop(page, None)

    def count(self, page: "Page") -> int:
        """Return the holder count of page. For assertions about balance. The
        cache needs only is_pinned()."""
        with self._lock:
            return self._counts.get(page, 0)

    def is_pinned(self, page: "Page | None") -> bool:
        with self._lock:
            return page in self._counts

    def bracket(self, page: "Page | None", ready_to_clear: bool) -> "Page | None":
        """Pin on False or release on True, and return the named page.
        Pass the False result to True; rereading active_page can leak the worked page and unprotect a concurrently installed page."""
        if ready_to_clear:
            self.unpin(page)
            return page
        return self.pin(page)

    def reserve_fetch(self, page: "Page | None", deck_controller: "DeckController") -> None:
        """Replace this deck's outstanding fetch, pinning before release.
        A repeat fetch of one page therefore retains at least one holder throughout."""
        # One slot per deck bounds abandoned fetches but lets another caller retire
        # it; window cycling can break screensaver handoff, and reload can release another caller's slot.
        with self._lock:
            self.pin(page)
            previous = self._reservations.pop(deck_controller, None)
            if page is not None:
                self._reservations[deck_controller] = weakref.ref(page)
        self._retire(previous)

    def release_fetch(self, deck_controller: "DeckController") -> None:
        """Retire the reservation when its page arrives, is abandoned, or its deck is gone."""
        with self._lock:
            reference = self._reservations.pop(deck_controller, None)
        self._retire(reference)

    def _retire(self, reference: "weakref.ref[Page] | None") -> None:
        """Release a popped weak reservation outside the lock that removed it.
        The resolved Page can be the last strong reference and run plugin finalizers, which must not run under this leaf lock."""
        page = reference() if reference is not None else None
        self.unpin(page)
