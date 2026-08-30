"""
Author: Core447
Year: 2025

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from collections.abc import Callable
from io import BytesIO
from typing import Any
import uuid
from PIL import Image

import globals as gl

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.Subclasses.RemoteDeckManager import RemoteDeckManager

class RemoteDeck:
    def __init__(self, remote_deck_manager: "RemoteDeckManager | None", serial_number: "str | None" = None, deck_type: "str | None" = None) -> None:
        self.remote_deck_manager: "RemoteDeckManager | None" = remote_deck_manager
        if serial_number is None:
            # The settings store requires a serial key.
            raise ValueError("RemoteDeck needs a serial number")
        self.serial_number: str = serial_number
        self._deck_type = deck_type

        self.is_fake = True

        self._key_layout = [3, 5]

        self._is_touch = False
        self._dial_count = 0

        # RemoteDeckManager can receive a press before set_key_callback runs.
        self.key_callback: Callable[..., Any] | None = None

    def deck_type(self) -> "str | None":
        return self._deck_type
    def get_serial_number(self) -> str:
        return self.serial_number
    def key_layout(self) -> "list[int]":
        return self._key_layout
    def is_open(self) -> bool:
        return True
    def reset(self) -> None:
        return
    def key_count(self) -> int:
        return self.key_layout()[0] * self.key_layout()[1]
    def set_key_callback(self, callback: Callable[..., Any]) -> None:
        self.key_callback = callback
    def set_dial_callback(self, *args: Any, **kwargs: Any) -> None:
        return
    def set_touchscreen_callback(self, *args: Any, **kwargs: Any) -> None:
        return
    def set_brightness(self, *args: Any, **kwargs: Any) -> None:
        return
    def set_key_image(self, key: int, image: bytes) -> None:
        if self.remote_deck_manager is None:
            # A deck built without a manager has no browser to send to.
            return
        pillow_image = Image.open(BytesIO(image)).rotate(180)
        self.remote_deck_manager.send_button_image(key, pillow_image)
    def key_states(self) -> "list[bool]":
        return [False] * self.key_count()
    def key_image_format(self) -> "dict[str, Any]":
        return {'size': (72, 72), 'format': 'JPEG', 'flip': (True, True), 'rotation': 0}
    def id(self) -> str:
        return str(uuid.uuid4())
    def connected(self) -> bool:
        return True
    def __enter__(self) -> "RemoteDeck":
        return self
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return True
    
    def set_key_layout(self, layout: list[int]) -> None:
        """
        Sets and saves a new key layout
        """
        self._key_layout = layout

        settings = gl.settings_manager.get_deck_settings(self.serial_number)
        settings["key-layout"] = layout
        gl.settings_manager.save_deck_settings(self.serial_number, settings)

    def open(self, *args: Any, **kwargs: Any) -> None:
        return
    
    def close(self) -> None:
        return
    
    def is_visual(self) -> bool:
        return True
    
    def is_touch(self) -> bool:
        # Return the stored flag; this bound method itself is always truthy.
        return self._is_touch
    
    def dial_count(self) -> int:
        return self._dial_count
    
    def touchscreen_image_format(self) -> dict[str, Any]:
        return{
            "size": (800, 100),
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 0
        }
    
    def set_touchscreen_image(self, *args: Any, **kwargs: Any) -> None:
        return
