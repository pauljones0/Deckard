"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import contextlib
import os
import uuid
import cv2
from loguru import logger as log
from typing import cast

from PIL import Image, ImageDraw, ImageSequence

import os, psutil
process = psutil.Process()

from src.backend.DeckManagement.HelperMethods import is_svg, sha256, file_in_dir, svg_to_pil


import globals as gl

class MediaManager:
    def __init__(self) -> None:
        pass

    def get_fallback_thumbnail(self) -> Image.Image:
        """Return a tagged fallback that remains out of the disk cache.
        This keeps a corrupt file retryable."""
        img = Image.new("RGBA", (250, 180), (58, 58, 58, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle((95, 60, 155, 120), outline=(170, 170, 170, 255), width=3)
        draw.line((95, 60, 155, 120), fill=(170, 170, 170, 255), width=3)
        draw.line((155, 60, 95, 120), fill=(170, 170, 170, 255), width=3)
        img.info["sc_broken"] = True
        return img

    @staticmethod
    def save_image_atomic(image: Image.Image, path: str) -> None:
        """Save through a unique same-directory file and atomic replacement.
        A failed save leaves the destination unchanged."""
        tmp_path = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            image.save(tmp_path, format="PNG")
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                with contextlib.suppress(OSError):
                    os.remove(tmp_path)

    def get_thumbnail(self, file_path: str) -> Image.Image:
        # Contain hash, cache, and lazy-decode failures inside this UI path.
        # Callers require an image rather than an exception.
        try:
            hash = sha256(file_path)

            thumbnail_dir = os.path.join(gl.DATA_PATH, "cache", "thumbnails")
            thumbnail_path = os.path.join(thumbnail_dir, f"{hash}.png")

            os.makedirs(thumbnail_dir, exist_ok=True)


            cached = file_in_dir(f"{hash}.png", thumbnail_dir)
            if cached is None:
                cached = False

            if cached:
                try:
                    with Image.open(thumbnail_path) as img:
                        img.thumbnail((250, 250), resample=Image.Resampling.LANCZOS)
                        return cast(Image.Image, img.copy())
                except Exception as e:
                    # Remove an unreadable cache entry before fresh generation.
                    # A bad entry must not pin a valid source to the fallback.
                    log.opt(exception=True).warning(
                        f"Poisoned thumbnail cache entry for {file_path}, regenerating: {e}")
                    with contextlib.suppress(OSError):
                        os.remove(thumbnail_path)

            thumbnail = self.generate_thumbnail(file_path)
            thumbnail.thumbnail((250, 250), resample=Image.Resampling.LANCZOS)
            if not thumbnail.info.get("sc_broken"):
                # Do not cache the fallback under the source hash.
                # A transient failure must remain retryable.
                self.save_image_atomic(thumbnail, thumbnail_path)
            return thumbnail
        except Exception as e:
            log.opt(exception=True).warning(f"Could not create thumbnail for {file_path}: {e}")
            return self.get_fallback_thumbnail()

    def generate_thumbnail(self, file_path: str) -> Image.Image:
        # Keep corrupt media from killing import, Custom Assets, or startup work.
        # Log decode failures and return the tagged fallback.
        try:
            extension = os.path.splitext(file_path)[1].lower()
            if extension in (".jpg", ".jpeg", ".png"):
                thumbnail = self.generate_image_thumbnail(file_path)
            elif extension == ".gif":
                thumbnail = self.generate_gif_thumbnail(file_path)
            elif is_svg(file_path):
                thumbnail = self.generate_svg_thumbnail(file_path)
            else:
                thumbnail = self.generate_video_thumbnail(file_path)

            if thumbnail is None:
                raise ValueError("decoder returned no image")
            # Image.open is lazy. Force the decode here, so a truncated file
            # raises inside this guard and not later in the caller.
            thumbnail.load()
            return thumbnail
        except Exception as e:
            # Include the traceback to distinguish decoder defects from bad media.
            log.opt(exception=True).warning(f"Could not generate thumbnail for {file_path}: {e}")
            return self.get_fallback_thumbnail()

    def generate_video_thumbnail(self, video_path: str) -> Image.Image:
        cap = cv2.VideoCapture(video_path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 1])
        try:
            if not cap.isOpened():
                raise ValueError(f"could not open video: {video_path}")
            cap.set(cv2.CAP_PROP_POS_FRAMES, 1)
            ret, frame = cap.read()
        finally:
            cap.release()

        if not ret or frame is None:
            # For a 0-byte or corrupt video cap.read() returns (False, None),
            # and cv2.cvtColor(None) raises an opaque cv2.error.
            raise ValueError(f"could not read a frame from video: {video_path}")

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(frame_rgb)
        return pil_image
    
    def generate_svg_thumbnail(self, file_path: str) -> Image.Image:
        return svg_to_pil(file_path, 1024)

    def generate_image_thumbnail(self, file_path: str) -> Image.Image:
        return Image.open(file_path)
    
    def generate_gif_thumbnail(self, file_path: str) -> Image.Image:
        gif = Image.open(file_path)
        iterator = ImageSequence.Iterator(gif)
        n_frames = 0
        for frame in iterator: n_frames += 1 #TODO: Find a better way to do this
        frame = iterator[n_frames // 2] # A GIF often starts with an empty frame
        frame = frame.convert("RGBA")

        del gif, iterator, n_frames

        return frame
