"""Verify pooled LIFO decoding; targets removed before decode cancel decoding
and delivery, while targets removed after decode cancel only delivery."""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import gc

from src.backend.DeckManagement.Subclasses.byte_lru_cache import ByteLRUCache
from src.backend.deadline_pool import DeadlinePool
from src.windows.AssetManager.thumbnail_loader import (
    ThumbnailLoader,
    build_loader,
)


class FakePool:
    """Collect tasks for FIFO execution while each takes the newest decode."""

    def __init__(self):
        self.tasks: list = []
        self.shutdowns = 0

    def submit(self, fn):
        self.tasks.append(fn)
        return None

    def run_all(self):
        # Tasks submitted during this drain wait for the next call.
        pending, self.tasks = self.tasks, []
        for task in pending:
            task()

    def shutdown(self, wait=False, *, cancel_futures=False):
        self.shutdowns += 1


class FakeMainLoop:
    """Queue callbacks so decodes accumulate before a main-loop flush."""

    def __init__(self):
        self.queued: list = []
        self.calls = 0
        self.in_marshal = False

    def marshal(self, fn):
        self.calls += 1
        self.queued.append(fn)

    def pump(self):
        fns, self.queued = self.queued, []
        for fn in fns:
            self.in_marshal = True
            try:
                fn()
            finally:
                self.in_marshal = False


class Target:
    """A stand-in for a preview card. Identity is all the loader needs."""


class Recorder:
    """A fake decode and a fake apply that record what ran and when."""

    def __init__(self, main: FakeMainLoop):
        self.main = main
        self.decoded: list[str] = []
        self.delivered: list[tuple[object, bytes | None]] = []
        self.applied_off_main = False

    def decode(self, key: str) -> bytes | None:
        self.decoded.append(key)
        return f"pixels:{key}".encode()

    def apply(self, target, data) -> None:
        if not self.main.in_marshal:
            # The real path would touch a GTK widget off the main thread.
            self.applied_off_main = True
        self.delivered.append((target, data))


def make_loader(main: FakeMainLoop, recorder: Recorder, pool: FakePool,
                cache: ByteLRUCache) -> ThumbnailLoader:
    return ThumbnailLoader(decode=recorder.decode, apply=recorder.apply,
                           marshal=main.marshal, cache=cache, pool=pool)


def make_loader_fixture():
    main = FakeMainLoop()
    recorder = Recorder(main)
    pool = FakePool()
    cache = ByteLRUCache(max_bytes=8 * 1024 * 1024)
    loader = make_loader(main, recorder, pool, cache)
    return main, recorder, pool, cache, loader


def check_decode_is_deferred() -> int:
    """A miss submits to the pool instead of decoding on the caller's thread."""
    main, recorder, pool, cache, loader = make_loader_fixture()
    target = Target()
    loader.request("k", target)
    if recorder.decoded:
        print(f"FAIL(deferred): request() decoded inline ({recorder.decoded}); "
              f"the decode must go to the pool, off the main loop")
        return 1
    if len(pool.tasks) != 1:
        print(f"FAIL(deferred): request() queued {len(pool.tasks)} pool tasks, "
              f"expected 1")
        return 1
    pool.run_all()
    if recorder.decoded != ["k"]:
        print(f"FAIL(deferred): the pool task decoded {recorder.decoded}, "
              f"expected ['k']")
        return 1
    print("PASS: a decode is deferred to the pool, not run inline on the caller")
    return 0


def check_lifo_order() -> int:
    """The newest request decodes first."""
    main, recorder, pool, cache, loader = make_loader_fixture()
    targets = {name: Target() for name in ("a", "b", "c")}
    for name in ("a", "b", "c"):
        loader.request(name, targets[name])
    # Every request is pending before any task runs, so the tasks run FIFO but
    # each takes the newest entry: c, then b, then a.
    pool.run_all()
    if recorder.decoded != ["c", "b", "a"]:
        print(f"FAIL(lifo): decoded {recorder.decoded}, expected ['c', 'b', 'a'] "
              f"-- the newest request must decode first")
        return 1
    main.pump()
    order = [data for _target, data in recorder.delivered]
    if order != [b"pixels:c", b"pixels:b", b"pixels:a"]:
        print(f"FAIL(lifo): delivered in order {order}, expected c, b, a")
        return 1
    print("PASS: the newest request decodes and delivers first (LIFO)")
    return 0


def check_weakref_cancels_before_decode() -> int:
    """A target gone before its task runs takes no decode, delivery, or raise."""
    main, recorder, pool, cache, loader = make_loader_fixture()
    target = Target()
    loader.request("k", target)
    del target
    gc.collect()
    pool.run_all()
    main.pump()
    if recorder.decoded:
        print(f"FAIL(weakref-decode): decoded {recorder.decoded} for a gone "
              f"target -- a cancelled request must not decode")
        return 1
    if recorder.delivered:
        print(f"FAIL(weakref-decode): delivered {recorder.delivered} to a gone "
              f"target")
        return 1
    print("PASS: a target gone before its decode cancels the decode and the "
          "delivery, with no raise")
    return 0


def check_weakref_cancels_before_delivery() -> int:
    """A target gone after decode but before the flush takes no delivery."""
    main, recorder, pool, cache, loader = make_loader_fixture()
    target = Target()
    loader.request("k", target)
    pool.run_all()
    if recorder.decoded != ["k"]:
        print(f"FAIL(weakref-deliver): decoded {recorder.decoded}, expected "
              f"['k'] before the target was dropped")
        return 1
    del target
    gc.collect()
    main.pump()
    if recorder.delivered:
        print(f"FAIL(weakref-deliver): delivered {recorder.delivered} to a "
              f"target gone before the flush")
        return 1
    print("PASS: a target gone before the flush takes no delivery, with no raise")
    return 0


def check_cache_serves_repeat() -> int:
    """A repeated key is delivered from the cache without another decode."""
    main, recorder, pool, cache, loader = make_loader_fixture()
    first = Target()
    loader.request("k", first)
    pool.run_all()
    main.pump()

    second = Target()
    loader.request("k", second)
    if pool.tasks:
        print(f"FAIL(cache): a cache hit queued {len(pool.tasks)} pool tasks, "
              f"expected 0 -- the repeat must be served from the cache")
        return 1
    main.pump()

    if recorder.decoded != ["k"]:
        print(f"FAIL(cache): decoded {recorder.decoded}, expected one decode for "
              f"two requests of the same key")
        return 1
    delivered_targets = [t for t, _data in recorder.delivered]
    if delivered_targets != [first, second]:
        print(f"FAIL(cache): delivered to {delivered_targets}, expected both "
              f"targets")
        return 1
    print("PASS: a repeat is served from the cache without a second decode")
    return 0


def check_page_flip_cancels_pending() -> int:
    """A page flip cancels requests that have not decoded."""
    main, recorder, pool, cache, loader = make_loader_fixture()
    stale = Target()
    loader.request("stale", stale)
    loader.begin_generation()
    pool.run_all()
    main.pump()
    if recorder.decoded:
        print(f"FAIL(flip-pending): decoded {recorder.decoded} after a page flip "
              f"cleared the request")
        return 1
    if recorder.delivered:
        print(f"FAIL(flip-pending): delivered {recorder.delivered} after a page "
              f"flip")
        return 1
    print("PASS: a page flip cancels a request it overtook before its decode")
    return 0


def check_page_flip_drops_stale_result() -> int:
    """A page flip drops old decoded results but permits new requests."""
    main, recorder, pool, cache, loader = make_loader_fixture()
    old = Target()
    loader.request("old", old)
    pool.run_all()  # decoded under the old epoch, waiting for the flush
    loader.begin_generation()  # the page flips before the flush runs
    main.pump()
    if recorder.delivered:
        print(f"FAIL(flip-result): a result decoded before the flip painted "
              f"anyway ({recorder.delivered})")
        return 1

    new = Target()
    loader.request("new", new)
    pool.run_all()
    main.pump()
    delivered_targets = [t for t, _data in recorder.delivered]
    if delivered_targets != [new]:
        print(f"FAIL(flip-result): delivered to {delivered_targets}, expected "
              f"only the post-flip target")
        return 1
    print("PASS: a stale result does not paint over the new grid, a fresh one "
          "does")
    return 0


def check_batched_delivery() -> int:
    """A decode wave is delivered in one marshalled main-loop pass."""
    main, recorder, pool, cache, loader = make_loader_fixture()
    targets = [Target() for _ in range(5)]
    for i, target in enumerate(targets):
        loader.request(f"k{i}", target)
    pool.run_all()  # all five decode and queue for delivery
    if main.calls != 1:
        print(f"FAIL(batch): {main.calls} marshalled callbacks for five decodes, "
              f"expected 1 -- delivery must batch, not idle once per card")
        return 1
    main.pump()
    if len(recorder.delivered) != 5:
        print(f"FAIL(batch): delivered {len(recorder.delivered)} of 5 in the "
              f"batch")
        return 1
    if recorder.applied_off_main:
        print("FAIL(batch): a thumbnail was applied without going through the "
              "marshal -- an off-main widget touch on the real path")
        return 1
    print("PASS: a wave of decodes delivers in one marshalled pass, on the main "
          "loop")
    return 0


def check_real_pixbuf_roundtrip() -> int:
    """Serialization preserves all pixbuf bytes and dimensions, including a
    padded RGB rowstride."""
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    from src.windows.AssetManager.thumbnail_loader import _deserialize, _serialize

    for has_alpha in (False, True):
        source = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, has_alpha, 8,
                                      250, 180)
        # A fixed fill makes the pixel bytes deterministic. The high byte is the
        # alpha, unused without an alpha channel.
        source.fill(0x336699FF if has_alpha else 0x336699AA)

        if has_alpha is False and source.get_rowstride() == \
                source.get_width() * source.get_n_channels():
            print(f"NOTE(roundtrip): rowstride {source.get_rowstride()} is not "
                  f"padded on this build; the leg still checks the round trip "
                  f"but not the padding case")

        restored = _deserialize(_serialize(source))

        if restored.get_pixels() != source.get_pixels():
            print(f"FAIL(roundtrip has_alpha={has_alpha}): the pixel bytes "
                  f"changed across serialize and deserialize -- a rowstride the "
                  f"serializer got wrong corrupts the padded rows")
            return 1
        for name, want, got in (
            ("width", source.get_width(), restored.get_width()),
            ("height", source.get_height(), restored.get_height()),
            ("has_alpha", source.get_has_alpha(), restored.get_has_alpha()),
            ("n_channels", source.get_n_channels(), restored.get_n_channels()),
            ("rowstride", source.get_rowstride(), restored.get_rowstride()),
        ):
            if want != got:
                print(f"FAIL(roundtrip has_alpha={has_alpha}): {name} is {got}, "
                      f"expected {want}")
                return 1
    print("PASS: the real serialize and deserialize preserve a pixbuf byte for "
          "byte, padded rowstride and all")
    return 0


def check_real_wiring() -> int:
    """build_loader() wires the shared DeadlinePool and the shared ByteLRUCache,
    so the real path reuses those primitives."""
    loader = build_loader()
    if not isinstance(loader._pool, DeadlinePool):
        print(f"FAIL(wiring): the loader's pool is {type(loader._pool).__name__}, "
              f"expected DeadlinePool -- reuse the shutdown-disciplined primitive")
        return 1
    if not isinstance(loader._cache, ByteLRUCache):
        print(f"FAIL(wiring): the loader's cache is {type(loader._cache).__name__}, "
              f"expected the shared ByteLRUCache")
        return 1
    second = build_loader()
    if second._cache is not loader._cache or second._pool is not loader._pool:
        print("FAIL(wiring): two loaders got different cache or pool instances "
              "-- the cache and the pool must be shared, not per grid")
        return 1
    print("PASS: build_loader wires the shared DeadlinePool and ByteLRUCache")
    return 0


def main() -> int:
    fixtures.start_watchdog(60, label="scenario_thumbnail_loader")
    rc = 0
    rc |= check_decode_is_deferred()
    rc |= check_lifo_order()
    rc |= check_weakref_cancels_before_decode()
    rc |= check_weakref_cancels_before_delivery()
    rc |= check_cache_serves_repeat()
    rc |= check_page_flip_cancels_pending()
    rc |= check_page_flip_drops_stale_result()
    rc |= check_batched_delivery()
    rc |= check_real_pixbuf_roundtrip()
    rc |= check_real_wiring()
    if rc == 0:
        print("ALL PASS: scenario_thumbnail_loader")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
