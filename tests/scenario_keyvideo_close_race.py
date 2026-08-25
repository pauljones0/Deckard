"""InputVideo.close() racing a concurrent get_next_frame().

A per-instance lock serializes the two, so close() waits for an in-flight
frame and no frame starts against a released reader.
"""
import os
import threading
import time

import fixtures

import cv2
import numpy as np

import globals as gl
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo


class RacyStubCache:
    """Mimics the KeyVideoCache surface InputVideo reads, with sleeps.

    The sleeps inside the accessors make a concurrent close() land
    mid-get_next_frame with high probability. It records use after release.
    """

    def __init__(self, n_frames: int = 10):
        self._n_frames = n_frames
        self.released = False
        self.calls_after_release = 0

    def _check(self):
        if self.released:
            self.calls_after_release += 1

    @property
    def n_frames(self) -> int:
        self._check()
        time.sleep(0.0002)
        return self._n_frames

    def is_cache_complete(self) -> bool:
        self._check()
        time.sleep(0.0002)
        return True

    def get_source_fps(self) -> float:
        self._check()
        return 30.0

    def get_frame(self, n: int):
        self._check()
        time.sleep(0.0002)
        return n

    # mp4_tile_cache.release(reader) calls reader.close(), and no registry
    # bookkeeping runs, because the registry attributes are absent.
    def close(self) -> None:
        self.released = True


def make_video(cache) -> InputVideo:
    v = InputVideo.__new__(InputVideo)
    v.fps = 30
    v.loop = True
    v.natural_speed = False
    v.active_frame = -1
    v._play_start = None
    v._last_frame_tick = None
    v.video_cache = cache
    v._close_lock = threading.Lock()  # __init__ sets this; __new__ bypasses it
    return v


def check_close_race_hammer(rounds: int = 150) -> None:
    for r in range(rounds):
        cache = RacyStubCache()
        video = make_video(cache)
        errors: list = []
        stop = threading.Event()

        def render_loop():
            try:
                while not stop.is_set():
                    video.get_next_frame()
            except Exception as e:  # noqa: BLE001  (records any exception)
                errors.append(e)

        t = threading.Thread(target=render_loop, name=f"hammer-{r}", daemon=True)
        t.start()
        # Let the renderer get mid-flight, then close underneath it. Vary the
        # delay, so close lands at different points of the body.
        time.sleep(0.0001 + (r % 7) * 0.0002)
        video.close()
        stop.set()
        t.join(timeout=5.0)
        assert not t.is_alive(), f"round {r}: render thread wedged (deadlock?)"

        # The historical failure was an AttributeError on NoneType.
        assert not errors, f"round {r}: get_next_frame raised under concurrent close: {errors[0]!r}"

        # Serialization, not just crash avoidance. Once close() released the
        # reader, no cache access may happen, because a straggler get_frame on
        # a released reader can resurrect and leak a capture through
        # _maybe_adopt_shared_cache.
        assert cache.calls_after_release == 0, (
            f"round {r}: {cache.calls_after_release} cache accesses after "
            f"release -- close() and get_next_frame() are not serialized"
        )

        # Post-close behavior. A quiet None, and an idempotent close.
        assert video.get_next_frame() is None
        video.close()

    print(f"PASS: close-vs-get_next_frame hammer ({rounds} rounds, no errors, no use-after-release)")


class _StubDeckControllerReal:
    def get_display_saturation(self) -> float:
        return 1.0


class _StubControllerInputReal:
    """Exactly what InputVideo.__init__ reads.

    Those are .deck_controller, through SingleKeyAsset, and get_image_size().
    """

    def __init__(self):
        self.deck_controller = _StubDeckControllerReal()

    def get_image_size(self) -> tuple[int, int]:
        return (48, 48)


def _make_test_video(path: str, n_frames: int = 20, size=(64, 64)) -> None:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, size)
    assert writer.isOpened(), f"could not open test video writer for {path}"
    for i in range(n_frames):
        frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        frame[:, :] = (10 * i % 255, 80, 200)
        writer.write(frame)
    writer.release()


def check_real_inputvideo_close() -> None:
    # The registry and cv2 tier. __init__ must wire the lock itself, where the
    # hammer above hand-sets it, and close() must run the real release path
    # while a ticker is mid-frame.
    fixtures.install_stub_globals()

    video_path = os.path.join(gl.DATA_PATH, "close_race_source.mp4")
    _make_test_video(video_path)

    video = InputVideo(
        controller_input=_StubControllerInputReal(),
        video_path=video_path, fps=30, loop=True,
    )
    assert video.get_next_frame() is not None, "sanity: a real frame decodes"

    errors: list = []
    stop = threading.Event()

    def render_loop():
        try:
            while not stop.is_set():
                video.get_next_frame()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=render_loop, name="real-hammer", daemon=True)
    t.start()
    time.sleep(0.05)
    video.close()
    stop.set()
    t.join(timeout=5.0)
    assert not t.is_alive(), "real-registry render thread wedged"
    assert not errors, f"real-registry close raced get_next_frame: {errors[0]!r}"
    assert video.video_cache is None
    assert video.get_next_frame() is None
    video.close()  # idempotent

    print("PASS: real-registry InputVideo close under concurrent ticks")


def live_tile_cache_builders() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "tile-cache-builder" and t.is_alive()]


def assert_no_tile_cache_builders(context: str) -> None:
    """No detached tile-cache builder may outlive the release that signalled it.

    release() sets the stop event and joins the builder, so this needs no wait
    of its own. A survivor here is a thread still inside cv2 with nothing left
    to join it: when the release is followed by process exit, the C++ runtime
    is destroyed under a live decode and the process aborts with "terminate
    called without an active exception" after every check has passed.
    """
    alive = live_tile_cache_builders()
    assert not alive, (
        f"{len(alive)} tile-cache-builder thread(s) still decoding after "
        f"{context} -- release() did not join the builder it signalled, and "
        f"they would be killed mid-cv2 at interpreter exit"
    )


def _enable_video_cache() -> None:
    gl.settings_manager.get_app_settings().setdefault("performance", {})["cache-videos"] = True


def check_release_joins_builder() -> None:
    """A release while the builder is mid-build joins it before it returns.

    The source is long enough that the build cannot finish inside the acquire,
    so the builder is provably still decoding at release time. Without the
    join in the detach path the thread is still alive the instant release()
    returns.
    """
    _enable_video_cache()
    from src.backend.DeckManagement.Subclasses import mp4_tile_cache

    # Long enough to still be building at release, small enough to stop within
    # one frame of the stop event.
    video_path = os.path.join(gl.DATA_PATH, "builder_join_source.mp4")
    _make_test_video(video_path, n_frames=900, size=(320, 240))

    reader = mp4_tile_cache.acquire(video_path, (72, 72))
    key = mp4_tile_cache._registry_key(video_path, (72, 72), 1.0)
    entry = mp4_tile_cache._registry.get(key)
    assert entry is not None, "acquire must register an entry"
    assert entry.builder_thread is not None, (
        "no builder started -- this check cannot prove anything about the join"
    )
    # Give the builder real work in flight, so the release below cannot land
    # before it started decoding.
    time.sleep(0.2)
    assert entry.builder_thread.is_alive(), (
        "the builder finished before the release -- the source is too short "
        "for this check"
    )

    started = time.monotonic()
    mp4_tile_cache.release(reader)
    elapsed = time.monotonic() - started

    assert_no_tile_cache_builders("a release taken while the builder was decoding")
    assert mp4_tile_cache._registry.get(key) is None, (
        "the last release must drop the registry entry"
    )
    assert elapsed < mp4_tile_cache._BUILDER_JOIN_TIMEOUT_S, (
        f"release() spent {elapsed:.2f}s joining a signalled builder; the stop "
        f"event is checked once per frame, so this must cost one frame decode"
    )
    print(f"PASS: release joins the builder it signalled ({elapsed * 1000:.0f} ms)")


def check_acquire_returns_refcount_on_reader_failure() -> None:
    """A reader constructor that raises must not strand the reference.

    acquire() bumps the refcount and can start a builder before it builds the
    reader. The constructor stats the source and makes the cache directory, so
    a source deleted since the hash, or an ENOSPC, raises there. An unbalanced
    bump leaves the entry pinned above zero forever: nothing ever signals its
    builder, which then decodes the whole source for a consumer that does not
    exist.
    """
    _enable_video_cache()
    from src.backend.DeckManagement.Subclasses import mp4_tile_cache

    video_path = os.path.join(gl.DATA_PATH, "reader_failure_source.mp4")
    _make_test_video(video_path, n_frames=900, size=(320, 240))

    real_cls = mp4_tile_cache.KeyVideoCache

    class FailingReader(real_cls):
        """Fails exactly like a reader constructor does, and leaves the builder
        alone. Patching the class outright would break the builder too, and
        then a dead builder could not tell an unbalanced refcount from a
        working one."""

        def __init__(self, *args, is_builder: bool = True, **kwargs):
            if is_builder:
                super().__init__(*args, is_builder=True, **kwargs)
                return
            raise OSError("simulated reader construction failure")

    mp4_tile_cache.KeyVideoCache = FailingReader
    try:
        raised = None
        try:
            mp4_tile_cache.acquire(video_path, (72, 72))
        except OSError as e:
            raised = e
        assert raised is not None, "acquire must re-raise the constructor failure"
    finally:
        mp4_tile_cache.KeyVideoCache = real_cls

    key = mp4_tile_cache._registry_key(video_path, (72, 72), 1.0)
    entry = mp4_tile_cache._registry.get(key)
    assert entry is None, (
        f"the failed acquire left its entry registered with refcount "
        f"{entry.refcount if entry is not None else '?'} -- the reference it "
        f"bumped was never returned, so this entry can never reach zero"
    )
    assert_no_tile_cache_builders("an acquire whose reader constructor raised")
    print("PASS: a failed reader construction returns its reference and stops the builder")


def main() -> None:
    fixtures.start_watchdog(60, "scenario_keyvideo_close_race")
    check_close_race_hammer()
    check_real_inputvideo_close()
    assert_no_tile_cache_builders("the real-registry InputVideo close")
    check_release_joins_builder()
    check_acquire_returns_refcount_on_reader_failure()
    print("PASS: scenario_keyvideo_close_race")


if __name__ == "__main__":
    main()
