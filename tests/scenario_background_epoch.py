"""Prevent a stale unlocked video render from overwriting a newer still background.
A gate forces the race; a still image has no later tick to repair stale tiles."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import os  # noqa: E402
import threading  # noqa: E402

import globals as gl  # noqa: E402
from PIL import Image  # noqa: E402

from fixtures import make_test_png, start_watchdog  # noqa: E402


class GatedVideo:
    """A background video whose frame render blocks until released."""

    def __init__(self, tiles):
        self.extend_touchscreen = False
        self.gate = threading.Event()
        self.rendering = threading.Event()
        self._tiles = tiles
        self.closed = False

    def get_next_tiles(self):
        self.rendering.set()
        self.gate.wait(timeout=30)
        return list(self._tiles), ("gated-video-md5", 0)

    def close(self):
        self.closed = True


def main() -> int:
    start_watchdog(60, "background_epoch")
    controller = fixtures.make_headless_controller(serial="bg-epoch-1")
    failures: list[str] = []
    try:
        background = controller.background
        key_count = controller.deck.key_count()

        video_tiles = [Image.new("RGB", (72, 72), (255, 0, 0))] * key_count
        video = GatedVideo(video_tiles)

        # Install the video as the current source, as a page's video
        # background would stand right before a media tick renders a frame.
        with background._render_state_lock:
            background.image = None
            background.video = video  # type: ignore[assignment]  # the render surface is duck-typed

        # The old tick: it snapshots the video and blocks inside the render.
        tick = threading.Thread(target=background.update_tiles, name="stale-tick")
        tick.start()
        assert video.rendering.wait(timeout=10), "the gated render never started"

        # The newer state lands while the old render is still in flight: a
        # still image replaces the video and publishes its own tiles.
        image_path = make_test_png(
            os.path.join(gl.DATA_PATH, "assets", "epoch.png"),
            size=(400, 400), color=(0, 255, 0))
        from src.backend.DeckManagement.deck_controller.background_media import BackgroundImage
        with Image.open(image_path) as img:
            background.set_image(BackgroundImage(controller, img.copy(), path=image_path),
                                 update=False)
        still_tiles = background.tiles
        if not video.closed:
            failures.append("set_image did not close the replaced video")

        # Release the stale render. Its publish must be discarded: the tiles
        # stay the still's, and no red video frame lands.
        video.gate.set()
        tick.join(timeout=10)
        if tick.is_alive():
            failures.append("the stale tick never finished")
        if background.tiles is not still_tiles:
            failures.append("the stale video render overwrote the newer still tiles")
        if background.get_identified_tile(0) is not None:
            failures.append("the stale render published its frame identity")
    finally:
        fixtures.teardown(controller)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: a stale background render is discarded; the newer still wins")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
