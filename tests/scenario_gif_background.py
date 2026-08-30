"""Check PIL-based GIF backgrounds, alpha, key tiles, and strip slices."""
import json
import os

import fixtures
import globals as gl
from fixtures import start_watchdog, teardown

from PIL import Image, ImageDraw, ImageOps

from src.backend.DeckManagement.InputIdentifier import Input

DISC_COLOR = (200, 40, 60, 255)  # asymmetric R/B so a BGR swap can't hide


def _make_gif(path: str, size=(64, 64), n_frames: int = 4) -> str:
    """Build a GIF with a transparent background and an opaque shifting disc.

    The frames stay distinct, so PIL never merges any at save time.
    """
    frames = []
    for i in range(n_frames):
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        x0 = 8 + i * 4
        draw.ellipse([x0, 12, x0 + 28, 40], fill=DISC_COLOR)
        frames.append(frame)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=100, loop=0, disposal=2,
    )
    return path


def _reference_canvas(gif_path: str, frame_index: int, canvas_size) -> Image.Image:
    """Decode and fit one reference frame independently with PIL and LANCZOS."""
    with Image.open(gif_path) as gif:
        gif.seek(frame_index)
        return ImageOps.fit(gif.convert("RGBA"), tuple(canvas_size), Image.Resampling.LANCZOS)


def _seed_deck_background(serial: str, gif_path: str) -> None:
    """Seed the extended background before its startup worker can read settings."""
    path = os.path.join(gl.DATA_PATH, "settings", "decks", f"{serial}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        # loop and fps match the defaults of set_from_path, which is what the
        # check calls, so the two applications agree in every argument.
        json.dump({"background": {
            "enable": True,
            "extend-to-touchscreen": True,
            "media-path": gif_path,
            "loop": True,
            "fps": 30,
        }}, f)


def _await_startup_background(controller) -> None:
    """Wait for a published tile, then use the load lock to fence worker exit."""
    assert fixtures.wait_until(
        lambda: controller.background.get_identified_tile(0) is not None, timeout=10.0
    ), "the controller never published its deck-scope background"
    with controller._background_load_lock:
        pass


def check_deck_background(controller, gif_path: str) -> None:
    _await_startup_background(controller)

    background = controller.background
    background.set_extend_to_touchscreen(True, update=False)
    background.set_from_path(gif_path)

    video = background.video
    assert type(video).__name__ == "GifBackground", (
        f"(a) a .gif deck background must land a GifBackground, got {type(video).__name__}"
    )
    assert video.extend_touchscreen, "fixture sanity: the FakeDeck is touch-capable, extension must stick"

    # Alpha survives the decode. The retained canvas frames carry both fully
    # transparent and fully opaque pixels.
    alphas = video.frames[0].getchannel("A").getextrema()
    assert alphas[0] == 0 and alphas[1] == 255, (
        f"(c) alpha did not survive the background decode (extrema {alphas})"
    )

    # Read tile and identity atomically before comparing with the reference decode.
    identified = background.get_identified_tile(0)
    assert identified is not None, "(b) no identified tile published after set_from_path"
    tile, (md5, frame_index) = identified
    assert md5 == video.video_md5, "(b) identity must carry the provider's source md5"
    assert tile.mode == "RGBA", f"(c) background tiles must be RGBA, got {tile.mode}"

    ref_canvas = _reference_canvas(gif_path, frame_index, video.canvas_size)
    ref_tile = ref_canvas.crop(video._key_regions[0])
    assert tile.tobytes() == ref_tile.tobytes(), (
        "(b) key tile does not match the independent PIL reference decode -- "
        "palette/BGR/geometry mangling on the background path"
    )

    # The strip slice is published for the extended background.
    strip = background.get_touchscreen_image()
    assert strip is not None, "(d) extended GIF background must publish a strip slice"
    assert tuple(strip.size) == tuple(controller.get_touchscreen_image_size()), (
        f"(d) strip slice must be at strip resolution, got {strip.size}"
    )
    assert strip.mode == "RGBA", f"(d) strip slice must be RGBA, got {strip.mode}"

    print("PASS: deck background routed to GifBackground; tiles/strip match reference, alpha intact")


def check_strip_background_route(controller, gif_path: str) -> None:
    ts = controller.inputs[Input.Touchscreen][0]
    ts_state = ts.get_active_state()

    frame = ts_state._get_background_video_frame(gif_path, fps=30, loop=True)
    # Capture local references before a media tick can release this temporary video.
    bg_video = ts_state.background_video
    assert type(bg_video).__name__ == "GifBackground", (
        f"(e) a .gif strip background must land a GifBackground, got {type(bg_video).__name__}"
    )
    frame_index = bg_video.active_frame

    strip_dims = tuple(ts.get_screen_dimensions())
    assert frame is not None and frame.mode == "RGBA", (
        f"(e) strip route must return an RGBA frame, got {None if frame is None else frame.mode}"
    )
    assert tuple(frame.size) == strip_dims, (
        f"(e) strip route must return an EXACT strip-size frame "
        f"(alpha_composite needs same-size), got {frame.size} vs {strip_dims}"
    )

    ref = _reference_canvas(gif_path, frame_index, strip_dims)
    assert frame.tobytes() == ref.tobytes(), (
        "(e) strip frame does not match the independent PIL reference decode"
    )

    print("PASS: per-touchscreen strip background routed to GifBackground, exact-size RGBA")


def main() -> None:
    start_watchdog(60, label="scenario_gif_background")
    gif_path = _make_gif(os.path.join(gl.DATA_PATH, "media", "bg.gif"))

    _seed_deck_background("gif-bg-1", gif_path)
    controller = fixtures.make_headless_controller(serial="gif-bg-1")
    try:
        check_deck_background(controller, gif_path)
        check_strip_background_route(controller, gif_path)
        print("PASS: scenario_gif_background")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
