"""Viewport math for background media: pan and zoom of the cover crop.

A background renders by cropping the source image to the deck canvas's
aspect and resizing, historically always centered at cover size. A viewport
generalizes that crop: a normalized center (x, y) pans it and a scale factor
zooms it. Scale 1.0 with a centered view reproduces the plain cover crop
byte for byte, so media without a stored view renders exactly as before.

The view travels as a plain (x, y, scale) tuple. normalize_view() is the
single reader of the persisted dict shape, so every consumer sees clamped
floats and a missing or malformed setting degrades to the default view
instead of raising out of a render.
"""
import math
from typing import Any

from PIL import Image

# The zoom bounds. The lower bound keeps at least a recognizable slice of
# the image on the canvas; the upper bound caps how much source resolution a
# render can demand.
MIN_SCALE = 0.25
MAX_SCALE = 8.0

DEFAULT_VIEW: tuple[float, float, float] = (0.5, 0.5, 1.0)

# One equality tolerance for "is this the default view", shared by the
# renderers and the cache naming, so the two cannot disagree about whether a
# view changes the output.
_EPSILON = 1e-4


def normalize_view(raw: Any) -> tuple[float, float, float]:
    """Read a persisted view value into a clamped (x, y, scale) tuple.

    Accepts the settings dict shape {"x": ..., "y": ..., "scale": ...} with
    any subset of keys. Anything else, including None, reads as the default
    view. The center clamps to [0, 1] and the scale to the zoom bounds, so a
    hand-edited or stale value cannot push the crop off the image entirely.
    """
    if not isinstance(raw, dict):
        return DEFAULT_VIEW
    try:
        x = float(raw.get("x", DEFAULT_VIEW[0]))
        y = float(raw.get("y", DEFAULT_VIEW[1]))
        scale = float(raw.get("scale", DEFAULT_VIEW[2]))
    except (TypeError, ValueError):
        return DEFAULT_VIEW
    # json.load accepts NaN and Infinity, and min/max pass NaN through, so
    # the finite check is what keeps a bad file from reaching a renderer.
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(scale)):
        return DEFAULT_VIEW
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    scale = min(max(scale, MIN_SCALE), MAX_SCALE)
    return (x, y, scale)


def is_default_view(view: tuple[float, float, float]) -> bool:
    return (abs(view[0] - DEFAULT_VIEW[0]) < _EPSILON
            and abs(view[1] - DEFAULT_VIEW[1]) < _EPSILON
            and abs(view[2] - DEFAULT_VIEW[2]) < _EPSILON)


def view_as_setting(view: tuple[float, float, float]) -> "dict[str, float] | None":
    """The stored shape of a view: the settings dict, or None for the default
    view, so an untouched background keeps its pre-view settings file. The
    inverse of normalize_view."""
    if is_default_view(view):
        return None
    return {"x": view[0], "y": view[1], "scale": view[2]}


def view_suffix(view: tuple[float, float, float]) -> str:
    """A filename component naming this view, empty for the default.

    A media cache whose content depends on the view carries this in its file
    name, beside the saturation suffix, so a view change misses instead of
    serving the previous crop. The default view stays suffix-free, which
    keeps every cache file from before this feature valid.

    The shape is a dot group like the saturation's ".satNNN", because the
    video cache sweeper parses cache names as dot-delimited groups: the md5
    is everything before the first dot, and its name pattern must recognize
    the group or the file reads as a legacy leftover. Fixed-point digits
    inside the group, so the group itself carries no further dots.
    """
    if is_default_view(view):
        return ""
    return f".v{round(view[0] * 10000):04d}-{round(view[1] * 10000):04d}-{round(view[2] * 10000):05d}"


def render_viewport_rgb(image: Image.Image, canvas_size: tuple[int, int],
                        view: tuple[float, float, float],
                        resample: Image.Resampling = Image.Resampling.LANCZOS) -> Image.Image:
    """render_viewport flattened onto black, for an opaque frame format.

    A rectangle inside the source resizes the RGB frame straight from its
    box, byte-identical to the RGBA path and without its three extra
    full-frame copies, which matters at one call per source video frame. A
    zoomed-out view letterboxes with transparency; a consumer that writes
    opaque frames (the video tile cache's mp4) composites that onto black
    here, which is also what the deck shows behind a background.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    left, top, right, bottom = viewport_rect(image.size, canvas_size, view)
    if left >= 0 and top >= 0 and right <= image.width and bottom <= image.height:
        return image.resize(canvas_size, resample, box=(left, top, right, bottom))
    framed = render_viewport(image, canvas_size, view, resample)
    out = Image.new("RGB", canvas_size)
    out.paste(framed, mask=framed)
    return out


def media_entries(raw: Any) -> "list[tuple[str, tuple[float, float, float]]]":
    """Slideshow entries as (path, view) pairs, from both stored shapes.

    A plain string entry is a path with the default view; an object entry
    carries the path and that image's own viewport. Old settings files hold
    strings, the writer moves an entry to an object when its view is set,
    and both shapes read here so no migration write is needed. Empty and
    non-string paths drop out.
    """
    entries: "list[tuple[str, tuple[float, float, float]]]" = []
    for entry in (raw or []):
        if isinstance(entry, dict):
            path = entry.get("path")
            view = normalize_view(entry.get("view"))
        else:
            path = entry
            view = DEFAULT_VIEW
        if isinstance(path, str) and path:
            entries.append((path, view))
    return entries


def viewport_rect(source_size: tuple[int, int], canvas_size: tuple[int, int],
                  view: tuple[float, float, float]) -> tuple[float, float, float, float]:
    """The source-space crop rectangle for a view, as a float (l, t, r, b).

    The base is the cover rect: the largest canvas-aspect rectangle inside
    the source, which is what the centered crop always used. The view scales
    its dimensions by 1/scale and centers it on (x, y) in normalized source
    coordinates.

    Clamping is per dimension. A rectangle side that fits inside the source
    shifts fully inside, so zooming never letterboxes an edge it could
    avoid. A side longer than the source (zoomed out) keeps its center and
    overhangs; the renderer letterboxes the overhang.
    """
    source_w, source_h = source_size
    canvas_w, canvas_h = canvas_size
    x, y, scale = view

    if source_w * canvas_h > source_h * canvas_w:
        cover_h = float(source_h)
        cover_w = source_h * (canvas_w / canvas_h)
    else:
        cover_w = float(source_w)
        cover_h = source_w * (canvas_h / canvas_w)

    rect_w = cover_w / scale
    rect_h = cover_h / scale

    left = x * source_w - rect_w / 2
    top = y * source_h - rect_h / 2

    if rect_w <= source_w:
        left = min(max(left, 0.0), source_w - rect_w)
    if rect_h <= source_h:
        top = min(max(top, 0.0), source_h - rect_h)

    return (left, top, left + rect_w, top + rect_h)


def canonical_view(source_size: tuple[int, int], canvas_size: tuple[int, int],
                   view: tuple[float, float, float]) -> tuple[float, float, float]:
    """The view whose center is the center of the rectangle actually
    rendered.

    viewport_rect clamps the rectangle inside the source, so a center pushed
    past the honored range renders the same crop as the edge center. Storing
    the pushed value would persist a view the renderer ignores, make two
    pixel-identical crops read as different views (and name two video cache
    files), and leave a drag dead zone at each edge. Reading the center back
    from the clamped rectangle removes all three; at scale 1.0 with matching
    aspect it collapses every center to the default view.
    """
    left, top, right, bottom = viewport_rect(source_size, canvas_size, view)
    source_w, source_h = source_size
    return ((left + right) / 2 / source_w, (top + bottom) / 2 / source_h, view[2])


def render_viewport(image: Image.Image, canvas_size: tuple[int, int],
                    view: tuple[float, float, float],
                    resample: Image.Resampling = Image.Resampling.LANCZOS) -> Image.Image:
    """Render the view of image onto a canvas-sized RGBA image.

    A rectangle fully inside the source resizes straight from its float box,
    the same operation the centered cover crop performed. An overhanging
    rectangle (zoomed out) composites the visible part of the source onto a
    transparent canvas, and the caller's base shows through the rest. With
    the center clamped to the source and the scale bounded below, the
    rectangle always overlaps the source, so the visible part is never empty.
    """
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    left, top, right, bottom = viewport_rect(image.size, canvas_size, view)

    if left >= 0 and top >= 0 and right <= image.width and bottom <= image.height:
        return image.resize(canvas_size, resample, box=(left, top, right, bottom))

    out = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    inner_l = max(left, 0.0)
    inner_t = max(top, 0.0)
    inner_r = min(right, float(image.width))
    inner_b = min(bottom, float(image.height))

    to_canvas_x = canvas_size[0] / (right - left)
    to_canvas_y = canvas_size[1] / (bottom - top)
    piece_w = max(1, round((inner_r - inner_l) * to_canvas_x))
    piece_h = max(1, round((inner_b - inner_t) * to_canvas_y))
    piece = image.resize((piece_w, piece_h), resample,
                         box=(inner_l, inner_t, inner_r, inner_b))
    out.paste(piece, (round((inner_l - left) * to_canvas_x),
                      round((inner_t - top) * to_canvas_y)))
    return out
