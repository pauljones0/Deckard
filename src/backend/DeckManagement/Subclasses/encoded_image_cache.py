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
from collections import deque

from src.backend.DeckManagement.Subclasses.byte_lru_cache import ByteLRUCache
from typing import Any, override


class EncodedImageCache(ByteLRUCache):
    """Byte-capped LRU of device-native images with repeat-based admission.
    The doorkeeper infers reuse because callers only have composed pixels."""

    # The 512-entry ring bounds keys, not pixel bytes, and retains a full content loop.
    # Repeated loop keys therefore remain eligible for admission.
    DOORKEEPER_SIZE = 512

    def __init__(self, max_bytes: int):
        super().__init__(max_bytes)
        # Pair an O(1) membership set with a bounded FIFO eviction order.
        self._doorkeeper_seen: set[Any] = set()
        self._doorkeeper_order: "deque[Any]" = deque()

    @override
    def _admit(self, key: object) -> bool:
        """Admit a key on its second sighting while the caller holds the lock.
        Looping keys warm; one-off high-entropy keys consume only bounded bookkeeping."""
        if key in self._doorkeeper_seen:
            return True
        self._doorkeeper_seen.add(key)
        self._doorkeeper_order.append(key)
        if len(self._doorkeeper_order) > self.DOORKEEPER_SIZE:
            oldest = self._doorkeeper_order.popleft()
            self._doorkeeper_seen.discard(oldest)
        return False

    @override
    def _on_clear_locked(self) -> None:
        """Reset the doorkeeper so old content cannot admit coincident new keys."""
        self._doorkeeper_seen.clear()
        self._doorkeeper_order.clear()
