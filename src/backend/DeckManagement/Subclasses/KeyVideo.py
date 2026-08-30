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
import threading

from src.backend.DeckManagement.Subclasses.SingleKeyAsset import SingleKeyAsset
from src.backend.DeckManagement.Subclasses import mp4_tile_cache
from src.backend.DeckManagement import media_loop
from src.backend.DeckManagement.media_loop import MEDIA_LOOP_FPS, FrameScheduled
from PIL import Image

from typing import Any, TYPE_CHECKING, override
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput

class InputVideo(SingleKeyAsset, FrameScheduled):
    def __init__(self, controller_input: "ControllerInput[Any]", video_path: str, fps: int = MEDIA_LOOP_FPS, loop: bool = True,
                 natural_speed: bool = False):
        super().__init__(
            controller_input=controller_input,
        )
        self.video_path = video_path
        self.fps = fps
        self.loop = loop
        # Natural playback uses source fps and treats fps as a render cap.
        # Otherwise fps controls playback speed for key and dial media.
        self.natural_speed = natural_speed

        # Each instance owns a reader; source, size, and saturation share the file and builder.
        # release() detaches this reader without necessarily removing the shared file.
        self.video_cache: mp4_tile_cache.KeyVideoCache | None = mp4_tile_cache.acquire(
            video_path,
            self.controller_input.get_image_size(),
            self.deck_controller.get_display_saturation(),
        )
        # Serialize close() with full frame reads so no read uses a released cache.
        # A post-release get_frame() can reopen and leak a capture.
        self._close_lock = threading.Lock()

        self.active_frame: int = -1
        # Clock state for real-time frame selection and gap clamping.
        self._play_start: float | None = None  # playback start on the media clock, set on first real-time frame
        self._last_frame_tick: float | None = None  # last real-time frame pick, for gap clamping

    def get_next_frame(self, now: float | None = None) -> Image.Image | None:
        # Check before and after locking because close() can win between them.
        # Hold the lock through selection and decode so close() waits for the read.
        if self.video_cache is None:
            return None
        with self._close_lock:
            # Re-read under the lock because close() can clear the attribute.
            cache: mp4_tile_cache.KeyVideoCache | None = self.video_cache
            if cache is None:
                return None

            if now is None:
                now = media_loop.now()

            # Reject zero-frame sources before cache completion and modulo checks.
            if cache.n_frames <= 0:
                return None

            if cache.is_cache_complete():
                # Pick completed-cache frames by clock to drop late frames and stay real-time.
                playback_fps = float(self.fps or MEDIA_LOOP_FPS)
                if self.natural_speed:
                    playback_fps = float(cache.get_source_fps() or playback_fps)
                if self._play_start is None:
                    # Seed from the current position because the cache can complete during playback.
                    # A zero base replays non-looping video or jumps looping video.
                    self._play_start = now - (self.active_frame + 1) / playback_fps
                elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
            # Shift across inactive-page gaps so playback resumes without fast-forwarding.
                    self._play_start += (now - self._last_frame_tick) - 1.0 / playback_fps
                self._last_frame_tick = now
                elapsed = now - self._play_start
                if self.natural_speed:
                    # Quantize here so other animated content cannot exceed the owner's fps cap.
                    # Stable picks within a cap window let hash dedup remove redundant writes.
                    cap = max(1.0, float(self.fps or MEDIA_LOOP_FPS))
                    elapsed = int(elapsed * cap) / cap
                frame = int(elapsed * playback_fps)
                n_frames = cache.n_frames
                self.active_frame = frame % n_frames if self.loop else min(frame, n_frames - 1)
            else:
                # Advance sequentially while building so every frame enters the cache.
                # Clock jumps would leave gaps and force locked seeks.
                self.active_frame += 1
                if self.active_frame >= cache.n_frames and self.loop:
                    self.active_frame = 0

            return cache.get_frame(self.active_frame)

    @override
    def _render_rate(self) -> float:
        """Return the loop-capped rate; natural playback also uses source rate and fps cap.
        Use loop rate while building so sequential cache decode does not slow."""
        cache = self.video_cache
        if cache is not None and not cache.is_cache_complete():
            return MEDIA_LOOP_FPS
        cap = max(1.0, float(self.fps or MEDIA_LOOP_FPS))
        if not self.natural_speed:
            return min(MEDIA_LOOP_FPS, cap)
        source = self.native_fps() or cap
        return min(MEDIA_LOOP_FPS, cap, source)

    def native_fps(self) -> float | None:
        """Return the uncapped container frame rate, or None without a usable reader rate.
        Natural playback uses this rate and treats fps only as a pick cap."""
        cache = self.video_cache
        if cache is None:
            return None
        return cache.get_source_fps()

    def set_playback(self, fps: int, loop: bool) -> None:
        """Apply fps and loop without changing the current playback position.
        Rebase non-natural playback because its elapsed time uses fps; natural playback does not."""
        if not self.natural_speed and (self.fps or MEDIA_LOOP_FPS) != (fps or MEDIA_LOOP_FPS) and self._play_start is not None:
            self._play_start = media_loop.now() - (self.active_frame + 1) / float(fps or MEDIA_LOOP_FPS)
        self.fps = fps
        self.loop = loop

    @override
    def get_raw_image(self) -> Image.Image | None:
        # Return None after reader closure or for a degenerate source.
        return self.get_next_frame()

    @override
    def close(self) -> None:
        """Idempotently detach this reader and release its VideoCapture.
        The close lock waits for an active frame and blocks later reads."""
        with self._close_lock:
            if self.video_cache is not None:
                mp4_tile_cache.release(self.video_cache)
                self.video_cache = None
