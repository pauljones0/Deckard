"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
from src.backend.DeckManagement import font_resolver
from PIL import ImageFont
from dataclasses import dataclass
from functools import lru_cache
from fontTools.ttLib import TTFont

import globals as gl


@lru_cache(maxsize=128)
def _load_font(font_path: str, font_size: float, encoding: str) -> ImageFont.FreeTypeFont:
    # Cache parsed FreeType faces for per-composite keys and scroll measurements.
    # Rasterized static labels use a separate cache.
    return ImageFont.truetype(font_path, font_size, encoding=encoding)


@lru_cache(maxsize=128)
def _is_symbol_font(font_path: str) -> bool:
    """Check if font uses symbol encoding (e.g., Webdings, Wingdings).

    A symbol font has a cmap table with platformID=3 (Windows) and
    platEncID=0 (Symbol encoding). The lru_cache holds the results.
    """
    try:
        font = TTFont(font_path)
        for table in font['cmap'].tables:
            if table.platformID == 3 and table.platEncID == 0:
                return True
        return False
    except Exception:
        return False


from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput

@dataclass
class KeyLabel:
    # Use the shared input base because keys and dials both own labels.
    controller_input: "ControllerInput[Any]"
    text: str | None = None
    # Persist the fractional size returned from Pango's 1024th-unit scale.
    font_size: float | None = None
    font_name: str | None = None
    font_weight: int | None = None
    style: str | None = None # normal, oblique, italic
    color: list[int] | None = None
    outline_width: int | None = None
    outline_color: list[int] | None = None
    alignment: str | None = None  # left, center, right

    def get_font_path(self) -> str | None:
        font_name = self.font_name
        if font_name is None or font_name == "":
            font_name = gl.fallback_font

        # Resolution is cached by file-selection attributes; size does not select a file.
        # None means no matcher is available or the match has no file field.
        return font_resolver.resolve(font_name, self.font_weight, self.style)

    def clear_values(self) -> None:
        self.text = None
        self.font_size = None
        self.font_name = None
        self.font_weight = None
        self.style = None
        self.color = None
        self.outline_width = None
        self.outline_color = None
        self.alignment = None

    def get_font(self) -> ImageFont.FreeTypeFont:
        font_path = self.get_font_path()
        font_size = self.font_size
        if font_path is None or font_size is None:
            # Defaults provide size before rendering; a missing path means no matched file.
            # Raise here instead of failing inside PIL's loader.
            raise RuntimeError(
                f"cannot load a font for this label (path={font_path!r}, size={font_size!r})")
        encoding = "symb" if _is_symbol_font(font_path) else "unic"
        return _load_font(font_path, font_size, encoding)
