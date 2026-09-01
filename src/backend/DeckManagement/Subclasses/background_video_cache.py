import os

import cv2
import numpy as np
import numpy.typing as npt
from PIL import Image
from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.Subclasses.mp4_tile_cache import Mp4FrameCache, VID_CACHE
from src.backend.DeckManagement.deck_controller.strip_band import clamp_box
from src.backend.DeckManagement.deck_controller.viewport import DEFAULT_VIEW
from src.backend.DeckManagement.strip_geometry import (
    STRIP_SIDES,
    StripBand,
    flat_band,
    oriented_band,
)

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
        # One rotation read for the whole snapshot: the grid spacing, strip_size,
        # and the band side below must come from the same turn, or a rotation
        # landing between two live reads mixes frames.
        rotation = self.deck_controller.deck.get_rotation()
        # key_layout() above already turned with the deck, so the asymmetric
        # SD+ gaps turn with it or every crop lands off its key.
        if rotation in (90, 270):
            self.spacing = (self.spacing[1], self.spacing[0])
        device_size = (self.deck_controller.device_touchscreen_image_size()
                       if self.extend_touchscreen else None)
        # The annotation follows the real value, a (width, height) pair.
        self.strip_size: tuple[int, int] | None = (
            ((device_size[1], device_size[0]) if rotation in (90, 270) else device_size)
            if device_size is not None else None)
        self.entries_per_frame = self.key_count + (1 if self.extend_touchscreen else 0)
        # _canvas_size() fills this before any crop runs: the encode canvas, the
        # key grid's origin inside it (the band can overhang the grid and takes
        # the top or the left edge on a turned deck), and the band's crop box.
        # It is a snapshot, so the render thread reads no controller state.
        self.band: "StripBand | None" = None

        # The band edge below exists only for the extended canvas; the plain
        # canvas is the key grid alone. The rotation itself still shapes that
        # grid through the turned layout and spacing.
        self.rotation = rotation
        self.key_layout_str = f"{self.key_layout[0]}x{self.key_layout[1]}"
        if self.extend_touchscreen:
            # The band edge is part of the frames, so a cache built for one
            # edge must not be served for another. The unturned deck keeps
            # the plain suffix, and its caches stay valid.
            side = STRIP_SIDES.get(self.rotation, "bottom")
            self.key_layout_str += "+strip" if side == "bottom" else f"+strip-{side}"

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
        # The quantized view suffix joins the cache name; equal encoded values share one file.
        # The default view keeps the suffix-free name.
        return os.path.join(cache_dir, f"{self.video_md5}{self._sat_suffix}{self._view_suffix}.mp4")

    def _canvas_size(self) -> tuple[int, int]:
        key_rows, key_cols = self.key_layout
        key_width, key_height = self.key_size
        spacing_x, spacing_y = self.spacing

        key_width *= key_cols
        key_height *= key_rows

        # Count the extra non-visible pixels that the deck bezel hides.
        grid = (key_width + spacing_x * (key_cols - 1),
                key_height + spacing_y * (key_rows - 1))

        # Extend to the key-grid and strip union, including gap and overhang --
        # the same strip_band layout BackgroundImage cuts from, turned onto the
        # edge the user sees the strip against. Snapshot layout here so the
        # render thread does not read controller state.
        band = flat_band(grid)
        if self.extend_touchscreen:
            band = oriented_band(self.deck_controller, self.rotation, grid)
        self.band = band
        return band.canvas_size

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
        band = self.band
        origin = (0, 0) if band is None else band.key_origin
        entries = [
            self.crop_key_image_from_deck_sized_image(canvas, key, origin)
            for key in range(self.key_count)
        ]
        if self.extend_touchscreen:
            entries.append(self.crop_strip_from_deck_sized_image(canvas, band))
        return entries

    def crop_strip_from_deck_sized_image(self, image: Image.Image,
                                         band: "StripBand | None" = None) -> Image.Image:
        """The strip's view of the extended canvas, at strip resolution. The
        band lies on the canvas edge the user sees beside the strip."""
        band = band or self.band
        if band is None:
            # Raise if layout was not initialized instead of resizing a 0x0 crop to black.
            raise RuntimeError(
                "this background video cache has no strip band (the canvas "
                "size was never computed for an extended cache)")
        strip_slice = image.crop(clamp_box(band.box, image.width, image.height))
        return strip_slice.resize(self._require_strip_size(), Image.Resampling.HAMMING)

    def crop_key_image_from_deck_sized_image(self, image: Image.Image, key: int,
                                             origin: "tuple[int, int]" = (0, 0)) -> Image.Image:
        key_rows, key_cols = self.key_layout
        key_width, key_height = self.key_size
        spacing_x, spacing_y = self.spacing

        # Determine which row and column the requested key is located on.
        row = key // key_cols
        col = key % key_cols

        # Offset from origin, where the key grid starts: the strip band moves it
        # when it overhangs the grid or takes the top or the left edge.
        start_x = origin[0] + col * (key_width + spacing_x)
        start_y = origin[1] + row * (key_height + spacing_y)

        # Compute the region of the larger deck image that is occupied by the given
        # key, and crop out that segment of the full image.
        region = (start_x, start_y, start_x + key_width, start_y + key_height)
        return image.crop(region)
