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

# The four colour and font helpers below import Gdk and Pango on demand.
# They are the only consumers, and their callers all live under src/windows/.
# A module-level import drags the widget stack into every engine import
# closure that touches HelperMethods.
if TYPE_CHECKING:
    from gi.repository import Gdk, Pango

from src.backend.DeckManagement import font_resolver

# The decorated method's return type, so instance_cache keeps its signature.
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
    """
    Check if a file is present in a directory.

    Args:
        file_path (str): The path of the file to check.
        dir (str, optional): The directory to check. Defaults to None.

    Returns:
        bool: True if the file is present in the directory, False otherwise;
            None if directory names something that is not a directory.
            Callers use the result in a boolean context, where that reads
            as "not present".
    """
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
    """sys.argv minus every argument starting with param_name, and the value
    after it.

    Returns a new list and never modifies sys.argv. An in-place version
    corrupts sys.argv for later readers, and pops past the end when the
    matched parameter is the last element of argv.
    """
    args = []
    skip_next = False
    for arg in sys.argv:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith(param_name):
            skip_next = True  # also drop the parameter's value, if present
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
    """The paste offset that centres item inside container.

    It serves the paste sites that centre something known to be no larger than
    what it goes on, which is what every caller does: an overlay drawn at a
    fraction of the tile, and the shrunken look of a pressed key. An item wider
    or taller than its container is outside the contract, because the two ways
    of writing this arithmetic disagree there, floor against truncation, and a
    caller that needs a crop must say which it wants.
    """
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
    """
    Downloads a file from the specified URL and saves it to the specified path.

    Args:
        url (str): The URL of the file to be downloaded.
        path (str): The path of the directory where the file will be saved. If a directory is provided, the filename will be extracted from the URL and appended to the path.

    Returns:
        path (str): The path of the downloaded file.

    Raises:
        requests.RequestException: on a network failure or an HTTP error
            status. Nothing is left on disk in either case.
        OSError: when the directory, the temporary file the download fills, or
            the rename onto the returned path fails.
    """

    # Import lazily. Nearly everything imports this module at startup, and
    # http_client imports requests.
    from src.backend import http_client

    if file_name is None:
        file_name = get_file_name_from_url(url)

    path = os.path.join(path, file_name)

    # Stream through the shared session. The timeout stops an indefinite block
    # on a black-holed or hung connection. An HTTP error status raises instead
    # of writing the error page into the asset cache under the requested file
    # name, where the extension-based is_image() check accepts it as an asset.
    http_client.download_to_file(url, path, timeout=10)

    return path

def natural_keys(s: str) -> "list[int | str]":
    # The elements alternate text and digit runs; two keys only compare
    # int against int at an index when both names carry digits there.
    # isdecimal() is the test that matches what int() accepts: isdigit() is
    # true for characters such as the superscript two, which int() rejects.
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
    """Per-instance method memoization.

    Results live in the instance __dict__ and die with the instance. The key
    is the positional args, which must be hashable. The check-then-set has no
    lock, so this is not thread-safe.
    """
    # Only a plain method carries this decorator, so it always has a __name__.
    func_name = cast(FunctionType, func).__name__
    attr = f"_instance_cache_{func_name}"

    @wraps(func)
    # self is positional-only: the declared return type takes its first
    # parameter positionally, and a named self would not assign to it.
    def wrapper(self: _Self, /, *args: _Params.args, **kwargs: _Params.kwargs) -> _Return:
        # A len test, not a truthiness test: a truthiness test drops the
        # ParamSpec kwargs identity, and the forwarded call below then fails
        # to type-check.
        if len(kwargs) > 0:
            # The cache key is the positional args only. A keyword call would
            # miss or alias a key, so it stays unsupported, as the
            # positional-only key always made it.
            raise TypeError(f"{func_name} is instance-cached; pass arguments positionally")
        # The dict lives in the instance __dict__; the annotation states what
        # this decorator stores in it.
        cache: dict[tuple[object, ...], _Return] | None = self.__dict__.get(attr)
        if cache is None:
            cache = self.__dict__[attr] = {}
        if args not in cache:
            # kwargs is empty here, per the guard above.
            cache[args] = func(self, *args, **kwargs)
        return cache[args]

    return wrapper


def _load_gdk() -> Any:
    """Imports Gdk on demand. See the TYPE_CHECKING note at the top."""
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk
    return Gdk


def _load_pango() -> Any:
    """Imports Pango on demand. See the TYPE_CHECKING note at the top."""
    from gi.repository import Pango
    return Pango


def color_values_to_gdk(color_values: Sequence[int]) -> "Gdk.RGBA":
    # The annotation is Sequence and not a tuple union. The persisted label
    # and font colors are JSON lists, and that is what most callers hand over.
    # The body copies into a list and works off the length, so it accepts any
    # 3- or 4-element sequence of channel values (scenario_helper_methods pins
    # that contract).
    gdk = _load_gdk()
    # Copy before normalizing. Callers pass tuples, which .append rejects,
    # and they reuse the sequence they passed in.
    values = list(color_values)
    if len(values) == 3:
        values.append(255)
    color = gdk.RGBA()
    # Every caller works in 0-255 on all four channels. gdk_color_to_values
    # hands back that range, and the label and font settings persist it. CSS
    # rgba() takes the channels in 0-255 but the alpha in 0-1, so this scales
    # the raw value. An unscaled alpha clamps every alpha at or above 1 to
    # fully opaque, and a semi-transparent label colour comes back opaque.
    color.parse(f"rgba({values[0]}, {values[1]}, {values[2]}, {values[3] / 255})")

    return cast("Gdk.RGBA", color)


def gdk_color_to_values(color: "Gdk.RGBA") -> tuple[int, int, int, int]:
    green = round(color.green * 255)
    blue = round(color.blue * 255)
    red = round(color.red * 255)
    alpha = round(color.alpha * 255)

    return red, green, blue, alpha


# The inverse of get_values_from_pango_font_description, which answers a
# fractional size, so this takes one back.
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
    # The size really is fractional, because Pango sizes are in 1024ths and
    # the division keeps the remainder. The family really can be unset. Both
    # values go into the persisted font settings, so the annotation follows
    # the data.
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
    """Detaches a shell command line and forgets about it.

    command is a command line, not an argv list. Callers, plugins included,
    rely on shell syntax such as pipes, && and variable expansion, and the
    flatpak prefix splices in as a string. This is de-facto plugin API
    surface. Do not change it to shlex.split and argv. Build the argv
    yourself and call subprocess directly if you need that.

    The command gets its own session, its stdio pointed at /dev/null and ~ as
    its cwd, so it outlives the app cleanly.
    """
    if command is None:
        return

    if is_flatpak():
        command = "flatpak-spawn --host " + command

    try:
        process = subprocess.Popen(command, shell=True, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, cwd=os.path.expanduser("~"))
    except OSError as e:
        # Log a spawn failure and never raise it. Callers are plugin action
        # callbacks, and they cannot handle an OSError from a missing /bin/sh,
        # a missing HOME, or a fork refused under load.
        log.error(f"Failed to run command {command!r}: {e}")
        return
    # Reap the direct child, or it stays a zombie for the life of the app.
    # One throwaway daemon thread per spawn does that. A
    # multiprocessing.Process wrapper instead forks the whole interpreter
    # (GTK, plugins and deck threads) only to orphan the grandchild.
    threading.Thread(target=process.wait, name="run_command_reaper", daemon=True).start()

def open_web(url: str) -> None:
    """Opens a URL in the user's default browser.

    Uses Gio instead of a shell call to xdg-open. GLib routes the call through
    the OpenURI portal when sandboxed, so this works in the flatpak without
    flatpak-spawn --host. A URL with shell metacharacters cannot become a
    command.
    """
    if not url.startswith("http"):
        url = f"https://{url}"
    try:
        Gio.AppInfo.launch_default_for_uri(url, None)
    except GLib.Error as e:
        # Gio raises on failure. Log it.
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


#: The width a page or action SVG asset is rasterized at before the layout
#: manager fits it to its target. It is passed as the width only, and
#: svg_to_pil keeps its own default height of 96, so the raster canvas is
#: 192x96 and a square icon lands letterboxed in the middle 96x96 of it. That
#: 96-pixel square is what the fit then downscales, so an SVG renders at about
#: half the linear size of a bitmap at the same tile. This names the width the
#: two former literals passed and is byte-identical to them; correcting the
#: half-size render is a separate change, not this one.
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