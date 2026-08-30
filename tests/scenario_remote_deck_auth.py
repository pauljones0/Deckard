"""Require a valid token on every endpoint without events or image leaks on failure.
Reject oversized bodies; a failed bind must keep the manager stopped and retryable."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import http.client  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

import globals as gl  # noqa: F401, E402  (resolves DATA_PATH for the manager leg)
from src.backend.DeckManagement.Subclasses.RemoteDecksLocalServerHandler import create_handler  # noqa: E402

TOKEN = "scenario-token-123"


class RecordingManager:
    def __init__(self):
        self.key_events = []

    def on_key_event(self, key, state):
        self.key_events.append((key, state))


def request(port, method, path, token=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if token is not None:
        headers["X-Deckard-Token"] = token
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def handler_leg():
    manager = RecordingManager()
    handler = create_handler(manager, TOKEN)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # No token: 401 on every data endpoint, and no key event runs.
        for method, path, body in (
            ("GET", "/status", None),
            ("GET", "/images", None),
            ("GET", "/images/0", None),
            ("POST", "/button", {"type": "down", "row": 0, "col": 0}),
        ):
            status, data = request(port, method, path, body=body)
            assert status == 401, f"{method} {path} without token: {status}"
            assert b"images" not in data or b"data:image" not in data
        assert manager.key_events == [], "an unauthenticated request drove a key event"

        # Wrong token: same refusal.
        status, _ = request(port, "POST", "/button", token="wrong",
                            body={"type": "down", "row": 0, "col": 0})
        assert status == 401, f"wrong token: {status}"
        assert manager.key_events == []

        # Right token: the event lands.
        status, _ = request(port, "POST", "/button", token=TOKEN,
                            body={"type": "down", "row": 1, "col": 2})
        assert status == 200, f"valid token: {status}"
        assert manager.key_events == [(7, True)], manager.key_events

        # Right token via Authorization: Bearer works too.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert conn.getresponse().status == 200
        conn.close()

        # Oversized body: refused before parsing, no event.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/button", body=b"x",
                     headers={"X-Deckard-Token": TOKEN,
                              "Content-Length": str(10 * 1024 * 1024)})
        assert conn.getresponse().status == 413
        conn.close()
        assert manager.key_events == [(7, True)]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def manager_leg():
    from src.backend.DeckManagement.Subclasses.RemoteDeckManager import RemoteDeckManager

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        manager = RemoteDeckManager(deck_manager=None, port=port)  # type: ignore[arg-type]  # the failure path touches no deck_manager attribute
        assert manager.start() is False, "start() claimed success on an occupied port"
        assert manager._is_running is False
        assert manager.httpd is None
        assert manager.deck_controllers == []
    finally:
        blocker.close()


handler_leg()
manager_leg()
print("scenario_remote_deck_auth: OK")
