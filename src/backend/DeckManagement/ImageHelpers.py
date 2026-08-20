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
from typing import TYPE_CHECKING

from PIL import Image

# image2pixbuf imports GLib and GdkPixbuf on demand. It is their only
# consumer, and all its callers live under src/windows/. A module-level
# import drags the widget stack into the engine's import closure, and
# ImageHelpers is core to the render path. The annotation below is a string
# for the same reason: a runtime one would evaluate the name at def time.
if TYPE_CHECKING:
    from gi.repository import GdkPixbuf


def is_transparent(img: Image.Image) -> bool:
    """
    Determines if an image has transparency.

    Args:
        img (PIL.Image.Image): The image to check for transparency.

    Returns:
        bool: True if the image has transparency, False otherwise.
    """
    return bool(img.has_transparency_data)


def image2pixbuf(img: Image.Image, force_transparency: bool = False) -> "GdkPixbuf.Pixbuf | None":
    """
    Converts an image to a GdkPixbuf.Pixbuf object.

    Args:
        img (PIL.Image.Image): The image to convert.

    Returns:
        GdkPixbuf.Pixbuf: The converted GdkPixbuf.Pixbuf object, or None when
        the image is not one GdkPixbuf accepts, which is usually a non-RGB one.
    """
    from gi.repository import GLib, GdkPixbuf

    img = img.convert("RGBA")
    force_transparency = True

    w, h = img.size
    # Two names, because the GLib wrapper is not the buffer it wraps.
    raw = img.tobytes()
    data = GLib.Bytes.new(raw)
    transparent = True if force_transparency else is_transparent(img)
    channels = 4 if transparent else 3

    try:
        return GdkPixbuf.Pixbuf.new_from_bytes(data, GdkPixbuf.Colorspace.RGB,
                transparent, 8, w, h, w * channels)
    except TypeError:
        # This usually happens if the image is a non RGB image
        return None