"""Verify viewport rendering through deck media, live updates, slideshow entries, and pages.
A two-color source distinguishes centered and panned crops."""
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

        # Default and zoomed single media
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

        # Live composite update
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

        # Deck settings view during background load
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

        # Slideshow entries in both settings shapes
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
            # A live swap on the showing frame must land in the rotation's
            # own view table, or the frame reverts when the rotation returns.
            background.update_view((0.3, 0.5, 2.0))
            if show.views.get(two_tone) != (0.3, 0.5, 2.0):
                failures.append(f"update_view left the rotation's stored view "
                                f"stale: {show.views.get(two_tone)}")
            # A view for a frame not on screen is stored without touching the
            # frame that is, so the deck does not jump.
            showing_before = background.image
            if not background.set_slideshow_view(second, (0.6, 0.5, 3.0)):
                failures.append("set_slideshow_view did not find the rotation entry")
            if background.image is not showing_before:
                failures.append("set_slideshow_view swapped the showing frame")
            if show.views.get(second) != (0.6, 0.5, 3.0):
                failures.append(f"set_slideshow_view did not store: {show.views.get(second)}")
            if background.set_slideshow_view("/not/in/rotation.png", (0.5, 0.5, 2.0)):
                failures.append("set_slideshow_view claimed a path the rotation lacks")

        # Page override view
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
