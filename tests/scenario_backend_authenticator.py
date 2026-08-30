"""Frontend rpyc servers accept only same-UID loopback peers.
Authentication precedes the protocol, so refusal exchanges no protocol data and legitimate backend children need no cooperation."""
import os
import threading
import time
import types

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl

# PluginManager pulls PluginBase, whose module import reads these registries.
gl.plugin_manager = types.SimpleNamespace(backends=[], backend_processes=[])

import rpyc  # noqa: E402
from rpyc.utils.server import ThreadedServer  # noqa: E402

from src.backend.PluginManager.PluginManager import frontend_authenticator  # noqa: E402
from src.backend.PluginManager.backend_guard import deckard_rpyc_guard as guard  # noqa: E402


class EchoService(rpyc.Service):
    def marker(self) -> str:
        return "frontend-alive"


def _connect(port: int, tries: int = 100, delay: float = 0.05) -> "rpyc.Connection":
    """Retry the connection while the daemon accept loop starts.
    The tries * delay bound stays below the watchdog and covers load-sensitive socket refusals."""
    last: Exception | None = None
    for _ in range(tries):
        try:
            return rpyc.connect("localhost", port, config={"allow_public_attrs": True})
        except (ConnectionError, OSError, EOFError) as e:
            last = e
            time.sleep(delay)
    raise AssertionError(f"server never accepted a connection within {tries * delay:.1f}s: {last!r}")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_backend_authenticator")

    server = ThreadedServer(EchoService(), hostname="localhost", port=0,
                            protocol_config={"allow_public_attrs": True},
                            authenticator=frontend_authenticator)
    threading.Thread(target=server.start, name="test_frontend", daemon=True).start()

    # A same-UID backend must pass; retry because a busy host can schedule the daemon accept loop late.
    connection = _connect(server.port)
    assert connection.root.marker() == "frontend-alive"
    connection.close()
    print("PASS: a same-uid loopback client connects through the authenticator")

    # Substitute a foreign UID because the suite has one real UID; refusal must close before the protocol and appear client-side.
    real = guard.uid_of_peer
    guard.uid_of_peer = lambda s: os.getuid() + 1
    try:
        refused = False
        try:
            connection = rpyc.connect("localhost", server.port,
                                      config={"allow_public_attrs": True})
            connection.root.marker()
        except (EOFError, ConnectionError, OSError):
            refused = True
        assert refused, "a foreign-uid peer was served"
    finally:
        guard.uid_of_peer = real
    print("PASS: a foreign-uid peer is refused before the protocol starts")

    # The refusal leaves the server serving: the next legitimate connect works.
    connection = _connect(server.port)
    assert connection.root.marker() == "frontend-alive"
    connection.close()
    server.close()
    print("PASS: the server keeps serving after a refusal")

    print("PASS: scenario_backend_authenticator")


if __name__ == "__main__":
    main()
