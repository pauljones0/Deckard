"""Remove unreferenced MD5-keyed video caches at startup.
Also remove unreadable legacy formats and abandoned writer temporary files."""
import contextlib
import hashlib
import math
import os
import re
import shutil
import time
from typing import Any

from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.HelperMethods import is_video
from src.backend.DeckManagement.Subclasses.mp4_tile_cache import (
    registry_cache_paths,
    remove_cache_file_if_unreferenced,
    sat_suffix,
)
from src.backend.PageManagement import page_flush

VID_CACHE = os.path.join(gl.DATA_PATH, "cache", "videos")

# A .tmp.mp4 younger than this can be a build in progress. An older one is a
# leftover from a crash.
TMP_MAX_AGE_S = 24 * 60 * 60

# Match the runtime's 1.0-1.5 saturation clamp when deriving protected suffixes.
# Raw invalid values can protect an unwritten variant and remove the active one.
MIN_DISPLAY_SATURATION = 1.0
MAX_DISPLAY_SATURATION = 1.5
DEFAULT_DISPLAY_SATURATION = 1.0


def _clamp_saturation(raw: Any) -> float:
    """Map persisted saturation to the runtime-clamped factor.
    Non-numeric and non-finite values use the default so cache filenames agree."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DISPLAY_SATURATION
    if not math.isfinite(value):
        return DEFAULT_DISPLAY_SATURATION
    return min(MAX_DISPLAY_SATURATION, max(MIN_DISPLAY_SATURATION, value))


# Match cache names with optional saturation, viewport, and rendering suffixes.
# Keep all views for referenced videos; sweep them by source and saturation.
_MP4_NAME_RE = re.compile(
    r"^(?P<hash>[0-9a-f]+)(?P<sat>\.sat\d+)?(?P<view>\.v[0-9-]+)?(?P<variant>\.[a-z]+)?\.mp4$")

# Match obsolete top-level JPEG-per-frame cache directories.
# No current reader can use their single_key or key-number layouts.
_LEGACY_KEY_DIR_RE = re.compile(r"^key: \d+$")


def _is_legacy_key_video_dir(name: str) -> bool:
    return name == "single_key" or bool(_LEGACY_KEY_DIR_RE.match(name))


def _sweep_legacy_key_video_dirs() -> None:
    """Idempotently remove unreadable JPEG-per-frame cache directories.
    Bypass source-reference checks because no current reader can decode them."""
    if not os.path.isdir(VID_CACHE):
        return
    freed = 0
    removed = 0
    for name in os.listdir(VID_CACHE):
        if not _is_legacy_key_video_dir(name):
            continue
        path = os.path.join(VID_CACHE, name)
        if not os.path.isdir(path):
            continue
        try:
            size = sum(
                os.path.getsize(os.path.join(root, fname))
                for root, _, files in os.walk(path) for fname in files
            )
            shutil.rmtree(path)
        except OSError:
            log.opt(exception=True).warning(f"Could not remove legacy key-video cache dir {path}")
            continue
        freed += size
        removed += 1
    if removed:
        log.success(f"Removed {removed} legacy key-video cache directories ({freed / 1e6:.1f} MB)")


def _collect_json_paths() -> list[str]:
    paths: list[str] = []
    decks_dir = os.path.join(gl.DATA_PATH, "settings", "decks")
    if os.path.isdir(decks_dir):
        paths.extend(
            os.path.join(decks_dir, name)
            for name in os.listdir(decks_dir) if name.endswith(".json")
        )
    # Include plugin custom pages and abort without a page manager.
    # An incomplete page set can delete live caches.
    page_manager = gl.page_manager
    if page_manager is None:
        raise RuntimeError(
            "video cache sweep started before the page manager exists -- "
            "refusing to sweep against an incomplete reference set")
    paths.extend(page_manager.get_pages(add_custom_pages=True, sort=False))
    # Scan plugin settings because they can reference media absent from decks and pages.
    # Omitting them can delete ready cache files.
    plugins_dir = os.path.join(gl.DATA_PATH, "settings", "plugins")
    if os.path.isdir(plugins_dir):
        for root, _, files in os.walk(plugins_dir):
            paths.extend(
                os.path.join(root, name) for name in files if name.endswith(".json")
            )
    return paths


def _walk_for_video_paths(node: Any, found: set[str]) -> None:
    """Collect every JSON string that points to an existing video.
    Structure-independent scanning covers backgrounds, screensavers, keys, and dials."""
    if isinstance(node, dict):
        for value in node.values():
            _walk_for_video_paths(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk_for_video_paths(value, found)
    elif isinstance(node, str):
        if is_video(node):
            found.add(node)


def _md5_of_file(path: str) -> str:
    # Same hashing as BackgroundVideoCache and KeyVideoCache, so keys match.
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        while block := f.read(2 ** 16):
            md5.update(block)
    return md5.hexdigest()


def collect_referenced_video_hashes() -> set[str]:
    video_paths: set[str] = set()
    # Flush all pending page writes before the full-set reference scan.
    # A debounced video reference would otherwise lose its cache.
    page_flush.get().flush_all()
    for json_path in _collect_json_paths():
        try:
            _walk_for_video_paths(gl.settings_manager.load_settings_from_file(json_path), video_paths)
        except Exception:
            log.opt(exception=True).warning(f"Could not scan {json_path} for video references")

    hashes = set()
    for path in video_paths:
        with contextlib.suppress(OSError):
            hashes.add(_md5_of_file(path))
    return hashes


def collect_active_sat_suffixes() -> set[str]:
    """Collect active display-saturation suffixes plus the default empty suffix.
    Remove other variants of referenced videos to prevent permanent disk growth."""
    suffixes = {""}
    decks_dir = os.path.join(gl.DATA_PATH, "settings", "decks")
    if not os.path.isdir(decks_dir):
        return suffixes
    for name in os.listdir(decks_dir):
        if not name.endswith(".json"):
            continue
        try:
            settings = gl.settings_manager.load_settings_from_file(
                os.path.join(decks_dir, name)
            ) or {}
            raw = settings.get("display", {}).get("saturation", 1.0)
            # Use runtime clamping so persisted invalid values cannot protect unwritten variants.
            suffixes.add(sat_suffix(_clamp_saturation(raw)))
        except Exception:
            # An unreadable deck can lose its variant, but an attached reader protects it.
            # A later reader invalidates a missing ready entry and rebuilds.
            log.opt(exception=True).warning(f"Could not read display saturation from {name}")
    return suffixes


@log.catch
def sweep_stale_video_caches(startup_delay: float = 0.0) -> None:
    if startup_delay:
        time.sleep(startup_delay)
    if not os.path.isdir(VID_CACHE):
        return

    _sweep_legacy_key_video_dirs()

    referenced = collect_referenced_video_hashes()
    active_sat_suffixes = collect_active_sat_suffixes()
    # Protect files with attached readers or builders even when the reference scan misses them.
    # An attached consumer is direct evidence of use.
    protected_paths = registry_cache_paths()
    freed = 0
    removed = 0

    for layout in os.listdir(VID_CACHE):
        if _is_legacy_key_video_dir(layout):
            # Keep failed legacy-directory removals out of source-reference handling.
            continue
        layout_dir = os.path.join(VID_CACHE, layout)
        if not os.path.isdir(layout_dir):
            continue
        for entry in os.listdir(layout_dir):
            entry_path = os.path.join(layout_dir, entry)
            entry_hash = entry.split(".")[0]

            try:
                if os.path.isdir(entry_path):
                    # No current format nests directories here; remove unreferenced remnants.
                    # Top-level legacy directories are handled before this loop.
                    if entry_hash in referenced:
                        continue
                    size = sum(
                        os.path.getsize(os.path.join(root, name))
                        for root, _, names in os.walk(entry_path) for name in names
                    )
                    shutil.rmtree(entry_path)
                elif ".tmp." in entry:
                    if time.time() - os.path.getmtime(entry_path) < TMP_MAX_AGE_S:
                        continue
                    size = os.path.getsize(entry_path)
                    os.remove(entry_path)
                elif entry.endswith(".cache"):
                    # Legacy pickle format. Current code cannot read it.
                    size = os.path.getsize(entry_path)
                    os.remove(entry_path)
                elif entry.endswith(".mp4"):
                    # Use the snapshot for an early skip, then check live state before removal.
                    # The second check closes the attachment race.
                    if entry_path in protected_paths:
                        continue
                    if entry_hash in referenced:
                        match = _MP4_NAME_RE.match(entry)
                        suffix = (match.group("sat") or "") if match else ""
                        if suffix in active_sat_suffixes:
                            continue
                        # Sweep referenced-video variants that no active saturation produces.
                    size = os.path.getsize(entry_path)
                    if not remove_cache_file_if_unreferenced(entry_path):
                        # A reader or builder attached to it since the snapshot.
                        continue
                else:
                    continue
            except OSError:
                log.opt(exception=True).warning(f"Could not sweep video cache entry {entry_path}")
                continue

            freed += size
            removed += 1

    if removed:
        log.success(f"Video cache sweep removed {removed} stale entries ({freed / 1e6:.1f} MB)")
