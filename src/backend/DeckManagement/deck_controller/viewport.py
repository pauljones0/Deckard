"""Pan-and-zoom viewport math with a centered cover crop as the default view.
Persisted values normalize to bounded tuples; missing or malformed values use that default."""
import math
from typing import Any

from PIL import Image

# The lower zoom bound keeps a recognizable image slice on the canvas.
# The upper bound caps requested source resolution.
MIN_SCALE = 0.25
MAX_SCALE = 8.0

DEFAULT_VIEW: tuple[float, float, float] = (0.5, 0.5, 1.0)

# Shared default-view tolerance keeps render output and cache naming decisions aligned.
_EPSILON = 1e-4


def normalize_view(raw: Any) -> tuple[float, float, float]:
    """Read a settings dict as a finite, clamped (x, y, scale) tuple.
    Missing, malformed, or nonfinite values use the default; center and scale use their bounds."""
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
    """Return the settings dict for a view, or None for the default.
    This is the inverse of normalize_view()."""
    if is_default_view(view):
        return None
    return {"x": view[0], "y": view[1], "scale": view[2]}


def view_suffix(view: tuple[float, float, float]) -> str:
    """Return a dot-delimited fixed-point cache suffix, or empty for the default view.
    The format keeps hash parsing unambiguous and makes different views miss the cache."""
    if is_default_view(view):
        return ""
    return f".v{round(view[0] * 10000):04d}-{round(view[1] * 10000):04d}-{round(view[2] * 10000):05d}"


def render_viewport_rgb(image: Image.Image, canvas_size: tuple[int, int],
                        view: tuple[float, float, float],
                        resample: Image.Resampling = Image.Resampling.LANCZOS) -> Image.Image:
    """Render a viewport to opaque RGB, flattening transparent letterboxes onto black.
    Resize an in-bounds source box directly to avoid RGBA full-frame copies per video frame."""
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
    """Return slideshow (path, view) pairs from string or object entries.
    Strings use the default view; entries with empty or non-string paths are omitted."""
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
    """Return the cover rectangle scaled by 1/scale around normalized source point (x, y).
    Shift fitting sides inside the source; keep longer sides centered so they letterbox."""
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
    """Return a view centered on the rectangle that viewport_rect() actually renders.
    This removes edge dead zones and duplicate cache IDs; matching-aspect 1x becomes default."""
    left, top, right, bottom = viewport_rect(source_size, canvas_size, view)
    source_w, source_h = source_size
    return ((left + right) / 2 / source_w, (top + bottom) / 2 / source_h, view[2])


def render_viewport(image: Image.Image, canvas_size: tuple[int, int],
                    view: tuple[float, float, float],
                    resample: Image.Resampling = Image.Resampling.LANCZOS) -> Image.Image:
    """Render a viewport to RGBA, resizing in-bounds boxes and letterboxing overhangs.
    Bounded scale and center values guarantee that the source remains visible."""
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
