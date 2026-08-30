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

Process-wide budget for the native-image caches.

Each deck's encode_memo and native_tile_cache carries its own byte cap, but
nothing caps their sum. Total image-cache RAM then scales with deck count, and
eviction stays per-silo, so a cold deck's full memo yields no byte to a hot one
however hard the hot one thrashes. This module adds the aggregate, a ceiling
over the sum of the evictable caches and a cross-cache LRU that sheds from
whichever cache owns the globally-oldest entry.

The overshoot bound
    Enforcement is deferred, so the ceiling does not bound the sum at every
    instant. The real bound is

        ceiling + (put rate x wake latency)

    Painters keep putting between the put that crosses the ceiling and the
    scheduling of the daemon. A painter can complete a whole tick of up to
    key-count puts in that window. Native key JPEGs run about 5-20 KiB, and
    the growing put fires the notify itself, so the window is one scheduler
    latency plus the damping interval and not the 60 s periodic pass. That is
    KBs to low MBs in practice. Once puts stop, the steady state settles at or
    below the ceiling. Strict synchronous enforcement is rejected, because it
    puts cross-cache work on the sole device writer's thread.

When it does nothing
    A ceiling of 0 disables global eviction (see ceiling_bytes), a malformed
    ceiling degrades to the default (see ceiling_bytes), and degenerate
    pressure stops the pass (see _warn_degenerate). In all three the sum of
    the local caps still bounds the total.
"""
import math
import os
import threading
import time
from weakref import WeakSet

from loguru import logger as log
from typing import Protocol, cast


class BudgetSource(Protocol):
    """What a caller hands to register(): anything that can say its size."""

    def budget_bytes(self) -> int: ...


class BudgetParticipant(Protocol):
    """The registered fields and methods read by the budget sweep.
    Accounting-only participants implement budget_bytes; eviction methods remain gated."""

    budget_label: str
    budget_evictable: bool
    budget_min_age_s: float
    budget_floor_bytes: int

    def budget_bytes(self) -> int: ...
    def budget_head_ts(self) -> "float | None": ...
    def budget_evict_oldest(self, want_bytes: int, min_age_s: float, floor_bytes: int) -> int: ...

ENV_CEILING = "DECKARD_IMAGE_CACHE_MB"

# Default to MemTotal/64 between 64 and 256 MiB; single-deck local caps total 96 MiB.
# Telemetry, eviction counters, and ENV_CEILING support field tuning.
DEFAULT_CEILING_MB = 256
MIN_DEFAULT_CEILING_MB = 64
MEM_TOTAL_DIVISOR = 64

# Per-registrant defaults, overridable at register().
DEFAULT_MIN_AGE_S = 2.0
DEFAULT_FLOOR_BYTES = 4 * 1024 * 1024
# Cap retuned min-age and use this value until loop duration is known.
# Over-protection retains stale entries; under-protection causes re-encoding.
MAX_MIN_AGE_S = 30.0

# Evict down to this fraction of the ceiling, so a hot loop that keeps
# crossing the line doesn't get one eviction pass per put.
TARGET_FRACTION = 0.95

# The periodic pass self-heals drift, e.g. a cache that shrank or a registrant
# that died. It is also the beat the census and telemetry reads ride on.
WAKE_INTERVAL_S = 60.0
# Damp wakes because eviction hysteresis does not limit notification churn.
# Without damping, warm-up scans every registrant at paint rate.
MIN_WAKE_INTERVAL_S = 0.05
# Back off when all entries are too young or all caches are at their floors.
# Notifications can continue at paint rate before entries become evictable.
DEGENERATE_BACKOFF_S = 5.0
# A put must grow its own cache by this much before it is worth a wake.
NOTIFY_WATERMARK_BYTES = 1024 * 1024

# Cap per-pass head scans to bound lock contention with painters.
# Larger deficits continue after MIN_WAKE_INTERVAL_S; common page changes fit one pass.
MAX_PICKS_PER_PASS = 2000

# Re-read the live sum periodically because concurrent clear() can make subtraction stale.
# Each recheck takes one budget_bytes lock per registrant.
RECHECK_EVERY_PICKS = 64

LOG_INTERVAL_S = 5.0

_lock = threading.Lock()
# Hold registrants weakly and keep labels on them to avoid hidden strong references.
# Snapshot under the lock because WeakSet does not tolerate concurrent additions.
_registry: "WeakSet[BudgetParticipant]" = WeakSet()

_wake = threading.Event()
_thread_started = False

_evictions = 0
_evicted_bytes = 0
_degenerate_passes = 0

_default_ceiling_cache: int | None = None
_warned_ceiling_values: set[str] = set()


def _mem_total_bytes() -> int | None:
    """Return MemTotal from /proc/meminfo, or None when unavailable."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def default_ceiling_bytes() -> int:
    """Return the cached RAM-derived default because MemTotal does not change."""
    global _default_ceiling_cache
    if _default_ceiling_cache is None:
        total = _mem_total_bytes()
        if not total:
            _default_ceiling_cache = DEFAULT_CEILING_MB * 1024 * 1024
        else:
            _default_ceiling_cache = max(
                MIN_DEFAULT_CEILING_MB * 1024 * 1024,
                min(DEFAULT_CEILING_MB * 1024 * 1024, total // MEM_TOTAL_DIVISOR),
            )
    return _default_ceiling_cache


def ceiling_bytes() -> int:
    """Read the process ceiling for evictable caches from DECKARD_IMAGE_CACHE_MB.
    A nonpositive value disables global eviction but leaves local caps active."""
    # Re-read the environment so the value can change within one process.
    raw = os.environ.get(ENV_CEILING)
    if raw is None:
        return default_ceiling_bytes()
    try:
        mb = float(raw)
        # Reject NaN and infinity before int conversion.
        # Invalid values must not disable enforcement or abort deck initialization.
        usable = math.isfinite(mb)
    except ValueError:
        # Bind mb on the exception path before the common validity branch.
        mb = 0.0
        usable = False
    if not usable:
        # Warn once per distinct value, because every pass reads this.
        if raw not in _warned_ceiling_values:
            _warned_ceiling_values.add(raw)
            log.warning(
                f"Ignoring malformed {ENV_CEILING}={raw!r}; using the default "
                f"{default_ceiling_bytes() // (1024 * 1024)} MiB"
            )
        return default_ceiling_bytes()
    if mb <= 0:
        return 0
    return int(mb * 1024 * 1024)


def register(cache: BudgetSource, *, label: str, evictable: bool = True,
             min_age_s: float = DEFAULT_MIN_AGE_S,
             floor_bytes: int = DEFAULT_FLOOR_BYTES) -> None:
    """Idempotently register a cache as evictable or accounting-only.
    Labels use group:instance; evictable caches implement head and eviction methods."""
    try:
        # Stamped fields complete the participant hand-off; eviction methods stay gated.
        participant = cast(BudgetParticipant, cache)
        participant.budget_label = label
        participant.budget_evictable = bool(evictable)
        participant.budget_min_age_s = float(min_age_s)
        participant.budget_floor_bytes = int(floor_bytes)
        with _lock:
            _registry.add(participant)
        _ensure_thread()
    except Exception as e:
        # Registration is housekeeping and must not abort deck initialization.
        log.warning(f"cache-budget: could not register {label!r}: {e}")


def unregister(cache: BudgetSource) -> None:
    """Remove a cache immediately instead of waiting for weak-reference collection.
    The call is optional because collected or cleared caches stop contributing."""
    try:
        with _lock:
            _registry.discard(cast(BudgetParticipant, cache))
    except Exception as e:
        log.warning(f"cache-budget: could not unregister: {e}")


def set_min_age(cache: BudgetParticipant, min_age_s: float) -> None:
    """Retune min-age protection to the active content-loop duration.
    Per-frame entries are touched once per loop, so a shorter age causes repeat encoding."""
    try:
        cache.budget_min_age_s = float(min_age_s)
    except Exception as e:
        log.warning(f"cache-budget: could not set min_age: {e}")


def notify_grew() -> None:
    """A cache calls this after it releases its own lock and after it grows
    past its notify watermark."""
    # Painter threads only set this event after releasing their cache lock.
    # Global locking, cross-cache scans, and cross-deck work stay off the paint path.
    _wake.set()


def _snapshot() -> list[BudgetParticipant]:
    with _lock:
        return list(_registry)


def totals() -> dict[str, int]:
    """Map each label group to its bytes summed across instances."""
    out: dict[str, int] = {}
    for cache in _snapshot():
        group = str(getattr(cache, "budget_label", "?")).split(":", 1)[0]
        try:
            out[group] = out.get(group, 0) + int(cache.budget_bytes())
        except Exception:
            continue
    return out


def evictable_bytes() -> int:
    """Total bytes over the evictable registrants. The ceiling governs this
    quantity."""
    total = 0
    for cache in _snapshot():
        if not getattr(cache, "budget_evictable", False):
            continue
        try:
            total += int(cache.budget_bytes())
        except Exception:
            continue
    return total


def eviction_stats() -> tuple[int, int]:
    """Return process-lifetime cumulative eviction count and bytes.
    Lock the pair because concurrent enforcement passes can interleave updates."""
    with _lock:
        return _evictions, _evicted_bytes


def degenerate_pass_count() -> int:
    """Return the process-lifetime count of pressured passes with no eviction.
    Count rate-limited warnings so throttling does not hide degenerate passes."""
    with _lock:
        return _degenerate_passes


def _ensure_thread() -> None:
    """Start one process budget daemon under a lock.
    Release the latch after start failure so the next registrant can retry."""
    global _thread_started
    if _thread_started:
        return
    with _lock:
        # Re-read under the lock because another thread can set the module flag.
        started: bool = _thread_started
        if started:
            return
        _thread_started = True
    try:
        threading.Thread(target=_budget_loop, name="cache_budget", daemon=True).start()
    except Exception as e:
        with _lock:
            _thread_started = False
        log.warning(f"cache-budget: could not start the budget thread: {e}")


def _budget_loop() -> None:
    # A daemon thread, abandoned at quit like every other housekeeping thread.
    # app.py joins non-daemon threads only, so there is no shutdown ceremony.
    next_allowed = 0.0
    while True:
        _wake.wait(timeout=WAKE_INTERVAL_S)
        # Damp before the clear, so the pass about to run absorbs the
        # notifications that arrive during the damping sleep.
        delay = next_allowed - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _wake.clear()
        try:
            degenerate = _drain_once()
        except Exception as e:
            # Contain each pass failure because the daemon does not respawn.
            log.warning(f"cache-budget: pass failed: {e}")
            degenerate = True
        next_allowed = time.monotonic() + (
            DEGENERATE_BACKOFF_S if degenerate else MIN_WAKE_INTERVAL_S
        )


_last_log_ts = 0.0
_last_degenerate_warn_ts = 0.0


def _drain_once() -> bool:
    """Run one thread-independent enforcement pass.
    Return True when nothing was evictable so the caller uses longer backoff."""
    ceiling = ceiling_bytes()
    caches = [c for c in _snapshot() if getattr(c, "budget_evictable", False)]
    _report_thrash(caches)
    # A ceiling of 0 disables global eviction. Every cache's own cap still
    # applies, so the sum of the local caps still bounds the total.
    if ceiling <= 0 or not caches:
        return True

    total = 0
    for cache in caches:
        total += int(cache.budget_bytes())
    if total <= ceiling:
        return False

    before = total
    target = int(ceiling * TARGET_FRACTION)
    # Keep total floors at or below half the ceiling.
    # Higher floors can make multi-deck or small-ceiling budgets inert.
    floor_cap = max(0, ceiling // (2 * len(caches)))

    # Skip exact cache instances that are empty, at floor, or entirely too young.
    # Strong snapshot references stabilize ids; MAX_PICKS_PER_PASS guarantees termination.
    skip: set[int] = set()
    freed = 0
    evicted = 0
    picks = 0
    while total > target:
        if picks >= MAX_PICKS_PER_PASS:
            # Re-arm after a capped burst so work continues after the damping interval.
            # Report progress so the caller does not use degenerate backoff.
            _wake.set()
            break
        picks += 1
        pick = None
        pick_ts = None
        for cache in caches:
            if id(cache) in skip:
                continue
            head_ts = cache.budget_head_ts()
            if head_ts is None:
                skip.add(id(cache))
                continue
            if pick_ts is None or head_ts < pick_ts:
                pick, pick_ts = cache, head_ts
        if pick is None:
            break
        floor = min(int(getattr(pick, "budget_floor_bytes", DEFAULT_FLOOR_BYTES)), floor_cap)
        # Immutable bytes remain alive while a paint holds a reference.
        # Eviction can cause re-encoding but cannot produce a wrong or torn frame.
        got = pick.budget_evict_oldest(
            total - target,
            float(getattr(pick, "budget_min_age_s", DEFAULT_MIN_AGE_S)),
            floor,
        )
        if got <= 0:
            # The head is too young, or the cache is at its floor. Take
            # nothing more from it this pass.
            skip.add(id(pick))
            continue
        total -= got
        freed += got
        evicted += 1
        if picks % RECHECK_EVERY_PICKS == 0:
            # Re-anchor because concurrent clear() can make the running total stale-high.
            # Otherwise a long pass can evict other decks unnecessarily.
            total = evictable_bytes()

    global _evictions, _evicted_bytes
    with _lock:
        _evictions += evicted
        _evicted_bytes += freed

    if evicted:
        _log_evictions(evicted, freed, before, total, ceiling)
        return False

    _warn_degenerate(before, ceiling)
    return True


def _log_evictions(evicted: int, freed: int, before: int, after: int, ceiling: int) -> None:
    global _last_log_ts
    now = time.monotonic()
    if now - _last_log_ts < LOG_INTERVAL_S:
        return
    _last_log_ts = now
    mb = 1024 * 1024
    log.info(
        f"cache-budget: evicted {evicted} entries / {freed / mb:.1f} MiB "
        f"(sum {before / mb:.1f}->{after / mb:.1f} of {ceiling / mb:.1f} MiB)"
    )


def _warn_degenerate(total: int, ceiling: int) -> None:
    """Stop and warn when every pressured cache is at floor or too young.
    Local caps still bound total use; forced eviction would only re-encode live frames."""
    # Count before rate limiting so suppressed logs do not hide degenerate passes.
    global _degenerate_passes
    with _lock:
        _degenerate_passes += 1

    global _last_degenerate_warn_ts
    now = time.monotonic()
    if now - _last_degenerate_warn_ts < WAKE_INTERVAL_S:
        return
    _last_degenerate_warn_ts = now
    mb = 1024 * 1024
    log.warning(
        f"cache-budget: {total / mb:.1f} MiB of image caches over a {ceiling / mb:.1f} MiB "
        f"ceiling, but nothing is evictable (every cache at its floor or younger "
        f"than its min-age). The live working set is larger than the ceiling; "
        f"raise {ENV_CEILING} or reduce the number of decks/pages in play."
    )


def _report_thrash(caches: list[BudgetParticipant]) -> None:
    """Report keys re-admitted after eviction as live-working-set thrash.
    Caches count without I/O on put; this reporter performs the warning."""
    for cache in caches:
        take = getattr(cache, "budget_take_thrash_count", None)
        if take is None:
            continue
        try:
            hits = take()
        except Exception:
            continue
        if hits:
            log.warning(
                f"cache-budget: {getattr(cache, 'budget_label', '?')} re-admitted "
                f"{hits} key(s) shortly after the budget evicted them -- the ceiling "
                f"is thrashing against a live working set (raise {ENV_CEILING})"
            )
