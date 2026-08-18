"""The frontend rpyc servers accept only same-UID loopback peers.

frontend_authenticator runs on the accepted socket before the rpyc protocol
starts, so a refusal closes the socket with no protocol exchange and a
legitimate backend child needs no cooperation to pass.
"""
import os
import threading
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


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_backend_authenticator")

    server = ThreadedServer(EchoService(), hostname="localhost", port=0,
                            protocol_config={"allow_public_attrs": True},
                            authenticator=frontend_authenticator)
    threading.Thread(target=server.start, name="test_frontend", daemon=True).start()

    # The same uid passes: this mirrors the unmodified backend child.
    connection = rpyc.connect("localhost", server.port, config={"allow_public_attrs": True})
    assert connection.root.marker() == "frontend-alive"
    connection.close()
    print("PASS: a same-uid loopback client connects through the authenticator")

    # A foreign uid is refused before the protocol starts. The uid lookup is
    # swapped because this suite runs under one real uid; the refusal then
    # surfaces client-side as a closed socket, not as a served connection.
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
    connection = rpyc.connect("localhost", server.port, config={"allow_public_attrs": True})
    assert connection.root.marker() == "frontend-alive"
    connection.close()
    server.close()
    print("PASS: the server keeps serving after a refusal")

    print("PASS: scenario_backend_authenticator")


if __name__ == "__main__":
    main()
