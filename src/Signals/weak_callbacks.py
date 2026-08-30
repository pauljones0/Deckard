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

CallbackRegistry is a locked holder for callback subscriptions. See
docs/memory-footprint-plan.md.

The registry stores a bound method as a weakref.WeakMethod, so the teardown
of an action or a controller drops its callback silently. A function, a
lambda and a functools.partial have no owner to weak-ref, so the registry
stores them strong. A lambda that captures an action therefore keeps that
action alive until an explicit remove() or disconnect().

Set SC_STRONG_CALLBACKS=1 in the environment (read once at import) to store
every callback strong. This isolates a plugin regression to this file.
"""
import os
import threading
import weakref
from typing import Any, Callable, Iterator, cast

from loguru import logger as log

# Read once at import, so this debugging knob cannot change behavior mid-run.
_STRONG_CALLBACKS = os.environ.get("SC_STRONG_CALLBACKS") == "1"

# An entry is a strong callback or a weak bound method.
# Both resolve to a live callable or None through _resolve_entry.
_Entry = object


def _is_bound_method(cb: Callable[..., Any]) -> bool:
    return hasattr(cb, "__self__") and hasattr(cb, "__func__")


def _same_callback(a: Callable[..., Any], b: Callable[..., Any]) -> bool:
    """Compare bound methods by owner and function identity, and others by identity.
    Never call plugin equality while the registry lock is held."""
    if a is b:
        return True
    a_self = getattr(a, "__self__", None)
    a_func = getattr(a, "__func__", None)
    if a_self is None or a_func is None:
        return False
    return a_self is getattr(b, "__self__", None) and a_func is getattr(b, "__func__", None)


def describe_callback(cb: Callable[..., Any]) -> str:
    """Return the callback identity used by signal failure and prune logs."""
    qualname: str = getattr(cb, "__qualname__", None) or repr(cb)
    module = getattr(cb, "__module__", None)
    return f"{module}.{qualname}" if module else qualname


class _WeakMethodEntry(weakref.WeakMethod[Any]):
    """Keep a method description after the weak target dies.
    The subclass instance dictionary stores the description for prune logs."""

    description: str

    def __new__(cls, meth: Callable[..., Any]) -> "_WeakMethodEntry":
        self = super().__new__(cls, meth)
        self.description = describe_callback(meth)
        return self


def _resolve_entry(entry: _Entry) -> Callable[..., Any] | None:
    """Return the live callable an entry refers to, or None if it died."""
    if isinstance(entry, weakref.WeakMethod):
        return entry()
    # The non-WeakMethod entry is the strong callable stored by _make_entry.
    return cast("Callable[..., Any]", entry)


class CallbackRegistry:
    """Store bound methods weakly in a thread-safe callback collection.
    Mutations and snapshots lock; snapshots also prune dead entries."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # A list and not a set, because callers depend on the connect order.
        self._entries: list[_Entry] = []

    def _make_entry(self, cb: Callable[..., Any]) -> _Entry:
        if not _STRONG_CALLBACKS and _is_bound_method(cb):
            return _WeakMethodEntry(cb)
        return cb

    def add(self, cb: Callable[..., Any]) -> bool:
        """Add a unique live callback and prune dead entries.
        Return True after an add and False after deduplication."""
        with self._lock:
            kept = []
            already_present = False
            for entry in self._entries:
                live = _resolve_entry(entry)
                if live is None:
                    continue
                kept.append(entry)
                if _same_callback(live, cb):
                    already_present = True
            self._entries = kept
            if already_present:
                return False
            try:
                entry = self._make_entry(cb)
            except TypeError:
                # Keep owners without weak-reference support connected strongly.
                # This includes __slots__ classes without __weakref__.
                log.debug(
                    f"CallbackRegistry: owner of {cb!r} is not weak-referenceable "
                    f"(__slots__ without __weakref__); storing a strong reference"
                )
                entry = cb
            self._entries.append(entry)
            return True

    def remove(self, cb: Callable[..., Any]) -> None:
        """Remove cb if present, else do nothing. Also prunes dead entries."""
        with self._lock:
            kept = []
            for entry in self._entries:
                live = _resolve_entry(entry)
                if live is None:
                    continue
                if _same_callback(live, cb):
                    continue
                kept.append(entry)
            self._entries = kept

    def snapshot(self) -> list[Callable[..., Any]]:
        """Return live callbacks, prune dead entries, and log each prune."""
        pruned: list[str] = []
        with self._lock:
            kept = []
            live_callbacks = []
            for entry in self._entries:
                live = _resolve_entry(entry)
                if live is None:
                    # Only a dead _WeakMethodEntry resolves to None.
                    pruned.append(getattr(entry, "description", repr(entry)))
                    continue
                kept.append(entry)
                live_callbacks.append(live)
            self._entries = kept
        # Log outside the lock so a sink cannot re-enter the locked registry.
        # Racing snapshots can log one dead entry twice without corrupting state.
        for description in pruned:
            log.debug(
                f"CallbackRegistry: pruning dead callback {description} "
                f"(owner was garbage-collected before it was removed)"
            )
        return live_callbacks

    def __iter__(self) -> Iterator[Callable[..., Any]]:
        return iter(self.snapshot())

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for entry in self._entries if _resolve_entry(entry) is not None)
