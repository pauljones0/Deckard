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
from urllib3.util.retry import Retry  # Canonical source; requests.adapters re-exports it

# Keep this at or above the store fetch limit to preserve live connections.
# StoreBackend.MAX_CONCURRENT_REQUESTS aliases this value.
POOL_MAXSIZE = 10

# Hidden fixed sidecar pattern for reaping, with a random collision guard.
SIDECAR_PREFIX = ".download."
SIDECAR_SUFFIX = ".part"

# Reap sidecars older than one hour unless this process owns them.
# mtime alone cannot protect buffered transfers or clock jumps.
STALE_SIDECAR_MAX_AGE = 60 * 60

# Absolute sidecars owned by active downloads; reapers skip them at any age.
# Register before creation and remove after publish or cleanup.
_in_flight_sidecars: set[str] = set()
_in_flight_lock = threading.Lock()

_session: requests.Session | None = None
_session_lock = threading.Lock()


def get_session() -> requests.Session:
    """Return the lazy session with two GET retries and 0.5 backoff for 429, 502, and 503.
    Honor Retry-After, retry reads but not connects, and return the final response."""
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
    """GET through the shared session with a required keyword-only timeout.
    The timeout prevents a black-holed connection from parking its worker."""
    return get_session().get(url, timeout=timeout, stream=stream)


def _reap_stale_sidecars(dir_path: str) -> None:
    """Remove stale module-owned sidecars that no active download owns.
    Ignore unlink races and filesystem errors so cleanup cannot break a download."""
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
    """Stream through a same-directory sidecar; network, HTTP, and filesystem errors publish nothing and propagate.
    Replacement is atomic and replaces symlinks, but unframed truncation can pass and no fsync protects power loss."""
    directory = os.path.dirname(target_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
        # Reap only an explicit target directory.
        # A bare relative target must not sweep the process working directory.
        _reap_stale_sidecars(directory)
    else:
        directory = "."

    sidecar = os.path.join(directory, f"{SIDECAR_PREFIX}{uuid.uuid4().hex}{SIDECAR_SUFFIX}")
    # Register before creation so reapers see either no file or an owned file.
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
