"""A viewport change misses every media cache instead of serving the old crop.

The video tile cache bakes the view into its frames, so the view joins its
file name the way the saturation does, the sweeper's name pattern must keep
recognizing such files, and the prebuild keep-check must rebuild on a view
change instead of keeping the old crop playing. The GIF provider decodes in
RAM, so for it the keep-check and the per-frame crop are the whole story.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import contextlib
import os

from PIL import Image

import globals as gl
from fixtures import make_headless_controller, make_test_mp4, start_watchdog, teardown

from src.backend.DeckManagement.deck_controller.background_media import BackgroundVideo
from src.backend.DeckManagement.Subclasses.video_cache_sweeper import _MP4_NAME_RE


def make_test_gif(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames = []
    for offset in (0, 64):
        # A gradient, not a solid color: a crop of a uniform frame is the
        # same bytes wherever the viewport sits, which hides the crop.
        frame = Image.new("RGBA", (64, 32))
        px = frame.load()
        for yy in range(32):
            for xx in range(64):
                px[xx, yy] = ((xx * 4 + offset) % 256, yy * 8 % 256, 60, 255)
        frames.append(frame)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=200, loop=0)
    return path


def main() -> int:
    start_watchdog(120, "wallpaper_viewport_cache")
    controller = make_headless_controller(serial="viewport-cache-1")
    failures: list[str] = []
    videos: "list[BackgroundVideo]" = []
    try:
        media_dir = os.path.join(gl.DATA_PATH, "media")
        video_path = make_test_mp4(os.path.join(media_dir, "vp_cache.mp4"))

        # --- A/B: the view joins the cache file name -------------------------
        plain = BackgroundVideo(controller, video_path)
        videos.append(plain)
        zoomed = BackgroundVideo(controller, video_path, view=(0.3, 0.5, 2.0))
        videos.append(zoomed)

        if ".v" in os.path.basename(plain.cache_path):
            failures.append(f"the default view must keep the pre-view cache "
                            f"name: {plain.cache_path}")
        if plain.cache_path == zoomed.cache_path:
            failures.append("a viewed cache resolved the default view's file")
        zoomed_name = os.path.basename(zoomed.cache_path)
        match = _MP4_NAME_RE.match(zoomed_name)
        if match is None:
            failures.append(f"the sweeper does not recognize a viewed cache "
                            f"name: {zoomed_name}")
        elif match.group("hash") != zoomed.video_md5:
            failures.append(f"the viewed cache name corrupts the md5 parse: "
                            f"{match.group('hash')}")

        # --- C: the prebuild keep-check rebuilds on a view change ------------
        background = controller.background
        background.set_from_path(video_path, update=False, view=(0.3, 0.5, 2.0))
        kind_same, payload_same = background.prebuild_from_path(
            video_path, view=(0.3, 0.5, 2.0))
        if kind_same != "keep":
            failures.append(f"an unchanged view must keep the playing video, "
                            f"got {kind_same}")
            background._discard_prebuilt(kind_same, payload_same)
        kind_new, payload_new = background.prebuild_from_path(
            video_path, view=(0.7, 0.5, 2.0))
        if kind_new != "video":
            failures.append(f"a view change must rebuild the video, got {kind_new}")
        background._discard_prebuilt(kind_new, payload_new)

        # --- D: the GIF provider bakes the view into its frames --------------
        gif_path = make_test_gif(os.path.join(media_dir, "vp_cache.gif"))
        background.set_from_path(gif_path, update=False)
        default_frames = [f.tobytes() for f in background.video.frames[:1]]
        background.set_from_path(gif_path, update=False, view=(0.0, 0.5, 2.0))
        if background.video.view != (0.0, 0.5, 2.0):
            failures.append("the GIF provider lost its view")
        zoomed_frames = [f.tobytes() for f in background.video.frames[:1]]
        if default_frames == zoomed_frames:
            failures.append("a zoomed GIF decoded the same frames as the default view")
    finally:
        for video in videos:
            with contextlib.suppress(Exception):
                video.close()
        teardown(controller)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: the view names the video cache, survives the sweeper's "
          "parse, forces the keep-check rebuild and re-decodes a GIF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
