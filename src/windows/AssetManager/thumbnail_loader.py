"""Off-main thumbnail decode for the asset-manager grids.

A page flip in an asset chooser rebinds a pool of preview cards, and each card
used to decode its thumbnail inline on the GTK main loop. Fifty decodes at
about 17 ms each, and far more for an oversized file, froze the window for most
of a second on every flip. This module moves the decode onto a worker pool and
applies the result on the main loop, so a flip returns at once and the
thumbnails fill in behind it.

ThumbnailLoader keeps four promises:

- LIFO. The newest request decodes first. A flip to a new page jumps its cards
  ahead of any straggler from the page the user just left. A worker takes the
  newest entry off the stack at the moment it is free, so the order the pool
  runs its tasks in does not decide the order the cards decode in.
- Weak targets. The card is held weakly. A card that is gone by the time its
  decode lands takes no pixbuf and raises nothing, which is what keeps a decode
  that finishes after the window closed from painting into a dead widget.
- One cache. Decoded bytes go into the shared ByteLRUCache, so a card the user
  scrolls back to is served without a second decode. The cache is the one both
  device caches already share, enrolled in the process-wide memory budget.
- Batched delivery. Finished decodes are applied to their cards in one main-loop
  pass, not one idle per card, so a wave of workers landing at once costs the
  main loop one callback and not fifty.

A new view calls begin_generation() first. That drops every request that has
not started and bumps an epoch, so a decode from the page just left neither
starts nor paints. The epoch, not a per-card check, is what a page flip cancels
stale work with.

The class takes its decode, its apply, its marshal, its cache and its pool as
arguments, so a headless test drives the whole thing with no GTK widget and no
real image. build_loader() wires the real collaborators, and the module holds
one shared cache and one shared pool for every grid to share.
"""
from __future__ import annotations

import struct
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

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
    """Decodes preview thumbnails off the main loop and applies them on it.

    One instance serves one grid: the epoch and the pending stack are its own,
    so a flip in one grid cancels only that grid's stale work. The cache and the
    pool are shared, passed in, so cache reuse and pool discipline are
    process-wide.
    """

    def __init__(self, *, decode: DecodeFn, apply: ApplyFn, marshal: MarshalFn,
                 cache: "ByteLRUCache", pool: "DeadlinePool") -> None:
        self._decode = decode
        self._apply = apply
        self._marshal = marshal
        self._cache = cache
        self._pool = pool

        self._lock = threading.Lock()
        # id(target) -> (key, weakref(target), epoch). An OrderedDict so the
        # newest request sits at the end and a worker pops it first, which is
        # the LIFO order. Keyed by the target's id so a card rebound before its
        # first decode ran replaces its own pending entry rather than stacking a
        # second one.
        self._pending: "OrderedDict[int, tuple[Key, weakref.ref[Any], int]]" = OrderedDict()
        # Finished decodes waiting for the next main-loop flush.
        self._ready: list[tuple[weakref.ref[Any], bytes | None, int]] = []
        # True from the moment a flush is handed to the marshal until that flush
        # drains. While it is set, a further completion only appends, so a wave
        # of workers costs one marshalled callback and not one each.
        self._flush_scheduled = False
        self._epoch = 0

    def begin_generation(self) -> None:
        """Start a new view.

        Drops every request that has not started and bumps the epoch. A decode
        already running finishes, but its delivery carries the old epoch and the
        flush drops it, so it never paints over the new grid.
        """
        with self._lock:
            self._pending.clear()
            self._epoch += 1

    def request(self, key: "Key | None", target: Any) -> None:
        """Ask for the thumbnail at key, to be applied to target.

        key is None for a card with no image; the target is cleared at once.
        A cache hit queues the bytes for the next flush. A miss goes on the LIFO
        stack and a worker is asked to take the newest entry.

        Called on the main loop, from the grid's rebind pass.
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
        """Take the newest pending request and decode it. Runs on a worker.

        It pops the newest entry, so the order the pool happens to run its
        tasks in does not decide which card decodes next: the stack does.
        """
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
        # The first completion to flip the flag drives the batch. Every other
        # completion until the flush runs just appends above, so the batch grows
        # and the marshal fires once.
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


# Real wiring. The serialized form is a small header and the raw pixels, so the
# apply side wraps the pixels back into a pixbuf with no second decode and no
# second scale.

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
    return header + pixbuf.get_pixels()


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


def _real_decode(key: Key) -> bytes | None:
    """Decode key at the preview size and serialize it. Runs on a worker.

    A GdkPixbuf decode is file I/O and not GTK work, so it is safe off the main
    thread. A missing, corrupt or unreadable file returns None, and the card
    then shows the broken-image icon.
    """
    from gi.repository import GdkPixbuf, GLib

    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            key, width=THUMBNAIL_WIDTH, height=THUMBNAIL_HEIGHT,
            preserve_aspect_ratio=True)
    except GLib.Error as e:
        log.warning(f"Could not load asset preview for {key}: {e}")
        return None
    return _serialize(pixbuf)


def _real_apply(target: Any, data: bytes | None) -> None:
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
    """The one thumbnail decode pool. Lifecycle only: it puts no deadline on a
    decode and replaces no executor, so a slow decode holds its worker until it
    returns and nothing else is disturbed."""
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
        decode=_real_decode,
        apply=_real_apply,
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
