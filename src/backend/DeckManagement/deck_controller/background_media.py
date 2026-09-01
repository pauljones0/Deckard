"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

Background composites the loaded media into the key tiles, and into the
touchscreen strip slice on an SD+. Media resolution runs in two phases:
prebuild_from_path() builds the new object lock-free because a video hash
and a capture open take seconds; apply_prebuilt() swaps under the lock.
"""
import gc
import os
import threading
import time

from PIL import Image, ImageEnhance
from loguru import logger as log

from src.backend.DeckManagement.HelperMethods import is_image, is_video
from src.backend.DeckManagement.Subclasses.background_video_cache import BackgroundVideoCache
from src.backend.DeckManagement import media_loop
from src.backend.DeckManagement.media_loop import MEDIA_LOOP_FPS, FrameScheduled
from src.backend.DeckManagement.deck_controller.gif_pipeline import GifBackground, GifBudgetExceeded
from src.backend.DeckManagement.deck_controller.slideshow import IN_ORDER, Slideshow
from src.backend.DeckManagement.deck_controller.strip_band import band_layout, clamp_box
from src.backend.DeckManagement.deck_controller.viewport import (
    DEFAULT_VIEW, media_entries, normalize_view, render_viewport,
)
from src.backend.DeckManagement.strip_geometry import StripBand, flat_band, oriented_band

from typing import TYPE_CHECKING, Any, cast, override
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.PageManagement.Page import Page


def background_canvas_size(deck_controller: "DeckController", extend_touchscreen: bool) -> "tuple[int, int] | None":
    """Return the shared key-grid canvas, with bezel spacing and an enabled touchscreen strip.
    Return None when deck geometry is unavailable."""
    deck = getattr(deck_controller, "deck", None)
    if deck is None:
        return None
    key_rows, key_cols = deck.key_layout()
    key_width, key_height = deck_controller.get_key_image_size()
    spacing_x, spacing_y = deck_controller.key_spacing
    canvas_width = key_width * key_cols + spacing_x * (key_cols - 1)
    canvas_height = key_height * key_rows + spacing_y * (key_rows - 1)
    if extend_touchscreen and deck.is_touch():
        canvas_width, canvas_height, _grid_x, _band = \
            band_layout(deck_controller, canvas_width, canvas_height)
    return (canvas_width, canvas_height)


def resolve_background_entries(config: "Mapping[str, Any]") -> "list[tuple[str, tuple[float, float, float]]]":
    """Return all existing image-list entries, or the configured single path if none remain.
    The single path can be missing; return empty when it is absent or empty."""
    pairs = [(p, v) for p, v in media_entries(config.get("media-paths")) if is_image(p)]
    if pairs:
        return pairs
    single = config.get("media-path")
    if isinstance(single, str) and single:
        return [(single, normalize_view(config.get("view")))]
    return []


class Background:
    def __init__(self, deck_controller: "DeckController"):
        self.deck_controller = deck_controller

        # Guard tiles, strip slices, and identified tiles across media and load threads.
        # Keep this leaf lock away from deck calls to prevent lock-order inversion.
        self._render_state_lock = threading.RLock()

        # Increment on each source swap and publish rendered tiles only for the same epoch.
        # This stops a late old-source render from replacing a newer still image permanently.
        self._source_epoch = 0

        self.image: "BackgroundImage | None" = None
        # Either video provider: the cv2-backed one, or the PIL GIF one,
        # which carries the same playback surface without subclassing it.
        self.video: "BackgroundVideo | GifBackground | None" = None

        # A slideshow owns still-image order and timing and is mutually exclusive with video.
        # Single-image, video, and blank swaps clear it unless advancing its own frame.
        self.slideshow: Slideshow | None = None

        # Memoize still-image strip slices; refresh video strips once per frame.
        self.extend_to_touchscreen: bool = False
        self._touchscreen_slice: Image.Image | None = None
        self._video_strip: Image.Image | None = None

        # Replace tile sequences atomically; video fallback can supply None entries.
        self.tiles: Sequence[Image.Image | None] = [None] * deck_controller.deck.key_count()
        # Publish tiles with video MD5 and actual frame index only when identity is known.
        self._identified_tiles: "tuple[Sequence[Image.Image | None], tuple[str, int]] | None" = None

    def set_image(self, image: "BackgroundImage", update: bool = True,
                  _keep_slideshow: bool = False) -> None:
        # Publish under the leaf lock, then close video and clear caches outside it.
        # Preserve slideshow only for its own frame advance; external image sets end it.
        with self._render_state_lock:
            old_video = self.video
            self.image = image
            self.video = None
            self._source_epoch += 1
            if not _keep_slideshow:
                self.slideshow = None
            self._touchscreen_slice = None
            self._video_strip = None
            # Clear native entries orphaned by the background content change.
            self._identified_tiles = None
        if old_video is not None:
            old_video.close()
        self.deck_controller.clear_encoded_key_caches()
        self.deck_controller.refresh_tile_cache_min_age(None)
        if not _keep_slideshow:
            # Do not collect during slideshow advances on the sole writer.
            # Refcounting frees those acyclic frames; external page-load swaps can collect.
            gc.collect()

        self.update_tiles()
        if update:
            self.deck_controller.update_all_inputs()

    def set_video(self, video: "BackgroundVideo | GifBackground | None", update: bool = True) -> None:
        with self._render_state_lock:
            old_video = self.video
            self.image = None
            self.video = video
            self._source_epoch += 1
            # A video and a slideshow are mutually exclusive. Setting a video
            # ends any rotation, so slideshow_tick() stops advancing.
            self.slideshow = None
            self._touchscreen_slice = None
            self._video_strip = None
            # Clear old-video native entries; MD5 keys already prevent source collisions.
            self._identified_tiles = None
        if old_video is not None:
            old_video.close()
        self.deck_controller.clear_encoded_key_caches()
        # Shield the frame entries for the new video's loop duration.
        self.deck_controller.refresh_tile_cache_min_age(video)
        gc.collect()

        self.update_tiles()
        if update:
            self.deck_controller.update_all_inputs()

    def set_slideshow(self, paths: "Sequence[str]", interval: float, order: str = IN_ORDER,
                      update: bool = True, now: "float | None" = None,
                      views: "Sequence[tuple[float, float, float]] | None" = None) -> None:
        """Install an ordered or shuffled still rotation with aligned views.
        Fewer than two listed paths installs one still or clears after a failed load."""
        show = Slideshow(paths, interval, order=order)
        # Bind to page identity so a stale worker result cannot advance on a new active page.
        show.page = self.deck_controller.active_page
        # Align views by position; duplicate paths share one path-keyed view.
        if views is not None and len(views) != len(paths):
            log.warning(f"Slideshow views ({len(views)}) do not align with paths ({len(paths)}); "
                        "rendering every frame through the default view")
            views = None
        show.views = {p: v for p, v in zip(paths, views)} if views is not None else {}
        # Clear the old rotation before lock-free first-frame installation.
        # A racing media tick then no-ops instead of overwriting the new frame.
        with self._render_state_lock:
            self.slideshow = None
        first = show.current_path()
        # Build the first frame lock-free and preserve the cleared rotation slot during swap.
        # Skip stale, video, or otherwise unloadable paths.
        installed = (self._install_slideshow_frame(
            first, update=update, keep=True,
            view=show.views.get(first, DEFAULT_VIEW)) if first else False)
        if not installed and len(show) <= 1:
            # Blank an empty or single-unloadable slideshow instead of retaining old content.
            self.set_image_to_blank(update=update)
            return
        show.seed(time.monotonic() if now is None else now)
        with self._render_state_lock:
            self.slideshow = show

    def set_image_to_blank(self, update: bool = True) -> None:
        """Clear the background to nothing. This ends any video or slideshow
        and paints alpha keys on the next tile refresh."""
        self.set_video(None, update=False)
        with self._render_state_lock:
            self._touchscreen_slice = None
        self.update_tiles()
        if update:
            self.deck_controller.update_all_inputs()

    def slideshow_tick(self, now: "float | None" = None) -> bool:
        """Advance a due slideshow and return whether this pass swapped its image.
        Video, single-image, and blank backgrounds return after one attribute check."""
        show = self.slideshow
        if show is None:
            return False
        # Advance only for the active page because reload follows page change on a worker.
        # A stale due rotation must not place its next image on the new page.
        if show.page is not self.deck_controller.active_page:
            return False
        now = time.monotonic() if now is None else now
        next_path = show.maybe_advance(now)
        if next_path is None:
            return False
        return self._install_slideshow_frame(next_path, update=True, keep=True,
                                             view=show.views.get(next_path, DEFAULT_VIEW))

    def _install_slideshow_frame(self, path: "str | None", update: bool, keep: bool,
                                 view: "tuple[float, float, float]" = DEFAULT_VIEW) -> bool:
        """Load and swap one still through its view, retaining the previous frame on failure.
        Return success; keep preserves the rotation through its own frame swap."""
        if not path:
            return False
        try:
            # Skip corrupt files here so decode errors do not escape into the per-tick media guard.
            kind, payload = self.prebuild_from_path(path, allow_keep=False, view=view)
        except Exception:
            log.opt(exception=True).warning(
                f"Slideshow frame failed to decode, skipping it: {path}"
            )
            return False
        if kind == "image":
            self.set_image(cast("BackgroundImage", payload), update=update, _keep_slideshow=keep)
            return True
        # A video or non-file entry has no place in a still rotation. Release
        # whatever the prebuild built and leave the current frame showing.
        self._discard_prebuilt(kind, payload)
        return False

    def update_view(self, view: "tuple[float, float, float]") -> bool:
        """Set a still view before the epoch increment; post-increment composers see the new view.
        Pre-increment results are rejected; video and GIF views return False for reload."""
        with self._render_state_lock:
            image = self.image
            show = self.slideshow
        if image is None:
            return False
        image.set_view(view)
        # Keep the showing slideshow frame's stored view in sync so it does not revert
        # when the rotation returns to it.
        if show is not None and image.path is not None and image.path in show.views:
            show.views[image.path] = view
        with self._render_state_lock:
            self._source_epoch += 1
            self._touchscreen_slice = None
            self._identified_tiles = None
        # The composited-key and native caches hold the previous crop.
        self.deck_controller.clear_encoded_key_caches()
        self.deck_controller.refresh_tile_cache_min_age(None)
        self.update_tiles()
        self.deck_controller.update_all_inputs()
        return True

    def set_slideshow_view(self, path: str, view: "tuple[float, float, float]") -> bool:
        """Store a slideshow image's view for its next render without changing the current frame.
        Return False when the active rotation does not contain the path."""
        with self._render_state_lock:
            show = self.slideshow
        if show is None or path not in show.views:
            return False
        show.views[path] = view
        return True

    def showing_path(self) -> "str | None":
        """The file path of the still on screen, or None for a video, a GIF
        or a blank background."""
        with self._render_state_lock:
            image = self.image
        return image.path if image is not None else None

    def set_extend_to_touchscreen(self, extend: bool, update: bool = True) -> None:
        if extend == self.extend_to_touchscreen:
            return
        with self._render_state_lock:
            self.extend_to_touchscreen = extend
            self._touchscreen_slice = None
            self._video_strip = None

        self.update_tiles()
        if update:
            self.deck_controller.update_all_inputs()

    def _extend_effective(self) -> bool:
        return (
            self.extend_to_touchscreen
            and self.image is not None
            and self.deck_controller.deck.is_touch()
        )

    def get_touchscreen_image(self) -> Image.Image | None:
        """The strip-sized slice of the current background (image or video
        frame), or None if the background does not extend to the touchscreen."""
        with self._render_state_lock:
            if self.video is not None:
                # update_tiles() refreshes this once per video frame. None
                # unless the video carries extend_touchscreen.
                return self._video_strip
            image = self.image
            if image is None or not self._extend_effective():
                return None
            if self._touchscreen_slice is None:
                self._touchscreen_slice = image.get_touchscreen_image()
            return self._touchscreen_slice

    def prebuild_from_path(self, path: str | None, fps: int = MEDIA_LOOP_FPS, loop: bool = True,
                           allow_keep: bool = True,
                           view: "tuple[float, float, float]" = DEFAULT_VIEW) -> "tuple[str, BackgroundVideo | GifBackground | BackgroundImage | str | None]":
        """Build a background payload without mutating render state, then return its action kind.
        Kinds are blank, noop, keep, video, or image for apply_prebuilt."""
        if path == "":
            path = None
        if path is None:
            return ("blank", None)
        if is_video(path):
            extend = self.extend_to_touchscreen and self.deck_controller.deck.is_touch()
            if allow_keep:
                # Rebuild the same path when extension geometry or baked saturation changes.
                if (self.video is not None and self.video.video_path == path
                        and self.video.extend_touchscreen == extend
                        and self.video.view == view
                        and abs(self.video.saturation - self.deck_controller.get_display_saturation()) <= 0.001):
                    # Carry the path so apply_prebuilt validates this lock-free keep verdict.
                    # GIF fallback shares these fields and avoids repeated failed PIL decode.
                    return ("keep", path)
            if os.path.splitext(path)[1].lower() == ".gif":
                # Use PIL for GIF alpha and frame delays; cv2 loses both.
                # Fall back to opaque source-fps cv2 on decode or memory-budget failure.
                try:
                    return ("video", GifBackground(self.deck_controller, path, loop=loop, fps=fps, extend_touchscreen=extend, view=view))
                except GifBudgetExceeded as e:
                    log.warning(f"GIF background over budget, falling back to the opaque cv2 path: {e}")
                except Exception:
                    log.opt(exception=True).warning(f"GIF background decode failed, falling back to the opaque cv2 path: {path}")
            return ("video", BackgroundVideo(self.deck_controller, path, loop=loop, fps=fps, extend_touchscreen=extend, view=view))
        if not os.path.isfile(path):
            return ("noop", None)
        with Image.open(path) as image:
            return ("image", BackgroundImage(self.deck_controller, image.copy(), path=path, view=view))

    def _discard_prebuilt(self, kind: str, payload: "BackgroundVideo | GifBackground | BackgroundImage | str | None") -> None:
        """Close an unapplied video or image payload to release capture or PIL resources.
        Keep, noop, and blank payloads own nothing."""
        if kind not in ("video", "image") or payload is None or isinstance(payload, str):
            return
        try:
            payload.close()
        except Exception:
            log.opt(exception=True).warning(
                "Failed to close an orphaned prebuilt background payload during close()"
            )

    def apply_prebuilt(self, kind: str, payload: "BackgroundVideo | GifBackground | BackgroundImage | str | None", fps: int = MEDIA_LOOP_FPS, loop: bool = True, update: bool = True) -> None:
        """Apply a prebuilt payload without file I/O and update all inputs.
        Screensaver transitions call this under their load lock after generation validation."""
        # Reject payloads after closing starts; the resource sweep cannot release a late attachment.
        if getattr(self.deck_controller, "_closing", False):
            self._discard_prebuilt(kind, payload)
            return
        if kind == "noop":
            return
        if kind == "keep":
            # Recheck lock-free keep so it cannot change a raced-in video's settings.
            if self.video is not None and self.video.video_path == payload:
                self.video.page = self.deck_controller.active_page
                self.video.fps = fps
                self.video.loop = loop
            else:
                log.warning("Stale 'keep' background verdict (video swapped mid-transition); leaving current background untouched")
            return
        if kind == "video":
            # prebuild pairs the video kind with a video payload and the
            # image kind with an image; the casts state that pairing.
            self.set_video(cast("BackgroundVideo | GifBackground", payload), update=update)
        elif kind == "image":
            self.set_image(cast("BackgroundImage", payload), update=update)
        else:  # "blank"
            self.set_image_to_blank(update=update)

    def set_from_path(self, path: str | None, fps: int = MEDIA_LOOP_FPS, loop: bool = True, update: bool = True, allow_keep: bool = True,
                      view: "tuple[float, float, float]" = DEFAULT_VIEW) -> None:
        """Prebuild and apply for callers serialized by background or screensaver loading."""
        kind, payload = self.prebuild_from_path(
            path, fps=fps, loop=loop, allow_keep=allow_keep, view=view)
        self.apply_prebuilt(kind, payload, fps=fps, loop=loop, update=update)

    def get_identified_tile(self, key_index: int) -> "tuple[Image.Image, tuple[str, int]] | None":
        """Return a video tile with its MD5 and actual frame index, or None.
        Pixels and identity publish as one pair across media, GTK, and screensaver updates."""
        pair = self._identified_tiles
        if pair is None:
            return None
        tiles, identity = pair
        if key_index >= len(tiles):
            return None
        tile = tiles[key_index]
        if tile is None:
            return None
        return tile, identity

    def update_tiles(self) -> None:
        # Refcounting reclaims the old tiles. A close() here races a
        # concurrent composite that still holds one.
        try:
            # Snapshot source and epoch under the lock, then compose only from local references.
            with self._render_state_lock:
                image = self.image
                video = self.video
                epoch = self._source_epoch
            identity = None
            # Compose outside the leaf lock, then publish tiles, strip, and identity together.
            # Write video strip only when produced so stale work cannot clobber a concurrent reset.
            new_video_strip = None
            wrote_strip = False
            if image is not None:
                new_tiles: "Sequence[Image.Image | None]" = image.get_tiles(extend_touchscreen=self._extend_effective())
            elif video is not None:
                # An extended video frame carries the strip slice as one extra
                # entry after the key tiles. See BackgroundVideoCache.
                entries, identity = video.get_next_tiles()
                key_count = self.deck_controller.deck.key_count()
                if video.extend_touchscreen and len(entries) > key_count:
                    new_video_strip = entries[key_count]
                    wrote_strip = True
                    entries = entries[:key_count]
                new_tiles = entries
            else:
                new_tiles = [self.deck_controller.generate_alpha_key() for _ in range(self.deck_controller.deck.key_count())]
            with self._render_state_lock:
                if self._source_epoch != epoch:
                    # Discard a frame rendered across a source swap; do not replace newer content.
                    return
                self.tiles = new_tiles
                if wrote_strip:
                    self._video_strip = new_video_strip
                self._identified_tiles = None if identity is None else (new_tiles, identity)
        except Exception:
            # Keep old tiles and rate-limit repeated failures instead of killing the media thread.
            now = time.time()
            if now - getattr(self, "_last_tile_error_log", 0) > 10:
                self._last_tile_error_log = now
                log.opt(exception=True).error("Failed to update background tiles; keeping previous")

class BackgroundImage:
    def __init__(self, deck_controller: "DeckController", image: Image.Image, path: str | None = None,
                 view: "tuple[float, float, float]" = DEFAULT_VIEW) -> None:
        self.deck_controller = deck_controller
        # Retain source path for re-decode when runtime strip extension outgrows the fitted image.
        self.path = path

        # Normalized (x, y, scale) viewport; the default is the centered cover crop.
        # set_view() swaps it without replacing this background object.
        self.view = view

        # Bake saturation once for both key tiles and strip; 1.0 preserves original bytes.
        image = self._prepare_image(image)
        # Record source resolution so compose skips decodes that cannot recover more pixels.
        self._native_size: tuple[int, int] = image.size
        # Set by _fit_to_canvas; the budget the retained copy was fitted for.
        self._fitted_budget: tuple[int, int] = image.size
        # close() sets this to None. _ensure_fits_canvas() and
        # create_full_deck_sized_image() both handle the released state.
        self.image: Image.Image | None = self._fit_to_canvas(image, self._extend_effective())

    def set_view(self, view: "tuple[float, float, float]") -> None:
        """Swap the viewport, and re-decode when the zoom now demands more
        source resolution than the retained, budgeted copy holds."""
        self.view = view
        self._ensure_fits_canvas(self._extend_effective())

    def _extend_effective(self) -> bool:
        # Read extension from Background but test only touch capability, not current image presence.
        background = getattr(self.deck_controller, "background", None)
        extend = bool(getattr(background, "extend_to_touchscreen", False)) if background is not None else False
        deck = getattr(self.deck_controller, "deck", None)
        return extend and deck is not None and deck.is_touch()

    def _prepare_image(self, image: Image.Image) -> Image.Image:
        saturation = self.deck_controller.get_display_saturation()
        if abs(saturation - 1.0) > 0.001:
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            image = ImageEnhance.Color(image).enhance(saturation)
        return image

    def _grid_size(self) -> tuple[int, int]:
        """The key grid's canvas size: keys plus the bezel spacing, in the
        frame the user sees, because key_layout() already turns with the
        deck."""
        key_rows, key_cols = self.deck_controller.deck.key_layout()
        key_width, key_height = self.deck_controller.get_key_image_size()
        spacing_x, spacing_y = self.deck_controller.key_spacing
        return (key_width * key_cols + spacing_x * (key_cols - 1),
                key_height * key_rows + spacing_y * (key_rows - 1))

    def _band(self, extend_touchscreen: bool) -> "StripBand | None":
        """Where the strip band lies beside the key grid, or None when the
        deck reports no geometry at all.

        A canvas that carries no band, because the deck has no strip or the
        extension is off, is the key grid alone. Otherwise the band comes
        from strip_band, turned into the frame the user sees, so the
        wallpaper continues onto the strip along the edge the user sees it
        against.
        """
        deck = getattr(self.deck_controller, "deck", None)
        if deck is None:
            return None
        grid = self._grid_size()
        if not extend_touchscreen or not deck.is_touch():
            return flat_band(grid)
        return oriented_band(self.deck_controller, deck.get_rotation(), grid)

    def _canvas_size(self, extend_touchscreen: bool) -> "tuple[int, int] | None":
        """Return target canvas size, including strip extension when active.
        Return None without deck geometry so callers skip fitting and re-decode.
        The band gives the canvas in the frame the user sees, so its shape
        follows the deck's rotation."""
        band = self._band(extend_touchscreen)
        return None if band is None else band.canvas_size

    def _budget_multiplier(self) -> float:
        """Return the retained source-resolution multiplier in canvas widths.
        Clamp it from 1x through 4x to bound memory; views above 4x can soften."""
        return min(max(self.view[2], 1.0), 4.0)

    def _budget(self, canvas: tuple[int, int]) -> tuple[int, int]:
        multiplier = 2 * self._budget_multiplier()
        return (int(canvas[0] * multiplier), int(canvas[1] * multiplier))

    def _fit_to_canvas(self, image: Image.Image, extend_touchscreen: bool) -> Image.Image:
        canvas = self._canvas_size(extend_touchscreen)
        if canvas is None:
            return image
        budget = self._budget(canvas)
        if image.width > budget[0] or image.height > budget[1]:
            image.thumbnail(budget, Image.Resampling.LANCZOS)
        # Store the requested budget, not thumbnail pixels, because aspect mismatch can keep one
        # pixel axis below demand and otherwise cause a decode on every compose.
        self._fitted_budget = budget
        return image

    def _ensure_fits_canvas(self, extend_touchscreen: bool) -> None:
        """Refit or re-decode when extension or zoom changes the resolution budget.
        Shrink held pixels when demand falls; decode at most once per growth step."""
        if not self.path or self.image is None:
            return
        canvas = self._canvas_size(extend_touchscreen)
        if canvas is None:
            return
        budget = self._budget(canvas)
        fitted = self._fitted_budget
        if budget[0] <= fitted[0] and budget[1] <= fitted[1]:
            if budget != fitted:
                self.image = self._fit_to_canvas(self.image, extend_touchscreen)
            return
        # The source has no more pixels than the fitted copy already holds,
        # so a decode would produce the same image again.
        if self._native_size[0] <= fitted[0] and self._native_size[1] <= fitted[1]:
            self._fitted_budget = budget
            return
        try:
            with Image.open(self.path) as fresh:
                fresh = fresh.copy()
        except (OSError, FileNotFoundError):
            return
        fresh = self._prepare_image(fresh)
        self._native_size = fresh.size
        # Drop the previous copy without closing it because the media thread can still render it.
        # The pixels are freed after the final reference is released.
        self.image = self._fit_to_canvas(fresh, extend_touchscreen)

    def close(self) -> None:
        """Release the retained source-resolution PIL image."""
        if self.image is not None:
            self.image.close()
            self.image = None

    def create_full_deck_sized_image(self, extend_touchscreen: bool = False) -> Image.Image:
        self._ensure_fits_canvas(extend_touchscreen)
        # The canvas covers the grid and strip union, including calibrated SD+
        # overhang and bezel gap. _band turns that layout onto the edge the
        # user sees the strip against, so the shape follows the rotation.
        band = self._band(extend_touchscreen)
        if band is None:
            raise RuntimeError("the deck reports no geometry to fit a background to")
        canvas_width, canvas_height = band.canvas_size

        # Raise after close so update_tiles retains old tiles and logs the failure.
        # A transparent fallback would silently blank every key on each refresh.
        source = self.image
        if source is None:
            raise RuntimeError(
                "background image was released (close()) while its tiles were "
                "still being composed"
            )

        # Convert before resizing to preserve alpha; zoomed-out regions expose the black deck base.
        # The default view remains a centered cover crop.
        img_rgba = source.convert("RGBA")
        return render_viewport(img_rgba, (canvas_width, canvas_height), self.view)

    def get_touchscreen_image(self) -> Image.Image:
        """The strip's view of the extended canvas, at strip resolution.

        The band is cut from the edge of the wallpaper the user sees beside
        the strip, which moves with the deck's rotation. The result is the
        logical strip size, so it stands on its side on a quarter-turned
        deck."""
        band = self._band(True)
        if band is None:
            raise RuntimeError("the deck reports no geometry to cut a strip band from")
        canvas = self.create_full_deck_sized_image(extend_touchscreen=True)
        strip_slice = canvas.crop(clamp_box(band.box, canvas.width, canvas.height))
        return strip_slice.resize(self.deck_controller.get_touchscreen_image_size(),
                                  Image.Resampling.LANCZOS)

    def crop_key_image_from_deck_sized_image(self, image: Image.Image, key: int,
                                             origin: "tuple[int, int]" = (0, 0)) -> Image.Image:
        deck = self.deck_controller.deck


        key_rows, key_cols = deck.key_layout()
        key_width, key_height = deck.key_image_format()['size']
        spacing_x, spacing_y = self.deck_controller.key_spacing

        row = key // key_cols
        col = key % key_cols

        # Find the X and Y offset of the key in the full-size image. origin is
        # where the key grid starts, which is not the canvas corner when the
        # band overhangs the grid, nor when the band takes the top or the
        # left edge.
        start_x = origin[0] + col * (key_width + spacing_x)
        start_y = origin[1] + row * (key_height + spacing_y)

        region = (start_x, start_y, start_x + key_width, start_y + key_height)
        segment = image.crop(region)

        # Convert to RGBA to keep transparency.
        return segment.convert("RGBA")

    def get_tiles(self, extend_touchscreen: bool = False) -> list[Image.Image]:
        # The extended canvas holds the key grid at an offset, because the
        # band can overhang the grid and can take the top or the left edge,
        # so the key crops shift with it.
        band = self._band(extend_touchscreen)
        origin = (0, 0) if band is None else band.key_origin
        full_deck_sized_image = self.create_full_deck_sized_image(extend_touchscreen)

        tiles: list[Image.Image] = []
        for key in range(self.deck_controller.deck.key_count()):
            key_image = self.crop_key_image_from_deck_sized_image(full_deck_sized_image, key, origin)
            tiles.append(key_image)

        return tiles

class BackgroundVideo(BackgroundVideoCache, FrameScheduled):
    def __init__(self, deck_controller: "DeckController", video_path: str, loop: bool = True, fps: int = MEDIA_LOOP_FPS, extend_touchscreen: bool = False,
                 view: "tuple[float, float, float]" = DEFAULT_VIEW) -> None:
        self.deck_controller = deck_controller
        self.video_path = video_path
        self.loop = loop
        self.fps = fps

        self.page: Page | None = self.deck_controller.active_page

        self.active_frame: int = -1
        self._play_start: float | None = None  # playback start on the media clock, set on the first real-time frame
        self._last_frame_tick: float | None = None  # last real-time frame pick, for gap clamping
        # Sync tile min-age to loop period on the first complete-cache tick.
        # Sequential build playback has no known source-rate loop period.
        self._min_age_synced: bool = False

        super().__init__(video_path, deck_controller=deck_controller, extend_touchscreen=extend_touchscreen, view=view)

    @override
    def _render_rate(self) -> float:
        """Return the minimum of loop, page cap, and known source rate.
        Zero cap uses loop rate; during build, page cap paces sequential decode."""
        cap = self.fps or MEDIA_LOOP_FPS
        source = self.get_source_fps() or MEDIA_LOOP_FPS
        return min(MEDIA_LOOP_FPS, cap, source)

    def get_next_tiles(self) -> "tuple[list[Image.Image | None], tuple[str, int] | None]":
        """Return copied tiles with video MD5 and actual source index.
        Use None identity for fallback or alpha payloads that have no proven frame."""
        if self.is_cache_complete():
            if not self._min_age_synced:
                # Replace maximum min-age with real loop period when clock playback starts.
                self._min_age_synced = True
                self.deck_controller.refresh_tile_cache_min_age(self)
            # Pick complete-cache frames by source-rate clock so late ticks drop frames.
            # Page fps limits rendering but does not change playback speed.
            playback_fps = float(self.get_source_fps() or self.fps or MEDIA_LOOP_FPS)
            now = media_loop.now()
            if self._play_start is None:
                # Seed from current position because completion mid-play must not replay or jump.
                self._play_start = now - (self.active_frame + 1) / playback_fps
            elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
                # Ticks stop while the page is away. Shift the timebase across
                # the gap so playback continues in place, with no fast-forward.
                self._play_start += (now - self._last_frame_tick) - 1.0 / playback_fps
            self._last_frame_tick = now
            frame = int((now - self._play_start) * playback_fps)
            self.active_frame = frame % self.n_frames if self.loop else min(frame, self.n_frames - 1)
        else:
            # Advance sequentially during build so every frame decodes without a gap or seek.
            self.active_frame += 1
            if self.active_frame >= self.n_frames and self.loop:
                self.active_frame = 0

        frame_index: int | None
        copied_tiles: list[Image.Image | None]
        tiles, frame_index = self.get_tiles_and_index(self.active_frame)
        try:
            # Normal paths return decoded, repeated, or alpha images.
            # Handle only an unexpected None tile from cache decode failure.
            copied_tiles = [tile.copy() for tile in tiles]
        except AttributeError:
            copied_tiles = [None for _ in range(len(tiles))]
            frame_index = None
        identity = None if frame_index is None else (self.video_md5, frame_index)
        return copied_tiles, identity
