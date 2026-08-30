"""Apply the live shrink-on-press preference to a held key image.
Callers must mark every pressed composite NO_STORE, even when shrinking is disabled."""
from PIL import Image

import globals as gl

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerKey


def apply(key: "ControllerKey", image: Image.Image) -> Image.Image:
    """Return the pressed image, preserving object identity when shrinking is disabled.
    The caller uses identity to close only unpainted images."""
    if not gl.settings_manager.app().shrink_on_press:
        return image
    return key.shrink_image(image)
