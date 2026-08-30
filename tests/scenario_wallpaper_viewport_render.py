"""The viewport reaches the composite through every settings shape.

Four seams: a view on the deck's single media, the live update_view swap the
drag preview uses, slideshow entries in both stored shapes (plain path
strings and objects with per-image views), and the page background override.
The color assertions render a half-red half-blue source, so a zoomed view
shows one color where the default shows both.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import os

from PIL import Image

import globals as gl
from fixtures import make_headless_controller, start_watchdog, teardown


def write_two_tone(path: str, width: int = 640, height: int = 320) -> str:
    img = Image.new("RGB", (width, height), (255, 0, 0))
    img.paste(Image.new("RGB", (width // 2, height), (0, 0, 255)),
              (width // 2, 0))
    img.save(path)
    return path


def color_shares(canvas: Image.Image) -> tuple[float, float]:
    """(red share, blue share) of opaque pixels, sampled on a grid."""
    rgba = canvas.convert("RGBA")
    red = blue = total = 0
    for yy in range(0, rgba.height, 4):
        for xx in range(0, rgba.width, 4):
            r, g, b, a = rgba.getpixel((xx, yy))
            if a < 128:
                continue
            total += 1
            if r > 128 and b < 128:
                red += 1
            elif b > 128 and r < 128:
                blue += 1
    if total == 0:
        return (0.0, 0.0)
    return (red / total, blue / total)


def main() -> int:
    start_watchdog(90, "wallpaper_viewport_render")
    controller = make_headless_controller(serial="viewport-render-1")
    failures: list[str] = []
    try:
        media_dir = os.path.join(gl.DATA_PATH, "media")
        os.makedirs(media_dir, exist_ok=True)
        two_tone = write_two_tone(os.path.join(media_dir, "two_tone.png"))

        background = controller.background

        # --- A: single media, default vs zoomed view ------------------------
        background.set_from_path(two_tone, update=False)
        default_canvas = background.image.create_full_deck_sized_image()
        red_share, blue_share = color_shares(default_canvas)
        if red_share < 0.3 or blue_share < 0.3:
            failures.append(f"default view lost a half: red {red_share:.2f} "
                            f"blue {blue_share:.2f}")

        background.set_from_path(two_tone, update=False, view=(0.2, 0.5, 2.0))
        zoomed_canvas = background.image.create_full_deck_sized_image()
        red_share, blue_share = color_shares(zoomed_canvas)
        if red_share < 0.95:
            failures.append(f"a left-panned 2x zoom must show the red half, "
                            f"got red {red_share:.2f} blue {blue_share:.2f}")

        # --- B: update_view swaps the live composite ------------------------
        before = background.image.create_full_deck_sized_image().tobytes()
        applied = background.update_view((0.8, 0.5, 2.0))
        if not applied:
            failures.append("update_view on a still image must report live application")
        after = background.image.create_full_deck_sized_image().tobytes()
        if before == after:
            failures.append("update_view did not change the composite")
        red_share, blue_share = color_shares(background.image.create_full_deck_sized_image())
        if blue_share < 0.95:
            failures.append(f"a right-panned 2x zoom must show the blue half, "
                            f"got red {red_share:.2f} blue {blue_share:.2f}")

        # --- C: the deck settings carry the view into load_background -------
        page = controller.active_page
        deck_config = gl.settings_manager.deck(controller.serial_number())
        deck_config.set("background", "enable", True)
        deck_config.set("background", "media-path", two_tone)
        deck_config.set("background", "view", {"x": 0.2, "y": 0.5, "scale": 2.0})
        deck_config.save()
        controller.load_background(page, update=False)
        if background.image is None or background.image.view != (0.2, 0.5, 2.0):
            failures.append(f"the deck settings view did not reach the image: "
                            f"{None if background.image is None else background.image.view}")

        # --- D: slideshow entries in both shapes ----------------------------
        second = write_two_tone(os.path.join(media_dir, "two_tone_b.png"))
        deck_config.set("background", "media-paths", [
            {"path": two_tone, "view": {"x": 0.8, "y": 0.5, "scale": 2.0}},
            second,
        ])
        deck_config.save()
        controller.load_background(page, update=False)
        show = background.slideshow
        if show is None:
            failures.append("a two-entry list did not install a slideshow")
        else:
            if show.views.get(two_tone) != (0.8, 0.5, 2.0):
                failures.append(f"the object entry's view was lost: {show.views}")
            if show.views.get(second) != (0.5, 0.5, 1.0):
                failures.append(f"the legacy string entry must read as the "
                                f"default view: {show.views}")
            if background.image is None or background.image.view != (0.8, 0.5, 2.0):
                failures.append("the first slideshow frame did not render "
                                "through its own view")

        # --- E: the page override carries its own view ----------------------
        deck_config.set("background", "enable", False)
        deck_config.set("background", "media-paths", [])
        deck_config.save()
        page.dict.setdefault("settings", {})["background"] = {
            "overwrite": True, "show": True, "media-path": two_tone,
            "view": {"x": 0.5, "y": 0.5, "scale": 4.0},
        }
        controller.load_background(page, update=False)
        if background.image is None or background.image.view != (0.5, 0.5, 4.0):
            failures.append(f"the page override view did not reach the image: "
                            f"{None if background.image is None else background.image.view}")
    finally:
        teardown(controller)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: the viewport reaches the composite from the deck settings, "
          "the live update seam, both slideshow shapes and the page override")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
