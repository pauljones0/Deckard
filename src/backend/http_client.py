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

The shared HTTP client is one process-wide requests.Session behind a retrying
adapter. Every outbound fetch uses it, which covers the store catalog, the
install archives, the asset URL imports, and the contributor list of the About
dialog.

The session gives connection reuse. A store page load issues about 150 small
requests to raw.githubusercontent.com. The top-level requests.get() builds a
throwaway Session per call, which costs a fresh TCP and TLS handshake per
fetch. A pooled Session spreads one handshake across the catalog load.

The session also holds the one retry policy. GitHub rate-limits by IP with a
429, and the store answers that with the stale-cache fallback in
StoreBackend.get_remote_file.

This module builds the session once and never mutates it, so the store
prepare pool, the UI install threads and the asset-manager worker thread can
share it. The connection pools of urllib3 and the cookie jar of requests are
each thread-safe; a concurrent reconfiguration of a Session is not.
"""
import contextlib
import os
import threading
import time
import uuid

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry  # requests.adapters re-exports this; import it canonically

# Connection-pool size for the shared session. It must stay at or above the
# store's concurrent-fetch cap, so every in-flight fetch holds a live
# keep-alive connection. StoreBackend.MAX_CONCURRENT_REQUESTS aliases this
# value, so the two cannot drift apart.
POOL_MAXSIZE = 10

# Name parts of the sidecar a download streams into. The name is hidden, it
# carries a fixed prefix and suffix so the reaper recognizes one, and it takes
# a random middle so two downloads never share a sidecar, whatever their target
# names are.
SIDECAR_PREFIX = ".download."
SIDECAR_SUFFIX = ".part"

# A hard kill mid-download orphans a sidecar, and nothing else cleans the cache
# directories. The next download into the same directory removes one whose last
# write is older than this (seconds). The age is a backstop for a sidecar no
# live download owns, such as one an earlier run left when it was killed. It is
# no guard for a running transfer: writes are buffered, so mtime stands still
# between flushes and a body that trickles in under the buffer size reads as
# untouched however long it runs, and a clock step forward ages every sidecar at
# once. _in_flight_sidecars is what keeps a running download's file.
STALE_SIDECAR_MAX_AGE = 60 * 60

# The sidecars this process is writing right now, as absolute paths. The reaper
# skips these whatever their age. Registration happens before the file is
# created and lasts until the download published or removed it.
_in_flight_sidecars: set[str] = set()
_in_flight_lock = threading.Lock()

_session: requests.Session | None = None
_session_lock = threading.Lock()


def get_session() -> requests.Session:
    """The process-wide session, built on first use.

    The retry policy is 2 retries (3 attempts) on 429, 502 and 503, with a
    0.5s backoff factor, and it obeys a server-sent Retry-After. raise_on_status
    stays off, so an exhausted retry budget returns the final response instead
    of raising. Each call site then keeps its own status handling, and the
    store keeps its stale-cache fallback.

    connect=0 takes connect errors out of the budget that total covers. This
    policy retries a status. A retry of a failed CONNECT gains nothing,
    because a black-holed or down host stays down for those seconds. It also
    costs three times the wall clock on every offline failure, so a 10s asset
    download becomes a 31s block. KeyGrid's GTK drop handler calls
    HelperMethods.download_file synchronously on the main thread. Read errors
    stay retryable, because those are transient.
    """
    global _session
    with _session_lock:
        if _session is None:
            retry = Retry(
                total=2,
                connect=0,
                backoff_factor=0.5,
                status_forcelist=(429, 502, 503),
                allowed_methods=frozenset({"GET"}),
                raise_on_status=False,
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_maxsize=POOL_MAXSIZE)
            session = requests.Session()
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            _session = session
    return _session


def get(url: str, *, timeout: float, stream: bool = False) -> requests.Response:
    """GET url through the shared session.

    timeout is keyword-only and required, so every HTTP call passes an
    explicit timeout. A request without a timeout parks its worker thread on a
    black-holed connection. A required argument holds that rule for a new call
    site too.
    """
    return get_session().get(url, timeout=timeout, stream=stream)


def _reap_stale_sidecars(dir_path: str) -> None:
    """Remove download sidecars that a hard kill orphaned in this directory.

    Only the names this module writes count. A sidecar a download of this
    process holds open is skipped whatever its age, which is the guard that
    protects a running transfer; the age test then covers what is left, and
    what is left has no owner alive to break. An unlink race with another
    reaper is harmless, and every filesystem error is swallowed, because a
    failed tidy-up must not break the download that triggered it.
    """
    try:
        entries = os.listdir(dir_path)
    except OSError:
        return
    with _in_flight_lock:
        live = set(_in_flight_sidecars)
    now = time.time()
    for entry in entries:
        if not (entry.startswith(SIDECAR_PREFIX) and entry.endswith(SIDECAR_SUFFIX)):
            continue
        path = os.path.join(dir_path, entry)
        if os.path.abspath(path) in live:
            continue
        try:
            if now - os.stat(path).st_mtime > STALE_SIDECAR_MAX_AGE:
                os.remove(path)
        except OSError:
            pass


def download_to_file(url: str, target_path: str, *, timeout: float = 30, chunk_size: int = 8192) -> None:
    """Stream url into target_path through the shared session.

    Raises the usual requests exceptions on a network error and on an HTTP
    error status, and OSError when the directory, the sidecar or the rename
    fails. An error page therefore never lands on disk as the requested file.

    The body streams into a sidecar in the target's own directory, and only a
    complete, status-checked body moves onto target_path, with os.replace. Same
    directory means same filesystem, which is what keeps that rename atomic.
    A killed process therefore leaves no torn file under the name a cache
    lookup trusts, and a transfer failure the client detects publishes nothing.

    What lands is only as complete as what the server framed. A body with a
    declared length that falls short, and a truncated chunked body, both raise
    and publish nothing. A server that instead declares no length and closes
    the connection early ends the stream cleanly, requests reports success, and
    that short body is published like any other. No client can tell those
    apart, so this guards a killed process and a detected failure, not every
    truncation.

    A symlink at target_path is replaced rather than written through, so a
    stale link in a cache directory cannot redirect a download out of it.

    This does not fsync, which is the case it guards: a rename carries a
    completed write past a killed process without one. It orders nothing
    against power loss, where the plausible state at target_path is a
    zero-length or holed file. These targets are cache files that a re-download
    replaces, so the flush a large media download would cost is not paid here.
    """
    directory = os.path.dirname(target_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
        # Reap only in a directory the caller named. A bare relative target
        # lands in the working directory of the process, which is nobody's
        # cache, so this sweeps no names there.
        _reap_stale_sidecars(directory)
    else:
        directory = "."

    sidecar = os.path.join(directory, f"{SIDECAR_PREFIX}{uuid.uuid4().hex}{SIDECAR_SUFFIX}")
    # Register before the file exists. A reaper that lists the directory in
    # between then finds no sidecar to consider, and one that lists it later
    # finds it registered.
    with _in_flight_lock:
        _in_flight_sidecars.add(os.path.abspath(sidecar))
    try:
        # Call the module-level get(), so a test that patches http_client.get
        # covers this path too.
        with get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with open(sidecar, "wb") as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    f.write(chunk)
        os.replace(sidecar, target_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(sidecar)
        raise
    finally:
        with _in_flight_lock:
            _in_flight_sidecars.discard(os.path.abspath(sidecar))
