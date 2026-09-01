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

The native encode wrappers, one per kind of thing an input paints. Each
takes the image its input composited and returns the device-ready JPEG the
media thread writes, and each owns the cache that decides whether an encode
runs at all: the encode memo for a key composite, the native tile cache for
a background-video tile, and no cache for the touchscreen strip, which
composites once per frame anyway.

The profiling boundaries belong with the wrappers. A key wrapper starts its
timer before the cache lookup and adds to the encode metric only when it
missed, so that metric holds what a miss costs, lookup included, and the hit
and miss counters split the two paths. Timing a wrapper from its call site
instead would fold the lookup into the caller's composite metric and lose
the split.

These functions take their input as the first argument rather than living on
it, because what they hold is encode policy and not input state. They sit
between two neighbours: media_writer below does the pixel work, the inputs
above own the composites. A key wrapper calls back into its input for the
rotated RGB form of a composite, which stays there because the rotation is
the input's own device geometry. Nothing here keeps state of its own.
"""
import time

from PIL import Image
from loguru import logger as log

from src.backend.DeckManagement.Subclasses.media_pipeline_profiler import media_prof
from src.backend.DeckManagement.deck_controller.media_writer import (
    KEY_ENCODE_QUALITY,
    encode_native_key,
    encode_native_touchscreen,
)

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerKey, ControllerTouchScreen


def _encode_key_native(key: "ControllerKey", image: Image.Image, img_hash: int) -> bytes:
    """The device-ready JPEG for a composited key image, from the encode
    memo when the same composite was encoded before."""
    _t0 = 0.0  # Initialize for the guarded profiling read.
    if media_prof:
        _t0 = time.perf_counter()
    memo_key = (img_hash, key.deck_controller.deck.get_rotation())
    native_image = key.deck_controller.encode_memo.get(memo_key)
    if native_image is None:
        rgb_image = key._to_rotated_rgb(image)
        native_image = encode_native_key(key.deck_controller.deck, rgb_image)
        rgb_image.close()
        key.deck_controller.encode_memo.put(memo_key, native_image)
        if media_prof:
            media_prof.add("encode", time.perf_counter() - _t0)
            media_prof.count("memo_miss")
    elif media_prof:
        media_prof.count("memo_hit")
    return native_image


def _encode_tile_native(key: "ControllerKey", tile: Image.Image, video_md5: str, frame_index: int) -> bytes:
    """Return a background tile JPEG from frame-identity cache or encoding.
    The key includes every input that affects the native bytes."""
    _t0 = 0.0  # Initialize for the guarded profiling read.
    if media_prof:
        _t0 = time.perf_counter()
    cache_key = (video_md5, frame_index, key.present_state.key_index,
                 key.deck_controller.deck.get_rotation(),
                 KEY_ENCODE_QUALITY,
                 key.deck_controller.native_key_format_sig())
    native_image = key.deck_controller.native_tile_cache.get(cache_key)
    if native_image is None:
        rgb_image = key._to_rotated_rgb(tile)
        native_image = encode_native_key(key.deck_controller.deck, rgb_image)
        rgb_image.close()
        key.deck_controller.native_tile_cache.put(cache_key, native_image)
        if media_prof:
            media_prof.add("encode", time.perf_counter() - _t0)
            media_prof.count("native_id_miss")
    elif media_prof:
        media_prof.count("native_id_hit")
    return native_image


def _encode_strip_native(touchscreen: "ControllerTouchScreen", image: Image.Image) -> bytes:
    """Encode the strip as an oriented JPEG, flattening RGBA onto black.
    Preserve the caller's image for preview and close every intermediate."""
    logical_size = touchscreen.deck_controller.deck.logical_touchscreen_size()
    # A composite left the wrong size by a turn in flight is dropped as empty bytes, which
    # the strip ticket reads as nothing to write; the repaint at the new size follows.
    if logical_size is not None and image.size != logical_size:
        log.debug(f"dropping a {image.size} strip composite; the deck now "
                  f"composes at {logical_size}")
        return b""
    if image.mode == "RGBA":
        device_image = Image.new("RGB", image.size, (0, 0, 0))
        device_image.paste(image, (0, 0), image)
    else:
        device_image = image
    try:
        return encode_native_touchscreen(touchscreen.deck_controller.deck, device_image)
    finally:
        if device_image is not image:
            device_image.close()
