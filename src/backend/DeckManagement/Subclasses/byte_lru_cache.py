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

---

The byte-capped LRU shared by both native-image caches.

EncodedImageCache (pixel-hash keys plus a doorkeeper) and NativeTileCache
(frame-identity keys plus a kill switch) need the same OrderedDict-as-LRU and
the same byte accounting. This module holds that core once. The subclasses
supply only their admission policy and their teardown bookkeeping.

The paint path runs on the sole device writer's thread, which is why this
class has this shape. Its cost there:

    get() hit   one dict lookup, one move_to_end, one float store.
    put()       the same, plus the local-cap eviction loop, plus one
                Event.set() per about 1 MiB admitted (see cache_budget's
                wake damping). Nothing outside this instance's lock.
    eviction    popitem scale, one lock, never nested with another.

Instances also implement cache_budget.BudgetParticipant, through the budget_
methods at the foot of the class.
"""
import threading
import time
from collections import OrderedDict

from src.backend.DeckManagement.Subclasses import cache_budget

# Retain recent global evictions to detect re-admission against a live working set.
# The time and count bounds apply only to key-and-timestamp bookkeeping.
THRASH_WINDOW_S = 30.0
THRASH_RING_SIZE = 256


class ByteLRUCache:
    """Byte-capped LRU of immutable bytes with exact accounting and one lock.
    Subclasses can change admission and locked teardown, but share get and put."""

    def __init__(self, max_bytes: int) -> None:
        # A nonpositive cap makes get miss and put discard without caller branching.
        self._max_bytes = max(0, max_bytes)
        self._lock = threading.Lock()
        # Immutable bytes stay valid by reference after eviction while a paint holds them.
        # Eviction therefore affects cost, not correctness.
        self._entries: "OrderedDict[object, bytes]" = OrderedDict()
        # Keep same-key timestamps beside entries under the same lock.
        # Separate stamps preserve bytes identity and avoid tuple allocation on hits.
        self._stamps: dict[object, float] = {}
        self._total_bytes = 0

        # Track only recent global-budget evictions and count immediate re-admissions.
        # Local-cap evictions do not indicate global pressure.
        self._recent_evicted: "OrderedDict[object, float]" = OrderedDict()
        self._thrash_hits = 0

        # Notify only after the byte watermark so warm-up does not wake at paint rate.
        # Eviction hysteresis does not limit notification churn.
        self._bytes_since_notify = 0

    @property
    def enabled(self) -> bool:
        return self._max_bytes > 0

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, key: object) -> bytes | None:
        if self._max_bytes <= 0:
            return None
        with self._lock:
            data = self._entries.get(key)
            if data is not None:
                self._entries.move_to_end(key)
                self._stamps[key] = time.monotonic()
            return data

    def put(self, key: object, data: bytes) -> None:
        if self._max_bytes <= 0:
            return
        notify = False
        with self._lock:
            if key not in self._entries and not self._admit(key):
                return
            if self._recent_evicted and self._was_recently_evicted(key):
                self._thrash_hits += 1
            old = self._entries.pop(key, None)
            if old is not None:
                self._total_bytes -= len(old)
            self._entries[key] = data
            self._stamps[key] = time.monotonic()
            self._total_bytes += len(data)
            self._bytes_since_notify += len(data)
            while self._total_bytes > self._max_bytes and self._entries:
                self._pop_oldest_locked()
            if self._bytes_since_notify >= cache_budget.NOTIFY_WATERMARK_BYTES:
                self._bytes_since_notify = 0
                notify = True
        # Outside the lock, and only past the watermark. This Event.set() is
        # the entire cost of the global ceiling to a painter thread.
        if notify:
            cache_budget.notify_grew()

    def clear(self) -> None:
        """Drop all entries and subclass bookkeeping.
        Content, rotation, and deck teardown changes orphan every encoded entry."""
        with self._lock:
            self._entries.clear()
            self._stamps.clear()
            self._total_bytes = 0
            self._on_clear_locked()

    def _admit(self, key: object) -> bool:
        """Return whether a new key gets a cache slot while the caller holds the lock.
        The default admits first sightings; EncodedImageCache adds a doorkeeper."""
        return True

    def _on_clear_locked(self) -> None:
        """Extra teardown a subclass needs inside clear()'s critical section.
        The caller holds _lock. It does nothing by default."""
        pass

    def _pop_oldest_locked(self) -> int:
        """Drops the least-recently-used entry and returns its byte size.
        The caller holds _lock."""
        key, evicted = self._entries.popitem(last=False)
        self._stamps.pop(key, None)
        self._total_bytes -= len(evicted)
        return len(evicted)

    def _was_recently_evicted(self, key: object) -> bool:
        """Return whether the budget evicted key within the tripwire window.
        The caller holds the lock; lookup removes the bounded FIFO entry."""
        stamp = self._recent_evicted.get(key)
        if stamp is None:
            return False
        del self._recent_evicted[key]
        return (time.monotonic() - stamp) <= THRASH_WINDOW_S

    def _note_evicted_locked(self, key: object) -> None:
        self._recent_evicted[key] = time.monotonic()
        self._recent_evicted.move_to_end(key)
        while len(self._recent_evicted) > THRASH_RING_SIZE:
            self._recent_evicted.popitem(last=False)

    # Expose LRU heads so the process budget can evict the globally oldest entry.
    # Each operation takes only this cache lock and performs no cross-cache work.

    def budget_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    def budget_head_ts(self) -> float | None:
        """Return the oldest entry's last-use time, or None when empty.
        OrderedDict keeps the head lookup O(1)."""
        with self._lock:
            for key in self._entries:
                return self._stamps.get(key, 0.0)
            return None

    def budget_evict_oldest(self, want_bytes: int, min_age_s: float, floor_bytes: int) -> int:
        """Evict one LRU head, or return 0 at the floor, when empty, or when too young.
        One entry preserves global order; want_bytes is only a batching hint."""
        with self._lock:
            if self._total_bytes <= floor_bytes:
                return 0
            head = next(iter(self._entries), None)
            if head is None:
                return 0
            if (time.monotonic() - self._stamps.get(head, 0.0)) < min_age_s:
                return 0
            freed = self._pop_oldest_locked()
            self._note_evicted_locked(head)
            return freed

    def budget_take_thrash_count(self) -> int:
        """Take the budget-evicted key re-admission count since the last call.
        The budget daemon reports it because put must not perform I/O."""
        with self._lock:
            hits = self._thrash_hits
            self._thrash_hits = 0
            return hits
