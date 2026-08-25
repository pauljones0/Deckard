"""
Regression test for the store fan-out pool on the quit path.

No network is involved.
"""

# The catalog prepare tasks run on StoreBackend._prepare_pool, whose workers
# are not daemons and park on the work queue between passes. The quit path
# joins every non-daemon thread with a bound, so a pool that nothing releases
# costs that bound on every quit after the first catalog pass, and the
# force-quit backstop after it. shutdown() releases the pool and stops a pass
# that is in flight before its next fetch.
import threading
import time

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

import src.backend.Store.StoreBackend as sb_module
from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.prepare_pool import PreparePool
from src.backend.Store.store_result import StoreFetchError

# What the quit path allows one thread, well under its own 5 s join bound.
JOIN_BOUND_S = 2.0


class FakeResponse:
    """What the store fetch path reads off a response."""
    status_code = 200
    content = b"[]"

    def json(self):
        return []

    def close(self):
        pass


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__, which spawns a fetch thread
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    sb.official_authors = []
    sb._fetch_limiter = threading.Semaphore(StoreBackend.MAX_CONCURRENT_REQUESTS)
    sb._prepare_pool = PreparePool(StoreBackend.MAX_CONCURRENT_REQUESTS)
    return sb


def _pool_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("store-prepare")]


def test_shutdown_ends_idle_workers() -> None:
    """A worker that ran one task and went idle must end when shutdown()
    runs, so the quit path's join meets it at once."""
    sb = _make_backend()
    sb._prepare_pool.submit(lambda: None).result()

    workers = _pool_threads()
    assert workers, "the pass must leave at least one pool worker to join"
    assert all(not t.daemon for t in workers), (
        "pool workers are not daemons, so the quit path joins them"
    )

    sb.shutdown()

    deadline = time.monotonic() + JOIN_BOUND_S
    for worker in workers:
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
    alive = [t.name for t in workers if t.is_alive()]
    assert not alive, (
        f"pool workers must end within {JOIN_BOUND_S}s of shutdown, still alive: {alive}"
    )


def test_shutdown_stops_a_pass_in_flight() -> None:
    """A prepare task that is running when the app quits must not start
    another fetch. Its next request raises and reaches no network."""
    calls: list[str] = []

    def recording_get(url, stream=False, timeout=30):
        calls.append(url)
        return FakeResponse()

    real_get = sb_module.http_client.get
    sb_module.http_client.get = recording_get
    try:
        sb = _make_backend()
        sb.request_from_url("https://example.invalid/before")
        assert calls == ["https://example.invalid/before"], (
            f"the fetch path must reach http_client before shutdown, got {calls}"
        )

        sb.shutdown()
        try:
            sb.request_from_url("https://example.invalid/after")
        except StoreFetchError:
            pass
        else:
            raise AssertionError("a fetch after shutdown must raise StoreFetchError")
        assert calls == ["https://example.invalid/before"], (
            f"a fetch after shutdown must reach no network, got {calls}"
        )
    finally:
        sb_module.http_client.get = real_get


def test_submit_after_shutdown_is_cancelled() -> None:
    """A pass that submits into the released pool must collect a cancelled
    future rather than a RuntimeError out of the executor."""
    sb = _make_backend()
    sb.shutdown()

    future = sb._prepare_pool.submit(lambda: "ran")
    assert future.cancelled(), f"submit after shutdown must hand back a cancelled future, got {future!r}"


def test_shutdown_blocks_a_new_catalog_pass() -> None:
    """A catalog pass that starts after shutdown must report no catalog and
    reach the network for nothing."""
    calls: list[str] = []

    def recording_get(url, stream=False, timeout=30):
        calls.append(url)
        return FakeResponse()

    real_get = sb_module.http_client.get
    sb_module.http_client.get = recording_get
    try:
        sb = _make_backend()
        sb.get_stores = lambda: [("https://github.com/Example/Store", "main")]
        sb.shutdown()
        result = sb.process_store_data("plugins.json", sb.prepare_plugin, None, object)
        assert result is None, f"a pass after shutdown must report no catalog, got {result!r}"
        assert not calls, f"a pass after shutdown must fetch nothing, got {calls}"
    finally:
        sb_module.http_client.get = real_get


def test_quit_path_calls_shutdown_before_the_join() -> None:
    """The teardown must release the pool before it flushes the store cache
    index and before it joins the non-daemon threads. A later release lets a
    pass in flight dirty the index after the flush wrote it, and a release
    after the join loop stops nothing."""
    import os
    source_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "app.py")
    with open(source_path, encoding="utf-8") as source_file:
        source = source_file.read()

    call_at = source.find("store_backend.shutdown()")
    flush_at = source.find("store_cache.flush_index()")
    join_at = source.find("did not exit in time")
    assert call_at != -1, "the quit path must release the store fan-out pool"
    assert flush_at != -1, "the quit path's cache flush moved; update this test"
    assert join_at != -1, "the quit path's bounded join loop moved; update this test"
    assert call_at < flush_at, (
        "the store pool must be released before the quit path flushes the cache index"
    )
    assert call_at < join_at, (
        "the store pool must be released before the quit path joins the threads"
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_quit_pool")
    test_shutdown_ends_idle_workers()
    test_shutdown_stops_a_pass_in_flight()
    test_submit_after_shutdown_is_cancelled()
    test_shutdown_blocks_a_new_catalog_pass()
    test_quit_path_calls_shutdown_before_the_join()
    print("scenario_store_quit_pool: PASS")


if __name__ == "__main__":
    main()
