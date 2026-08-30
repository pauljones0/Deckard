"""Build and atomically promote low-resolution MP4 caches without retaining raw frames.
Background caches build inline; key caches use one shared builder and per-consumer readers."""
import contextlib
import hashlib
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable

import cv2
import numpy as np
import numpy.typing as npt
from PIL import Image, ImageEnhance, ImageOps
from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.Subclasses import cache_budget
from src.backend.DeckManagement.deck_controller.viewport import DEFAULT_VIEW, is_default_view, render_viewport_rgb, view_suffix
from typing import Generic, TypeVar, cast, override

VID_CACHE = os.path.join(gl.DATA_PATH, "cache", "videos")
os.makedirs(VID_CACHE, exist_ok=True)


_md5_memo_lock = threading.Lock()
# Bound source identities because each video edit creates a new key.
# The 256-entry limit exceeds normal working sets, and eviction only causes re-hashing.
_MD5_MEMO_MAX = 256
# Key: (path, st_dev, st_ino, st_size, st_mtime_ns). See _video_identity.
_md5_memo: "OrderedDict[tuple[str, int, int, int, int], str]" = OrderedDict()


def _video_identity(st: "os.stat_result") -> tuple[int, int, int, int]:
    """Return digest identity from device, inode, size, and nanosecond mtime.
    Inode detects replacement; nanosecond mtime detects subsecond in-place rewrites."""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def get_video_md5(path: str, attempts: int = 3) -> str:
    """Return an MD5 memoized by stable file identity in a bounded LRU.
    Compare identity before and after hashing; retry changed files and memoize only stable reads."""
    digest = ""
    for _ in range(max(1, attempts)):
        st_before = os.stat(path)
        key = (path, *_video_identity(st_before))
        with _md5_memo_lock:
            cached = _md5_memo.get(key)
            if cached is not None:
                _md5_memo.move_to_end(key)
        if cached is not None:
            return cached

        md5 = hashlib.md5()
        with open(path, "rb") as f:
            block = f.read(2 ** 16)
            while len(block) != 0:
                md5.update(block)
                block = f.read(2 ** 16)
        digest = md5.hexdigest()

        st_after = os.stat(path)
        if _video_identity(st_after) != _video_identity(st_before):
            # The file changed under the hash. This digest may name a torn mix
            # of old and new bytes, so do not memoize it, and read again.
            continue

        with _md5_memo_lock:
            _md5_memo[key] = digest
            _md5_memo.move_to_end(key)
            while len(_md5_memo) > _MD5_MEMO_MAX:
                _md5_memo.popitem(last=False)
        return digest

    # Return the last digest without memoizing after repeated concurrent changes.
    # A later stable read can then cache under its own identity.
    return digest


def _sat_centi(saturation: float) -> int:
    """Return the saturation bucket in integer hundredths.
    Keys, paths, and baked frames must share one rounding to prevent permanent adoption misses."""
    return int(round(float(saturation) * 100))


def canonical_saturation(saturation: float) -> float:
    """Map raw saturation to the canonical value used by keys, paths, and enhancement."""
    return _sat_centi(saturation) / 100.0


def sat_suffix(saturation: float) -> str:
    """Encode nondefault saturation in fixed hundredths, such as .sat130.
    Default saturation has no suffix and uses the same rounding as the registry key."""
    centi = _sat_centi(saturation)
    # Hundredth rounding treats [0.995, 1.005) as default.
    # The UI emits exact 0.05 steps, so it cannot reach the different still-image threshold gap.
    return "" if centi == 100 else f".sat{centi}"


# What one decoded frame becomes: a single tile image for the key cache,
# or the per-key tile list of the background cache.
PayloadT = TypeVar("PayloadT")


class Mp4FrameCache(Generic[PayloadT]):
    """Build or reuse one MP4 per source, output size, and saturation without retaining frames.
    Builders atomically promote temporary files; readers decode the cache or source without writing."""

    # Decode and discard through forward jumps up to this threshold because it is cheaper than seeking.
    # Seek for larger or backward jumps.
    MAX_DECODE_AHEAD = 30

    # Registry entry points attach these fields only to their readers.
    # Direct instances lack them, so reads use getattr defaults.
    _registry_key: "tuple[str, tuple[int, int], float, str] | None"
    _registry_entry: "_TileCacheEntry | None"

    def __init__(self, source_path: str, out_size: tuple[int, int], saturation: float = 1.0,
                 cache_path: str | None = None, is_builder: bool = True,
                 view: tuple[float, float, float] = DEFAULT_VIEW) -> None:
        self.lock = threading.Lock()

        self.source_path = source_path
        self.out_size = out_size
        self.saturation = canonical_saturation(saturation)
        self._sat_suffix = sat_suffix(self.saturation)
        # The viewport baked into every cached frame; it joins the file name
        # like the saturation, so a view change builds a new cache.
        self.view, self._view_suffix = view, view_suffix(view)
        # Background video combines builder and consumer; key video separates one builder from its readers.
        self.is_builder = is_builder

        self.video_md5 = get_video_md5(source_path)

        self.cache_path = cache_path or self._default_cache_path()
        cache_dir = os.path.dirname(self.cache_path)
        os.makedirs(cache_dir, exist_ok=True)
        # Use a unique temporary path per writer; os.replace makes any promotion collision atomic.
        self._writer_tmp_path = os.path.join(
            cache_dir,
            f"{os.path.basename(self.cache_path)}.{os.getpid()}-{id(self):x}.tmp.mp4",
        )

        self._complete = False
        self._cache_cap: cv2.VideoCapture | None = None
        self._cache_pos = 0  # index of the next frame _cache_cap will return
        self._last_entry: "tuple[int, PayloadT] | None" = None
        self.last_payload: "PayloadT | None" = None  # last good decode, served over a transient failure
        self.last_payload_index: int | None = None  # source frame last_payload holds (see get_frame_and_index)
        self._adopt_failures = 0  # failed shared-cache adoptions (see _maybe_adopt_shared_cache)

        self.cap: cv2.VideoCapture | None = None
        self._writer: cv2.VideoWriter | None = None
        self._frames_written = 0
        self.last_frame_index = -1  # source decode position while building/reading

        self.n_frames = 0
        self._source_fps: float | None = None

        if not self._open_existing_cache():
            self._open_source()

        # Register reader RAM for accounting only; eviction would cause per-tick decode.
        # Register last so budget_bytes never observes partial construction.
        cache_budget.register(
            self,
            label=f"video_readers:{self.video_md5[:8]}@{self.out_size[0]}x{self.out_size[1]}",
            evictable=False,
        )

    # Use a flat allowance because Python cannot inspect FFmpeg decoder buffers.
    # The census tracks reader count and approximate memory, not exact libavcodec use.
    CAPTURE_OVERHEAD_BYTES = 2 * 1024 * 1024

    def budget_bytes(self) -> int:
        """Estimate reader image RAM without taking the decode lock.
        Atomic local reads can give one stale sample but cannot stall process-wide eviction."""
        payload = self.last_payload
        total = 0
        if payload is not None:
            frames = payload if isinstance(payload, (list, tuple)) else (payload,)
            # Both payload kinds hold PIL images; anything else falls through
            # the except below, as it always did.
            for frame in cast("Iterable[Image.Image]", frames):
                try:
                    total += frame.width * frame.height * len(frame.getbands())
                except Exception:
                    continue
        for cap in (self.cap, self._cache_cap):
            if cap is not None:
                total += self.CAPTURE_OVERHEAD_BYTES
        return total

    def get_source_fps(self) -> float | None:
        """Return source fps from the open source or same-rate cache, or None."""
        if self._source_fps is None:
            with self.lock:
                cap = self._cache_cap if self._cache_cap is not None else self.cap
                if cap is not None:
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    if fps and fps > 0:
                        self._source_fps = float(fps)
        return self._source_fps

    def _default_cache_path(self) -> str:
        raise NotImplementedError

    def _payload_from_bgr(self, frame_bgr: "npt.NDArray[np.uint8]") -> PayloadT:
        """Convert target-resolution BGR to the subclass payload.
        Key caches return one RGB tile; background caches return cropped tiles and strip."""
        raise NotImplementedError

    def _fallback_payload(self) -> "PayloadT | None":
        """Return a fallback before the first decode or after unrecoverable early failure."""
        return None

    def _on_promoted(self) -> None:
        """Run when an existing or newly promoted cache becomes complete.
        The default does nothing; background caches remove their unreadable old format."""
        pass

    def _writer_enabled(self) -> bool:
        """Return whether a builder opens its writer.
        Key registries gate at acquisition; self-contained background caches override for live settings."""
        return True

    def _open_cache_capture(self) -> cv2.VideoCapture:
        # Use one FFmpeg thread because small tile and canvas streams decode faster than needed.
        return cv2.VideoCapture(self.cache_path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 1])

    def _open_existing_cache(self) -> bool:
        if not os.path.isfile(self.cache_path):
            return False
        cap = self._open_cache_capture()
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if cap.isOpened() else 0
        cached_size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                       int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))) if cap.isOpened() else (0, 0)
        # mp4v truncates odd dimensions; compare against the writable even size.
        # Comparing the requested odd size would cause an endless rebuild loop.
        writable_size = (self.out_size[0] - self.out_size[0] % 2,
                         self.out_size[1] - self.out_size[1] % 2)
        if n_frames <= 0 or cached_size != writable_size:
            # A size mismatch means render geometry changed and crop coordinates are stale.
            # Delete only when this instance can rebuild; otherwise decode the source and retain the file.
            cap.release()
            can_rebuild = self.is_builder and self._writer_enabled()
            if n_frames <= 0:
                log.warning(f"Removing unreadable video cache {self.cache_path}")
            elif can_rebuild:
                log.info(f"Removing stale video cache ({cached_size[0]}x{cached_size[1]}, "
                         f"need {writable_size[0]}x{writable_size[1]}): {self.cache_path}")
            else:
                log.info(f"Ignoring stale video cache ({cached_size[0]}x{cached_size[1]}, "
                         f"need {writable_size[0]}x{writable_size[1]}, cache writer off): "
                         f"{self.cache_path}")
                return False
            with contextlib.suppress(OSError):
                os.remove(self.cache_path)
            return False
        self._cache_cap = cap
        self._cache_pos = 0
        self.n_frames = n_frames
        self._complete = True
        self._on_promoted()
        log.info(f"Using cached tile video ({n_frames} frames): {self.cache_path}")
        return True

    def _open_source(self) -> None:
        # Give detached builders four decode threads but keep each consumer reader at one.
        threads = 4 if self.is_builder else 1
        self.cap = cv2.VideoCapture(self.source_path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, threads])
        self.n_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not self.is_builder or not self._writer_enabled():
            return
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        writer = cv2.VideoWriter(self._writer_tmp_path, cv2.VideoWriter.fourcc(*"mp4v"), fps, self.out_size)
        if writer.isOpened():
            self._writer = writer
        else:
            log.warning(f"Could not open tile cache writer for {self.source_path}; playing uncached")

    def get_frame(self, n: int) -> "PayloadT | None":
        return self.get_frame_and_index(n)[0]

    def get_frame_and_index(self, n: int) -> "tuple[PayloadT | None, int | None]":
        """Return a payload and its actual source index, not the requested index.
        Clamp requests, repeat transient failures, and use None when provenance is unknown."""
        if not self._complete:
            self._maybe_adopt_shared_cache()
        with self.lock:
            if self._complete:
                payload = self._get_cached_frame(n)
            else:
                payload = self._decode_source_frame(n)
            # Publish under the close lock so an in-flight decode cannot retain a frame after teardown.
            if payload is not None:
                self.last_payload = payload
                # Claim the clamped index only when _last_entry contains this exact payload.
                if self._last_entry is not None and self._last_entry[1] is payload:
                    self.last_payload_index = self._last_entry[0]
                else:
                    self.last_payload_index = None
                return payload, self.last_payload_index
            # Repeat the last good payload and its index over transient decode failure.
            if self.last_payload is not None:
                return self.last_payload, self.last_payload_index
        return self._fallback_payload(), None

    # After bounded adoption failures, invalidate and detach from the missing ready file.
    # Future acquisition can rebuild, and current playback stops per-frame file checks.
    MAX_ADOPT_FAILURES = 3

    def _maybe_adopt_shared_cache(self) -> None:
        """Switch an attached non-builder source reader to its promoted shared cache.
        Direct background instances and builder instances have no registry entry and do nothing."""
        entry = getattr(self, "_registry_entry", None)
        if entry is None or not entry.ready:
            return
        with self.lock:
            if self._complete:
                return
            if self._open_existing_cache():
                if self.cap is not None:
                    self.cap.release()
                    self.cap = None
                return
            # Bound retries when a ready registry file is missing or unreadable.
            # Then invalidate for rebuild and detach this reader to source decode.
            self._adopt_failures += 1
            give_up = self._adopt_failures >= self.MAX_ADOPT_FAILURES
        if not give_up:
            return
        log.warning(
            f"Shared tile cache {self.cache_path} is marked ready but cannot be "
            f"opened; invalidating its registry entry and continuing uncached "
            f"from {self.source_path}"
        )
        # Clear both reader registry fields under its lock before returning one reference.
        # Concurrent release then cannot detach twice, and no registry lock nests with self.lock.
        with self.lock:
            key = getattr(self, "_registry_key", None)
            self._registry_key = None
            self._registry_entry = None
        if key is None:
            return
        with _registry_lock:
            entry.ready = False
            # Keep a live builder handle to enforce one builder per key.
            # Clear only finished handles; attached consumers otherwise remain uncached until detachment.
            if entry.builder_thread is not None and not entry.builder_thread.is_alive():
                entry.builder_thread = None
        # Return this reader's reference now; release will find it detached.
        # A zero count signals and joins a builder whose output has no consumer.
        _detach_entry(key, entry)

    def _get_cached_frame(self, n: int) -> "PayloadT | None":
        n = max(0, min(n, self.n_frames - 1))
        if self._last_entry is not None and self._last_entry[0] == n:
            return self._last_entry[1]
        cap = self._cache_cap
        if cap is None:
            return None
        if n < self._cache_pos or n > self._cache_pos + self.MAX_DECODE_AHEAD:
            cap.set(cv2.CAP_PROP_POS_FRAMES, n)
            self._cache_pos = n
        frame = None
        while self._cache_pos <= n:
            success, frame = cap.read()
            if not success:
                # Container metadata overcounted; clamp to what is readable.
                self.n_frames = max(1, self._cache_pos)
                return None
            self._cache_pos += 1
        if frame is None:
            # Seek position guarantees one read, but keep a cheap media-thread guard instead of an assertion.
            return None
        # cv2's stubs erase the dtype; a decoded video frame is uint8 BGR.
        payload = self._payload_from_bgr(cast("npt.NDArray[np.uint8]", frame))
        self._last_entry = (n, payload)
        return payload

    def _decode_source_frame(self, n: int) -> "PayloadT | None":
        if self.cap is None:
            return None
        if self.n_frames > 0:
            n = max(0, min(n, self.n_frames - 1))
        if self._last_entry is not None and self._last_entry[0] == n:
            return self._last_entry[1]

        # Abort a partial build before a backward request can append out of order.
        # Plain readers only seek; a later builder can restart from scratch.
        if n < self.last_frame_index:
            if self.is_builder:
                self._abort_writer()
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, n)
            self.last_frame_index = n - 1

        payload = None
        while self.last_frame_index < n:
            success, frame = self.cap.read()
            if not success:
                self._end_of_source()
                if self._complete:
                    return self._get_cached_frame(n)
                return None
            self.last_frame_index += 1
            target_bgr = self._fit_to_target(cast("npt.NDArray[np.uint8]", frame))
            if self._writer is not None:
                self._writer.write(target_bgr)
                self._frames_written += 1
            if self.last_frame_index == n:
                payload = self._payload_from_bgr(target_bgr)

        # Promote when all metadata-promised frames are written because EOF may not be read.
        if self.n_frames > 0 and self.last_frame_index >= self.n_frames - 1:
            self._end_of_source()

        if payload is not None:
            self._last_entry = (n, payload)
        return payload

    def _fit_to_target(self, frame_bgr: "npt.NDArray[np.uint8]") -> "npt.NDArray[np.uint8]":
        """Fit BGR through the viewport and bake saturation once during cache build."""
        pil_image = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
        if is_default_view(self.view):
            # The centered cover crop of before, byte-identical, so a cache
            # built without views stays valid.
            canvas = ImageOps.fit(pil_image, self.out_size, Image.Resampling.HAMMING)
        else:
            canvas = render_viewport_rgb(pil_image, self.out_size, self.view, Image.Resampling.HAMMING)
        # BGR conversion guarantees RGB for enhancement; default saturation skips it.
        if self._sat_suffix:
            canvas = ImageEnhance.Color(canvas).enhance(self.saturation)
        # cv2's stubs erase the dtype; the conversion of an RGB canvas is
        # uint8 BGR.
        return cast("npt.NDArray[np.uint8]", cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR))

    def _end_of_source(self) -> None:
        """Finish on EOF or decode failure, promoting written frames or clamping count.
        Always release the source capture, including failure before the first frame."""
        if self._writer is not None:
            self._writer.release()
            self._writer = None
            if self._frames_written > 0:
                try:
                    os.replace(self._writer_tmp_path, self.cache_path)
                except OSError:
                    log.opt(exception=True).error("Failed to store tile video cache")
                else:
                    cap = self._open_cache_capture()
                    if cap.isOpened():
                        self._cache_cap = cap
                        self._cache_pos = 0
                        self.n_frames = self._frames_written
                        self._complete = True
                        self._on_promoted()
                        log.success(
                            f"Cached tile video ({self._frames_written} frames, "
                            f"{os.path.getsize(self.cache_path) / 1e6:.1f} MB): {self.cache_path}"
                        )
            else:
                self._remove_writer_tmp()

        if not self._complete and self.last_frame_index >= 0:
            self.n_frames = self.last_frame_index + 1

        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def is_cache_complete(self) -> bool:
        return self._complete

    def is_build_terminal(self) -> bool:
        """Return whether source release without cache completion prevents further progress.
        This follows writer, promotion, reopen, or truncation failure after a frame request."""
        return self.cap is None and not self._complete

    def _abort_writer(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        self._remove_writer_tmp()

    def _remove_writer_tmp(self) -> None:
        try:
            if os.path.isfile(self._writer_tmp_path):
                os.remove(self._writer_tmp_path)
        except OSError:
            pass

    def close(self) -> None:
        # Unregister immediately so census counts do not retain closed readers until collection.
        cache_budget.unregister(self)
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            if self._cache_cap is not None:
                self._cache_cap.release()
                self._cache_cap = None
            self._abort_writer()
            self._complete = False
            self._last_entry = None
            self.last_payload = None
            self.last_payload_index = None


class KeyVideoCache(Mp4FrameCache[Image.Image]):
    """Decode one uncropped tile-size image per key or dial frame.
    Instances act as either the detached registry builder or one consumer reader."""

    @override
    def _payload_from_bgr(self, frame_bgr: "npt.NDArray[np.uint8]") -> Image.Image:
        # One RGB tile image per frame, decoded at tile resolution.
        return Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))

    @override
    def _default_cache_path(self) -> str:
        size_str = f"{self.out_size[0]}x{self.out_size[1]}"
        cache_dir = os.path.join(VID_CACHE, f"keys_{size_str}")
        return os.path.join(cache_dir, f"{self.video_md5}{self._sat_suffix}.mp4")


# Share cache files, not reader instances, by source, size, and saturation.
# Interleaved readers would abort monotonic builds and seek-thrash independent timelines.

def cache_videos_enabled() -> bool:
    return gl.settings_manager.app().cache_videos


class _TileCacheEntry:
    __slots__ = ("path", "refcount", "ready", "builder_thread", "stop_event",
                 "last_build_failure")

    def __init__(self, path: str):
        self.path = path
        self.refcount = 0
        # A previous run can have built this exact cache and left it on disk.
        # No builder is needed then, and the first acquire() only reads it.
        self.ready = os.path.isfile(path)
        self.builder_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        # Store last failure monotonic time, or zero, so acquisition enforces rebuild cooldown.
        self.last_build_failure = 0.0


_registry_lock = threading.Lock()
_registry: dict[tuple[str, tuple[int, int], float, str], _TileCacheEntry] = {}

# Join signalled builders inline to stop orphaned decoding, but bound the media-thread wait.
# Builders test stop each frame; longer exits enter the lingering list.
_BUILDER_JOIN_TIMEOUT_S = 0.5

# Delay builder restart after failure so broken sources do not rebuild on every acquire.
_BUILD_RETRY_COOLDOWN_S = 30.0

# Share one quit-time join budget across all builders.
# Per-thread budgets can exceed the force-quit deadline and skip later teardown.
_SHUTDOWN_JOIN_BUDGET_S = 2.0

# Track signalled builders that outlive inline joins for one bounded shutdown join.
_lingering_builders: list[threading.Thread] = []


def _join_builder(thread: threading.Thread | None, timeout: float = _BUILDER_JOIN_TIMEOUT_S) -> None:
    """Bound the join of a signalled builder so cv2 decode ends before runtime teardown.
    Never self-join because builder failure can reach the detach path."""
    if thread is None or thread is threading.current_thread():
        return
    thread.join(timeout=timeout)
    if not thread.is_alive():
        return
    log.warning(
        f"Tile cache builder did not stop within {timeout}s of being signalled; "
        f"leaving it to finish in the background"
    )
    with _registry_lock:
        # Retain only live stragglers so dead Thread objects do not accumulate.
        _lingering_builders[:] = [t for t in _lingering_builders if t.is_alive()]
        _lingering_builders.append(thread)


def shutdown_builders(timeout: float = _SHUTDOWN_JOIN_BUDGET_S) -> None:
    """Signal attached and lingering builders, then join within one shared deadline.
    A per-thread timeout can multiply past the force-quit deadline."""
    with _registry_lock:
        threads = [entry.builder_thread for entry in _registry.values()]
        threads.extend(_lingering_builders)
        _lingering_builders.clear()
        for entry in _registry.values():
            entry.stop_event.set()
    deadline = time.monotonic() + timeout
    for thread in threads:
        _join_builder(thread, max(0.0, deadline - time.monotonic()))


def _registry_key(source_path: str, out_size: tuple[int, int], saturation: float,
                  variant: str = "") -> tuple[str, tuple[int, int], float, str]:
    # Use the path's canonical saturation rounding in the key.
    # Include rendering variant so noninterchangeable pixels cannot share a file.
    return (get_video_md5(source_path), out_size, canonical_saturation(saturation), variant)


def _cache_file_path(md5: str, out_size: tuple[int, int], saturation: float,
                     variant: str = "") -> str:
    size_str = f"{out_size[0]}x{out_size[1]}"
    return os.path.join(VID_CACHE, f"keys_{size_str}",
                        f"{md5}{sat_suffix(saturation)}{variant}.mp4")


def acquire(source_path: str, out_size: tuple[int, int], saturation: float = 1.0) -> KeyVideoCache:
    """Attach a fresh reader to a shared real-video cache by source, size, and saturation.
    Release it with release; externally composited sources use acquire_from_frames."""
    key = _registry_key(source_path, out_size, saturation)
    path = _cache_file_path(key[0], out_size, saturation)

    # Carry the exact builder out of the lock so another acquire cannot replace it before start.
    start_builder: threading.Thread | None = None
    with _registry_lock:
        entry = _registry.get(key)
        if entry is None:
            entry = _TileCacheEntry(path)
            _registry[key] = entry
        entry.refcount += 1
        # Start one builder only for an uncached, enabled, non-cooling entry.
        # Failure cooldown prevents rebuild on every acquire.
        cooling = (entry.last_build_failure != 0.0
                   and time.monotonic() - entry.last_build_failure < _BUILD_RETRY_COOLDOWN_S)
        if not entry.ready and entry.builder_thread is None and not cooling and cache_videos_enabled():
            entry.builder_thread = threading.Thread(
                target=_run_builder,
                args=(entry, source_path, out_size, saturation),
                name="tile-cache-builder",
                daemon=True,
            )
            start_builder = entry.builder_thread

    if start_builder is not None:
        start_builder.start()

    # Balance the pre-incremented reference if reader construction fails.
    # Otherwise a builder can decode for a nonexistent consumer and never receive stop.
    try:
        reader = KeyVideoCache(source_path, out_size, saturation, cache_path=path, is_builder=False)
    except BaseException:
        _detach_entry(key, entry)
        raise
    reader._registry_key = key
    reader._registry_entry = entry
    return reader


def release(reader: KeyVideoCache) -> None:
    """Close and detach one acquired reader.
    At zero references, signal its builder and drop the entry for later disk discovery or rebuild."""
    reader.close()

    key = getattr(reader, "_registry_key", None)
    entry = getattr(reader, "_registry_entry", None)
    if key is None or entry is None:
        return
    _detach_entry(key, entry)


def _detach_entry(key: tuple[str, tuple[int, int], float, str], entry: "_TileCacheEntry") -> None:
    """Drop one entry reference and balance callers that never received a reader."""
    stopped: threading.Thread | None = None
    with _registry_lock:
        # Compare identity so a late release cannot remove a newer entry for the same key.
        if _registry.get(key) is not entry:
            return
        entry.refcount -= 1
        if entry.refcount <= 0:
            entry.stop_event.set()
            stopped = entry.builder_thread
            entry.builder_thread = None
            del _registry[key]
    # Outside the lock. Under it, one builder finishing its current frame would
    # stall every other acquire() and release() in the app for that long.
    _join_builder(stopped)


# GIF pixels must come from PIL because FFmpeg differs on disposal and partial frames.
# External entry points cache caller-composited frames and never demux GIFs here.

# This rate supplies required container timestamps only; consumers select indices on their own timelines.
EXTERNAL_TILE_FPS = 15.0


def attach_promoted(source_path: str, out_size: tuple[int, int],
                    saturation: float = 1.0, variant: str = "") -> KeyVideoCache | None:
    """Attach only to a complete promoted cache, or return None without building or source decode.
    Artifact existence proves buildability only for the exact rendering variant."""
    key = _registry_key(source_path, out_size, saturation, variant)
    path = _cache_file_path(key[0], out_size, saturation, variant)
    with _registry_lock:
        entry = _registry.get(key)
        if entry is None:
            if not os.path.isfile(path):
                return None
            entry = _TileCacheEntry(path)
            _registry[key] = entry
        elif not entry.ready:
            # A build is in flight, or something abandoned it. The caller does
            # its own cold pass rather than wait on another one.
            return None
        entry.refcount += 1
    return _attach_promoted_reader(source_path, out_size, saturation, key, entry, path)


def acquire_from_frames(source_path: str, out_size: tuple[int, int], saturation: float,
                        frames: "Iterable[Image.Image]", fps: float = EXTERNAL_TILE_FPS,
                        variant: str = "") -> KeyVideoCache | None:
    """Write caller-composited frames to a shared cache and attach its reader.
    Return None on write or readback failure without falling through to source decode."""
    # Separate noninterchangeable renderings of the same source and size.
    # In particular, degraded alpha-dropped GIFs must not prove that a lossless GIF is opaque.
    key = _registry_key(source_path, out_size, saturation, variant)
    path = _cache_file_path(key[0], out_size, saturation, variant)

    with _registry_lock:
        entry = _registry.get(key)
        if entry is None:
            entry = _TileCacheEntry(path)
            _registry[key] = entry
        entry.refcount += 1
        # Two keys showing the same GIF share one file and one entry, so skip
        # the write when the artifact already exists.
        needs_build = not entry.ready

    if needs_build:
        # Consume any image iterable lazily and once so generators remain O(1) memory.
        if _write_tile_mp4(path, out_size, frames, fps) <= 0:
            _detach_entry(key, entry)
            return None
        with _registry_lock:
            if _registry.get(key) is entry:
                entry.ready = True

    reader = _attach_promoted_reader(source_path, out_size, saturation, key, entry, path)
    if reader is None:
        log.warning(f"Tile cache written for {source_path} but not readable back")
    return reader


def _attach_promoted_reader(source_path: str, out_size: tuple[int, int], saturation: float,
                            key: tuple[str, tuple[int, int], float, str], entry: "_TileCacheEntry", path: str) -> KeyVideoCache | None:
    """Return a promoted-cache reader, or drop the caller's reference and return None.
    Reject source fallback because externally composited GIFs must not use FFmpeg demux."""
    # Same balance as acquire(): the caller bumped the refcount before this
    # call, so a constructor raise has to give it back.
    try:
        reader = KeyVideoCache(source_path, out_size, saturation, cache_path=path, is_builder=False)
    except BaseException:
        _detach_entry(key, entry)
        raise
    reader._registry_key = key
    reader._registry_entry = entry
    if not reader.is_cache_complete():
        release(reader)
        return None
    return reader


def _write_tile_mp4(path: str, out_size: tuple[int, int], frames: "Iterable[Image.Image]", fps: float) -> int:
    """Atomically encode frames through a per-writer temporary file.
    Return the frame count or 0 on failure; cache failure must not fail the key."""
    written = 0
    tmp_path = f"{path}.{os.getpid()}-{threading.get_ident():x}.tmp.mp4"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        writer = cv2.VideoWriter(tmp_path, cv2.VideoWriter.fourcc(*"mp4v"), fps, out_size)
        if not writer.isOpened():
            log.warning(f"Could not open tile cache writer for {path}; playing uncached")
            return 0
        try:
            for frame in frames:
                # Resize only off-size frames. The even-dimension clamp mp4v
                # needs can leave an already-fitted frame a pixel off.
                if frame.size != out_size:
                    frame = frame.resize(out_size, Image.Resampling.LANCZOS)
                if frame.mode != "RGB":
                    frame = frame.convert("RGB")
                writer.write(cv2.cvtColor(np.asarray(frame), cv2.COLOR_RGB2BGR))
                written += 1
        finally:
            writer.release()
        if written == 0:
            return 0
        os.replace(tmp_path, path)
        log.info(f"Cached tile video ({written} frames, "
                 f"{os.path.getsize(path) / 1e6:.1f} MB): {path}")
        return written
    except Exception:
        log.opt(exception=True).warning(f"Failed to write tile video cache {path}")
        return 0
    finally:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def registry_cache_paths() -> set[str]:
    """Return cache paths used by all live registry readers and builders.
    The sweeper protects them even when source deletion prevents hash discovery."""
    with _registry_lock:
        return {entry.path for entry in _registry.values()}


def remove_cache_file_if_unreferenced(path: str) -> bool:
    """Atomically check live registry use and remove an unreferenced cache file.
    Return true if absent or removed, false if protected, and propagate removal errors."""
    with _registry_lock:
        if any(entry.path == path for entry in _registry.values()):
            return False
        try:
            os.remove(path)
        except FileNotFoundError:
            return True
        return True


def _run_builder(entry: _TileCacheEntry, source_path: str, out_size: tuple[int, int], saturation: float) -> None:
    # Construct inside try so finally clears a failed builder handle.
    # A stale dead handle would prevent every later acquisition from retrying.
    builder: "KeyVideoCache | None" = None
    failed = False
    try:
        builder = KeyVideoCache(source_path, out_size, saturation, cache_path=entry.path, is_builder=True)
        while not builder.is_cache_complete():
            if entry.stop_event.is_set():
                return
            if builder.n_frames <= 0:
                return
            builder.get_frame(builder.last_frame_index + 1)
            if builder.is_build_terminal():
                # Exit after terminal failure because repeated get_frame calls would busy-spin.
                # A fresh acquisition can retry once, while current playback remains uncached.
                log.error(
                    f"Tile cache build cannot complete for {source_path} -- "
                    f"leaving uncached playback"
                )
                return
        entry.ready = True
    except Exception:
        failed = True
        log.opt(exception=True).error(f"Tile cache builder failed for {source_path}")
    finally:
        if builder is not None:
            builder.close()
        # Clear only this thread's builder slot because invalidation can replace it.
        # Stamp failures so acquisition cooldown prevents rebuild on the next paint.
        with _registry_lock:
            if entry.builder_thread is threading.current_thread():
                entry.builder_thread = None
            if failed and not entry.ready:
                entry.last_build_failure = time.monotonic()
