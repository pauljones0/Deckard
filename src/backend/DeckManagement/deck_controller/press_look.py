"""What a key shows for as long as it is held down.

A press draws the key's picture smaller, centred on a transparent margin, so
the background shows through at the edges and the key reads as pushed in.
That look is a preference. A page whose keys carry one picture between them
shows a seam at every margin, and the general settings turn the shrink off
for it. The default is on, which is the feedback the app has always given.

The refusal to keep the picture is no preference. The covered-key cache holds
one composite per key state and reuses it for as long as nothing it depends
on moves, and a press is one of the gates that keeps a composite out of it.
The branch that draws a gated look is the branch that decides the store, so
the caller pairs this call with NO_STORE whatever comes back from it. With the
shrink off a pressed composite is the picture the key already shows, so a
stored one would carry the same bytes and harm nothing, and the rule stays the
one cover_cache states: no kept composite carries a pressed look.

The setting is read per composite rather than kept on the key, so a change in
the settings dialog reaches the next press with nothing to invalidate. A
pressed picture lives as long as the finger does.
"""
from PIL import Image

import globals as gl

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerKey


def apply(key: "ControllerKey", image: Image.Image) -> Image.Image:
    """The picture a held key shows, given the one it shows at rest.

    It hands back image itself while the shrink is off. The caller closes the
    images it did not paint and tells them apart by identity, so the same
    object coming back out keeps that test right.
    """
    if not gl.settings_manager.app().shrink_on_press:
        return image
    return key.shrink_image(image)
