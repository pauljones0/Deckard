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
import ctypes
import ctypes.util
import gc
import itertools
import os
import threading
import time
from typing import override

from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.Subclasses import cache_budget

# Keep smaps_rollup sampling at 60 seconds; each read holds mmap_lock.
# Measured cost reached 20 ms at 6.1 GB of VmData.
SAMPLE_INTERVAL = 60.0

# Trim only after 120 idle seconds and 600 seconds after the previous trim.
# MALLOC_ARENA_MAX=2 makes trimming hold a shared allocation lock.
IDLE_SECONDS = 120.0
MIN_TRIM_INTERVAL = 600.0

CSV_HEADER = (
    "timestamp,vmrss_kb,vmswap_kb,private_dirty_kb,threads,fds,gc0,gc1,gc2,"
    "page_switches,trim_ms,trim_rss_before_kb,trim_rss_after_kb,"
    "img_cache_kb,img_cache_evictions,img_cache_evicted_kb,"
    "video_readers_kb,gif_frames_kb\n"
)


class _PageSwitchCounter:
    """Count page switches lock-free across threads under the GIL.
    A torn counter and timestamp read affects only one diagnostic sample."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.value = 0
        self.last_switch_monotonic = time.monotonic()

    def bump(self) -> None:
        self.value = next(self._counter)
        self.last_switch_monotonic = time.monotonic()


page_switches = _PageSwitchCounter()


def _read_status_fields() -> tuple[int, int]:
    """Return (VmRSS, VmSwap) in kB from /proc/self/status."""
    vmrss = vmswap = 0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    vmrss = int(line.split()[1])
                elif line.startswith("VmSwap:"):
                    vmswap = int(line.split()[1])
    except OSError:
        pass
    return vmrss, vmswap


def _read_private_dirty_kb() -> int:
    """Private_Dirty from /proc/self/smaps_rollup, in kB."""
    try:
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                if line.startswith("Private_Dirty:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def _thread_count() -> int:
    try:
        return len(os.listdir("/proc/self/task"))
    except OSError:
        return threading.active_count()


def _fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def _image_cache_fields() -> tuple[int, int, int, int, int]:
    """Return cache kB, eviction count and kB, video-reader kB, and GIF-frame kB.
    Values are cheap counters; video readers and GIF frames are outside the ceiling."""
    try:
        totals = cache_budget.totals()
        evictions, evicted_bytes = cache_budget.eviction_stats()
        return (
            cache_budget.evictable_bytes() // 1024,
            evictions,
            evicted_bytes // 1024,
            totals.get("video_readers", 0) // 1024,
            totals.get("gif_frames", 0) // 1024,
        )
    except Exception as e:
        log.debug(f"mem_telemetry: image-cache fields unavailable: {e}")
        return 0, 0, 0, 0, 0


_libc = None


def _malloc_trim() -> None:
    """Call libc malloc_trim(0) only through the sampler idle and interval gate."""
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
    _libc.malloc_trim(ctypes.c_size_t(0))


class MemTelemetrySampler(threading.Thread):
    """Sample memory and run idle malloc_trim unless SC_MALLOC_TRIM=0.
    CSV needs SC_MEM_TELEMETRY; without it, cheap status reads log trim deltas."""

    def __init__(self) -> None:
        super().__init__(name="mem_telemetry", daemon=True)
        self._stop_event = threading.Event()
        self._trim_enabled = os.environ.get("SC_MALLOC_TRIM", "1") != "0"
        self._csv_enabled = bool(os.environ.get("SC_MEM_TELEMETRY"))
        self._last_trim_monotonic = 0.0
        logs_dir = os.path.join(gl.DATA_PATH, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        self.csv_path = os.path.join(logs_dir, "mem_telemetry.csv")
        if self._csv_enabled:
            self._ensure_header()

    def _ensure_header(self) -> None:
        """Write the header, rotating a nonempty file with another schema to .old.
        This prevents wider rows from misaligning existing columns."""
        try:
            if os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0:
                with open(self.csv_path) as f:
                    existing = f.readline()
                if existing == CSV_HEADER:
                    return
                os.replace(self.csv_path, self.csv_path + ".old")
                log.info(
                    f"mem_telemetry: column schema changed; rotated the old CSV to "
                    f"{self.csv_path}.old"
                )
            with open(self.csv_path, "a") as f:
                f.write(CSV_HEADER)
        except OSError as e:
            log.warning(f"mem_telemetry: could not prepare {self.csv_path}: {e}")

    def stop(self) -> None:
        self._stop_event.set()

    @override
    def run(self) -> None:
        while not self._stop_event.wait(SAMPLE_INTERVAL):
            try:
                self._sample()
            except Exception as e:
                log.debug(f"mem_telemetry: sample failed: {e}")

    def _idle(self) -> bool:
        return (time.monotonic() - page_switches.last_switch_monotonic) >= IDLE_SECONDS

    def _trim_due(self) -> bool:
        return (time.monotonic() - self._last_trim_monotonic) >= MIN_TRIM_INTERVAL

    def _maybe_trim(self, rss_before: int) -> tuple[str, str, str]:
        if not (self._trim_enabled and self._idle() and self._trim_due()):
            return "", "", ""
        t0 = time.perf_counter()
        try:
            _malloc_trim()
        except Exception as e:
            log.debug(f"mem_telemetry: malloc_trim failed: {e}")
            return "", "", ""
        duration_ms = (time.perf_counter() - t0) * 1000
        rss_after, _ = _read_status_fields()
        self._last_trim_monotonic = time.monotonic()
        log.info(f"mem_telemetry: malloc_trim took {duration_ms:.1f}ms, RSS {rss_before}->{rss_after}kB")
        return f"{duration_ms:.1f}", str(rss_before), str(rss_after)

    def _sample(self) -> None:
        vmrss, vmswap = _read_status_fields()
        trim_result = self._maybe_trim(vmrss)
        if not self._csv_enabled:
            return
        private_dirty = _read_private_dirty_kb()
        threads = _thread_count()
        fds = _fd_count()
        gc0, gc1, gc2 = gc.get_count()
        trim_ms, trim_before, trim_after = trim_result
        img_kb, evictions, evicted_kb, video_kb, gif_kb = _image_cache_fields()
        row = (
            f"{time.time():.0f},{vmrss},{vmswap},{private_dirty},{threads},{fds},"
            f"{gc0},{gc1},{gc2},{page_switches.value},{trim_ms},{trim_before},{trim_after},"
            f"{img_kb},{evictions},{evicted_kb},{video_kb},{gif_kb}\n"
        )
        with open(self.csv_path, "a") as f:
            f.write(row)


_sampler: MemTelemetrySampler | None = None


def start_if_enabled() -> None:
    """Start once unless trimming is disabled and CSV telemetry is unset.
    CSV recording additionally requires SC_MEM_TELEMETRY."""
    global _sampler
    if _sampler is not None:
        return
    trim_on = os.environ.get("SC_MALLOC_TRIM", "1") != "0"
    csv_on = bool(os.environ.get("SC_MEM_TELEMETRY"))
    if not trim_on and not csv_on:
        return
    _sampler = MemTelemetrySampler()
    _sampler.start()
    if csv_on:
        log.info(f"mem_telemetry: sampler started, writing to {_sampler.csv_path}")
    else:
        log.info("mem_telemetry: idle malloc_trim active (CSV off; enable with SC_MEM_TELEMETRY=1)")
