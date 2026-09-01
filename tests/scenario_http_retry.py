"""Check 429, 502, and 503 retries, pooling, and atomic concurrent downloads.

Downloads use distinct sidecars and expose the target only after a complete transfer.
"""
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from src.backend import http_client


BODY = b"payload" * 4096  # about 28 KiB, several iter_content chunks


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 and Content-Length keep responses eligible for connection reuse.
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the scenario's output to its own PASS lines

    def _count(self) -> int:
        state = self.server.state
        with state["lock"]:
            state["seen"].append((self.path, self.client_address[1]))
            state["counts"][self.path] = state["counts"].get(self.path, 0) + 1
            return state["counts"][self.path]

    def _respond(self, status: int, body: bytes, retry_after: str = None) -> None:
        self.send_response(status)
        if retry_after is not None:
            self.send_header("Retry-After", retry_after)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        n = self._count()

        if self.path == "/ok":
            self._respond(200, b"ok")
        elif self.path == "/pooled":
            self._respond(200, b"{}")
        elif self.path == "/flaky":
            # 429, 429, then success, which is one retry budget's worth.
            if n > 2:
                self._respond(200, BODY)
            else:
                self._respond(429, b"slow down", retry_after="0")
        elif self.path == "/flaky-download":
            if n > 1:
                self._respond(200, BODY)
            else:
                self._respond(429, b"slow down", retry_after="0")
        elif self.path == "/asset":
            self._respond(200, BODY)
        elif self.path == "/retry-after":
            # A nonzero Retry-After distinguishes server delay from zero first backoff.
            if n > 1:
                self._respond(200, b"ok")
            else:
                self._respond(429, b"slow down", retry_after="1")
        elif self.path == "/always-429":
            self._respond(429, b"slow down", retry_after="0")
        elif self.path == "/truncated":
            # Declares more body than it sends, then drops the connection, so
            # the client hits an IncompleteRead partway through the stream.
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            try:
                self.wfile.write(BODY[:128])
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True
        else:
            self._respond(404, b"nope")


def _start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = {"lock": threading.Lock(), "counts": {}, "seen": []}
    threading.Thread(target=server.serve_forever, daemon=True, name="http-retry-server").start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def _count(server: ThreadingHTTPServer, path: str) -> int:
    with server.state["lock"]:
        return server.state["counts"].get(path, 0)


def _ports(server: ThreadingHTTPServer, path: str) -> list[int]:
    with server.state["lock"]:
        return [port for seen_path, port in server.state["seen"] if seen_path == path]


class _StubResponse:
    """Supply controlled chunks to model failed and overlapping streamed responses."""

    def __init__(self, chunks):
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = None):
        return self._chunks


def _sidecars(dir_path: str) -> list[str]:
    """The in-flight download sidecars sitting in dir_path."""
    return sorted(
        entry for entry in os.listdir(dir_path)
        if entry.startswith(http_client.SIDECAR_PREFIX) and entry.endswith(http_client.SIDECAR_SUFFIX)
    )


def _scratch_dir(name: str) -> str:
    """A private directory per leg, so a sidecar listing names only its own."""
    path = os.path.join(_SCRATCH, name)
    os.makedirs(path, exist_ok=True)
    return path


def test_retries_transient_429(server, base) -> None:
    start = time.monotonic()
    response = http_client.get(f"{base}/flaky", timeout=5)
    elapsed = time.monotonic() - start

    assert response.status_code == 200, (
        f"a 429 that clears on retry must surface as the eventual 200, got "
        f"{response.status_code}"
    )
    assert response.content == BODY, "retried response body did not arrive intact"
    assert _count(server, "/flaky") == 3, (
        f"expected 2 retries after the initial attempt, server saw "
        f"{_count(server, '/flaky')} requests"
    )
    # Two retries include one 1-second backoff; Retry-After zero uses that ladder.
    assert elapsed < 2.0, f"retrying one fetch took {elapsed:.2f}s -- backoff is unbounded"
    print("PASS: a transient 429 is retried and the eventual 200 is returned")


def test_respects_retry_after(server, base) -> None:
    start = time.monotonic()
    response = http_client.get(f"{base}/retry-after", timeout=5)
    elapsed = time.monotonic() - start

    assert response.status_code == 200
    assert _count(server, "/retry-after") == 2
    # A one-second first-retry delay can only come from the Retry-After header.
    assert elapsed >= 0.9, (
        f"retry fired after {elapsed:.2f}s despite Retry-After: 1 -- the "
        f"server's requested delay is being ignored"
    )
    print("PASS: a server-sent Retry-After is honored before retrying")


def test_exhausted_retries_return_response(server, base) -> None:
    """Check that exhausted status retries return the final response."""
    response = http_client.get(f"{base}/always-429", timeout=5)

    assert response.status_code == 429, (
        f"exhausted retries must yield the final response, got {response.status_code}"
    )
    assert _count(server, "/always-429") == 3, (
        f"expected 3 attempts (1 + 2 retries), server saw "
        f"{_count(server, '/always-429')}"
    )
    print("PASS: exhausted retries return the final 429 instead of raising")


def test_session_is_shared_and_pooled(server, base) -> None:
    assert http_client.get_session() is http_client.get_session(), (
        "get_session() must hand out one process-wide session"
    )

    http_client.get(f"{base}/ok", timeout=5).close()
    http_client.get(f"{base}/ok", timeout=5).close()

    ports = _ports(server, "/ok")
    assert len(ports) == 2, f"expected 2 requests to /ok, saw {len(ports)}"
    assert ports[0] == ports[1], (
        f"the two fetches arrived on different source ports ({ports}) -- the "
        f"connection is not being reused, so every store fetch still pays a "
        f"fresh TCP/TLS handshake"
    )
    print("PASS: consecutive fetches reuse one pooled connection")


def test_connect_failures_are_not_retried(server, base) -> None:
    """Check status retries with connect retries disabled for fast offline failure."""
    retry = http_client.get_session().adapters["https://"].max_retries
    assert retry.connect == 0, (
        f"connect retries must stay off, got connect={retry.connect!r} -- an "
        f"offline download now blocks for 3x its timeout"
    )
    assert retry.total == 2, f"status retry budget changed: total={retry.total!r}"

    # An unused local port exposes any added retry-backoff delay.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    start = time.monotonic()
    raised = None
    try:
        http_client.get(f"http://127.0.0.1:{dead_port}/", timeout=2)
    except Exception as e:
        raised = e
    elapsed = time.monotonic() - start

    assert raised is not None, "a refused connection must still raise"
    assert elapsed < 0.5, (
        f"a refused connection took {elapsed:.2f}s -- the connect error is "
        f"being retried through the backoff ladder"
    )
    print("PASS: connect errors fail on the first attempt (status retries intact)")


def test_non_200_returns_connection(server, base) -> None:
    """Check that non-200 bodies are consumed so the pooled socket remains reusable."""
    from src.backend.Store.StoreBackend import StoreBackend
    from src.backend.Store.store_result import StoreFetchError

    sb = StoreBackend.__new__(StoreBackend)  # skip __init__ (spawns threads)
    sb._fetch_limiter = threading.Semaphore(http_client.POOL_MAXSIZE)

    assert sb.request_from_url(f"{base}/pooled").status_code == 200
    # Drain the 404 body before StoreFetchError so the socket remains pooled.
    try:
        sb.request_from_url(f"{base}/absent")
        raise AssertionError("a 404 must raise StoreFetchError")
    except StoreFetchError:
        pass
    assert sb.request_from_url(f"{base}/pooled").status_code == 200

    ports = _ports(server, "/pooled") + _ports(server, "/absent")
    assert len(ports) == 3, f"expected 3 requests, saw {len(ports)}"
    assert len(set(ports)) == 1, (
        f"the fetches spanned {len(set(ports))} connections ({ports}) -- an "
        f"unread non-200 body tore the pooled connection down"
    )
    print("PASS: a non-200 store fetch keeps the pooled connection alive")


def test_download_writes_full_body(server, base) -> None:
    target = os.path.join(_SCRATCH, "download.bin")

    http_client.download_to_file(f"{base}/flaky-download", target, timeout=5)

    with open(target, "rb") as f:
        assert f.read() == BODY, "downloaded file does not match the served body"
    assert _count(server, "/flaky-download") == 2, "the download path did not retry the 429"
    print("PASS: download_to_file streams the whole body (retrying a 429)")


def test_download_leaves_no_partial_file(server, base) -> None:
    target = os.path.join(_SCRATCH, "partial.bin")

    raised = None
    try:
        http_client.download_to_file(f"{base}/truncated", target, timeout=5)
    except Exception as e:
        raised = e

    assert raised is not None, (
        "a connection dropped mid-body must raise, not report a short file as success"
    )
    assert not os.path.exists(target), (
        "a partial/zero-byte file was left behind after a broken transfer"
    )
    print("PASS: a broken transfer raises and leaves no partial file")


def test_download_rejects_error_status(server, base) -> None:
    """Reject HTTP error bodies instead of persisting them as assets."""
    target = os.path.join(_SCRATCH, "missing.bin")

    raised = None
    try:
        http_client.download_to_file(f"{base}/does-not-exist", target, timeout=5)
    except Exception as e:
        raised = e

    assert raised is not None, "an HTTP 404 must raise instead of writing the error body"
    assert not os.path.exists(target), "an HTTP error body was written to the target path"
    print("PASS: an HTTP error status raises and writes nothing")


def test_completed_download_leaves_no_sidecar(server, base) -> None:
    directory = _scratch_dir("complete")
    target = os.path.join(directory, "whole.bin")

    http_client.download_to_file(f"{base}/asset", target, timeout=5)

    with open(target, "rb") as f:
        assert f.read() == BODY, "the download did not land whole"
    assert sorted(os.listdir(directory)) == ["whole.bin"], (
        f"a successful download left more than its target behind: "
        f"{sorted(os.listdir(directory))}"
    )
    print("PASS: a completed download lands whole and its sidecar is gone")


def test_atomic_target_visibility() -> None:
    """Check target invisibility after a chunk lands but before a failed transfer ends."""
    directory = _scratch_dir("torn")
    target = os.path.join(directory, "asset.png")
    seen = {}

    def chunks():
        yield BODY[:128]
        seen["target_exists"] = os.path.exists(target)
        seen["sidecars"] = _sidecars(directory)
        raise ConnectionError("connection dropped mid-body")

    previous = http_client.get
    http_client.get = lambda url, **kwargs: _StubResponse(chunks())
    try:
        raised = None
        try:
            http_client.download_to_file("http://stub/asset.png", target, timeout=5)
        except Exception as e:
            raised = e
    finally:
        http_client.get = previous

    assert raised is not None, "a transfer that dies mid-body must raise"
    assert seen.get("target_exists") is False, (
        "the target path held a partial body while the download was still "
        "running -- a kill at that moment leaves a torn file behind"
    )
    assert len(seen.get("sidecars", [])) == 1, (
        f"expected the body to stream into exactly one sidecar, saw "
        f"{seen.get('sidecars')}"
    )
    assert not os.path.exists(target), "a partial body was moved onto the target"
    assert _sidecars(directory) == [], "the failed download left its own sidecar behind"
    print("PASS: the target path stays absent until the whole body arrived")


def test_sidecar_age_cleanup(server, base) -> None:
    """Check age-based cleanup of stale sidecars while preserving recent ones."""
    directory = _scratch_dir("orphans")
    target = os.path.join(directory, "asset.bin")
    stale = os.path.join(directory, f"{http_client.SIDECAR_PREFIX}5ta1e{http_client.SIDECAR_SUFFIX}")
    live = os.path.join(directory, f"{http_client.SIDECAR_PREFIX}11ve{http_client.SIDECAR_SUFFIX}")
    for path in (stale, live):
        with open(path, "wb") as f:
            f.write(b"orphaned body")
    aged = time.time() - http_client.STALE_SIDECAR_MAX_AGE - 60
    os.utime(stale, (aged, aged))

    http_client.download_to_file(f"{base}/asset", target, timeout=5)

    with open(target, "rb") as f:
        assert f.read() == BODY, "the retried download did not land whole"
    assert not os.path.exists(stale), (
        "an orphaned sidecar survived the next download -- killed transfers "
        "pile up in the cache directory"
    )
    assert os.path.exists(live), (
        "a sidecar written moments ago was reaped -- the age test is the only "
        "thing standing between a fresh orphan and an unlink"
    )
    os.remove(live)
    print("PASS: the next download reaps a stale sidecar and keeps a recent one")


def test_reaper_preserves_inflight_sidecar(server, base) -> None:
    """Check that the in-flight registry protects a live sidecar despite old mtime."""
    directory = _scratch_dir("in-flight")
    slow_url = "http://stub/slow.bin"
    slow_target = os.path.join(directory, "slow.bin")
    other_target = os.path.join(directory, "other.bin")
    payload = b"slow-body-" * 512
    aged = threading.Event()
    reaped = threading.Event()
    seen = {}

    def chunks():
        yield payload[:128]
        # The sidecar exists and holds a chunk. Backdate it past the age test,
        # so nothing but the in-flight register can save it now.
        seen["before_reap"] = _sidecars(directory)
        old = time.time() - http_client.STALE_SIDECAR_MAX_AGE - 60
        for name in seen["before_reap"]:
            os.utime(os.path.join(directory, name), (old, old))
        aged.set()
        reaped.wait(timeout=20)
        yield payload[128:]

    errors: list[BaseException] = []

    def run_slow() -> None:
        try:
            http_client.download_to_file(slow_url, slow_target, timeout=5)
        except BaseException as e:
            errors.append(e)

    previous = http_client.get

    def stub_get(url, **kwargs):
        # Only the slow transfer is stubbed. The second download runs for real
        # against the local server, so its reaper is the shipping one.
        if url == slow_url:
            return _StubResponse(chunks())
        return previous(url, **kwargs)

    http_client.get = stub_get
    try:
        worker = threading.Thread(target=run_slow, name="slow-download")
        worker.start()
        assert aged.wait(timeout=20), "the stubbed transfer never reached its first chunk"
        http_client.download_to_file(f"{base}/asset", other_target, timeout=5)
        reaped.set()
        worker.join(timeout=25)
    finally:
        http_client.get = previous
        reaped.set()

    assert not worker.is_alive(), "the slow download never finished"
    assert seen.get("before_reap") and len(seen["before_reap"]) == 1, (
        f"expected one sidecar while the slow transfer held it, saw "
        f"{seen.get('before_reap')}"
    )
    assert not errors, (
        f"a download running beside the reap died: {errors} -- its sidecar was "
        f"removed under it and the rename found nothing"
    )
    with open(slow_target, "rb") as f:
        assert f.read() == payload, "the slow download did not land whole"
    with open(other_target, "rb") as f:
        assert f.read() == BODY, "the download that ran the reaper did not land whole"
    assert _sidecars(directory) == [], "a sidecar survived both downloads"
    print("PASS: a running download keeps its sidecar through another download's reap")


def test_concurrent_downloads_take_separate_sidecars() -> None:
    """Check distinct sidecar names and payloads for concurrent downloads."""
    directory = _scratch_dir("concurrent")
    count = 4
    payloads = {f"http://stub/file-{i}.bin": f"body-{i}-".encode() * 512 for i in range(count)}
    barrier = threading.Barrier(count, timeout=20)
    peak: list[list[str]] = []
    peak_lock = threading.Lock()

    def chunks(payload: bytes):
        yield payload[:128]
        # The barrier exposes all sidecars after first chunks and before completion.
        barrier.wait()
        with peak_lock:
            peak.append(_sidecars(directory))
        yield payload[128:]

    errors: list[BaseException] = []

    def run(url: str) -> None:
        try:
            http_client.download_to_file(
                url, os.path.join(directory, os.path.basename(url)), timeout=5)
        except BaseException as e:  # a broken barrier must fail the leg, not hang it
            errors.append(e)

    previous = http_client.get
    http_client.get = lambda url, **kwargs: _StubResponse(chunks(payloads[url]))
    try:
        threads = [threading.Thread(target=run, args=(url,), name=f"dl-{i}")
                   for i, url in enumerate(payloads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)
    finally:
        http_client.get = previous

    assert not errors, f"a concurrent download failed: {errors}"
    assert all(not thread.is_alive() for thread in threads), "a download thread never finished"
    for url, payload in payloads.items():
        with open(os.path.join(directory, os.path.basename(url)), "rb") as f:
            landed = f.read()
        assert landed == payload, (
            f"{url} landed {len(landed)} bytes that do not match its body -- "
            f"two downloads wrote through one sidecar"
        )
    widest = max(peak, key=len)
    assert len(widest) == count, (
        f"{count} downloads were mid-body at once but {len(widest)} sidecars "
        f"existed ({widest}) -- two of them share a name"
    )
    assert _sidecars(directory) == [], "a sidecar survived a successful download"
    print("PASS: concurrent downloads take separate sidecars and land whole")


_SCRATCH = os.path.join(gl.DATA_PATH, "http-retry-scratch")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_http_retry")
    os.makedirs(_SCRATCH, exist_ok=True)
    server, base = _start_server()
    try:
        test_retries_transient_429(server, base)
        test_respects_retry_after(server, base)
        test_exhausted_retries_return_response(server, base)
        test_session_is_shared_and_pooled(server, base)
        test_connect_failures_are_not_retried(server, base)
        test_non_200_returns_connection(server, base)
        test_download_writes_full_body(server, base)
        test_download_leaves_no_partial_file(server, base)
        test_download_rejects_error_status(server, base)
        test_completed_download_leaves_no_sidecar(server, base)
        test_atomic_target_visibility()
        test_sidecar_age_cleanup(server, base)
        test_reaper_preserves_inflight_sidecar(server, base)
        test_concurrent_downloads_take_separate_sidecars()
    finally:
        server.shutdown()
        server.server_close()
    print("scenario_http_retry: PASS")


if __name__ == "__main__":
    main()
