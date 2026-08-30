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

from PIL import Image
import os

import globals as gl

from typing import Any, TYPE_CHECKING, cast
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput

_error_image: Image.Image | None = None

class SingleKeyAsset:
    def __init__(self, controller_input: "ControllerInput[Any]"):
        self.controller_input = controller_input
        self.deck_controller = controller_input.deck_controller

    def get_raw_image(self) -> Image.Image | None:
        # The hierarchy permits None because InputImage returns it after close().
        global _error_image
        if _error_image is None:
            # Resolve against the repo root so startup CWD does not affect the asset path.
            path = os.path.join(gl.top_level_dir, "Assets", "images", "error.png")
            with Image.open(path) as img:
                _error_image = img.copy()
        # Return a copy so callers can composite or close it freely.
        return cast("Image.Image", _error_image.copy())
    
    def close(self) -> None:
        pass
