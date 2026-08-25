import os

import cv2
import numpy as np
import numpy.typing as npt
from PIL import Image
from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.Subclasses.mp4_tile_cache import Mp4FrameCache, VID_CACHE
from src.backend.DeckManagement.deck_controller.strip_band import clamp_box
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
    """Background video, cached as a re-encoded video at deck-canvas resolution.

    Mp4FrameCache (mp4_tile_cache.py) owns the build, promote and
    decode-ahead discipline, which decodes the source once and then decodes
    each frame on demand from the cache mp4. This class keeps the tiling,
    strip and saturation-crop logic of the background path. That covers one
    instance, a build interleaved with playback ticks, and the on-disk layout
    and naming.
    """

    def __init__(self, video_path: str, deck_controller: "DeckController", extend_touchscreen: bool = False) -> None:
        self.deck_controller = deck_controller

        self.key_layout = self.deck_controller.deck.key_layout()
        self.key_count = self.deck_controller.deck.key_count()
        self.key_size = self.deck_controller.deck.key_image_format()['size']
        self.spacing = self.deck_controller.key_spacing

        # When the frame extends onto the touchscreen strip, it carries the
        # strip slice as one extra entry after the key tiles, and the canvas
        # is taller. Extended caches are therefore incompatible with plain
        # ones and live in their own directory.
        self.extend_touchscreen = extend_touchscreen and self.deck_controller.deck.is_touch()
        # One rotation read for the whole snapshot: strip_size and the band
        # side below must come from the same turn, or a rotation landing
        # between two live reads mixes frames.
        rotation = (self.deck_controller.deck.get_rotation()
                    if self.extend_touchscreen else 0)
        device_size = (self.deck_controller.device_touchscreen_image_size()
                       if self.extend_touchscreen else None)
        # The annotation follows the real value, a (width, height) pair.
        self.strip_size: tuple[int, int] | None = (
            ((device_size[1], device_size[0]) if rotation in (90, 270) else device_size)
            if device_size is not None else None)
        self.entries_per_frame = self.key_count + (1 if self.extend_touchscreen else 0)
        # Filled by _canvas_size(), before any crop runs: the canvas the
        # frames were encoded at, where the key grid starts inside it (the
        # band can overhang the grid, and it takes the top or the left edge
        # on a turned deck) and the band's own crop box. It is a snapshot,
        # so the render thread reads no controller state.
        self.band: "StripBand | None" = None

        # The strip geometry exists only for the extended canvas, so the
        # rotation is read only there, as GifBackground reads it. Without the
        # extension the canvas is the key grid alone and no band edge follows
        # from it.
        self.rotation = rotation
        self.key_layout_str = f"{self.key_layout[0]}x{self.key_layout[1]}"
        if self.extend_touchscreen:
            # The band edge is part of the frames, so a cache built for one
            # edge must not be served for another. The unturned deck keeps
            # the plain suffix, and its caches stay valid.
            side = STRIP_SIDES.get(self.rotation, "bottom")
            self.key_layout_str += "+strip" if side == "bottom" else f"+strip-{side}"

        self._legacy_cache_path: str | None = None  # set by _default_cache_path()

        saturation = deck_controller.get_display_saturation()
        super().__init__(video_path, out_size=self._canvas_size(), saturation=saturation)

    # Geometry and cache-path hooks.

    @override
    def _default_cache_path(self) -> str:
        # entry.split(".")[0] in video_cache_sweeper.py still resolves this to
        # video_md5 with the suffix present, because the suffix comes after
        # the first dot-delimited component. The sweeper needs no change.
        # The directory carries the canvas size. Two decks with the same key
        # layout but different key sizes or bands (an SD+ and a Neo are both
        # 2x4) must not resolve one file, or each open finds the other's
        # frame size, removes the file as stale and re-encodes, in both
        # directions. The legacy pickle kept the size-less directory.
        legacy_dir = os.path.join(VID_CACHE, self.key_layout_str)
        self._legacy_cache_path = os.path.join(legacy_dir, f"{self.video_md5}.cache")
        cache_dir = os.path.join(
            VID_CACHE, f"{self.key_layout_str}@{self.out_size[0]}x{self.out_size[1]}")
        return os.path.join(cache_dir, f"{self.video_md5}{self._sat_suffix}.mp4")

    def _canvas_size(self) -> tuple[int, int]:
        key_rows, key_cols = self.key_layout
        key_width, key_height = self.key_size
        spacing_x, spacing_y = self.spacing

        key_width *= key_cols
        key_height *= key_rows

        # Count the extra non-visible pixels that the deck bezel hides.
        grid = (key_width + spacing_x * (key_cols - 1),
                key_height + spacing_y * (key_rows - 1))

        # Extend the canvas to the union of the key grid and the strip's
        # view, the same strip_band layout as BackgroundImage: bigger by the
        # gap plus the band, bigger again where the band overhangs the grid,
        # and turned onto the edge the user sees the strip against.
        # Snapshot the layout here; the render thread must not call back
        # into controller state.
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
        # This instance is self-contained and decides for itself whether to
        # build. KeyVideoCache instead gates once through its registry's
        # acquire(). Both read the "performance.cache-videos" setting.
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

    # Frame access.

    def _require_strip_size(self) -> tuple[int, int]:
        """The strip size, or a raise.

        strip_size is set when extend_touchscreen is on, which is the only
        condition under which the strip helpers below run, so a None here
        means the pairing broke: a cache that reports the extension and holds
        no size for it. The raise contains that at one site instead of a None
        unpack three call sites deep.
        """
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
        # Mp4FrameCache.get_frame prefers self.last_payload, the last tile
        # list it decoded, over this call. It reaches here only when no decode
        # has succeeded yet.
        return self._generate_alpha_frame()

    def get_tiles(self, n: int) -> list[Image.Image]:
        frame = self.get_frame(n)
        if frame is None:
            # The fallback override below always answers, so the base's None
            # path never reaches a caller.
            return self._fallback_payload()
        return frame

    def get_tiles_and_index(self, n: int) -> tuple[list[Image.Image], int | None]:
        """get_tiles() plus the source frame index the tiles come from. The
        index is None when unknown. See Mp4FrameCache.get_frame_and_index."""
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
            # The same class of miss as _require_strip_size: a subclass or
            # refactor that reaches a strip crop before _canvas_size() filled
            # the layout. A raise beats the silent black strip a 0x0 crop
            # resizes into.
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

        # Compute the starting X and Y offsets into the full size image that
        # the requested key should display. origin is where the key grid
        # starts, which the band moves when it overhangs the grid or takes
        # the top or the left edge.
        start_x = origin[0] + col * (key_width + spacing_x)
        start_y = origin[1] + row * (key_height + spacing_y)

        # Compute the region of the larger deck image that is occupied by the given
        # key, and crop out that segment of the full image.
        region = (start_x, start_y, start_x + key_width, start_y + key_height)
        return image.crop(region)
