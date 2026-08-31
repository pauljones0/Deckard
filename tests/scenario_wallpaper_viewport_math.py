"""Verify default cover-crop equivalence and pan, zoom, clamp, and letterbox geometry.
The default view must match ImageOps.fit byte for byte."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

from PIL import Image, ImageOps

from fixtures import start_watchdog

from src.backend.DeckManagement.deck_controller import viewport
from src.backend.DeckManagement.deck_controller.viewport import (
    DEFAULT_VIEW,
    canonical_view,
    normalize_view,
    render_viewport,
    view_suffix,
    viewport_rect,
)


def gradient_image(width: int, height: int) -> Image.Image:
    img = Image.new("RGBA", (width, height))
    px = img.load()
    for yy in range(height):
        for xx in range(width):
            px[xx, yy] = (xx % 256, yy % 256, (xx * 7 + yy * 13) % 256, 255)
    return img


def close(a: float, b: float, tolerance: float = 1e-6) -> bool:
    return abs(a - b) <= tolerance


def main() -> int:
    start_watchdog(60, "wallpaper_viewport_math")
    failures: list[str] = []

    # Normalized shapes, clamps, and defaults
    if normalize_view(None) != DEFAULT_VIEW:
        failures.append("None did not read as the default view")
    if normalize_view({"x": "junk"}) != DEFAULT_VIEW:
        failures.append("a malformed value did not degrade to the default view")
    if normalize_view({"x": -3, "y": 9, "scale": 100}) != (0.0, 1.0, viewport.MAX_SCALE):
        failures.append("clamping did not apply to out-of-range values")
    if normalize_view({"scale": 2.0}) != (0.5, 0.5, 2.0):
        failures.append("a partial dict did not fill missing keys from the default")
    if normalize_view({"scale": float("nan")}) != DEFAULT_VIEW:
        failures.append("NaN slipped through the clamp; json.load produces it")
    if normalize_view({"x": float("inf")}) != DEFAULT_VIEW:
        failures.append("Infinity slipped through the clamp")

    # At scale 2, the 500-pixel crop clamps centers below 0.25 to the left edge.
    # canonical_view() therefore returns 0.25 for a 1000-pixel source.
    if canonical_view((1000, 500), (200, 100), (0.0, 0.5, 2.0)) != (0.25, 0.5, 2.0):
        failures.append("a pushed-past-the-edge center did not canonicalize to the edge")
    if canonical_view((1000, 500), (200, 100), (0.83, 0.5, 1.0)) != (0.5, 0.5, 1.0):
        failures.append("at scale 1 on a matching aspect every center must read as default")
    if canonical_view((1000, 500), (200, 100), (0.6, 0.5, 2.0)) != (0.6, 0.5, 2.0):
        failures.append("a center inside the honored range must not move")

    # Default and nondefault cache suffixes
    if view_suffix(DEFAULT_VIEW) != "":
        failures.append("the default view must produce no cache suffix")
    if view_suffix((0.5, 0.5, 1.00001)) != "":
        failures.append("a within-epsilon view must count as default")
    s = view_suffix((0.25, 0.75, 2.0))
    if not s or s != view_suffix((0.25, 0.75, 2.0)):
        failures.append("a non-default view must produce a stable non-empty suffix")

    # Default view matches ImageOps.fit byte for byte
    for source_size, canvas_size in [((640, 480), (372, 174)),
                                     ((480, 640), (372, 174)),
                                     ((800, 100), (372, 236)),
                                     ((372, 174), (372, 174))]:
        img = gradient_image(*source_size)
        ours = render_viewport(img, canvas_size, DEFAULT_VIEW)
        fit = ImageOps.fit(img, canvas_size, Image.Resampling.LANCZOS)
        if ours.tobytes() != fit.tobytes():
            failures.append(f"default view diverged from ImageOps.fit for "
                            f"{source_size} -> {canvas_size}")

    # Pan and zoom rectangle geometry
    rect1 = viewport_rect((1000, 500), (200, 100), (0.5, 0.5, 1.0))
    if not (close(rect1[0], 0) and close(rect1[1], 0)
            and close(rect1[2], 1000) and close(rect1[3], 500)):
        failures.append(f"matching aspect at scale 1 must cover the source, got {rect1}")

    rect2 = viewport_rect((1000, 500), (200, 100), (0.5, 0.5, 2.0))
    if not (close(rect2[2] - rect2[0], 500) and close(rect2[3] - rect2[1], 250)):
        failures.append(f"scale 2 must halve the rect dimensions, got {rect2}")

    rect_left = viewport_rect((1000, 500), (200, 100), (0.0, 0.5, 2.0))
    if not close(rect_left[0], 0):
        failures.append(f"panning to x=0 at zoom-in must clamp to the left edge, got {rect_left}")

    # Zoom-in clamping and centered zoom-out overhang
    rect_in = viewport_rect((1000, 500), (200, 100), (1.0, 1.0, 4.0))
    if rect_in[2] > 1000 or rect_in[3] > 500 or rect_in[0] < 0 or rect_in[1] < 0:
        failures.append(f"a zoom-in rect escaped the source: {rect_in}")

    rect_out = viewport_rect((1000, 500), (200, 100), (0.5, 0.5, 0.5))
    if not (rect_out[0] < 0 and rect_out[2] > 1000):
        failures.append(f"a zoom-out rect must overhang a matching-aspect source: {rect_out}")

    # A zoom-out side that still fits inside the source shifts inside
    # instead of letterboxing: a wide source with a small cover width.
    rect_fit = viewport_rect((4000, 500), (200, 100), (0.0, 0.5, 0.5))
    if rect_fit[0] < 0:
        failures.append(f"a zoom-out side that fits must clamp inside, got {rect_fit}")

    # Transparent centered letterbox for zoom-out
    img = gradient_image(400, 200)
    out = render_viewport(img, (200, 100), (0.5, 0.5, 0.5))
    if out.size != (200, 100):
        failures.append(f"render size wrong: {out.size}")
    else:
        if out.getpixel((0, 0))[3] != 0 or out.getpixel((199, 99))[3] != 0:
            failures.append("zoom-out corners must be transparent letterbox")
        if out.getpixel((100, 50))[3] != 255:
            failures.append("zoom-out center must show the source")

    # Corner-panned zoom-out keeps part of the source visible
    out2 = render_viewport(gradient_image(100, 100), (200, 100), (0.0, 0.0, 0.25))
    opaque = sum(1 for yy in range(100) for xx in range(200)
                 if out2.getpixel((xx, yy))[3] == 255)
    if not 0 < opaque < 200 * 100:
        failures.append(f"a corner-panned zoom-out must show part of the source "
                        f"and letterbox the rest, got {opaque} opaque of {200 * 100}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: viewport math holds in both regimes and the default view "
          "matches the old centered cover crop byte for byte")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
