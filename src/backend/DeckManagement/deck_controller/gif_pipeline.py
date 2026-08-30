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

The GIF pipeline holds the one PIL compositor in the app, the budget ladder
that bounds what a GIF retains, and the two providers on them. GifBackground
draws a deck or strip canvas; KeyGIF draws one key.
"""
import bisect
import contextlib
import itertools
import math
import os
import threading
from dataclasses import dataclass

from PIL import Image, ImageEnhance, ImageOps, ImageSequence
from loguru import logger as log

from src.backend.DeckManagement.Subclasses import cache_budget
from src.backend.DeckManagement.deck_controller.viewport import (
    DEFAULT_VIEW, is_default_view, render_viewport,
)
from src.backend.DeckManagement.Subclasses import mp4_tile_cache
from src.backend.DeckManagement import media_loop
from src.backend.DeckManagement.media_loop import MEDIA_LOOP_FPS, FrameScheduled
from src.backend.DeckManagement.Subclasses.SingleKeyAsset import SingleKeyAsset
from src.backend.DeckManagement.Subclasses.mp4_tile_cache import get_video_md5
from src.backend.DeckManagement.deck_controller.strip_band import band_layout

from collections.abc import Generator
from typing import TYPE_CHECKING, Any, cast, override
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput
    from src.backend.PageManagement.Page import Page


#: Missing geometry on an extended background raises so update_tiles keeps old tiles and logs.
#: Returning key-only tiles would silently freeze the strip.
_STRIP_GEOMETRY_MISSING = (
    "extend_touchscreen is set but the strip geometry was never computed"
)


def gif_render_rate(fps: "int | None", fastest_frame_rate: float,
                    total_delay: float) -> float:
    """Return a GIF rate bounded by loop rate, page cap, and fastest frame.
    Read at least twice per animation loop; zero cap uses loop rate."""
    cap = max(1.0, float(fps or MEDIA_LOOP_FPS))
    rate = min(MEDIA_LOOP_FPS, cap, fastest_frame_rate)
    if total_delay > 0:
        rate = max(rate, min(MEDIA_LOOP_FPS, 2.0 / total_delay))
    return rate


class GifBudgetExceeded(Exception):
    """Signal before decode that estimated retained frames exceed the caller's budget."""


# Cap decoded GIF backgrounds by frame count times RGBA canvas size.
# Over-budget backgrounds use the bounded opaque cv2 route.
GIF_BG_BUDGET_MB = 128

# Cap each key's retained RGBA list; opaque GIFs use O(1) MP4-reader memory.
GIF_KEY_BUDGET_MB = 32

# Separate alpha-dropped over-budget artifacts from lossless GIF cache files.
# Under-budget loads must not read degraded pixels as opaque classification.
BOUNDED_TILE_VARIANT = ".bounded"

# Warn once per distinct invalid or very small GIF budget value per process.
_warned_gif_budget_values: "set[str]" = set()


def gif_key_budget_bytes() -> int:
    """Read the per-key retained-frame ceiling from DECKARD_GIF_KEY_BUDGET_MB.
    Nonpositive uses bounded MP4; malformed uses default, and values below 1 MiB warn."""
    raw = os.environ.get("DECKARD_GIF_KEY_BUDGET_MB")
    if raw is None:
        return GIF_KEY_BUDGET_MB * 1024 * 1024
    try:
        mb = float(raw)
        usable = math.isfinite(mb)
    except ValueError:
        # Bind mb on the exception path before the common validity branch.
        mb = 0.0
        usable = False
    if not usable:
        if raw not in _warned_gif_budget_values:
            _warned_gif_budget_values.add(raw)
            log.warning(
                f"Ignoring malformed DECKARD_GIF_KEY_BUDGET_MB={raw!r}; "
                f"using the default {GIF_KEY_BUDGET_MB}"
            )
        return GIF_KEY_BUDGET_MB * 1024 * 1024
    if mb < 0:
        return 0
    if 0 < mb < 1 and raw not in _warned_gif_budget_values:
        _warned_gif_budget_values.add(raw)
        log.warning(
            f"DECKARD_GIF_KEY_BUDGET_MB={raw!r} is under 1 MiB -- smaller than one "
            f"fitted GIF frame, so EVERY alpha-carrying GIF key will drop its "
            f"transparency for the bounded mp4 route"
        )
    return int(mb * 1024 * 1024)


def contained_size(source_size: "tuple[int, int]", max_size: "tuple[int, int]") -> "tuple[int, int]":
    """Return aspect-preserving, shrink-only contained dimensions.
    Frame-list and video routes share this calculation and can differ from ImageOps by one pixel."""
    src_w, src_h = source_size
    max_w, max_h = max_size
    if src_w <= max_w and src_h <= max_h:
        return src_w, src_h
    scale = min(max_w / src_w, max_h / src_h)
    return max(1, round(src_w * scale)), max(1, round(src_h * scale))


def tile_video_size(source_size: "tuple[int, int]", max_size: "tuple[int, int]") -> "tuple[int, int]":
    """Return shared tile-cache geometry rounded down to even axes of at least two pixels.
    mp4v truncates odd axes, so cached tiles can be one pixel smaller than retained frames."""
    out_w, out_h = contained_size(source_size, max_size)
    return max(2, out_w - out_w % 2), max(2, out_h - out_h % 2)


def normalize_gif_delay(raw: "int | None") -> int:
    """Normalize one GIF delay in milliseconds.
    Missing or below 20 ms becomes 100 ms; decode and probe share this rule."""
    if raw is None or raw < 20:
        return 100
    return raw


def cumulative_gif_delays(delays_ms: "list[int]") -> "list[float]":
    """Convert millisecond delays to cumulative frame-end seconds for bisect lookup."""
    return list(itertools.accumulate(d / 1000.0 for d in delays_ms))


@dataclass(frozen=True, slots=True)
class GifTimeline:
    """GIF frame count, delays, cumulative edges, and source size without retained pixels.
    PIL timing remains authoritative when pixels come from a tile MP4."""
    n_frames: int
    frame_delays: "list[int]"
    cum_delays: "list[float]"
    size: "tuple[int, int]"


def gif_header_geometry(path: str) -> "tuple[int, tuple[int, int]]":
    """(frame count, canvas size) from the GIF headers alone. No frame
    decodes and no seek runs, so a caller can price a decode first."""
    gif = Image.open(path)
    try:
        return getattr(gif, "n_frames", 1), gif.size
    finally:
        gif.close()


def probe_gif_timeline(path: str) -> GifTimeline:
    """Decode only enough to collect PIL frame delays in O(1) retained memory.
    Do not convert, fit, or saturate pixels; propagate corrupt-file errors."""
    gif = Image.open(path)
    try:
        size = gif.size
        n_frames = getattr(gif, "n_frames", 1)
        delays_ms: "list[int]" = []
        for index in range(n_frames):
            gif.seek(index)
            delays_ms.append(normalize_gif_delay(gif.info.get("duration")))
    finally:
        gif.close()

    return GifTimeline(
        n_frames=len(delays_ms),
        frame_delays=delays_ms,
        cum_delays=cumulative_gif_delays(delays_ms),
        size=size,
    )


def frame_has_alpha(frame: Image.Image) -> bool:
    """Return whether rendered RGBA pixels contain alpha below 255.
    Test pixels instead of header declarations because disposal can leave declared indices opaque."""
    if frame.mode != "RGBA":
        return False
    # The mode test above proves four bands, so getextrema returns four
    # (min, max) pairs, never the two-float shape of a single-band image.
    extrema = cast("tuple[tuple[int, int], ...]", frame.getextrema())
    return len(extrema) >= 4 and extrema[3][0] < 255


def gif_frame_walk(path: str, max_size: "tuple[int, int] | None" = None,
                   fit_size: "tuple[int, int] | None" = None,
                   saturation: float = 1.0,
                   view: tuple[float, float, float] = DEFAULT_VIEW) -> "Generator[tuple[Image.Image, int], None, None]":
    """Yield PIL-composited RGBA frames with normalized delays, view, and saturation.
    max_size shrinks only; fit_size fills exactly; always close the source when abandoned."""
    gif = Image.open(path)
    try:
        for frame in ImageSequence.Iterator(gif):
            decoded = frame.convert("RGBA")
            if fit_size is not None:
                if not is_default_view(view):
                    # The view crops even a size-matched frame, and its
                    # zoomed-out letterbox stays transparent RGBA, so alpha
                    # survives the way it does through the plain fit.
                    decoded = render_viewport(decoded, fit_size, view)
                elif decoded.size != fit_size:
                    decoded = ImageOps.fit(decoded, fit_size, Image.Resampling.LANCZOS)
            elif max_size is not None and (decoded.width > max_size[0] or decoded.height > max_size[1]):
                decoded = ImageOps.contain(decoded, max_size)
            if abs(saturation - 1.0) > 0.001:
                decoded = ImageEnhance.Color(decoded).enhance(saturation)
            yield decoded, normalize_gif_delay(gif.info.get('duration'))
    finally:
        gif.close()


def decode_gif_frames(path: str, max_size: "tuple[int, int] | None" = None,
                      fit_size: "tuple[int, int] | None" = None,
                      saturation: float = 1.0,
                      budget_bytes: int | None = None,
                      view: tuple[float, float, float] = DEFAULT_VIEW) -> "tuple[list[Image.Image], list[int], list[float]]":
    """Decode and retain all RGBA frames with normalized and cumulative delays.
    Estimate count times output RGBA size first and raise GifBudgetExceeded over budget."""
    if budget_bytes is not None:
        n_frames, (out_w, out_h) = gif_header_geometry(path)
        if fit_size is not None:
            out_w, out_h = fit_size
        elif max_size is not None:
            # The dimensions ImageOps.contain lands on. The video route's
            # tile size shares them. See contained_size.
            out_w, out_h = contained_size((out_w, out_h), max_size)
        estimate = n_frames * out_w * out_h * 4
        if estimate > budget_bytes:
            raise GifBudgetExceeded(
                f"{path}: ~{estimate / (1024 * 1024):.1f}MB decoded "
                f"({n_frames} frames at {out_w}x{out_h} RGBA) exceeds the "
                f"{budget_bytes / (1024 * 1024):.1f}MB budget"
            )

    frames: "list[Image.Image]" = []
    delays_ms: "list[int]" = []
    with contextlib.closing(gif_frame_walk(path, max_size=max_size, fit_size=fit_size,
                                           saturation=saturation, view=view)) as walk:
        for frame, delay in walk:
            frames.append(frame)
            delays_ms.append(delay)

    return frames, delays_ms, cumulative_gif_delays(delays_ms)


class GifBackground(FrameScheduled):
    """PIL RGBA background provider with per-frame delays and BackgroundVideo-compatible fields.
    Retain canvas-fitted frames under budget; extended decks append a strip slice."""

    def __init__(self, deck_controller: "DeckController", gif_path: str, loop: bool = True,
                 fps: int = MEDIA_LOOP_FPS, extend_touchscreen: bool = False,
                 canvas_size: "tuple[int, int] | None" = None,
                 view: tuple[float, float, float] = DEFAULT_VIEW) -> None:
        self.deck_controller = deck_controller
        self.video_path = gif_path
        self.loop = loop
        self.fps = fps

        self.page: Page | None = deck_controller.active_page
        self.saturation = deck_controller.get_display_saturation()
        # The viewport baked into every decoded frame. The prebuild
        # keep-check compares it, so a view change re-decodes instead of
        # keeping the old crop playing.
        self.view = view

        deck = deck_controller.deck
        self.extend_touchscreen = extend_touchscreen and deck.is_touch()

        # Strip size and box exist only for extended canvas geometry; readers guard both.
        self.strip_size: "tuple[int, int] | None" = None
        self._strip_box: "tuple[int, int, int, int] | None" = None
        if canvas_size is None:
            # Compute BackgroundVideoCache-compatible canvas boxes once for the immutable frame list.
            key_rows, key_cols = deck.key_layout()
            self.key_count = deck.key_count()
            key_w, key_h = deck.key_image_format()['size']
            spacing_x, spacing_y = deck_controller.key_spacing

            grid_w = key_w * key_cols + spacing_x * (key_cols - 1)
            grid_h = key_h * key_rows + spacing_y * (key_rows - 1)
            canvas_w, canvas_h, grid_x = grid_w, grid_h, 0

            if self.extend_touchscreen:
                # Use the shared grid-and-strip union and include grid offset for band overhang.
                self.strip_size = deck_controller.get_touchscreen_image_size()
                canvas_w, canvas_h, grid_x, self._strip_box = band_layout(
                    deck_controller, grid_w, grid_h)

            self._key_regions: "list[tuple[int, int, int, int]]" = []
            for key in range(self.key_count):
                row, col = divmod(key, key_cols)
                x = grid_x + col * (key_w + spacing_x)
                y = row * (key_h + spacing_y)
                self._key_regions.append((x, y, x + key_w, y + key_h))
            canvas_size = (canvas_w, canvas_h)
        else:
            # In strip-background mode it serves whole frames only.
            self.key_count = 0
            self._key_regions = []
            self.extend_touchscreen = False

        self.canvas_size = canvas_size

        self.frames, self.frame_delays, self._cum_delays = decode_gif_frames(
            gif_path, fit_size=canvas_size, saturation=self.saturation,
            budget_bytes=GIF_BG_BUDGET_MB * 1024 * 1024, view=view,
        )
        self._total_delay: float = self._cum_delays[-1] if self._cum_delays else 0.0

        # Publish BackgroundVideo-compatible MD5 and frame identity for native encode reuse.
        self.video_md5 = get_video_md5(gif_path)

        self.active_frame: int = -1
        # Clock timeline state. See _pick_frame.
        self._play_start: float | None = None
        self._last_frame_tick: float | None = None
        # Memoize one cropped frame across faster ticks, but return copies to callers.
        self._tiles_memo: "tuple[int | None, list[Image.Image] | None]" = (None, None)
        # Derive the fastest visible frame rate once from the shortest delay.
        shortest_ms = min(self.frame_delays) if self.frame_delays else 0
        self._fastest_frame_rate: float = (
            1000.0 / shortest_ms if shortest_ms > 0 else MEDIA_LOOP_FPS)

    @override
    def _render_rate(self) -> float:
        return gif_render_rate(self.fps, self._fastest_frame_rate, self._total_delay)

    def _pick_frame(self, now: float | None = None) -> int:
        """Pick a clock frame by cumulative-delay bisect with inactive-gap clamping."""
        # Snapshot timeline and frames for one generation against concurrent close().
        # This prevents zero modulo and indices beyond the caller's frame snapshot.
        cum = self._cum_delays
        total = self._total_delay
        n = len(self.frames)
        if n <= 1 or total <= 0 or not cum:
            self.active_frame = 0
            return 0

        if now is None:
            now = media_loop.now()

        if self._play_start is None:
            self._play_start = now
        elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
            self._play_start += (now - self._last_frame_tick) - cum[0]
        self._last_frame_tick = now

        elapsed = now - self._play_start
        # Treat fps as a sampling cap over the GIF delay timeline, not playback speed.
        # Read it once; omit loop-rate-or-higher caps to preserve exact uncapped picks.
        cap = max(1.0, float(self.fps or MEDIA_LOOP_FPS))
        # Read every pass at least twice, or a cap whose period is the whole
        # animation freezes it on one frame instead of running slowly.
        cap = max(cap, 2.0 / total)
        if self.loop:
            t = elapsed % total
            if cap < MEDIA_LOOP_FPS:
                t = int(t * cap) / cap
        else:
            if cap < MEDIA_LOOP_FPS:
                elapsed = int(elapsed * cap) / cap
            t = min(elapsed, total)

        frame = bisect.bisect_right(cum, t)
        if frame >= n:
            frame = n - 1  # clamp a float edge, or a non-loop t equal to total
        self.active_frame = frame
        return frame

    def get_next_tiles(self) -> "tuple[list[Image.Image], tuple[str, int] | None]":
        """Return copied RGBA key tiles, optional strip slice, and MD5/frame identity.
        Crops preserve alpha and follow the BackgroundVideo entry order."""
        frames = self.frames  # snapshot; close() empties this from other threads
        strip_size = self.strip_size
        strip_box = self._strip_box
        if not frames:
            entries = [self.deck_controller.generate_alpha_key() for _ in range(self.key_count)]
            if self.extend_touchscreen:
                if strip_size is None:
                    raise RuntimeError(_STRIP_GEOMETRY_MISSING)
                entries.append(Image.new("RGBA", strip_size, (0, 0, 0, 0)))
            return entries, None

        index = self._pick_frame()
        memo_index, memo_entries = self._tiles_memo
        if memo_index != index or memo_entries is None:
            frame = frames[index]
            memo_entries = [frame.crop(box) for box in self._key_regions]
            if self.extend_touchscreen:
                if strip_size is None or strip_box is None:
                    raise RuntimeError(_STRIP_GEOMETRY_MISSING)
                # Crop and HAMMING-resize the shared extended-canvas strip region.
                memo_entries.append(
                    frame.crop(strip_box).resize(strip_size, Image.Resampling.HAMMING)
                )
            self._tiles_memo = (index, memo_entries)
        return [entry.copy() for entry in memo_entries], (self.video_md5, index)

    def get_next_frame(self, now: float | None = None) -> Image.Image | None:
        """Return the retained canvas RGBA frame for strip backgrounds, or None after close.
        The caller copies it with convert before mutation."""
        frames = self.frames
        if not frames:
            return None
        return frames[self._pick_frame(now)]

    def set_playback(self, fps: int, loop: bool) -> None:
        """Set render cap and loop without rebasing the GIF delay-timeline clock."""
        self.fps = fps
        self.loop = loop

    def close(self) -> None:
        """Drop retained frames; in-flight ticks remain safe on their local snapshots."""
        self.frames = []
        self.frame_delays = []
        self._cum_delays = []
        self._total_delay = 0.0
        self._tiles_memo = (None, None)


class KeyGIF(SingleKeyAsset, FrameScheduled):
    """Play one key's GIF on PIL's per-frame timeline.
    Retain RGBA for alpha; stream opaque PIL-composited frames from the shared MP4 cache."""

    # Keep a class default for tests that construct arithmetic-only instances through __new__.
    video_cache: "mp4_tile_cache.KeyVideoCache | None" = None

    def __init__(self, controller_key: "ControllerInput[Any]", gif_path: str, fps: int = MEDIA_LOOP_FPS, loop: bool = True):
        # Accept shared controller inputs because dials also host KeyGIF and only deck_controller is required.
        super().__init__(controller_key)
        self.gif_path = gif_path
        self.fps = fps
        self.loop = loop

        self.active_frame: int = -1
        # Track wall-clock playback against cumulative per-frame delays, not fixed fps.
        self._play_start: float | None = None
        self._last_frame_tick: float | None = None

        # Serialize close against video reads so a post-release read cannot reopen and leak a capture.
        self._close_lock = threading.Lock()

        self.frames: "list[Image.Image]" = []
        self._frames_bytes = 0

        # Cap shrink-only frames at the UI's maximum visible size of twice the key tile.
        # Both routes use tile_video_size for identical aspect-preserving geometry.
        tile_w, tile_h = self.deck_controller.get_key_image_size()
        fit_size = (max(1, tile_w * 2), max(1, tile_h * 2))

        # Bake saturation during decode; page reload rebuilds after a change and cache keys include the factor.
        saturation = self.deck_controller.get_display_saturation()

        self.frame_delays: "list[int]" = []
        self._cum_delays: "list[float]" = []
        self._total_delay: float = 0.0
        # Apply no native rate limit until _adopt_timeline learns frame delays.
        self._fastest_frame_rate: float = float("inf")

        # Without disk cache, retain PIL-composited frames and never create an FFmpeg reader.
        # Read the setting once so one object cannot change route during its life.
        if not mp4_tile_cache.cache_videos_enabled():
            frames, _ = self._decode_all(fit_size, saturation)
            self._hold_frame_list(frames)
            budget = gif_key_budget_bytes()
            if budget and self._frames_bytes > budget:
                log.warning(
                    f"{self.gif_path}: {self._frames_bytes / (1024 * 1024):.1f}MB of "
                    f"GIF frames retained, over the "
                    f"{budget / (1024 * 1024):.1f}MB per-GIF budget -- the bounded "
                    f"route needs performance.cache-videos, which is disabled"
                )
            return

        # Let corrupt-header errors reach the construction site, which falls back to InputVideo.
        n_frames, source_size = gif_header_geometry(self.gif_path)
        out_size = tile_video_size(source_size, fit_size)

        # Select lossless or bounded artifact before decode, using retained RGBA geometry for the estimate.
        # Separate variants prevent a prior alpha-dropped stream from classifying the source as opaque.
        retained_size = contained_size(source_size, fit_size)
        estimate = n_frames * retained_size[0] * retained_size[1] * 4
        budget = gif_key_budget_bytes()
        over_budget = estimate > budget
        variant = BOUNDED_TILE_VARIANT if over_budget else ""

        # On an artifact hit, probe only delays and attach without retaining decoded pixels.
        reader = mp4_tile_cache.attach_promoted(self.gif_path, out_size, saturation,
                                                variant=variant)
        if reader is not None:
            try:
                self._adopt_timeline(probe_gif_timeline(self.gif_path).frame_delays)
            except Exception:
                # Release the registry reference before propagating constructor failure.
                mp4_tile_cache.release(reader)
                raise
            self.video_cache = reader
            return

        # On a cold start, one PIL walk chooses the retained or streaming route.
        if over_budget:
            self._cold_streaming_walk(fit_size, saturation, out_size,
                                      estimate, budget, n_frames, retained_size)
        else:
            self._cold_retained_walk(fit_size, saturation, out_size)

    @override
    def _render_rate(self) -> float:
        return gif_render_rate(self.fps, self._fastest_frame_rate, self._total_delay)

    def _adopt_timeline(self, delays_ms: "list[int]") -> None:
        """Install one normalized playback timeline for every storage route."""
        self.frame_delays = list(delays_ms)
        self._cum_delays = cumulative_gif_delays(self.frame_delays)
        self._total_delay = self._cum_delays[-1] if self._cum_delays else 0.0
        shortest_ms = min(self.frame_delays) if self.frame_delays else 0
        if shortest_ms > 0:
            self._fastest_frame_rate = 1000.0 / shortest_ms

    def _composited_walk(self, fit_size: "tuple[int, int]", saturation: float,
                         delays_out: "list[int]", alpha_out: "list[bool]") -> "Generator[Image.Image, None, None]":
        """Yield shrink-only PIL-composited frames with baked saturation while recording delays and alpha.
        Stop alpha checks after the first positive result; callers retain or stream each frame."""
        with contextlib.closing(gif_frame_walk(
                self.gif_path, max_size=fit_size, saturation=saturation)) as walk:
            for frame, delay in walk:
                delays_out.append(delay)
                if not alpha_out[0] and frame_has_alpha(frame):
                    alpha_out[0] = True
                yield frame

    def _decode_all(self, fit_size: "tuple[int, int]",
                    saturation: float) -> "tuple[list[Image.Image], bool]":
        """Decode and retain the GIF, install its timeline, and return frames plus rendered alpha."""
        delays: "list[int]" = []
        alpha = [False]
        frames = list(self._composited_walk(fit_size, saturation, delays, alpha))
        self._adopt_timeline(delays)
        return frames, alpha[0]

    def _hold_frame_list(self, frames: "list[Image.Image]") -> None:
        """Retain alpha-capable frames and register their bytes for accounting only.
        Keep them nonevictable because tick-time eviction would force repeated GIF decoding."""
        self.frames = frames
        self._frames_bytes = sum(
            frame.width * frame.height * len(frame.getbands()) for frame in frames
        )
        cache_budget.register(
            self, label=f"gif_frames:{os.path.basename(self.gif_path)}", evictable=False)

    def _cold_retained_walk(self, fit_size: "tuple[int, int]", saturation: float,
                            out_size: "tuple[int, int]") -> None:
        """Decode an under-budget GIF once, retaining alpha frames or caching opaque frames.
        Keep construction synchronous because opaque encoding temporarily holds the fitted list."""
        frames, has_alpha = self._decode_all(fit_size, saturation)
        if has_alpha:
            self._hold_frame_list(frames)
            return

        reader = mp4_tile_cache.acquire_from_frames(
            self.gif_path, out_size, saturation, frames)
        if reader is None:
            # If cache storage or its codec is unavailable, retain frames so playback still works.
            log.warning(
                f"Could not build the tile cache for {self.gif_path}; keeping "
                f"{len(frames)} frames in RAM for this key instead"
            )
            self._hold_frame_list(frames)
            return
        self.video_cache = reader

    def _cold_streaming_walk(self, fit_size: "tuple[int, int]", saturation: float,
                             out_size: "tuple[int, int]", estimate: int, budget: int,
                             n_frames: int, retained_size: "tuple[int, int]") -> None:
        """Stream an over-budget GIF into the tile cache without retaining frames.
        This bounds memory but drops alpha and logs that loss once during construction."""
        log.warning(
            f"{self.gif_path}: ~{estimate / (1024 * 1024):.1f}MB of frames "
            f"({n_frames} at {retained_size[0]}x{retained_size[1]} RGBA) exceeds the "
            f"{budget / (1024 * 1024):.1f}MB per-GIF budget -- streaming it into the "
            f"mp4 tile cache instead, at one frame of RAM"
        )
        delays: "list[int]" = []
        alpha = [False]
        reader = mp4_tile_cache.acquire_from_frames(
            self.gif_path, out_size, saturation,
            self._composited_walk(fit_size, saturation, delays, alpha),
            variant=BOUNDED_TILE_VARIANT)
        if reader is None:
            raise RuntimeError(
                f"GIF is over the per-GIF frame budget and its tile cache could "
                f"not be built: {self.gif_path}"
            )
        if alpha[0]:
            log.warning(
                f"{self.gif_path} carries transparency, which the bounded mp4 route "
                f"cannot keep -- this key plays opaque. Raise "
                f"DECKARD_GIF_KEY_BUDGET_MB above "
                f"{estimate / (1024 * 1024):.1f} to keep it in RAM instead"
            )
        self.video_cache = reader
        if not delays:
            # If another key published first, the generator did not run; probe delays separately.
            delays = probe_gif_timeline(self.gif_path).frame_delays
        self._adopt_timeline(delays)

    def _source_index(self, cache: "mp4_tile_cache.KeyVideoCache", index: int) -> int:
        """Map a PIL timeline index into the reader's current frame range.
        Scale count mismatches to avoid over-read while PIL keeps timing authority."""
        n_video = cache.n_frames
        n_timeline = len(self._cum_delays)
        if n_video <= 0 or n_timeline <= 0 or n_video == n_timeline:
            return index
        return min(n_video - 1, index * n_video // n_timeline)

    def _video_frame(self, index: int) -> Image.Image | None:
        """Read one shared-cache frame while close waits for any in-flight decode.
        Use an unlocked first check to keep post-close calls fast."""
        if self.video_cache is None:
            return None
        with self._close_lock:
            # Re-read under the lock because close can clear the narrowed attribute.
            cache: mp4_tile_cache.KeyVideoCache | None = self.video_cache
            if cache is None:
                return None
            return cache.get_frame(self._source_index(cache, index))

    def _frame_at(self, index: int) -> Image.Image | None:
        """Return a picked retained or cached frame, or None after release."""
        frames = self.frames
        if frames:
            return frames[index]
        return self._video_frame(index)

    def budget_bytes(self) -> int:
        """Return immutable retained-frame bytes for the image-cache census.
        Return zero on the video route because reader memory is counted separately."""
        return self._frames_bytes

    def _frame_count(self) -> int:
        """Return retained-list count for alpha or timeline count for cached video."""
        if self.frames:
            return len(self.frames)
        return len(self._cum_delays) if self.video_cache is not None else 0

    def get_next_frame(self, now: float | None = None) -> Image.Image | None:
        # Snapshot the timeline for one tick so concurrent close cannot cause zero modulo or stale indexing.
        cum_delays = self._cum_delays
        total_delay = self._total_delay
        n = self._frame_count()
        if n == 0:
            return None
        if n == 1 or total_delay <= 0:
            # A single frame or unusable timing has no alternate frame to pick.
            self.active_frame = 0
            return self._frame_at(0)

        if now is None:
            now = media_loop.now()

        if self._play_start is None:
            self._play_start = now
        elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
            # Shift the timebase across inactive gaps so playback resumes without fast-forward.
            frame_period = cum_delays[0] if cum_delays else total_delay / n
            self._play_start += (now - self._last_frame_tick) - frame_period
        self._last_frame_tick = now

        elapsed = now - self._play_start
        # Treat fps as a sampling cap over GIF time, so it limits frame changes without changing playback speed.
        # Read it once; omit loop-rate-or-higher caps to preserve exact uncapped picks.
        cap = max(1.0, float(self.fps or MEDIA_LOOP_FPS))
        # Sample each loop at least twice so a long cap period cannot freeze one position.
        cap = max(cap, 2.0 / total_delay)
        if self.loop:
            # Quantize position after modulo; quantizing elapsed first can step backward or collapse the loop.
            t = elapsed % total_delay
            if cap < MEDIA_LOOP_FPS:
                t = int(t * cap) / cap
        else:
            # Non-loop elapsed is monotonic, and post-quantization clamping keeps the last frame reachable.
            if cap < MEDIA_LOOP_FPS:
                elapsed = int(elapsed * cap) / cap
            t = min(elapsed, total_delay)

        frame = bisect.bisect_right(cum_delays, t)
        if frame >= n:
            frame = n - 1  # clamp a float edge, or a non-loop t equal to total
        self.active_frame = frame

        return self._frame_at(self.active_frame)

    def get_frame_delay(self) -> float:
        """The delay of the current frame, in seconds."""
        if self.active_frame < 0 or self.active_frame >= len(self.frame_delays):
            return 1.0 / self.fps  # fall back to fps-based timing
        return self.frame_delays[self.active_frame] / 1000.0

    def native_fps(self) -> float | None:
        """Return average uncapped frame rate across one GIF loop.
        Return None without a timeline, including after close."""
        total = self._total_delay
        n = len(self._cum_delays)
        if n <= 0 or total <= 0:
            return None
        return n / total

    def set_playback(self, fps: int, loop: bool) -> None:
        """Set render cap and loop without rebasing the GIF timeline.
        The cap changes sampling frequency, not playback position or speed."""
        self.fps = fps
        self.loop = loop

    def get_raw_image(self) -> "Image.Image | None":
        # Return None after close, consistent with the shared media hierarchy contract.
        return self.get_next_frame()
    
    def close(self) -> None:
        """Clear timeline and retained frames before releasing this key's shared-cache reader.
        Empty containers keep late ticks and repeated close calls safe; the lock waits for active reads."""
        self.frames = []
        self.frame_delays = []
        self._cum_delays = []
        self._total_delay = 0.0
        self._frames_bytes = 0
        cache_budget.unregister(self)
        with self._close_lock:
            if self.video_cache is not None:
                mp4_tile_cache.release(self.video_cache)
                self.video_cache = None
