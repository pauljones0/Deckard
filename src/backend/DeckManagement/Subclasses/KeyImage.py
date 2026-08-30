"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
from src.backend.DeckManagement.Subclasses.SingleKeyAsset import SingleKeyAsset
from PIL import Image, ImageEnhance

from typing import Any, TYPE_CHECKING, override
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput

class InputImage(SingleKeyAsset):
    # Fit for the UI's 200% maximum and retain 2x resolution headroom.
    # Unbounded plugin layouts can trigger a later source re-decode.
    MAX_LAYOUT_SCALE = 2.0

    def __init__(self, controller_input: "ControllerInput[Any]", image: Image.Image, path: str | None = None):
        """
        Initialize the class with the given controller key, image, fill mode, size, vertical alignment, and horizontal alignment.

        Parameters:
            controller_key (ControllerKey): The key of the controller.
            image (Image.Image): The image to be displayed.
            path (str, optional): The source file that image was decoded from,
                if any. None for plugin-supplied in-memory images and SVG
                thumbnails, which have no cheap higher-resolution re-decode.
                Kept so a later composed layout that needs more resolution
                than the fitted copy retains can re-decode from source instead
                of upscaling a blurry copy.
            fill_mode (str, optional): The mode for filling the image. Defaults to "cover".
            size (float, optional): The size of the image. Defaults to 1.
            valign (float, optional): The vertical alignment of the image. Defaults to 0. Ranges from -1 to 1.
            halign (float, optional): The horizontal alignment of the image. Defaults to 0. Ranges from -1 to 1.
        """
        super().__init__(controller_input)
        image = image.convert("RGBA")

        # Apply display saturation once to raw media before label compositing.
        # Retain the factor for later source re-decodes.
        self._saturation = self.deck_controller.get_display_saturation()
        if abs(self._saturation - 1.0) > 0.001:
            image = ImageEnhance.Color(image).enhance(self._saturation)

        self.path = path
        # Capture native source size on the first re-decode; the input can be fitted.
        self._source_native_size: tuple[int, int] | None = None
        # Readers guard both None and deletion after close().
        self.image: Image.Image | None = self._fit_to_budget(image)

    def _budget_size(self) -> "tuple[int, int] | None":
        """Return the largest retained resolution before a later re-decode.
        Return None without a visual target to avoid a lossy near-zero fit."""
        tile_w, tile_h = self.controller_input.get_image_size()
        if tile_w <= 0 or tile_h <= 0:
            return None
        return (
            int(tile_w * self.MAX_LAYOUT_SCALE * 2),
            int(tile_h * self.MAX_LAYOUT_SCALE * 2),
        )

    def _fit_to_budget(self, image: Image.Image) -> Image.Image:
        budget = self._budget_size()
        if budget is None:
            return image
        if image.width > budget[0] or image.height > budget[1]:
            # thumbnail() mutates in place and preserves the aspect ratio.
            image.thumbnail(budget, Image.Resampling.LANCZOS)
        return image

    def _ensure_fits_composed(self) -> None:
        """Re-decode when an unbounded composed layout outgrows the image.
        Without a source path, in-memory and SVG images upscale the retained copy."""
        if not self.path:
            return
        if not hasattr(self, "image") or self.image is None:
            return
        active_state = self.controller_input.get_active_state()
        if active_state is None:
            return
        tile_w, tile_h = self.controller_input.get_image_size()
        if tile_w <= 0 or tile_h <= 0:
            return
        layout = active_state.layout_manager.get_composed_layout()
        size = layout.size if layout.size is not None else 1
        needed_w = int(tile_w * max(size, 0))
        needed_h = int(tile_h * max(size, 0))
        # Clamp to memoized native size so undersized sources do not re-decode each frame.
        # Source paths must remain stable; media changes create a new InputImage.
        if self._source_native_size is not None:
            needed_w = min(needed_w, self._source_native_size[0])
            needed_h = min(needed_h, self._source_native_size[1])
        if needed_w <= self.image.width and needed_h <= self.image.height:
            return

        try:
            with Image.open(self.path) as fresh:
                native_size = fresh.size
                fresh = fresh.convert("RGBA")
        except (OSError, FileNotFoundError):
            return
        self._source_native_size = native_size

        if abs(self._saturation - 1.0) > 0.001:
            fresh = ImageEnhance.Color(fresh).enhance(self._saturation)

        # Refit for the requesting layout with 2x headroom.
        # Large plugin scales must not retain full source resolution unnecessarily.
        budget = (needed_w * 2, needed_h * 2)
        if fresh.width > budget[0] or fresh.height > budget[1]:
            fresh.thumbnail(budget, Image.Resampling.LANCZOS)

        # Do not close the old image while the media thread can still composite it.
        # Drop the reference so collection waits for the last composite.
        self.image = fresh

    @override
    def get_raw_image(self) -> Image.Image | None:
        if not hasattr(self, "image") or self.image is None:
            return None
        self._ensure_fits_composed()
        return self.image

    @override
    def close(self) -> None:
        if not hasattr(self, "image"):
            # Already closed
            return
        if self.image is not None:
            self.image.close()
        self.image = None
        del self.image
        return
