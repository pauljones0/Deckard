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


def hides_background(image: Image.Image, left: int, top: int,
                     background_size: tuple[int, int]) -> bool:
    """Whether pasting image at (left, top) leaves no pixel of a background of
    background_size visible.

    Two conditions, both exact. The paste must reach every pixel of the
    background, and every pixel it lays down must be fully opaque. An image
    with no alpha data replaces what it lands on outright; one with alpha is
    pasted through itself as a mask, and a mask of 255 replaces the
    destination pixel just as completely. Either way the result of the
    composite is the same whatever the background held, which is what lets a
    caller keep the composite and stop rebuilding it per frame.

    Nothing here has a tolerance. One translucent pixel, or one row the paste
    misses, and the composite depends on the background again. An answer of
    False costs a composite that was not needed; a wrong True freezes a stale
    frame on the device, so every case this cannot prove reads False. A
    palette image that carries its transparency in info, and not in a band,
    is one such case.
    """
    background_width, background_height = background_size
    if left > 0 or top > 0:
        return False
    if image.width + left < background_width or image.height + top < background_height:
        return False
    if not image.has_transparency_data:
        return True
    try:
        alpha = image.getchannel("A")
    except ValueError:
        return False
    lowest, _highest = alpha.getextrema()
    return lowest == 255


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