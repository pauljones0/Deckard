import contextlib
from http.server import ThreadingHTTPServer
import os
import secrets
import threading

from loguru import logger as log

import globals as gl
from src.backend import ui_port
from src.backend.DeckManagement.deck_controller.controller import DeckController
from src.backend.DeckManagement.Subclasses.RemoteDeck import RemoteDeck
from src.backend.DeckManagement.Subclasses.RemoteDecksLocalServerHandler import create_handler

PORT = 8765
TOKEN_FILE_NAME = "remote_deck_token"
# Loopback is the default bind. The server carries real deck control, so it
# leaves the host only when the user sets this variable themselves.
LAN_ENV_VAR = "DECKARD_REMOTE_DECK_LAN"

from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from PIL import Image

    from src.backend.DeckManagement.DeckManager import DeckManager

class RemoteDeckManager:
    def __init__(self, deck_manager: "DeckManager", port: int = PORT):
        self.deck_manager = deck_manager
        self.port = port
        self.deck_controllers: list[DeckController] = []
        self.httpd: ThreadingHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        # create_handler builds the class at runtime, so Any is its real type.
        self.handler_class: Any = None
        self._is_running = False

    @property
    def token_path(self) -> str:
        return os.path.join(gl.DATA_PATH, TOKEN_FILE_NAME)

    def start(self) -> bool:
        """Bind the server and register the remote deck. Returns False and
        leaves no partial state when the bind fails, so an occupied port
        cannot abort startup or strand a true _is_running."""
        if self._is_running:
            return True
        try:
            self.start_server()
        except OSError as error:
            log.error(f"Remote Decks did not start: port {self.port} is unavailable ({error}). Continuing without Remote Decks.")
            self.handler_class = None
            self.httpd = None
            ui_port.get().notify_user(
                f"Port {self.port} is in use. Close the other program or change the port, then re-enable Remote Decks.",
                title="Remote Decks did not start")
            return False
        self._is_running = True

        deck = RemoteDeck(self, serial_number="remote-deck-1", deck_type="Remote Deck 1")
        self.deck_controllers.append(DeckController(self.deck_manager, deck))
        return True

    def stop(self) -> None:
        if not self._is_running:
            return
        self._is_running = False
        self.stop_server()

        self.deck_controllers.clear()


    def _write_token(self) -> str:
        """Generate this run's access token and persist it for the local
        client, readable by the owning user only. A new token per start keeps
        a leaked one from outliving the session."""
        token = secrets.token_urlsafe(32)
        fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        return token

    def start_server(self) -> None:
        """Bind and start the HTTP server on its own thread. Raises OSError
        when the bind fails; the caller owns the recovery."""
        token = self._write_token()
        host = "0.0.0.0" if os.environ.get(LAN_ENV_VAR) == "1" else "127.0.0.1"
        self.handler_class = create_handler(self, token)
        # Bind before any state commits. ThreadingHTTPServer keeps one slow
        # or held connection from blocking every other request.
        self.httpd = ThreadingHTTPServer((host, self.port), self.handler_class)

        log.info(f"Remote Decks server listening on {host}:{self.port}; every request needs the token from {self.token_path} in the X-Deckard-Token header; set {LAN_ENV_VAR}=1 before launch to serve the local network")

        # A separate thread runs the server, so it does not block the caller.
        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()

    def stop_server(self) -> None:
        """Stop the HTTP server and retire this run's token."""
        if self.httpd:
            log.info("Stopping the Remote Decks server")
            self.httpd.shutdown()
            self.httpd = None
            if self.server_thread:
                self.server_thread.join(timeout=5)
                self.server_thread = None
        with contextlib.suppress(OSError):
            os.remove(self.token_path)

    def on_key_event(self, key: int, state: bool) -> None:
        log.debug(f"Remote deck key event: {key}, {state}")
        for deck_controller in self.deck_controllers:
            raw_deck = deck_controller.deck.deck
            # The handle holds no callback until the controller installs its
            # key remapper. A client key event can arrive before that.
            if raw_deck.key_callback is not None:
                raw_deck.key_callback(raw_deck, key, state)

    def send_button_image(self, button_id: int, image: "Image.Image") -> None:
        """
        Send a PIL image for a specific button to the browser.

        Args:
            button_id: The button identifier (e.g., row * 5 + col)
            image: PIL Image object
        """
        if self.handler_class:
            self.handler_class.send_button_image(button_id, image)
