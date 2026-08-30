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
import math
import os

from loguru import logger as log

from src.backend.DeckManagement.Subclasses.byte_lru_cache import ByteLRUCache

DEFAULT_MAX_MB = 64


def native_tile_cache_max_bytes() -> int:
    """Read the per-deck native tile byte cap from DECKARD_NATIVE_TILE_CACHE_MB.
    Zero disables identity caching; malformed values use the default."""
    raw = os.environ.get("DECKARD_NATIVE_TILE_CACHE_MB")
    if raw is None:
        return DEFAULT_MAX_MB * 1024 * 1024
    try:
        mb = float(raw)
        # Reject NaN and infinity before sign and int conversion.
        # This parser must not abort deck initialization.
        usable = math.isfinite(mb)
    except ValueError:
        # Bind mb on the exception path before the common validity branch.
        mb = 0.0
        usable = False
    if not usable:
        log.warning(
            f"Ignoring malformed DECKARD_NATIVE_TILE_CACHE_MB={raw!r}; "
            f"using the default {DEFAULT_MAX_MB}"
        )
        return DEFAULT_MAX_MB * 1024 * 1024
    if mb < 0:
        return 0
    return int(mb * 1024 * 1024)


class NativeTileCache(ByteLRUCache):
    """Cache native background tiles by finite frame identity with first-sighting admission.
    Separate identity keys from pixel hashes to prevent collisions and independent sizing."""
