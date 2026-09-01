import atexit
import contextlib
import json
import os
import tempfile
import threading
import time
import weakref
from loguru import logger as log

import globals as gl
from src.backend.atomic_json import atomic_write_json
from src.backend.Store.StoreURL import parse_repo_url
from collections.abc import Callable
from typing import Any, IO, Literal, TYPE_CHECKING, cast, overload

if TYPE_CHECKING:
    from types import TracebackType


# Hold live caches weakly so the exit hook drains deferred writes without extending lifetimes.
_live_caches: "weakref.WeakSet[StoreCache]" = weakref.WeakSet()


@atexit.register
def _flush_live_caches() -> None:
    """Flush deferred index writes for every live cache at interpreter exit.
    The GTK quit path must flush explicitly because os._exit skips atexit."""
    for cache in list(_live_caches):
        try:
            cache.flush_index()
        except Exception as e:
            log.warning(f"Could not flush the store cache index at exit: {e}")


class _AtomicCacheWriter:
    """Atomically replace one cache file and stamp its index only after commit.
    Hold the per-file lock until close or abort so writers for one key serialize."""

    def __init__(self, final_path: str, mode: str, lock: threading.Lock,
                 on_committed: Callable[[], None]) -> None:
        self._final_path = final_path
        self._lock = lock
        self._on_committed = on_committed
        self._finished = False
        fd, self._tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(final_path),
            prefix=os.path.basename(final_path) + ".",
            suffix=".tmp",
        )
        try:
            self._file = os.fdopen(fd, mode)
        except Exception:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.remove(self._tmp_path)
            raise

    def write(self, data: "str | bytes") -> int:
        # The selected write mode narrows the file's str or bytes half at runtime.
        return self._file.write(data)

    def __enter__(self) -> "_AtomicCacheWriter":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: "TracebackType | None") -> Literal[False]:
        if exc_type is not None:
            self.abort()
        else:
            self.close()
        return False

    def abort(self) -> None:
        """Discard the pending write. The previous cache content survives."""
        if self._finished:
            return
        self._finished = True
        try:
            self._file.close()
        finally:
            with contextlib.suppress(OSError):
                os.remove(self._tmp_path)
            self._lock.release()

    def close(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            os.replace(self._tmp_path, self._final_path)
            # Stamp only after the complete content reaches its final path.
            self._on_committed()
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(self._tmp_path)
            raise
        finally:
            self._lock.release()

    def __del__(self) -> None:
        # Discard a handle that was dropped without close or abort.
        if not getattr(self, "_finished", True):
            with contextlib.suppress(Exception):
                self.abort()


class StoreCache:
    """Index downloaded blobs with separate last-use and content-age clocks.
    Commit and eviction writes are synchronous; read-clock writes are locked and debounced."""

    DAYS_TO_KEEP = 3

    # Allow tests to shorten the trailing debounce for read-clock writes.
    FLUSH_DEBOUNCE_S = 2.0

    def __init__(self) -> None:
        self.CACHE_PATH = os.path.join(gl.DATA_PATH, "Store" , "cache")

        self.files_json = os.path.join(self.CACHE_PATH, "files.json")
        self.files_dir = os.path.join(self.CACHE_PATH, "files")

        self.write_lock = threading.Lock()

        # Serialize writers per cache key; readers see either complete file after os.replace.
        # Keep locks for the session because eviction could remove a lock still held by a writer.
        self._file_locks: dict[str, threading.Lock] = {}
        self._file_locks_guard = threading.Lock()

        # Initialize write_lock-protected debounce state before set_files can run.
        self._index_dirty = False
        self._flush_timer: threading.Timer | None = None
        _live_caches.add(self)

        self.files = self.get_files()
        self.remove_old_cache_files()

        self.create_cache_dirs()
        self.create_cache_files()

    def get_files(self) -> dict[str, Any]:
        if not os.path.exists(self.files_json):
            return {}
        try:
            with open(self.files_json, "r") as f:
                root = json.load(f)
            # Treat a non-object JSON root as an unusable index.
            if not isinstance(root, dict):
                log.error(f"Cache index {self.files_json} does not hold a JSON object; reading it as empty")
                return {}
            return cast(dict[str, Any], root)
        except json.decoder.JSONDecodeError as e:
            log.error(e)
            return {}

    def set_files(self, files: dict[str, Any]) -> None:
        """Persist an index immediately.
        Only writing the live index clears deferred renewal state."""
        with self.write_lock:
            self._write_index_locked(files)

    def _write_index_locked(self, files: dict[str, Any] | None = None) -> None:
        """Write an index while the caller holds write_lock.
        Clear dirty state only after the live index writes successfully."""
        if files is None:
            files = self.files
        atomic_write_json(self.files_json, files.copy())
        if files is self.files:
            self._index_dirty = False

    def _mark_index_dirty_locked(self) -> None:
        """Mark a locked read-clock change and arm one trailing flush."""
        self._index_dirty = True
        if self._flush_timer is not None:
            return  # A flush is already pending and covers this mark.
        timer = threading.Timer(self.FLUSH_DEBOUNCE_S, self.flush_index)
        timer.name = "store-cache-index-flush"
        timer.daemon = True
        try:
            timer.start()
        except Exception as e:
            # Leave the timer slot empty so a later mark can retry after thread exhaustion.
            log.warning(f"Could not arm the store cache index flush: {e}")
            return
        # Publish only a started timer while write_lock prevents an early flush race.
        self._flush_timer = timer

    def flush_index(self) -> None:
        """Write deferred read-clock renewals when the index is dirty."""
        with self.write_lock:
            timer, self._flush_timer = self._flush_timer, None
            if self._index_dirty:
                self._write_index_locked()
        if timer is not None:
            # Cancel a pending wake-up after an explicit flush.
            timer.cancel()

    def remove_old_cache_files(self) -> None:
        # Called only from __init__, before other threads can require write_lock.
        now = time.time()
        for string in self.files.copy():
            entry = self.files[string]
            path = entry.get("path")
            if not path or not os.path.exists(path):
                # Remove index entries with no existing blob.
                self.files.pop(string)
                continue
            # For legacy entries, fall back from last-use to fetched time and file mtime.
            date = entry.get("date")
            if date is None:
                date = entry.get("fetched")
            if date is None:
                try:
                    date = os.path.getmtime(path)
                except OSError:
                    date = None

            if date is None or now - date > self.DAYS_TO_KEEP * 24 * 60 * 60:
                try:
                    os.remove(path)
                except OSError as e:
                    # Keep the index entry so a later pass can retry deletion.
                    log.warning(f"Could not remove old cache file {path}: {e}")
                    continue
                self.files.pop(string)

        self.set_files(self.files)

    def create_cache_dirs(self) -> None:
        os.makedirs(self.CACHE_PATH, exist_ok=True)

    def create_cache_files(self) -> None:
        files = [self.files_json]

        for file in files:
            if not os.path.exists(file):
                atomic_write_json(file, {})

    def get_user_name(self, repo_url:str) -> str:
        ref = parse_repo_url(repo_url)
        if ref is None:
            # Cache callers must supply a URL already accepted by parse_repo_url.
            raise ValueError(f"Not a store repository url: {repo_url!r}")
        return ref.user

    def get_repo_name(self, repo_url:str) -> str | None:
        ref = parse_repo_url(repo_url)
        return None if ref is None else ref.repo

    def generate_cache_string(self, url: str, path: str, branch: "str | None" = "main", data_type: str = "text") -> str:
        # Keep None as a distinct key for the corresponding invalid fetch URL.
        user = self.get_user_name(url)
        repo = self.get_repo_name(url)
        return f"{user}::{repo}::{branch}::{data_type}::{path}"

    def get_cache_path(self, url: str, path: str, branch: "str | None" = "main", data_type: str = "text") -> str:
        # return os.path.join(self.files_dir, self.generate_cache_string(url, path, branch, data_type))

        cache_string = self.generate_cache_string(url, path, branch, data_type)
        if cache_string in self.files:
            recorded = self.files[cache_string].get("path")
            # Rebuild a missing recorded path instead of returning None to filesystem calls.
            if recorded is not None:
                return cast(str, recorded)

        path = os.path.join(self.files_dir, cache_string)
        # Record a new blob path and defer its last-use clock; commit stamps content age.
        with self.write_lock:
            self.files[cache_string] = {
                "path": path,
                "date": time.time()
            }
            self._mark_index_dirty_locked()
        return path

    def is_cached(self, url: str, path: str, branch: "str | None" = "main", data_type: str = "text") -> bool:
        cache_string = self.generate_cache_string(url, path, branch, data_type)
        if cache_string not in self.files:
            return False

        if self.files[cache_string].get("path") is None:
            return False

        return os.path.exists(self.files[cache_string].get("path"))

    def _get_file_lock(self, cache_string: str) -> threading.Lock:
        with self._file_locks_guard:
            return self._file_locks.setdefault(cache_string, threading.Lock())

    def _stamp_committed(self, cache_string: str, cache_path: str) -> None:
        """Update the index after atomic replacement lands complete content."""
        with self.write_lock:
            entry = self.files.get(cache_string, {})
            entry["path"] = cache_path
            entry["date"] = time.time()     # last use (eviction clock)
            entry["fetched"] = time.time()  # content age (staleness clock)
            self.files[cache_string] = entry
            # Persist a committed blob record synchronously under the mutation lock.
            self._write_index_locked()

    @overload
    def open_cache_file(self, url: str, path: str, branch: "str | None" = ..., data_type: str = ...,
                        mode: Literal["r"] = ...) -> IO[str]: ...

    @overload
    def open_cache_file(self, url: str, path: str, branch: "str | None" = ..., data_type: str = ...,
                        mode: Literal["rb"] = ...) -> IO[bytes]: ...

    @overload
    def open_cache_file(self, url: str, path: str, branch: "str | None" = ..., data_type: str = ...,
                        mode: Literal["r", "rb"] = ...) -> "IO[str] | IO[bytes]": ...

    @overload
    def open_cache_file(self, url: str, path: str, branch: "str | None" = ..., data_type: str = ...,
                        mode: Literal["w", "wb"] = ...) -> "_AtomicCacheWriter": ...

    @overload
    def open_cache_file(self, url: str, path: str, branch: "str | None" = ..., data_type: str = ...,
                        mode: str = ...) -> "_AtomicCacheWriter | IO[str] | IO[bytes]": ...

    def open_cache_file(self, url: str, path: str, branch: "str | None" = "main",
                        data_type: str = "text",
                        mode: str = "r") -> "_AtomicCacheWriter | IO[str] | IO[bytes]":
        cache_path = self.get_cache_path(url, path, branch, data_type)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)

        cache_string = self.generate_cache_string(url, path, branch, data_type)

        if any(flag in mode for flag in ("w", "a", "x", "+")):
            if mode not in ("w", "wb"):
                # Reject modes that cannot use fresh-file atomic replacement.
                raise ValueError(f"unsupported cache write mode {mode!r}: only 'w'/'wb' are supported")
            lock = self._get_file_lock(cache_string)
            lock.acquire()
            try:
                return _AtomicCacheWriter(
                    cache_path, mode, lock,
                    on_committed=lambda: self._stamp_committed(cache_string, cache_path),
                )
            except Exception:
                lock.release()
                raise

        # Defer last-use renewal and preserve content age on reads.
        with self.write_lock:
            entry = self.files.get(cache_string, {})
            entry["path"] = cache_path
            entry["date"] = time.time()
            self.files[cache_string] = entry
            self._mark_index_dirty_locked()

        return cast("IO[str] | IO[bytes]", open(cache_path, mode))

    def get_fetched_timestamp(self, url: str, path: str, branch: "str | None" = "main", data_type: str = "text") -> float | None:
        """Return content age from its fetched clock or legacy file mtime.
        Never use the read-renewed last-use clock to decide staleness."""
        entry = self.files.get(self.generate_cache_string(url, path, branch, data_type), {})
        fetched = entry.get("fetched")
        if fetched is not None:
            return cast(float | None, fetched)
        cache_path = entry.get("path")
        if cache_path and os.path.exists(cache_path):
            try:
                return os.path.getmtime(cache_path)
            except OSError:
                return None
        return None
