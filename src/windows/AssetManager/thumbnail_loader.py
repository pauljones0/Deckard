"""Decode LIFO thumbnail requests off-main with weak targets and batched delivery.

Per-grid epochs cancel stale work; all grids share the byte cache and worker pool.
"""
from __future__ import annotations

import struct
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from loguru import logger as log

if TYPE_CHECKING:
    from src.backend.DeckManagement.Subclasses.byte_lru_cache import ByteLRUCache
    from src.backend.deadline_pool import DeadlinePool

# A cache key names one thumbnail. It is the file path decoded at the fixed
# preview size, so the path alone identifies the decoded bytes.
Key = str
# Decode runs on a worker and touches no widget: key in, serialized bytes out,
# or None when the file will not decode.
DecodeFn = Callable[[Key], "bytes | None"]
# Apply runs on the main loop: it turns the bytes back into a pixbuf and shows
# it on the target, or shows the broken-image icon when the bytes are None.
ApplyFn = Callable[[Any, "bytes | None"], None]
# Marshal hands a callable to the main loop. run_on_main in the real wiring.
MarshalFn = Callable[[Callable[[], None]], None]


class ThumbnailLoader:
    """Decode for one grid off-main with a local epoch and shared cache and pool."""

    def __init__(self, *, decode: DecodeFn, apply: ApplyFn, marshal: MarshalFn,
                 cache: "ByteLRUCache", pool: "DeadlinePool") -> None:
        self._decode = decode
        self._apply = apply
        self._marshal = marshal
        self._cache = cache
        self._pool = pool

        self._lock = threading.Lock()
        # LIFO by target id so rebinding replaces that card's pending request
        self._pending: "OrderedDict[int, tuple[Key, weakref.ref[Any], int]]" = OrderedDict()
        # Finished decodes waiting for the next main-loop flush.
        self._ready: list[tuple[weakref.ref[Any], bytes | None, int]] = []
        # True while one marshalled flush owns all additional completions
        self._flush_scheduled = False
        self._epoch = 0

    def begin_generation(self) -> None:
        """Clear pending requests and stale running deliveries with a new epoch."""
        with self._lock:
            self._pending.clear()
            self._epoch += 1

    def request(self, key: "Key | None", target: Any) -> None:
        """Clear a None key, flush cache hits, or queue misses in LIFO order.

        The caller must run on-main because the None branch applies immediately.
        """
        if key is None:
            # No path. Clear the card now, on the caller's thread, which is the
            # main loop, so a recycled card does not keep the earlier image.
            self._apply(target, None)
            return

        ref: "weakref.ref[Any]" = weakref.ref(target)
        cached = self._cache.get(key)
        with self._lock:
            epoch = self._epoch
            if cached is not None:
                self._ready.append((ref, cached, epoch))
                schedule = not self._flush_scheduled
                self._flush_scheduled = True
            else:
                tid = id(target)
                self._pending[tid] = (key, ref, epoch)
                self._pending.move_to_end(tid)
                schedule = False
        if cached is not None:
            if schedule:
                self._marshal(self._flush)
            return
        # Outside the lock. submit() returns None on a shut-down pool, which is
        # a no-op here: nothing waits on the future.
        self._pool.submit(self._decode_next)

    def _decode_next(self) -> None:
        """Decode the newest pending request on a worker, independent of pool order."""
        with self._lock:
            if not self._pending:
                # A begin_generation cleared the stack, or another worker took
                # the last entry. Either way there is nothing to do.
                return
            _tid, (key, ref, epoch) = self._pending.popitem(last=True)

        if ref() is None:
            # The card is gone already. Cancelled before the decode: no decode,
            # no delivery, no raise.
            return

        try:
            data = self._decode(key)
        except Exception as e:
            # A decode that raises leaves the card on the broken-image icon, the
            # same as a file that will not decode. It must not kill the worker.
            log.opt(exception=True).warning(f"thumbnail decode failed for {key!r}: {e}")
            data = None

        if data is not None:
            self._cache.put(key, data)
        self._enqueue_ready(ref, data, epoch)

    def _enqueue_ready(self, ref: "weakref.ref[Any]", data: "bytes | None",
                       epoch: int) -> None:
        """Queue one decoded result for the next flush and schedule that flush
        once for the whole wave."""
        with self._lock:
            self._ready.append((ref, data, epoch))
            if self._flush_scheduled:
                return
            self._flush_scheduled = True
        # The first completion schedules one flush; later completions join its batch
        self._marshal(self._flush)

    def _flush(self) -> None:
        """Apply every queued result in one pass. Runs on the main loop."""
        with self._lock:
            batch = self._ready
            self._ready = []
            self._flush_scheduled = False
            epoch = self._epoch
        for ref, data, item_epoch in batch:
            if item_epoch != epoch:
                # A page flip overtook this decode. Dropping it here is what
                # keeps a stale batch from painting over the new grid.
                continue
            target = ref()
            if target is None:
                # The card went away between the decode and now. No delivery.
                continue
            try:
                self._apply(target, data)
            except Exception as e:
                # One bad apply must not abort the rest of the batch.
                log.opt(exception=True).warning(f"applying a thumbnail failed: {e}")


# Serialized headers and raw pixels avoid a second decode or scale during apply

# The preview cards decode at this fixed size. See Preview.decode_pixbuf.
THUMBNAIL_WIDTH = 250
THUMBNAIL_HEIGHT = 180

# width, height, rowstride, n_channels, bits-per-sample, has-alpha.
_HEADER = struct.Struct("<IIIBBB")

# How much decoded thumbnail data to keep. A card is 250x180 RGB(A), about
# 135 to 180 KiB each, so this holds a few hundred cards, well past a page.
THUMBNAIL_CACHE_BYTES = 24 * 1024 * 1024
# How many decodes run at once. File I/O bound, so a handful overlaps the reads
# without swamping the machine.
THUMBNAIL_WORKERS = 4

_shared_lock = threading.Lock()
_shared_cache: "ByteLRUCache | None" = None
_shared_pool: "DeadlinePool | None" = None


def _serialize(pixbuf: Any) -> bytes:
    header = _HEADER.pack(
        pixbuf.get_width(),
        pixbuf.get_height(),
        pixbuf.get_rowstride(),
        pixbuf.get_n_channels(),
        pixbuf.get_bits_per_sample(),
        1 if pixbuf.get_has_alpha() else 0,
    )
    # get_pixels() copies the pixel buffer into a bytes object, so the value is
    # immutable and safe to hand out of the cache by reference.
    return cast(bytes, header + pixbuf.get_pixels())


def _deserialize(data: bytes) -> Any:
    from gi.repository import GdkPixbuf, GLib

    width, height, rowstride, _n_channels, bits, has_alpha = _HEADER.unpack(
        data[:_HEADER.size])
    pixels = data[_HEADER.size:]
    # new_from_bytes wraps the pixels; it neither decodes nor scales, so this
    # costs almost nothing on the main loop.
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(pixels), GdkPixbuf.Colorspace.RGB, bool(has_alpha),
        bits, width, height, rowstride)


def _decode_thumbnail_bytes(key: Key) -> bytes | None:
    """Decode and serialize off-main; return None for unreadable files."""
    from gi.repository import GdkPixbuf, GLib

    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            key, width=THUMBNAIL_WIDTH, height=THUMBNAIL_HEIGHT,
            preserve_aspect_ratio=True)
    except GLib.Error as e:
        log.warning(f"Could not load asset preview for {key}: {e}")
        return None
    return _serialize(pixbuf)


def _apply_thumbnail_bytes(target: Any, data: bytes | None) -> None:
    """Show the decoded thumbnail on target, or the broken-image icon on a
    failed decode. Runs on the main loop."""
    if data is None:
        target.set_pixbuf(None)
        return
    target.set_pixbuf(_deserialize(data))


def shared_cache() -> "ByteLRUCache":
    """The one thumbnail cache, built and enrolled in the memory budget once."""
    global _shared_cache
    with _shared_lock:
        if _shared_cache is None:
            from src.backend.DeckManagement.Subclasses import cache_budget
            from src.backend.DeckManagement.Subclasses.byte_lru_cache import (
                ByteLRUCache,
            )
            _shared_cache = ByteLRUCache(max_bytes=THUMBNAIL_CACHE_BYTES)
            cache_budget.register(_shared_cache, label="thumbnail_cache:asset")
        return _shared_cache


def shared_pool() -> "DeadlinePool":
    """Shared lifecycle pool where a slow decode holds only its worker."""
    global _shared_pool
    with _shared_lock:
        if _shared_pool is None:
            from src.backend.deadline_pool import DeadlinePool
            _shared_pool = DeadlinePool(max_workers=THUMBNAIL_WORKERS,
                                        thread_name_prefix="thumb-decode")
        return _shared_pool


def build_loader() -> ThumbnailLoader:
    """A loader wired to the real decode, apply, marshal, cache and pool."""
    from src.backend.main_loop import run_on_main

    def marshal(fn: Callable[[], None]) -> None:
        # run_on_main takes further arguments this never uses; the wrapper pins
        # the one-argument shape the loader marshals with.
        run_on_main(fn)

    return ThumbnailLoader(
        decode=_decode_thumbnail_bytes,
        apply=_apply_thumbnail_bytes,
        marshal=marshal,
        cache=shared_cache(),
        pool=shared_pool(),
    )


def shutdown_thumbnail_pool() -> None:
    """Stop the decode pool; call on app quit. A no-op when no grid ever built
    a loader."""
    with _shared_lock:
        pool = _shared_pool
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
