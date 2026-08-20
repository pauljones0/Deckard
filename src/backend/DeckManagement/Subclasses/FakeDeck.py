"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import uuid

import globals as gl
from typing import Any, cast

class FakeDeck:
    def __init__(self, serial_number: "str | None" = None, deck_type: "str | None" = None, key_layout: list[int] | None = None) -> None:
        if serial_number is None:
            # The settings store is keyed by the deck serial and refuses a
            # None key, so the settings read below failed anyway.
            raise ValueError("FakeDeck needs a serial number")
        self.serial_number: str = serial_number
        self._deck_type = deck_type

        self.is_fake = True

        default_layout = key_layout if key_layout is not None else [2, 4]
        self._key_layout = gl.settings_manager.get_deck_settings(self.serial_number).get("key-layout", default_layout)

        self._is_touch = True
        self._dial_count = 4

        # Keep a stable per-instance identity. A real deck returns the same
        # physical id() on every call. A fresh uuid per call breaks callers that compare
        # ids to de-dup already-loaded decks (DeckManager.connect_new_decks).
        self._id = str(uuid.uuid4())

    def deck_type(self) -> "str | None":
        return self._deck_type
    def get_serial_number(self) -> str:
        return self.serial_number
    def key_layout(self) -> "list[int]":
        return cast("list[int]", self._key_layout)
    def is_open(self) -> bool:
        return True
    def reset(self) -> None:
        return
    def key_count(self) -> int:
        return self.key_layout()[0] * self.key_layout()[1]
    def set_key_callback(self, *args: Any, **kwargs: Any) -> None:
        return
    def set_dial_callback(self, *args: Any, **kwargs: Any) -> None:
        return
    def set_touchscreen_callback(self, *args: Any, **kwargs: Any) -> None:
        return
    def set_brightness(self, *args: Any, **kwargs: Any) -> None:
        return
    def set_key_image(self, *args: Any, **kwargs: Any) -> None:
        return
    def key_states(self) -> "list[bool]":
        return [False] * self.key_count()
    def key_image_format(self) -> "dict[str, Any]":
        return {'size': (72, 72), 'format': 'JPEG', 'flip': (True, True), 'rotation': 0}
    def id(self) -> str:
        return self._id
    def connected(self) -> bool:
        return True
    def __enter__(self) -> "FakeDeck":
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

    def set_key_color(self, *args: Any, **kwargs: Any) -> None:
        return

    def screen_image_format(self) -> dict[str, Any]:
        return {'size': (0, 0), 'format': 'JPEG', 'flip': (False, False), 'rotation': 0}

    def set_screen_image(self, *args: Any, **kwargs: Any) -> None:
        return

    def touch_key_count(self) -> int:
        return 0

    def get_firmware_version(self) -> str:
        return "fake-1.0"

    def vendor_id(self) -> int:
        return 0

    def product_id(self) -> int:
        return 0

    def set_poll_frequency(self, hz: float) -> None:
        return

    def dial_states(self) -> "list[bool]":
        return [False] * self._dial_count