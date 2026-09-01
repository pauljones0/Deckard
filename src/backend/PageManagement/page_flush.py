"""
Disk readers must flush first: get_page_data, asset sweeps, exports, duplicates, page moves,
boot backup zip, and video cache. Replacing importers discard; all calls are thread-safe.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Protocol

from loguru import logger as log

# Keep Page's import closure free of widgets and GI. The process wheel works
# without a main loop and keeps fsync off the GTK thread, unlike a GLib timeout.
from src.backend import timer_wheel
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.atomic_json import atomic_write_json


class PageFlushSource(Protocol):
    """A Page or PageDocument that holds a page's unwritten shared content.
    Off-deck edits mark the document because no Page exists; the record selects the serializer."""

    json_path: str

    def snapshot_for_save(self) -> dict[str, Any]:
        """Give the content as it goes into the file, without live objects."""
        ...

    def move_key_to_end(self, dictionary: dict[str, Any], key: str) -> None:
        """Re-file one top-level key last in the caller's snapshot."""
        ...

    def make_backup(self, json_path: str) -> None:
        """Copy json_path into pages/backups/ before the write overwrites it."""
        ...


# One second absorbs a typed label before synchronous actions flush. Five seconds
# bounds deferral before a write attempt; failed writes can remain dirty past it.
DEBOUNCE_S = 1.0
MAX_DIRTY_AGE_S = 5.0
# Retry transient failures without spinning on a stuck filesystem; a short outage
# recovers within a few debounce windows, and quit flush provides the final retry.
RETRY_S = 2.0


def canonical_path(path: str) -> str:
    """Return the canonical key used by document, lock, pending, and backup registries.
    Resolving aliases prevents separate locks and invisible pending edits for one file."""
    # Under a normal data directory, resolution costs about 8 us against a
    # guarded write's 150 us; keep the canonicalization rule here.
    return os.path.realpath(path)


# Share one lock across all Page objects for a file so controllers cannot save
# concurrently; the user's page count bounds this permanent registry.
_save_locks: dict[str, threading.Lock] = {}
_save_locks_guard = threading.Lock()


def save_lock(path: str) -> threading.Lock:
    """Return the per-file lock below page-load/document-load locks and outside cache locks.
    It may nest I/O, _pending_guard, or timer locks; external timer callbacks must not take it."""
    # Canonicalize here because trusting raw caller spelling would silently give
    # one file two locks; the page cache lock is never held across this lock.
    with _save_locks_guard:
        return _save_locks.setdefault(canonical_path(path), threading.Lock())


class CancelHandle(Protocol):
    """What a scheduler hands back: enough to disarm the timer."""

    def cancel(self) -> None: ...


class Scheduler(Protocol):
    """The two operations the deferral needs from a timer source."""

    def schedule(self, delay_s: float, callback: Callable[[], None]) -> CancelHandle:
        """Arm a one-shot timer and return a handle for cancel()."""
        ...

    def cancel(self, handle: CancelHandle) -> None:
        """Disarm a timer that has not fired yet. Idempotent after a fire."""
        ...


class TimerWheelScheduler:
    """Schedule each fire on a separate short-lived timer-wheel daemon thread.
    Slow writes do not delay timers; quit must flush because os._exit stops daemon threads."""

    def schedule(self, delay_s: float, callback: Callable[[], None]) -> timer_wheel.TimerHandle:
        return timer_wheel.schedule(delay_s, callback, name="page_flush")

    def cancel(self, handle: CancelHandle) -> None:
        handle.cancel()


class _Pending:
    """One file's source, marked spelling, first-mark time, and write timer."""

    # Replacement lets a flush distinguish edits marked during its write. Keep
    # raw path for backup naming because the canonical key and moved json_path differ.
    __slots__ = ("source", "path", "first_marked", "handle")

    def __init__(self, source: PageFlushSource, path: str, first_marked: float) -> None:
        self.source = source
        self.path = path
        self.first_marked = first_marked
        self.handle: "CancelHandle | None" = None


class PageFlush:
    """Track pages ahead of disk and schedule when each file catches up.
    Injected scheduler and clock let headless tests assert timing in virtual time without sleep."""

    def __init__(self, scheduler: "Scheduler | None" = None,
                 clock: "Callable[[], float] | None" = None) -> None:
        # Keep pending sources strong so cache eviction cannot drop unwritten edits;
        # the next Page reads the resulting file through the barrier.
        self._pending: dict[str, _Pending] = {}
        # Back up once per path per session; only discard removes the record when
        # another writer takes the file, and the user's page count bounds the set.
        self._backed_up_paths: set[str] = set()
        # Take this guard alone, or after a save lock when both are needed. Never
        # acquire a save lock under it; file writes and backup copies stay outside it.
        self._pending_guard = threading.Lock()
        self._scheduler: Scheduler = scheduler if scheduler is not None else TimerWheelScheduler()
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic

    def mark_dirty(self, source: PageFlushSource) -> None:
        """Record the latest shared source and re-arm at DEBOUNCE_S without file I/O.
        MAX_DIRTY_AGE_S bounds deferral before an attempt; failed writes can exceed it."""
        # One entry per file is sufficient because every Page and its document
        # share one dictionary; a clean completed write starts the next age window.
        with self._pending_guard:
            # Read json_path under the map guard so a move's repoint-then-discard
            # order cannot let an old-path mark land after the discard.
            path = source.json_path
            key = canonical_path(path)
            now = self._clock()
            previous = self._pending.get(key)
            entry = _Pending(source, path,
                             previous.first_marked if previous is not None else now)
            self._pending[key] = entry
            if previous is not None and previous.handle is not None:
                self._scheduler.cancel(previous.handle)
            # Use a trailing delay only until the age cap; at the deadline, clamp
            # to zero instead of extending the write by another keystroke.
            deadline = entry.first_marked + MAX_DIRTY_AGE_S
            delay = min(DEBOUNCE_S, max(0.0, deadline - now))
            entry.handle = self._scheduler.schedule(delay, lambda: self._fire(key))

    def pending_source(self, path: str) -> "PageFlushSource | None":
        """Return the pending source for external ordering assertions, or None."""
        with self._pending_guard:
            entry = self._pending.get(canonical_path(path))
            return entry.source if entry is not None else None

    def _fire(self, key: str) -> None:
        """Timer dispatch. It logs a failure, because it has no caller."""
        # Timer-thread fsync keeps GTK responsive. Without write failures, crashes
        # lose at most DEBOUNCE_S or MAX_DIRTY_AGE_S of pending edits.
        try:
            self.flush_path(key)
        except Exception:
            log.opt(exception=True).error(f"Deferred write of page {key} failed")

    def flush_path(self, path: str) -> None:
        """Write pending edits now; retain OSError failures and retire all other failures.
        No-pending and failure paths return for read barriers; retained edits retry later."""
        # Write before retiring under the marked path's save lock so readers never
        # observe an unmarked stale file and moves cannot redirect an old-path write.
        key = canonical_path(path)
        # This atomic unguarded test may miss only a later mark the caller need not
        # await; every later decision reads _pending under its guard.
        if key not in self._pending:
            return

        with save_lock(key):
            with self._pending_guard:
                entry = self._pending.get(key)
            if entry is None:
                # Another thread's flush wrote it while this call waited on
                # the lock above.
                return
            source = entry.source

            try:
                self._back_up_once(key, entry.path, source)

                without_objects = source.snapshot_for_save()
                for type in Input.KeyTypes:
                    source.move_key_to_end(without_objects, type)
                # Atomic replace, so an interrupted write leaves no truncated
                # page.
                atomic_write_json(entry.path, without_objects)
            except OSError as e:
                # Full disks, read-only mounts, and lost shares leave bytes unwritten;
                # retain the source for the retry timer or the final quit flush.
                self._retain_for_retry(key, entry, e)
                return
            except Exception:
                # Do not retry non-OSError failures; log and retire them so read
                # barriers do not fail repeatedly.
                log.opt(exception=True).error(
                    f"Discarding an unserializable pending edit of page {key}")
                self._retire(key, entry)
                return
            self._retire(key, entry)

    def _retire(self, key: str, entry: "_Pending") -> None:
        """Drop only the written or unrecoverable entry and cancel its timer.
        A replacement marked during the write stays pending with its timer, ahead of these bytes."""
        with self._pending_guard:
            if self._pending.get(key) is entry:
                del self._pending[key]
                if entry.handle is not None:
                    self._scheduler.cancel(entry.handle)

    def _retain_for_retry(self, key: str, entry: "_Pending", error: OSError) -> None:
        """Re-arm a transiently failed entry only if no newer save replaced it.
        A superseding entry owns its own retry."""
        with self._pending_guard:
            if self._pending.get(key) is not entry:
                return
            log.warning(
                f"Deferred write of page {key} failed transiently ({error}); "
                f"keeping the edit and retrying in {RETRY_S}s")
            if entry.handle is not None:
                self._scheduler.cancel(entry.handle)
            entry.handle = self._scheduler.schedule(RETRY_S, lambda: self._fire(key))

    def _back_up_once(self, key: str, path: str, source: PageFlushSource) -> None:
        """Back up the locked path once per session before overwriting it.
        The corrupt-primary heal copy uses path because moves can repoint source.json_path."""
        # Atomic writes cannot corrupt the primary, so one pre-write copy serves external
        # corruption. Missing or invalid files count as done; only a raised I/O error retries.
        with self._pending_guard:
            if key in self._backed_up_paths:
                return
        # Copy outside the registry guard so GTK marks do not wait on file I/O;
        # the held save lock prevents two copies for this path.
        source.make_backup(path)
        with self._pending_guard:
            self._backed_up_paths.add(key)

    def flush_all(self) -> None:
        """Flush every pending path for whole-tree readers and process exit.
        Log failures per path instead of raising; timer daemon threads cannot be trusted at exit."""
        with self._pending_guard:
            paths = list(self._pending)
        for path in paths:
            try:
                self.flush_path(path)
            except Exception:
                log.opt(exception=True).error(f"Could not write pending edits of page {path}")

    def discard_path(self, path: str) -> None:
        """Discard pending edits and the backup record before deletion, move, or import.
        Hold the save lock so an active flush cannot restore old state over replacement content."""
        # The next flush must back up the new file; retaining the old heal source
        # could restore a deleted or moved page over its replacement.
        key = canonical_path(path)
        with save_lock(key):
            with self._pending_guard:
                entry = self._pending.pop(key, None)
                self._backed_up_paths.discard(key)
        if entry is not None and entry.handle is not None:
            self._scheduler.cancel(entry.handle)


# The process-wide flush seam. A module singleton and not a gl slot, because
# this protocol exists to shrink the shared namespace.
_flush = PageFlush()


def get() -> PageFlush:
    """Give the process-wide page flush. Never None."""
    return _flush
