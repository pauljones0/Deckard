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
from collections.abc import Callable, Sequence
from datetime import datetime
from functools import wraps
import hashlib
from io import BytesIO
import os
import subprocess
import sys
import math
import re
import threading
from types import FunctionType
from typing import Concatenate, ParamSpec, TYPE_CHECKING, Any, TypeVar, cast
from urllib.parse import urlparse
from loguru import logger as log
from PIL import Image

import gi
from gi.repository import Gio, GLib

# Import Gdk and Pango on demand to keep GTK out of engine imports.
if TYPE_CHECKING:
    from gi.repository import Gdk, Pango

from src.backend.DeckManagement import font_resolver

_Return = TypeVar("_Return")
_Params = ParamSpec("_Params")
_Self = TypeVar("_Self")

# Import globals
from autostart import is_flatpak
import globals as gl


def sha256(text: str) -> str:
    """
    Calculates the sha256 hash of a file or string.

    Args:
        text (str): The file path or string.

    Returns:
        str: The sha256 hash of the file or string.
    """
    hash_sha256 = hashlib.sha256()
    if os.path.exists(text):
        with open(text, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
    else:
        hash_sha256.update(text.encode('utf-8'))
    return hash_sha256.hexdigest()


def file_in_dir(file_path: str, directory: str) -> bool | None:
    """Return whether the directory contains the file, or None if directory is invalid."""
    if not os.path.isdir(directory) and directory is not None:
        return None

    return os.path.split(file_path)[1] in os.listdir(directory)


def recursive_hasattr(obj: object, attr_string: str) -> bool:
    """
    Check if an attribute exists in an object.

    Args:
        obj (object): The object to check.
        attr_string (str): The attributes to check separated with dots. e.g.: foo.bar

    Returns:
        bool: True if the attribute exists, False otherwise.
    """
    attrs = attr_string.split('.')
    for attr in attrs:
        if not hasattr(obj, attr):
            return False
        obj = getattr(obj, attr)
    return True


def font_path_from_name(font_name: str) -> "str | None":
    return font_resolver.resolve(font_name, 400, "normal")


def font_name_from_path(font_path: str) -> "str | None":
    return font_resolver.font_name_from_path(font_path)


def get_last_dir(path: str) -> str | None:
    if os.path.isdir(path):
        return os.path.basename(os.path.normpath(path))
    elif os.path.isfile(path):
        return os.path.basename(os.path.normpath(os.path.dirname(path)))
    return None


def has_dict_recursive(dictionary: dict[str, Any], *args: Any) -> bool:
    working_dict: Any = dictionary
    for arg in args:
        working_dict = working_dict.get(arg)
        if working_dict is None:
            return False
    return True


def get_sys_param_value(param_name: str) -> str | None:
    for i, param in enumerate(sys.argv):
        if param.startswith(param_name):
            if i + 1 < len(sys.argv):
                return sys.argv[i + 1]
    return None


def get_sys_args_without_param(param_name: str) -> list[str]:
    """Return a new argv without matching parameters and their following values.
    A terminal matching parameter has no value, and sys.argv remains unchanged."""
    args = []
    skip_next = False
    for arg in sys.argv:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith(param_name):
            skip_next = True
            continue
        args.append(arg)
    return args


def is_video(path: str | None) -> bool:
    if path is None:
        return False
    if os.path.isfile(path):
        return os.path.splitext(path)[1][1:].lower().replace(".", "") in gl.video_extensions

    return False


def is_image(path: str | None) -> bool:
    if path is None:
        return False
    if os.path.isfile(path):
        return os.path.splitext(path)[1][1:].lower().replace(".", "") in gl.image_extensions

    return False


def is_svg(path: str | None) -> bool:
    if path is None:
        return False
    if os.path.isfile(path):
        return os.path.splitext(path)[1][1:].lower().replace(".", "") in gl.svg_extensions

    return path.startswith("<svg ")


def centered_paste_offset(container: "tuple[int, int]", item: "tuple[int, int]") -> "tuple[int, int]":
    """Return the centered offset for an item no larger than its container.
    Larger items are unsupported because floor division and truncation differ."""
    return ((container[0] - item[0]) // 2, (container[1] - item[1]) // 2)


def get_image_aspect_ratio(img: Image.Image) -> str:
    width, height = img.size
    gcd = math.gcd(width, height)
    aspect_ratio = f"{width//gcd}:{height//gcd}"
    return aspect_ratio


def get_file_name_from_url(url: str) -> str:
    """
    Extracts the file name from a given URL.

    Args:
        url (str): The URL from which to extract the file name.

    Returns:
        str: The file name extracted from the URL.
    """
    # Parse the url to extract the path
    parsed_url = urlparse(url)
    # Extract the file name from the path
    return os.path.basename(parsed_url.path)


def download_file(url: str, path: str = "", file_name: str | None = None) -> str:
    """Download the URL and return its final path.
    Network, HTTP, and file errors propagate without leaving a partial destination."""

    # Import lazily to keep requests out of startup imports.
    from src.backend import http_client

    if file_name is None:
        file_name = get_file_name_from_url(url)

    path = os.path.join(path, file_name)

    # Use the shared session with a timeout; HTTP errors must not enter the asset cache.
    # The extension-only image check can accept an error page as an asset.
    http_client.download_to_file(url, path, timeout=10)

    return path

def natural_keys(s: str) -> "list[int | str]":
    # Split text and decimal runs; isdecimal() excludes digits that int() rejects.
    return [int(text) if text.isdecimal() else text.lower() for text in re.split('([0-9]+)', s)]


def natural_sort(strings_list: list[str]) -> list[str]:
    return sorted(strings_list, key=natural_keys)


def natural_sort_by_filenames(paths_list: list[str]) -> list[str]:
    sorted_paths = sorted(
        paths_list, key=lambda path: natural_keys(os.path.basename(path)))
    return sorted_paths


def add_default_keys(d: dict[str, Any], keys: list[Any]) -> None:
    """
    Add nested default keys to a dictionary.

    :param d: The dictionary to add keys to.
    :param keys: A list of keys to create nested dictionaries for.
    :return: None; the dictionary is modified in place.
    """
    current_level = d
    for key in keys:
        if key not in current_level:
            current_level[key] = {}
        current_level = current_level[key]


def instance_cache(func: Callable[Concatenate[_Self, _Params], _Return]) -> Callable[Concatenate[_Self, _Params], _Return]:
    """Memoize positional method calls per instance.
    Arguments must be hashable; keyword calls and concurrent use are unsupported."""
    func_name = cast(FunctionType, func).__name__
    attr = f"_instance_cache_{func_name}"

    @wraps(func)
    # Keep self positional-only to match the declared callable return type.
    def wrapper(self: _Self, /, *args: _Params.args, **kwargs: _Params.kwargs) -> _Return:
        # A truthiness test loses the ParamSpec kwargs identity.
        if len(kwargs) > 0:
            # Keyword calls can miss or alias the positional cache key.
            raise TypeError(f"{func_name} is instance-cached; pass arguments positionally")
        cache: dict[tuple[object, ...], _Return] | None = self.__dict__.get(attr)
        if cache is None:
            cache = self.__dict__[attr] = {}
        if args not in cache:
            cache[args] = func(self, *args, **kwargs)
        return cache[args]

    return wrapper


def _load_gdk() -> Any:
    """Import Gdk on demand."""
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk
    return Gdk


def _load_pango() -> Any:
    """Import Pango on demand."""
    from gi.repository import Pango
    return Pango


def color_values_to_gdk(color_values: Sequence[int]) -> "Gdk.RGBA":
    # Accept any 3- or 4-channel sequence because persisted colors are JSON lists.
    gdk = _load_gdk()
    # Copy before appending alpha to preserve the caller's sequence.
    values = list(color_values)
    if len(values) == 3:
        values.append(255)
    color = gdk.RGBA()
    # Input channels are 0-255, but CSS alpha is 0-1; scale it to preserve transparency.
    color.parse(f"rgba({values[0]}, {values[1]}, {values[2]}, {values[3] / 255})")

    return cast("Gdk.RGBA", color)


def gdk_color_to_values(color: "Gdk.RGBA") -> tuple[int, int, int, int]:
    green = round(color.green * 255)
    blue = round(color.blue * 255)
    red = round(color.red * 255)
    alpha = round(color.alpha * 255)

    return red, green, blue, alpha


# Accept the fractional size returned by get_values_from_pango_font_description.
def get_pango_font_description(font_family: str, font_size: float, font_weight: int, font_style: str) -> "Pango.FontDescription":
    pango = _load_pango()
    if font_style == "italic":
        font_style = pango.Style.ITALIC
    elif font_style == "oblique":
        font_style = pango.Style.OBLIQUE
    else:
        font_style = pango.Style.NORMAL

    desc = pango.FontDescription()
    desc.set_family(font_family)
    desc.set_absolute_size(font_size * pango.SCALE)
    desc.set_weight(font_weight)
    desc.set_style(font_style)

    return cast("Pango.FontDescription", desc)


def get_values_from_pango_font_description(desc: "Pango.FontDescription") -> tuple[str | None, float, int, str]:
    # Pango size is fractional in 1024ths, and the family can be unset.
    Pango = _load_pango()
    font_family = desc.get_family()
    font_size: float = desc.get_size() / Pango.SCALE
    font_weight = desc.get_weight()
    font_style = desc.get_style()

    if font_style == Pango.Style.ITALIC:
        style_name = "italic"
    elif font_style == Pango.Style.OBLIQUE:
        style_name = "oblique"
    else:
        style_name = "normal"

    return font_family, font_size, font_weight, style_name


def get_sub_folders(parent: str) -> list[str]:
    if not os.path.isdir(parent):
        return []

    return [folder for folder in os.listdir(parent) if os.path.isdir(os.path.join(parent, folder))]


def sort_times(time_list: "list[str]") -> "list[str]":
    """
    Sort a list of datetime strings in ascending order.

    Parameters:
    time_list (list of str): List of datetime strings to be sorted.

    Returns:
    list of str: Sorted list of datetime strings.
    """
    return sorted(time_list, key=lambda x: datetime.fromisoformat(x))


def run_command(command: "str | None") -> None:
    """Run a shell command with HOME, null stdio, and a detached session.
    The plugin API accepts shell syntax; use subprocess directly for an argv."""
    if command is None:
        return

    if is_flatpak():
        command = "flatpak-spawn --host " + command

    try:
        process = subprocess.Popen(command, shell=True, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, cwd=os.path.expanduser("~"))
    except OSError as e:
        # Log spawn failures because plugin action callbacks cannot handle them.
        log.error(f"Failed to run command {command!r}: {e}")
        return
    # Reap the direct child in a daemon thread to prevent a lifetime zombie.
    # Do not fork the full GTK and plugin process only to orphan the grandchild.
    threading.Thread(target=process.wait, name="run_command_reaper", daemon=True).start()

def open_web(url: str) -> None:
    """Open a URL through Gio so sandboxed calls use the OpenURI portal.
    The URL never becomes a shell command."""
    if not url.startswith("http"):
        url = f"https://{url}"
    try:
        Gio.AppInfo.launch_default_for_uri(url, None)
    except GLib.Error as e:
        log.error(f"Failed to open URL {url}: {e}")

def svg_string_to_pil(svg_string: str, width: int = 96, height: int = 96) -> Image.Image:
    """
    Convert an SVG string to a PIL Image object.
    
    Args:
        svg_string (str): String containing SVG data
        width (int, optional): Desired width of the output image
        height (int, optional): Desired height of the output image
        
    Returns:
        PIL.Image: The converted image
    """
    import cairosvg

    # Convert SVG string to PNG using cairosvg
    png_data = cairosvg.svg2png(
        bytestring=svg_string.encode('utf-8'),
        output_width=width,
        output_height=height
    )

    # Create PIL Image from PNG data
    img = Image.open(BytesIO(png_data))

    return img


#: Rasterize assets at 192x96; square icons occupy the centered 96x96 region.
#: The layout therefore renders square SVGs at about half the bitmap size.
SVG_RASTER_WIDTH_PX = 192


def svg_to_pil(svg_path: str, width: int = 96, height: int = 96) -> Image.Image:
    """
    Convert an SVG file to a PIL Image object.
    
    Args:
        svg_path (str): Path to the SVG file or string containing SVG data
        width (int, optional): Desired width of the output image
        height (int, optional): Desired height of the output image
        
    Returns:
        PIL.Image: The converted image
    """
    import cairosvg

    # Read SVG file

    if os.path.exists(svg_path):
        with open(svg_path, 'rb') as f:
            svg_data = f.read()

        # Convert SVG to PNG using cairosvg
        png_data = cairosvg.svg2png(
            bytestring=svg_data,
            output_width=width,
            output_height=height
        )
        
        # Create PIL Image from PNG data
        img = Image.open(BytesIO(png_data))
        
        return img
    elif svg_path.startswith("<svg "):
        return svg_string_to_pil(svg_path, width, height)
    else:
        raise ValueError(f"Could not create SVG from string or path: {svg_path}")
