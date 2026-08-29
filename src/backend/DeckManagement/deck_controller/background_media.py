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

from PIL import Image, ImageEnhance, ImageOps
from loguru import logger as log

from src.backend.DeckManagement.HelperMethods import is_video
from src.backend.DeckManagement.Subclasses.background_video_cache import BackgroundVideoCache
from src.backend.DeckManagement.deck_controller.gif_pipeline import GifBackground, GifBudgetExceeded
from src.backend.DeckManagement.deck_controller.slideshow import IN_ORDER, Slideshow
from src.backend.DeckManagement.deck_controller.strip_band import band_layout, clamp_box

from typing import TYPE_CHECKING, cast
if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.PageManagement.Page import Page


class Background:
    def __init__(self, deck_controller: "DeckController"):
        self.deck_controller = deck_controller

        # Guards the shared render-state that the media tick writes from one
        # thread while a GTK, load or screensaver thread swaps the background
        # from another: tiles, _video_strip, _touchscreen_slice and the
        # _identified_tiles pair. It is a leaf lock, held only across those
        # field reads and writes and never while calling update_all_inputs or
        # the deck, so it cannot invert against the media, BetterDeck or load
        # locks. _identified_tiles keeps its one-atomic-pair publish and now
        # publishes under this lock beside its siblings.
        self._render_state_lock = threading.RLock()

        # Bumped under the lock by every source swap. update_tiles snapshots
        # it with the source and publishes only while it still matches, so a
        # render of the old source that finishes after a newer set_image or
        # set_video discards its result instead of overwriting the newer
        # tiles. A still image has no later tick to repair such an overwrite,
        # which is why a lock around the publish alone is not enough.
        self._source_epoch = 0

        self.image: "BackgroundImage | None" = None
        # Either video provider: the cv2-backed one, or the PIL GIF one,
        # which carries the same playback surface without subclassing it.
        self.video: "BackgroundVideo | GifBackground | None" = None

        # The still-image rotation, or None when the background is a single
        # image, a video, or blank. It owns the index and the interval clock;
        # slideshow_tick() reads it each media pass and swaps self.image when
        # an image comes due. It and self.video are mutually exclusive: a
        # slideshow is a list of stills, so set_video() and every single-image
        # or blank swap clear it, and set_slideshow() nulls self.video.
        self.slideshow: Slideshow | None = None

        # Extend the background onto the SD+ touchscreen strip. An image slice
        # is memoized; the strip re-composites on every dial label change.
        # update_tiles() refreshes _video_strip once per video frame.
        self.extend_to_touchscreen: bool = False
        self._touchscreen_slice: Image.Image | None = None
        self._video_strip: Image.Image | None = None

        # update_tiles() replaces the whole list and nothing mutates it in
        # place. Sequence accepts both element types: a source yields all-Image
        # entries, or None entries on the video cache fallback path.
        self.tiles: Sequence[Image.Image | None] = [None] * deck_controller.deck.key_count()
        # (tiles, (video md5, frame index)) for the frame tiles holds. None
        # when the frame has no name. See get_identified_tile().
        # Published only with a real identity; see update_tiles.
        self._identified_tiles: "tuple[Sequence[Image.Image | None], tuple[str, int]] | None" = None

    def set_image(self, image: "BackgroundImage", update: bool = True,
                  _keep_slideshow: bool = False) -> None:
        # Publish the swap under the lock, then close the old video and clear
        # caches outside it, so the leaf lock never wraps a deck call.
        #
        # _keep_slideshow is the one internal caller's flag: the slideshow's
        # own frame advance swaps the image and must not tear down the
        # rotation it belongs to. Every other caller ends any slideshow,
        # because an external single-image set replaces the whole background.
        with self._render_state_lock:
            old_video = self.video
            self.image = image
            self.video = None
            self._source_epoch += 1
            if not _keep_slideshow:
                self.slideshow = None
            self._touchscreen_slice = None
            self._video_strip = None
            # A content change orphans every cached native. Each key holds the
            # previous background's composited pixels, hashes or frames. Clear
            # them here, or they stay dead until LRU eviction reaches them.
            self._identified_tiles = None
        if old_video is not None:
            old_video.close()
        self.deck_controller.clear_encoded_key_caches()
        self.deck_controller.refresh_tile_cache_min_age(None)
        if not _keep_slideshow:
            # A slideshow advance runs this on the media thread once per
            # interval, and a full collection there is a needless hitch on the
            # sole writer. The orphaned previous frame is a plain object with no
            # reference cycle, so refcounting frees it and its PIL image at the
            # reassignment above without a collection. An external single-image
            # set keeps the collect: it runs off the writer, on a page-load
            # worker.
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
            # As in set_image(), a content change orphans every cached native.
            # The md5 in a native tile key makes a source swap collision-free.
            # The clear stops the old video's frames from lingering.
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
                      update: bool = True, now: "float | None" = None) -> None:
        """Install a still-image rotation over paths.

        This loads the first frame at once and arms the interval clock, so the
        first swap lands one interval later. slideshow_tick() drives the rest.
        A list with fewer than two loadable images installs the one image (or
        clears the background) and no rotation, which keeps a one-element list
        behaving like a single-image background.

        now is the monotonic reading the interval clock starts from; the media
        tick and a test both pass it. order is in-order or shuffle.
        """
        show = Slideshow(paths, interval, order=order)
        # Bind the rotation to the page it loads for. This load runs on a page
        # switch's worker, so a media tick can still hold the old page's
        # rotation for a moment; slideshow_tick() reads this to refuse to
        # advance a rotation whose page is no longer active, the way the
        # background video guards its own repaint on video.page.
        show.page = self.deck_controller.active_page
        # Drop the current rotation before installing the first frame below.
        # The install runs off the lock, and a media tick during it would
        # otherwise advance the old rotation and overwrite the frame installed
        # here (the new show publishes only at the end). With no rotation set,
        # a racing tick no-ops instead.
        with self._render_state_lock:
            self.slideshow = None
        first = show.current_path()
        # Build the first frame lock-free, then swap it in. keep=True leaves the
        # (now cleared) rotation slot untouched, so nothing re-arms the old one.
        # A path that is not a loadable image (a stale entry, or a video the
        # caller did not filter) is skipped, so the rotation starts on the first
        # frame that renders.
        installed = self._install_slideshow_frame(first, update=update, keep=True) if first else False
        if not installed and len(show) <= 1:
            # One entry that would not load, or an empty list, leaves nothing
            # to rotate. Clear to a blank background rather than hold whatever
            # showed before.
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
        """Advance the rotation when its interval has elapsed. Returns True
        when this pass swapped the image.

        The media-player tick calls this each pass with a monotonic reading.
        It no-ops when no slideshow is set, so a video or single-image
        background costs one attribute read and one branch per tick.
        """
        show = self.slideshow
        if show is None:
            return False
        # Advance only while this rotation's page is the active one. A page
        # switch bumps active_page synchronously but reloads the background on a
        # worker, so between the two self.slideshow can still hold the old
        # page's rotation. Without this guard a due tick in that window advances
        # it and swaps the old rotation's next image onto the new page.
        if show.page is not self.deck_controller.active_page:
            return False
        now = time.monotonic() if now is None else now
        next_path = show.maybe_advance(now)
        if next_path is None:
            return False
        return self._install_slideshow_frame(next_path, update=True, keep=True)

    def _install_slideshow_frame(self, path: "str | None", update: bool, keep: bool) -> bool:
        """Load path as a still and swap it in as the background image. Returns
        True on success. A path that does not resolve to an image is discarded
        and the previous frame stays, so one bad entry does not blank the deck.
        keep leaves the rotation in place through the swap."""
        if not path:
            return False
        try:
            # prebuild_from_path opens and decodes the file. A file that exists
            # but is corrupt raises here rather than returning a kind, so catch
            # it and discard cleanly, which is what a missing entry already
            # does. Without this a corrupt frame raises into the media loop's
            # per-tick guard instead of being skipped.
            kind, payload = self.prebuild_from_path(path, allow_keep=False)
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

    def prebuild_from_path(self, path: str | None, fps: int = 30, loop: bool = True,
                           allow_keep: bool = True) -> "tuple[str, BackgroundVideo | GifBackground | BackgroundImage | str | None]":
        """Build the new background object lock-free, without a touch on
        self.video, self.image or the deck. apply_prebuilt() swaps it in.

        Returns (kind, payload). blank clears the background. noop keeps the
        current one. keep refreshes page, fps and loop only. video or image
        carries a new object."""
        if path == "":
            path = None
        if path is None:
            return ("blank", None)
        if is_video(path):
            extend = self.extend_to_touchscreen and self.deck_controller.deck.is_touch()
            if allow_keep:
                # The extend mode and the saturation factor bake into the
                # video's canvas geometry and its cache file. A change to
                # either forces a rebuild for the same path, or a playing
                # video keeps showing the old factor.
                if (self.video is not None and self.video.video_path == path
                        and self.video.extend_touchscreen == extend
                        and abs(self.video.saturation - self.deck_controller.get_display_saturation()) <= 0.001):
                    # Carry the path so apply_prebuilt re-checks it. This
                    # verdict is lock-free, and a load_background that races
                    # it can swap self.video first. GifBackground carries the
                    # same three attributes, so a GIF that fell back to cv2
                    # keeps the fallback and skips the failed PIL decode.
                    return ("keep", path)
            if os.path.splitext(path)[1].lower() == ".gif":
                # A .gif goes to the PIL provider so alpha and the per-frame
                # delay timeline survive. The cv2 demuxer drops both. Over
                # budget or undecodable, fall back to the opaque source-fps
                # cv2 path below instead of an OOM risk. The keep-check
                # above stops the warning from repeating.
                try:
                    return ("video", GifBackground(self.deck_controller, path, loop=loop, fps=fps, extend_touchscreen=extend))
                except GifBudgetExceeded as e:
                    log.warning(f"GIF background over budget, falling back to the opaque cv2 path: {e}")
                except Exception:
                    log.opt(exception=True).warning(f"GIF background decode failed, falling back to the opaque cv2 path: {path}")
            return ("video", BackgroundVideo(self.deck_controller, path, loop=loop, fps=fps, extend_touchscreen=extend))
        if not os.path.isfile(path):
            return ("noop", None)
        with Image.open(path) as image:
            return ("image", BackgroundImage(self.deck_controller, image.copy(), path=path))

    def _discard_prebuilt(self, kind: str, payload: "BackgroundVideo | GifBackground | BackgroundImage | str | None") -> None:
        """Release the resources of a prebuilt payload that no caller applied.
        A video or image payload holds a cv2 capture or a PIL image, and a
        drop without close() leaks it. keep, noop and blank hold nothing."""
        if kind not in ("video", "image") or payload is None or isinstance(payload, str):
            # A keep verdict carries the path string; the kind gate above
            # already returns for it, and the isinstance restates that.
            return
        try:
            payload.close()
        except Exception:
            log.opt(exception=True).warning(
                "Failed to close an orphaned prebuilt background payload during close()"
            )

    def apply_prebuilt(self, kind: str, payload: "BackgroundVideo | GifBackground | BackgroundImage | str | None", fps: int = 30, loop: bool = True, update: bool = True) -> None:
        """Apply the result of prebuild_from_path(). The screensaver
        transition calls this under _background_load_lock, after it re-checks
        the generation. This does no file I/O; it assigns the objects and
        fans out update_all_inputs()."""
        # A load_background that passed its generation gate before close()
        # bumped the generation arrives here with a live payload. The close() sweep already ran, or waits on the lock, so an
        # attach now leaks. close() sets _closing before the sweep.
        if getattr(self.deck_controller, "_closing", False):
            self._discard_prebuilt(kind, payload)
            return
        if kind == "noop":
            return
        if kind == "keep":
            # Re-check the lock-free keep verdict against the current video.
            # A load_background that raced the prebuild can swap in a
            # different file. A mismatch does nothing and self-heals on the
            # next transition, instead of corrupting that video's settings.
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

    def set_from_path(self, path: str | None, fps: int = 30, loop: bool = True, update: bool = True, allow_keep: bool = True) -> None:
        """Prebuild and apply in one call, for a caller that does not need
        the lock-free split. Those callers are load_background, which already
        holds _background_load_lock, and the ScreenSaver setters that act
        while it shows."""
        kind, payload = self.prebuild_from_path(path, fps=fps, loop=loop, allow_keep=allow_keep)
        self.apply_prebuilt(kind, payload, fps=fps, loop=loop, update=update)

    def get_identified_tile(self, key_index: int) -> "tuple[Image.Image, tuple[str, int]] | None":
        """(tile, (video md5, frame index)) for a video background, or None
        when no tile has a nameable frame. Tiles and identity publish as one
        pair and read as one, so a concurrent update_tiles() cannot pair this
        frame's pixels with the next frame's identity. The media tick, the
        GTK thread and the screensaver thread all call update_tiles()."""
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
            # Snapshot the source under the lock, so a concurrent set_image or
            # set_video cannot null self.video between the branch test and the
            # reads inside it. Everything below composes from the local
            # snapshot.
            with self._render_state_lock:
                image = self.image
                video = self.video
                epoch = self._source_epoch
            identity = None
            # Compose the new frame outside the lock (get_tiles and
            # get_next_tiles do the heavy work and touch other caches), then
            # publish tiles, the strip slice and the identity pair together
            # under the leaf lock. _video_strip is written only when this frame
            # produced one, so an image or blank frame does not clobber the
            # None a concurrent set_image just published.
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
                    # A set_image or set_video swapped the source while this
                    # frame rendered. Its own update_tiles published the newer
                    # content; publishing this frame would put the old source
                    # back over it, and a still image would never repair that.
                    return
                self.tiles = new_tiles
                if wrote_strip:
                    self._video_strip = new_video_strip
                self._identified_tiles = None if identity is None else (new_tiles, identity)
        except Exception:
            # A tile error must not kill the media thread. Keep the old tiles
            # and rate-limit the log, because a broken video fails every
            # frame.
            now = time.time()
            if now - getattr(self, "_last_tile_error_log", 0) > 10:
                self._last_tile_error_log = now
                log.opt(exception=True).error("Failed to update background tiles; keeping previous")

class BackgroundImage:
    def __init__(self, deck_controller: "DeckController", image: Image.Image, path: str | None = None) -> None:
        self.deck_controller = deck_controller
        # The source file that image came from, or None for a caller with no
        # file (the test harness). An extend-to-touchscreen toggle can need
        # more canvas height than the fitted copy holds; _ensure_fits_canvas()
        # then re-decodes from this path.
        self.path = path

        # Bake the saturation into the source image once, at load time. The
        # key tiles and the strip slice both derive from self.image, so they
        # inherit one enhancement pass at no per-frame cost. Factor 1.0 skips
        # the ImageEnhance call and the mode conversion, so the bytes stay.
        image = self._prepare_image(image)
        # close() sets this to None. _ensure_fits_canvas() and
        # create_full_deck_sized_image() both handle the released state.
        self.image: Image.Image | None = self._fit_to_canvas(image, self._extend_effective())

    def _extend_effective(self) -> bool:
        # extend_to_touchscreen lives on Background, not on DeckController.
        # This repeats the deck.is_touch() condition of
        # Background._extend_effective without its image check; that check
        # asks if an image background exists, not how to size one.
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
        """The key grid's canvas size: keys plus the bezel spacing."""
        key_rows, key_cols = self.deck_controller.deck.key_layout()
        key_width, key_height = self.deck_controller.get_key_image_size()
        spacing_x, spacing_y = self.deck_controller.key_spacing
        return (key_width * key_cols + spacing_x * (key_cols - 1),
                key_height * key_rows + spacing_y * (key_rows - 1))

    def _canvas_size(self, extend_touchscreen: bool) -> "tuple[int, int] | None":
        """The canvas size that create_full_deck_sized_image() targets, with
        the touchscreen strip when extend is on. Returns None when the deck
        geometry is absent; the caller then skips the fit and the re-decode."""
        deck = getattr(self.deck_controller, "deck", None)
        if deck is None:
            return None
        canvas_width, canvas_height = self._grid_size()

        if extend_touchscreen and deck.is_touch():
            canvas_width, canvas_height, _grid_x, _band = \
                band_layout(self.deck_controller, canvas_width, canvas_height)

        return (canvas_width, canvas_height)

    def _fit_to_canvas(self, image: Image.Image, extend_touchscreen: bool) -> Image.Image:
        canvas = self._canvas_size(extend_touchscreen)
        if canvas is None:
            return image
        budget = (canvas[0] * 2, canvas[1] * 2)
        if image.width > budget[0] or image.height > budget[1]:
            image.thumbnail(budget, Image.Resampling.LANCZOS)
        return image

    def _ensure_fits_canvas(self, extend_touchscreen: bool) -> None:
        """Re-decode from path when the current canvas needs more resolution
        than the retained image holds. The canvas grows when the user toggles
        touchscreen-extend at runtime, with no fresh page load."""
        if not self.path or self.image is None:
            return
        canvas = self._canvas_size(extend_touchscreen)
        if canvas is None:
            return
        if canvas[0] <= self.image.width and canvas[1] <= self.image.height:
            return
        try:
            with Image.open(self.path) as fresh:
                fresh = fresh.copy()
        except (OSError, FileNotFoundError):
            return
        fresh = self._prepare_image(fresh)
        old_image = self.image
        self.image = self._fit_to_canvas(fresh, extend_touchscreen)
        if old_image is not None:
            old_image.close()

    def close(self) -> None:
        """Release the retained source-resolution PIL image."""
        if self.image is not None:
            self.image.close()
            self.image = None

    def create_full_deck_sized_image(self, extend_touchscreen: bool = False) -> Image.Image:
        self._ensure_fits_canvas(extend_touchscreen)
        canvas_width, canvas_height = self._grid_size()

        # Extend the canvas to the union of the key grid and the strip's
        # view: taller by the bezel gap plus the band, and wider when the
        # band overhangs the grid (the SD+ strip shows content beyond the
        # outer key columns). strip_band owns that geometry; on an SD+ it is
        # device-calibrated rather than derived from the spacing.
        if extend_touchscreen:
            canvas_width, canvas_height, _grid_x, _band = \
                band_layout(self.deck_controller, canvas_width, canvas_height)

        # close() releases the source image. Raise instead of composing a
        # transparent canvas. Background.update_tiles catches the raise and
        # keeps the previous tiles behind a rate-limited log. A blank canvas blanks
        # every key without a log, once per tile refresh.
        source = self.image
        if source is None:
            raise RuntimeError(
                "background image was released (close()) while its tiles were "
                "still being composed"
            )

        # Convert to RGBA before the resize to keep transparency.
        img_rgba = source.convert("RGBA")
        return ImageOps.fit(img_rgba, (canvas_width, canvas_height), Image.Resampling.LANCZOS)

    def get_touchscreen_image(self) -> Image.Image:
        """The strip's view of the extended canvas, at strip resolution."""
        canvas = self.create_full_deck_sized_image(extend_touchscreen=True)
        strip_width, strip_height = self.deck_controller.get_touchscreen_image_size()
        grid_w, grid_h = self._grid_size()
        _cw, _ch, _grid_x, band_crop = band_layout(self.deck_controller, grid_w, grid_h)
        strip_slice = canvas.crop(clamp_box(band_crop, canvas.width, canvas.height))
        return strip_slice.resize((strip_width, strip_height), Image.Resampling.LANCZOS)
    
    def crop_key_image_from_deck_sized_image(self, image: Image.Image, key: int,
                                             x_offset: int = 0) -> Image.Image:
        deck = self.deck_controller.deck


        key_rows, key_cols = deck.key_layout()
        key_width, key_height = deck.key_image_format()['size']
        spacing_x, spacing_y = self.deck_controller.key_spacing

        # Find the row and the column of the requested key.
        row = key // key_cols
        col = key % key_cols

        # Find the X and Y offset of the key in the full-size image. x_offset
        # is the grid's position on a canvas whose strip band overhangs it.
        start_x = x_offset + col * (key_width + spacing_x)
        start_y = row * (key_height + spacing_y)

        # Crop the region that the key occupies.
        region = (start_x, start_y, start_x + key_width, start_y + key_height)
        segment = image.crop(region)

        # Convert to RGBA to keep transparency.
        return segment.convert("RGBA")
    
    def get_tiles(self, extend_touchscreen: bool = False) -> list[Image.Image]:
        # The strip band can overhang the key grid, so the extended canvas
        # holds the grid at an offset and the key crops shift with it.
        full_deck_sized_image = self.create_full_deck_sized_image(extend_touchscreen)
        grid_x = 0
        if extend_touchscreen:
            grid_w, grid_h = self._grid_size()
            grid_x = band_layout(self.deck_controller, grid_w, grid_h)[2]

        tiles: list[Image.Image] = []
        for key in range(self.deck_controller.deck.key_count()):
            key_image = self.crop_key_image_from_deck_sized_image(
                full_deck_sized_image, key, x_offset=grid_x)
            tiles.append(key_image)

        return tiles

class BackgroundVideo(BackgroundVideoCache):
    def __init__(self, deck_controller: "DeckController", video_path: str, loop: bool = True, fps: int = 30, extend_touchscreen: bool = False) -> None:
        self.deck_controller = deck_controller
        self.video_path = video_path
        self.loop = loop
        self.fps = fps

        self.page: Page | None = self.deck_controller.active_page

        self.active_frame: int = -1
        self._play_start: float | None = None  # wall-clock playback start, set on the first real-time frame
        self._last_frame_tick: float | None = None  # last real-time frame pick, for gap clamping
        # True after the tile cache min-age moves to this video's loop period.
        # The first tick past cache completion sets it. Before that, playback
        # does not run at source fps and the loop period is unknown.
        self._min_age_synced: bool = False

        super().__init__(video_path, deck_controller=deck_controller, extend_touchscreen=extend_touchscreen)

    def get_next_tiles(self) -> "tuple[list[Image.Image | None], tuple[str, int] | None]":
        """(tiles, identity) for the frame this tick lands on. identity is
        (video md5, source frame index), or None for a fallback or alpha
        payload. One pair keeps the pixels with their identity, because
        Mp4FrameCache.get_frame_and_index can serve a different frame."""
        if self.is_cache_complete():
            if not self._min_age_synced:
                # First tick past cache completion. Until now the clamp
                # maximum shielded the frame set, because sequential build
                # playback has no loop period. From here the wall clock picks
                # frames at source fps, so the real loop period applies.
                self._min_age_synced = True
                self.deck_controller.refresh_tile_cache_min_age(self)
            # A full cache makes any frame a free lookup. Pick by wall clock so
            # a slow media loop drops frames instead of playing in slow motion.
            # Playback runs at the source fps. The page fps setting limits
            # how often the media loop renders a frame, and must not change
            # the speed.
            playback_fps = float(self.get_source_fps() or self.fps or 30)
            now = time.time()
            if self._play_start is None:
                # Seed the timebase from the current position. The cache
                # completes mid-play, and a zero base replays a non-looping
                # video or jumps a looping one.
                self._play_start = now - (self.active_frame + 1) / playback_fps
            elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
                # Ticks stop while the page is away. Shift the timebase across
                # the gap so playback continues in place, with no fast-forward.
                self._play_start += (now - self._last_frame_tick) - 1.0 / playback_fps
            self._last_frame_tick = now
            frame = int((now - self._play_start) * playback_fps)
            self.active_frame = frame % self.n_frames if self.loop else min(frame, self.n_frames - 1)
        else:
            # The cache is still decoding, so advance sequentially and let
            # the decoder read every frame. A wall-clock jump leaves a gap and
            # forces an expensive seek.
            self.active_frame += 1
            if self.active_frame >= self.n_frames and self.loop:
                self.active_frame = 0

        frame_index: int | None
        copied_tiles: list[Image.Image | None]
        tiles, frame_index = self.get_tiles_and_index(self.active_frame)
        try:
            # Every path through get_tiles_and_index() yields real Images:
            # decoded tiles, the last good payload, or the alpha fallback.
            # This catch fires only if a cache puts None in place of a tile
            # that it cannot decode.
            copied_tiles = [tile.copy() for tile in tiles]
        except AttributeError:
            copied_tiles = [None for _ in range(len(tiles))]
            frame_index = None
        identity = None if frame_index is None else (self.video_md5, frame_index)
        return copied_tiles, identity
