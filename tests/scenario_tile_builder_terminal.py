"""Verify that is_build_terminal ends tile-cache builders after promotion,
writer-open, or truncated-source failures."""

# A bounded join detects a builder that busy-spins instead of returning.
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import os
import threading
import time

import cv2
import numpy as np

import globals as gl
from fixtures import start_watchdog

from src.backend.DeckManagement.Subclasses import mp4_tile_cache as mtc

JOIN_TIMEOUT = 8.0  # a healthy builder exits in well under 1s, and a
                    # spinning one never exits, so this bounds the hang.


def make_mp4(path: str, n_frames: int = 10, size=(64, 64)) -> str:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, size)
    for i in range(n_frames):
        frame = np.full((size[1], size[0], 3), i * 20 % 255, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return path


def make_entry(cache_path: str):
    # A real _TileCacheEntry, so the builder's finally that clears the handle
    # and stamps a failure cooldown finds the fields it expects.
    entry = mtc._TileCacheEntry(cache_path)
    entry.ready = False
    entry.refcount = 1
    return entry


def run_builder_and_join(entry, source, out_size=(72, 72), saturation=1.0):
    """Start _run_builder and use a bounded join to detect a terminal spin."""
    t = threading.Thread(
        target=mtc._run_builder,
        args=(entry, source, out_size, saturation),
        daemon=True,
    )
    t.start()
    t.join(timeout=JOIN_TIMEOUT)
    return t


# A directory at the cache path makes end-of-source promotion fail.
def leg_promote_failure() -> int:
    source = make_mp4(os.path.join(gl.DATA_PATH, "source_promote.mp4"))

    cache_path = os.path.join(gl.DATA_PATH, "cache", "promote-target.mp4")
    os.makedirs(cache_path, exist_ok=True)  # a directory occupies the path

    entry = make_entry(cache_path)
    t = run_builder_and_join(entry, source)

    if t.is_alive():
        print("FAIL(1): builder thread still running after join -- busy-spinning "
              "on a terminal build (B-02, os.replace/promote failure)")
        return 1
    if entry.ready:
        print("FAIL(1): entry marked ready although promotion failed")
        return 1
    print("PASS(1): promote (os.replace) failure exits the builder thread")
    return 0


# A writer that never opens leaves no cache to promote.
class _DeadWriter:
    def isOpened(self):
        return False

    def write(self, *a, **k):  # never called, since it is never self._writer
        pass

    def release(self):
        pass


def leg_writer_open_fail() -> int:
    source = make_mp4(os.path.join(gl.DATA_PATH, "source_writer.mp4"))
    cache_path = os.path.join(gl.DATA_PATH, "cache", "writer-fail-target.mp4")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)

    entry = make_entry(cache_path)

    real_video_writer = mtc.cv2.VideoWriter

    def fake_video_writer(*a, **k):
        # Keep the builder's cache writer unopened.
        return _DeadWriter()

    mtc.cv2.VideoWriter = fake_video_writer
    try:
        t = run_builder_and_join(entry, source)
    finally:
        mtc.cv2.VideoWriter = real_video_writer

    if t.is_alive():
        print("FAIL(2): builder thread still running after join -- a build "
              "whose VideoWriter never opened busy-spun instead of exiting "
              "(B-02, writer-open failure)")
        return 1
    if entry.ready:
        print("FAIL(2): entry marked ready although the writer never opened")
        return 1
    if os.path.isfile(cache_path):
        print("FAIL(2): a cache file was promoted although the writer never opened")
        return 1
    print("PASS(2): VideoWriter-open failure exits the builder thread")
    return 0


# Model a source whose metadata promises frames but whose first read fails.
class _TruncatedCapture:
    """Report promised frames from an open capture whose reads always fail."""

    PROMISED = 60

    def __init__(self, *a, **k):
        pass

    def isOpened(self):
        return True

    def get(self, prop):
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return float(self.PROMISED)  # metadata promises 60...
        if prop == cv2.CAP_PROP_FPS:
            return 10.0
        return 0.0

    def set(self, *a, **k):
        return True

    def read(self):
        return (False, None)  # ...but the file is truncated to its header

    def release(self):
        pass


def leg_truncated_source() -> int:
    source = make_mp4(os.path.join(gl.DATA_PATH, "source_trunc.mp4"),
                      n_frames=10, size=(96, 96))

    cache_path = os.path.join(gl.DATA_PATH, "cache", "trunc-target.mp4")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)

    entry = make_entry(cache_path)

    real_capture = mtc.cv2.VideoCapture
    real_writer = mtc.cv2.VideoWriter

    def fake_capture(*a, **k):
        return _TruncatedCapture()

    # Avoid a real encoder file and keep the written-frame count at zero.
    mtc.cv2.VideoCapture = fake_capture
    mtc.cv2.VideoWriter = lambda *a, **k: _DeadWriter()
    try:
        t = run_builder_and_join(entry, source, out_size=(96, 96))
    finally:
        mtc.cv2.VideoCapture = real_capture
        mtc.cv2.VideoWriter = real_writer

    if t.is_alive():
        print("FAIL(3): builder thread still running after join -- a truncated "
              "source (metadata promises 60 frames, file delivers 0) busy-spun "
              "instead of exiting (B-02, truncated source)")
        return 1
    if entry.ready:
        print("FAIL(3): entry marked ready although the source delivered no frames")
        return 1
    if os.path.isfile(cache_path):
        print("FAIL(3): a cache file was promoted from a source that delivered "
              "no frames")
        return 1
    print("PASS(3): truncated source (over-promised metadata) exits the builder")
    return 0


def leg_failed_builder_is_retryable() -> int:
    # Construction failure must clear the handle and apply a retry cooldown.
    source = make_mp4(os.path.join(gl.DATA_PATH, "source_retry.mp4"))
    out_size = (72, 72)
    real_ctor = mtc.KeyVideoCache
    calls = {"n": 0}

    def failing_ctor(*a, **k):
        # Fail only the builder construction; the reader construction (the
        # acquire return) must still work, so gate on is_builder.
        if k.get("is_builder"):
            calls["n"] += 1
            raise RuntimeError("injected builder construction failure")
        return real_ctor(*a, **k)

    # Shrink the retry cooldown so the retry lands inside the scenario.
    real_cooldown = mtc._BUILD_RETRY_COOLDOWN_S
    mtc._BUILD_RETRY_COOLDOWN_S = 0.3
    mtc.KeyVideoCache = failing_ctor
    # acquire() gates on the cache-videos setting, which reads a settings
    # manager this scenario does not install. Force it on for the leg.
    real_enabled = mtc.cache_videos_enabled
    mtc.cache_videos_enabled = lambda: True
    readers = []
    try:
        readers.append(mtc.acquire(source, out_size))
        # The builder ran and failed; wait for it to clear its handle.
        key = mtc._registry_key(source, out_size, 1.0)
        entry = mtc._registry[key]
        for _ in range(200):
            if entry.builder_thread is None and calls["n"] >= 1:
                break
            time.sleep(0.01)
        if entry.builder_thread is not None:
            print("FAIL(retry): a failed builder left its handle set, so no "
                  "acquire can restart it")
            return 1
        if entry.last_build_failure == 0.0:
            print("FAIL(retry): a failed build did not stamp the cooldown")
            return 1

        # Within the cooldown, a second acquire does not restart the builder.
        before = calls["n"]
        readers.append(mtc.acquire(source, out_size))
        if calls["n"] != before:
            print("FAIL(retry): the cooldown did not hold off the retry")
            return 1

        # After the cooldown, a fresh acquire retries the build.
        time.sleep(0.35)
        readers.append(mtc.acquire(source, out_size))
        for _ in range(200):
            if calls["n"] > before:
                break
            time.sleep(0.01)
        if calls["n"] <= before:
            print("FAIL(retry): the builder never retried after the cooldown")
            return 1
    finally:
        mtc.KeyVideoCache = real_ctor
        mtc._BUILD_RETRY_COOLDOWN_S = real_cooldown
        mtc.cache_videos_enabled = real_enabled
        for reader in readers:
            mtc.release(reader)
    print("PASS(retry): a failed builder clears its handle and retries after a cooldown")
    return 0


def leg_lingering_list_prunes() -> int:
    # A finished builder thread must not sit in the lingering list until quit.
    mtc._lingering_builders.clear()
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    mtc._lingering_builders.append(dead)

    # A live builder that outlives its join triggers the prune on append.
    hold = threading.Event()
    live = threading.Thread(target=lambda: hold.wait(5))
    live.start()
    try:
        mtc._join_builder(live, timeout=0.05)  # outlives the join -> appended, prunes dead
        if dead in mtc._lingering_builders:
            print("FAIL(prune): a finished builder was not pruned on append")
            return 1
        if live not in mtc._lingering_builders:
            print("FAIL(prune): the live builder was not recorded")
            return 1
    finally:
        hold.set()
        live.join(timeout=5)
        mtc._lingering_builders.clear()
    print("PASS(prune): the lingering list prunes finished builders on append")
    return 0


def main() -> int:
    start_watchdog(40, "tile_builder_terminal")

    rc = 0
    rc |= leg_promote_failure()
    rc |= leg_writer_open_fail()
    rc |= leg_truncated_source()
    rc |= leg_failed_builder_is_retryable()
    rc |= leg_lingering_list_prunes()
    if rc == 0:
        print("PASS: scenario_tile_builder_terminal")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
