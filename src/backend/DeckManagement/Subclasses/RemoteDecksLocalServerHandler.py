from http.server import BaseHTTPRequestHandler
import hmac
import json
import time
from datetime import datetime
import base64
from io import BytesIO

from loguru import logger as log

from typing import TYPE_CHECKING, Any, override

if TYPE_CHECKING:
    from src.backend.DeckManagement.Subclasses.RemoteDeckManager import RemoteDeckManager
    from PIL import Image

# One button image is a small JPEG data URI; a control payload is a few keys.
# Anything larger is not this protocol, so refuse it before reading the body.
MAX_BODY_BYTES = 64 * 1024

def create_handler(remote_deck_manager: "RemoteDeckManager", token: str) -> "type[BaseHTTPRequestHandler]":
    """Create a handler bound to one manager and access token.
    All endpoints except CORS preflight authenticate before reading the body."""

    expected_token = token.encode("utf-8")

    class RemoteDecksLocalServerHandler(BaseHTTPRequestHandler):
        """Handle HTTP requests from the web client."""

        manager = remote_deck_manager

        # A held or trickled connection releases its thread after this many
        # seconds instead of occupying it for the session.
        timeout = 10

        # {button_id: {"data": <data: URI>, "timestamp": <epoch seconds>}}
        button_images: dict[int, dict[str, str | int]] = {}

        def _set_cors_headers(self) -> None:
            """Set CORS headers to allow cross-origin requests."""
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Deckard-Token, Authorization')

        def _send_json_response(self, status_code: int, data: Any) -> None:
            """Send a JSON response."""
            self.send_response(status_code)
            self._set_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode('utf-8'))

        def _authorized(self) -> bool:
            """Check the request token in constant time before any body read."""
            supplied = self.headers.get('X-Deckard-Token')
            if supplied is None:
                bearer = self.headers.get('Authorization', '')
                if bearer.startswith('Bearer '):
                    supplied = bearer[len('Bearer '):]
            if supplied is None:
                self._send_json_response(401, {'error': 'Missing token'})
                return False
            if not hmac.compare_digest(supplied.encode('utf-8'), expected_token):
                self._send_json_response(401, {'error': 'Invalid token'})
                return False
            return True

        def _read_body(self) -> bytes | None:
            """The request body, or None after a 413/400 for one that is
            oversized or unparseable in length."""
            try:
                content_length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                self._send_json_response(400, {'error': 'Invalid Content-Length'})
                return None
            if content_length > MAX_BODY_BYTES:
                self._send_json_response(413, {'error': 'Body too large'})
                return None
            return self.rfile.read(content_length)

        @classmethod
        def send_button_image(cls, button_id: int, image: "Image.Image") -> None:
            """
            Store a PIL image for a specific button to be sent to the browser.

            Args:
                button_id: The button identifier (e.g., row * 5 + col)
                image: PIL Image object
            """
            # Convert PIL image to base64-encoded JPEG
            buffered = BytesIO()
            image.save(buffered, format="JPEG")
            img_bytes = buffered.getvalue()
            img_base64 = base64.b64encode(img_bytes).decode('utf-8')

            cls.button_images[button_id] = {
                'data': f"data:image/jpeg;base64,{img_base64}",
                'timestamp': int(time.time())
            }

        def do_OPTIONS(self) -> None:
            """Handle unauthenticated CORS preflight requests."""
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()

        def do_GET(self) -> None:
            """Handle GET requests."""
            if not self._authorized():
                return
            if self.path == '/status':
                response_data = {
                    'status': 'online',
                    'message': 'Deckard Remote Decks server is running',
                    'timestamp': int(time.time()),
                    'datetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                self._send_json_response(200, response_data)
            elif self.path == '/images':
                response_data = {
                    'status': 'ok',
                    'images': self.button_images,
                    'timestamp': int(time.time())
                }
                self._send_json_response(200, response_data)
            elif self.path.startswith('/images/'):
                try:
                    button_id = int(self.path.split('/')[-1])
                    if button_id in self.button_images:
                        response_data = {
                            'status': 'ok',
                            'button_id': button_id,
                            'image': self.button_images[button_id],
                            'timestamp': int(time.time())
                        }
                        self._send_json_response(200, response_data)
                    else:
                        self._send_json_response(404, {'error': f'No image found for button {button_id}'})
                except (ValueError, IndexError):
                    self._send_json_response(400, {'error': 'Invalid button ID'})
            else:
                self._send_json_response(404, {'error': 'Not found'})

        def do_POST(self) -> None:
            """Handle POST requests."""
            if not self._authorized():
                return
            if self.path == '/message':
                post_data = self._read_body()
                if post_data is None:
                    return

                try:
                    data = json.loads(post_data.decode('utf-8'))
                    received_message = data.get('message', '')

                    response_text = f"Echo: {received_message} | Received at {datetime.now().strftime('%H:%M:%S')}"

                    response_data = {
                        'status': 'success',
                        'response': response_text,
                        'timestamp': int(time.time())
                    }

                    self._send_json_response(200, response_data)

                except json.JSONDecodeError:
                    self._send_json_response(400, {'error': 'Invalid JSON'})
                except Exception as e:
                    self._send_json_response(500, {'error': str(e)})
            elif self.path == '/button':
                # Handle button press/release events from the client UI
                post_data = self._read_body()
                if post_data is None:
                    return

                try:
                    data = json.loads(post_data.decode('utf-8'))
                    event_type = data.get('type')  # expected: "down" | "up"
                    row = data.get('row')
                    col = data.get('col')

                    # Validate payload
                    if event_type not in {"down", "up"}:
                        self._send_json_response(400, {'error': 'Invalid or missing "type" (use "down" or "up")'})
                        return

                    if not isinstance(row, int) or not isinstance(col, int):
                        self._send_json_response(400, {'error': '"row" and "col" must be integers'})
                        return

                    # Stable button id. The layout has 5 columns.
                    button_id = row * 5 + col

                    log.debug(f"Remote deck button event: type={event_type} row={row} col={col} id={button_id}")

                    self.manager.on_key_event(button_id, event_type == "down")

                    response_data = {
                        'status': 'ok',
                        'received': {
                            'type': event_type,
                            'row': row,
                            'col': col,
                            'id': button_id,
                        },
                        'timestamp': int(time.time()),
                    }

                    self._send_json_response(200, response_data)

                except json.JSONDecodeError:
                    self._send_json_response(400, {'error': 'Invalid JSON'})
                except Exception as e:
                    self._send_json_response(500, {'error': str(e)})
            else:
                self._send_json_response(404, {'error': 'Not found'})

        @override
        def log_message(self, format: str, *args: Any) -> None:
            """Override to keep request lines out of stderr."""
            pass

    return RemoteDecksLocalServerHandler
