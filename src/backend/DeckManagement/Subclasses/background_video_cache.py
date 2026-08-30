import os

import cv2
import numpy as np
import numpy.typing as npt
from PIL import Image
from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.Subclasses.mp4_tile_cache import Mp4FrameCache, VID_CACHE
from src.backend.DeckManagement.deck_controller.strip_band import band_layout, clamp_box
from src.backend.DeckManagement.deck_controller.viewport import DEFAULT_VIEW

# Import typing
from typing import TYPE_CHECKING, override
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController

class BackgroundVideoCache(Mp4FrameCache[list[Image.Image]]):
    """Cache background video as deck-canvas MP4 and crop frames into tiles.
    Mp4FrameCache owns build, promotion, and decode-ahead behavior."""

    def __init__(self, video_path: str, deck_controller: "DeckController", extend_touchscreen: bool = False,
                 view: "tuple[float, float, float]" = DEFAULT_VIEW) -> None:
        self.deck_controller = deck_controller

        self.key_layout = self.deck_controller.deck.key_layout()
        self.key_count = self.deck_controller.deck.key_count()
        self.key_size = self.deck_controller.deck.key_image_format()['size']
        self.spacing = self.deck_controller.key_spacing

        # Extended frames append a strip slice after the key tiles.
        # Their larger canvas makes them incompatible with plain caches.
        self.extend_touchscreen = extend_touchscreen and self.deck_controller.deck.is_touch()
        self.strip_size: tuple[int, int] | None = (
            self.deck_controller.get_touchscreen_image_size()
            if self.extend_touchscreen else None)
        self.entries_per_frame = self.key_count + (1 if self.extend_touchscreen else 0)
        # _canvas_size() sets the grid offset and strip crop box before any crop.
        # Both use canvas coordinates because the strip can overhang the grid.
        self.grid_x = 0
        self.strip_band_box: "tuple[int, int, int, int] | None" = None

        self.key_layout_str = f"{self.key_layout[0]}x{self.key_layout[1]}"
        if self.extend_touchscreen:
            self.key_layout_str += "+strip"

        self._legacy_cache_path: str | None = None

        saturation = deck_controller.get_display_saturation()
        super().__init__(video_path, out_size=self._canvas_size(), saturation=saturation, view=view)

    @override
    def _default_cache_path(self) -> str:
        # Include canvas size so equal key layouts with different geometry cannot share files.
        # The suffix follows the first dot component, so the sweeper still extracts video_md5.
        legacy_dir = os.path.join(VID_CACHE, self.key_layout_str)
        self._legacy_cache_path = os.path.join(legacy_dir, f"{self.video_md5}.cache")
        cache_dir = os.path.join(
            VID_CACHE, f"{self.key_layout_str}@{self.out_size[0]}x{self.out_size[1]}")
        # The view joins the name like the saturation: a view change builds a
        # new cache file, and the default view keeps the pre-view name.
        return os.path.join(cache_dir, f"{self.video_md5}{self._sat_suffix}{self._view_suffix}.mp4")

    def _canvas_size(self) -> tuple[int, int]:
        key_rows, key_cols = self.key_layout
        key_width, key_height = self.key_size
        spacing_x, spacing_y = self.spacing

        key_width *= key_cols
        key_height *= key_rows

        # Compute the total number of extra non-visible pixels that are obscured by
        # the bezel of the StreamDeck.
        total_spacing_x = spacing_x * (key_cols - 1)
        total_spacing_y = spacing_y * (key_rows - 1)

        canvas_width = key_width + total_spacing_x
        canvas_height = key_height + total_spacing_y

        # Extend to the key-grid and strip union, including gap and overhang.
        # Snapshot layout here so the render thread does not read controller state.
        if self.extend_touchscreen:
            canvas_width, canvas_height, self.grid_x, self.strip_band_box = \
                band_layout(self.deck_controller, canvas_width, canvas_height)

        return (canvas_width, canvas_height)

    @override
    def _on_promoted(self) -> None:
        self._remove_legacy_cache()

    @override
    def _writer_enabled(self) -> bool:
        # Background caches gate each instance; key caches gate at registry acquisition.
        # Both use the cache-videos setting.
        return gl.settings_manager.app().cache_videos

    def _remove_legacy_cache(self) -> None:
        # A legacy cache is a bz2 pickle of raw frame tiles. It is large, and
        # no code here can read it.
        if self._legacy_cache_path and os.path.isfile(self._legacy_cache_path):
            try:
                os.remove(self._legacy_cache_path)
                log.info(f"Removed legacy pickle video cache {self._legacy_cache_path}")
            except OSError:
                pass

    def _require_strip_size(self) -> tuple[int, int]:
        """Return the strip size or raise for a plain cache or dead deck.
        An extended cache can lack a size when its deck disappears during construction."""
        strip_size = self.strip_size
        if strip_size is None:
            raise RuntimeError(
                "this background video cache has no touchscreen strip size "
                "(not extended, or the deck was already gone when it was built)")
        return strip_size

    def _generate_alpha_frame(self) -> list[Image.Image]:
        """Fallback frame of transparent key tiles, plus the strip slice when
        the frame extends onto the touchscreen."""
        entries = [self.deck_controller.generate_alpha_key() for _ in range(self.key_count)]
        if self.extend_touchscreen:
            entries.append(Image.new("RGBA", self._require_strip_size(), (0, 0, 0, 0)))
        return entries

    @override
    def _fallback_payload(self) -> list[Image.Image]:
        # Use transparent tiles only before any decode has produced a last payload.
        return self._generate_alpha_frame()

    def get_tiles(self, n: int) -> list[Image.Image]:
        frame = self.get_frame(n)
        if frame is None:
            # The fallback override guarantees a payload for the base None path.
            return self._fallback_payload()
        return frame

    def get_tiles_and_index(self, n: int) -> tuple[list[Image.Image], int | None]:
        """Return tiles and their source frame index, or None when unknown."""
        frame, index = self.get_frame_and_index(n)
        if frame is None:
            return self._fallback_payload(), None
        return frame, index

    @override
    def _payload_from_bgr(self, frame_bgr: npt.NDArray[np.uint8]) -> list[Image.Image]:
        # Crop the key tiles and the strip slice out of the canvas frame per
        # request, so no frame data stays in RAM beyond the decoder's buffers.
        canvas = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
        entries = [
            self.crop_key_image_from_deck_sized_image(canvas, key)
            for key in range(self.key_count)
        ]
        if self.extend_touchscreen:
            entries.append(self.crop_strip_from_deck_sized_image(canvas))
        return entries

    def crop_strip_from_deck_sized_image(self, image: Image.Image) -> Image.Image:
        """The strip's view of the extended canvas, at strip resolution."""
        if self.strip_band_box is None:
            # Raise if layout was not initialized instead of resizing a 0x0 crop to black.
            raise RuntimeError(
                "this background video cache has no strip band (the canvas "
                "size was never computed for an extended cache)")
        strip_slice = image.crop(clamp_box(self.strip_band_box, image.width, image.height))
        return strip_slice.resize(self._require_strip_size(), Image.Resampling.HAMMING)

    def crop_key_image_from_deck_sized_image(self, image: Image.Image, key: int) -> Image.Image:
        key_rows, key_cols = self.key_layout
        key_width, key_height = self.key_size
        spacing_x, spacing_y = self.spacing

        # Determine which row and column the requested key is located on.
        row = key // key_cols
        col = key % key_cols

        # Offset from grid_x because the strip band can overhang the key grid.
        start_x = self.grid_x + col * (key_width + spacing_x)
        start_y = row * (key_height + spacing_y)

        # Compute the region of the larger deck image that is occupied by the given
        # key, and crop out that segment of the full image.
        region = (start_x, start_y, start_x + key_width, start_y + key_height)
        return image.crop(region)
