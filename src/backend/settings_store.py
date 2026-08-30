"""Own settings-surface locations, corruption handling, writes, and sparse read-time schemas.
This toolkit-free module covers deck, asset, app, page-manager, and plugin JSON surfaces."""
from __future__ import annotations

import copy
import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import cast, Any

from loguru import logger as log

import globals as gl
from src.backend.atomic_json import (
    atomic_write_json,
    prune_corrupt_sidecars,
    quarantine_corrupt_file,
)


@dataclass(frozen=True)
class SurfaceSpec:
    """Describe one settings file with value-based equality and hashing.
    Equal caller-built specs behave like module constants; identity is not used."""

    #: Human name, used in error messages only.
    name: str
    #: Resolve each keyed or keyless access at call time; the store retains no
    #: import-time path, although callers can snapshot path() for their own use.
    path_fn: Callable[..., str]
    #: Empty root type: dict for objects or list for arrays; missing and corrupt
    #: files both read as root().
    root: type[dict[str, Any]] | type[list[Any]] = dict
    #: Optional sparse-read schema: excluded from hashing because mappings are
    #: unhashable, but included in equality so different schemas remain different specs.
    schema: Mapping[str, Any] | None = field(default=None, hash=False)
    #: Serve reads from memory, deep-copied per call, dropped on write.
    cached: bool = False
    #: Whether path_fn takes a key.
    keyed: bool = False
    #: Hand every reader the cached object instead of a copy; this requires cached.
    shared: bool = False

    def __post_init__(self) -> None:
        # Reject invalid module-level specs at import; an uncached surface has
        # no persistent object to share.
        if self.shared and not self.cached:
            raise ValueError(f"the {self.name} surface is shared but not cached: there is nothing to share")

    def path(self, key: str | None = None) -> str:
        """Return this surface's path, requiring a key exactly when configured.
        Missing or surplus keys raise so the store cannot invent an unread per-device path."""
        if self.keyed:
            if key is None:
                raise ValueError(f"the {self.name} surface is keyed: a key is required")
            return self.path_fn(key)
        if key is not None:
            raise ValueError(f"the {self.name} surface takes no key, got {key!r}")
        return self.path_fn()


# Define each deck default once; names map to section tables or bare top-level values.
DECK_DEFAULTS: dict[str, Any] = {
    "brightness": {
        # What the deck runs at while nothing chooses a brightness. The
        # device layer and the page-level brightness UI both use 75.
        "value": 75,
    },
    "screensaver": {
        "enable": False,
        "media-path": None,
        # Loop old screensaver media instead of freezing on its final frame;
        # ScreenSaver.loop, both Background setters, BackgroundVideo, and GifBackground use True.
        "loop": True,
        "fps": 30,
        # Minutes of no input before it shows.
        "time-delay": 5,
        # Dim to 30 instead of normal brightness 75; the device already uses 30
        # when this key is absent, so the editor and existing decks agree.
        "brightness": 30,
    },
    "background": {
        "enable": False,
        "media-path": None,
        # Loop old deck backgrounds instead of freezing on the final frame;
        # page backgrounds retain False because a single pass on entry is valid.
        "loop": True,
        "fps": 30,
        "extend-to-touchscreen": False,
        # Two or more still paths form a slideshow that overrides media-path;
        # fewer fall back to media-path, and an empty default preserves old backgrounds.
        "media-paths": [],
        # The pan-and-zoom viewport for the single media-path, as
        # {"x": ..., "y": ..., "scale": ...} with a normalized center and a
        # zoom factor. None means the default view, the centered cover crop
        # every background rendered with before views existed. A slideshow
        # entry carries its own view inside its media-paths object instead.
        "view": None,
        # Seconds one slideshow image shows before the next. A non-positive
        # value holds the first image rather than flickering.
        "slideshow-interval": 10,
        # "in-order" walks the list as built; "shuffle" walks a random
        # permutation, reshuffled each cycle.
        "slideshow-order": "in-order",
    },
    "display": {
        # Neutral factor; readers that feed ImageEnhance and cache keys own
        # clamping and non-finite validation because defaults cover only absence.
        "saturation": 1.0,
    },
    # Degrees. Stored as a bare int rather than a section, so it reads and
    # writes without a key.
    "rotation": 0,
    # Bare chosen name; empty means use the model name. Use "" rather than None
    # because the editor needs a string and display_name treats both as absent.
    "name": "",
    # Fake-deck constructors own key-layout and supply their own fallback, so
    # no single layout belongs in this table.
}

#: Limit chosen names because the header switcher does not shorten labels;
#: both the editor and display_name enforce this against hand-edited files.
DECK_NAME_MAX_LENGTH = 32

#: One cached file per serial for repeated render reads; deep-copy each result
#: because callers commonly mutate and save it.
DECK = SurfaceSpec(
    name="deck settings",
    path_fn=lambda serial: os.path.join(gl.DATA_PATH, "settings", "decks", f"{serial}.json"),
    root=dict,
    schema=DECK_DEFAULTS,
    cached=True,
    keyed=True,
)

#: Uncached array-rooted asset index; absent, corrupt, or object-rooted content
#: reads as an empty library, and the backend retains its one construction read.
ASSET_LIBRARY = SurfaceSpec(
    name="asset library",
    path_fn=lambda: os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets.json"),
    root=list,
)


# Every app-settings default, defined exactly once. Callers read through
# AppSettings, rather than repeat .get(section, {}).get(key, default).
APP_DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {
        "hold-time": 0.5,
        "rolling-labels": True,
        # True gives held-key shrink feedback; False preserves composite images
        # whose seams would break when one key shrinks.
        "shrink-on-press": True,
        "app-launches": 0,
        "show-donate-window": True,
        "default-font": {},
    },
    "ui": {
        "tray-icon": True,
        "allow-white-mode": False,
        "show-notifications": True,
        "auto-open-action-config": True,
    },
    "key-grid": {
        "emulate-at-double-click": True,
    },
    "warnings": {
        "enable-fps-warnings": True,
    },
    "system": {
        # Three states. None means "never asked", and it makes
        # mainWindow.on_close raise the KeepRunningDialog.
        "keep-running": None,
        "autostart": True,
        "lock-on-lock-screen": True,
    },
    "performance": {
        "n-cached-pages": 3,
        "cache-videos": True,
        # "screensaver" relies on its transition to release page media;
        # "system-idle" additionally pauses deck animation while idle or locked.
        "animation-pause-mode": "screensaver",
        "animation-idle-minutes": 5,
    },
    "store": {
        "auto-update": True,
        "install-scripts": "ask",
        "responsibility-notes-agreed": False,
        "enable-custom-stores": False,
        "enable-custom-plugins": False,
        "custom-stores": [],
        "custom-plugins": [],
    },
    "dev": {
        "n-fake-decks": 0,
        "n-remote-decks": 0,
    },
}


def _fallback_font() -> str:
    # Resolve lazily because globals.__getattr__ runs a system font scan on first
    # access, which must not occur while this module imports.
    return cast(str, gl.fallback_font)


# default-font subkeys use these values when stored data is absent or falsy;
# keep this separate from absent-only schemas, and resolve callable defaults late.
APP_FONT_DEFAULTS: dict[str, Any] = {
    "font-family": _fallback_font,
    "font-size": 15,
    "font-weight": 400,
    "font-style": "normal",
    "font-color": (255, 255, 255, 255),
    # Alpha 255, and not 1. This value feeds color_values_to_gdk, which reads
    # 0 to 255 on all four channels, and the render fallback is (0,0,0,255).
    "outline-color": (0, 0, 0, 255),
    "outline-width": 2,
}

#: Share one cached app-settings dictionary so concurrent holders see edits;
#: construction snapshots must use read_fresh to avoid exposing unfinished work.
APP = SurfaceSpec(
    name="app settings",
    path_fn=lambda: os.path.join(gl.DATA_PATH, "settings", "settings.json"),
    root=dict,
    schema=APP_DEFAULTS,
    cached=True,
    shared=True,
)



# Uncached schema-free page-manager bookkeeping for default and warm pages;
# read-modify-write callers use edit() so concurrent updates are retained.
PAGES = SurfaceSpec(
    name="page manager settings",
    path_fn=lambda: os.path.join(gl.DATA_PATH, "settings", "pages.json"),
    root=dict,
)

#: Uncached fixed-location data-path override; globals.py alone reads it before
#: imports to define gl.DATA_PATH, and only that bootstrap keeps a quiet fallback.
STATIC = SurfaceSpec(
    name="static settings",
    path_fn=lambda: gl.STATIC_SETTINGS_FILE_PATH,
    root=dict,
)


# Default both filters to True so disabled defaults alone cannot empty the chooser;
# one schema prevents divergent inline defaults across readers and writers.
UI_ASSET_MANAGER_DEFAULTS: dict[str, Any] = {
    "video-toggle": True,
    "image-toggle": True,
}

#: Uncached asset-manager UI state, read once per opening through the shared
#: toggle schema.
UI_ASSET_MANAGER = SurfaceSpec(
    name="asset manager ui state",
    path_fn=lambda: os.path.join(gl.DATA_PATH, "settings", "ui", "AssetManager.json"),
    root=dict,
    schema=UI_ASSET_MANAGER_DEFAULTS,
)



#: Uncached schema-free plugin settings keyed by PluginBase's resolved path,
#: not plugin id; the app owns only the envelope and cannot validate plugin keys.
PLUGIN = SurfaceSpec(
    name="plugin settings",
    path_fn=lambda settings_path: settings_path,
    root=dict,
    keyed=True,
)

#: Current envelope version, with plugin keys under "settings"; other shapes
#: are pre-envelope settings and migrate on first read.
PLUGIN_FILE_VERSION = "2.0"


class SettingsStore:
    """The process-wide settings store. get() reaches it."""

    def __init__(self) -> None:
        # Cache one master value per resolved file, not per reader; copied reads
        # do not expose it, and growth follows settings files seen, not read count.
        self._cache: dict[str, Any] = {}
        # Count invalidations per resolved path so a cold read caches only if no
        # write occurred during parse; counts distinguish changes that flags can hide.
        self._invalidations: dict[str, int] = {}
        # Memoize raw-to-resolved read keys because repeated realpath costs 30 times
        # a shared lookup; writes resolve afresh and clear this stale-on-retarget memo.
        self._resolved: dict[str, str] = {}
        # A leaf lock. No holder keeps it across file I/O, and no holder
        # keeps it across the edit lock.
        self._cache_lock = threading.Lock()
        # Retain one lazily built edit lock per resolved file; the number of
        # settings files bounds the registry.
        self._edit_locks: dict[str, threading.Lock] = {}
        self._edit_locks_guard = threading.Lock()

    def read(self, spec: SurfaceSpec, key: str | None = None) -> Any:
        """Return content or an empty root; cached reads copy unless the surface is shared.
        Mutations need write() to persist; shared readers see each other's in-memory changes."""
        data, _corrupt = self.read_reporting_corruption(spec, key)
        return data

    def read_reporting_corruption(self, spec: SurfaceSpec, key: str | None = None) -> tuple[Any, bool]:
        """Return content and whether this read found an existing unparseable or wrong-root file.
        Missing, {}, later, and cached reads are false; do not cache or depend on quarantine."""
        path = spec.path(key)
        if not spec.cached:
            return self.load_file(path, root=spec.root)

        resolved = self._resolve_for_read(path)
        with self._cache_lock:
            if resolved in self._cache:
                return self._handout(spec, self._cache[resolved]), False
            # Record generation under the miss lock so every later write must
            # change it before this read can populate the cache.
            generation = self._invalidations.get(resolved, 0)

        # Parse outside the lock. A parse must not block another surface's
        # reader.
        data, corrupt = self.load_file(path, root=spec.root)

        with self._cache_lock:
            if self._invalidations.get(resolved, 0) == generation:
                # Concurrent cold readers parsed the same generation; setdefault
                # makes both adopt one object, which shared surfaces require.
                data = self._cache.setdefault(resolved, data)
            # A changed generation means this parse predates a write; return it
            # to this caller but do not cache it, so the next reader loads afresh.
        return self._handout(spec, data), corrupt

    def read_fresh(self, spec: SurfaceSpec, key: str | None = None) -> Any:
        """Read a private disk snapshot without reading or filling any cache entry.
        Editors use this so final writes match shown data and unfinished changes stay private."""
        data, _corrupt = self.load_file(spec.path(key), root=spec.root)
        return data

    def view(self, spec: SurfaceSpec, key: str | None = None) -> SchemaView:
        """Wrap one schema-backed read, aliasing shared surfaces and copying others.
        Raise when the surface has no schema."""
        if spec.schema is None:
            raise ValueError(f"the {spec.name} surface has no schema to read through")
        return SchemaView(self.read(spec, key), spec.schema, shared=spec.shared)

    def write(self, spec: SurfaceSpec, data: Any, key: str | None = None) -> None:
        """Persist this surface atomically and drop its cached entry."""
        self.save_file(spec.path(key), data)

    @contextmanager
    def edit(self, spec: SurfaceSpec, key: str | None = None) -> Iterator[Any]:
        """Serialize disk read-modify-write blocks per file, writing only after normal block exit.
        Plain writes are last-wins; keyed files lock separately, and cached reads are fresh."""
        path = spec.path(key)
        with self._edit_lock(path):
            data, _corrupt = self.load_file(path, root=spec.root)
            yield data
            self.save_file(path, data)

    def load_file(self, file_path: str, root: type[dict[str, Any]] | type[list[Any]] = dict) -> tuple[Any, bool]:
        """Read and quarantine one path-level JSON file, returning (data, corrupt).
        root supplies absent or corrupt content; this path API knows no surface."""
        empty = root()
        if not os.path.exists(file_path):
            return empty, False
        try:
            with open(file_path) as f:
                parsed = json.load(f)
            # Treat scalar, null, or wrong-container roots as corrupt before they
            # reach schema accessors; no configured root uses int, so bool is irrelevant.
            if not isinstance(parsed, root):
                raise ValueError(
                    f"root is {type(parsed).__name__}, expected {root.__name__}")
            return parsed, False
        except FileNotFoundError:
            # A concurrent quarantine moved the file between the exists()
            # check and the open.
            return empty, False
        except ValueError as e:
            # ValueError covers decode, JSON, and wrong-root failures. Quarantine
            # before a save can overwrite the only copy; backup healing uses the flag.
            moved, dest = quarantine_corrupt_file(file_path)
            if moved:
                log.error(f"Invalid json in {file_path}: {e} -- preserved at {dest}, loading empty")
                # Prune only this newly quarantined path, not a startup-wide scan;
                # all page, deck, and app corruption reaches this loader.
                for pruned in prune_corrupt_sidecars(file_path, protect=dest):
                    log.info(f"Pruned old quarantined copy {pruned}")
            else:
                log.error(
                    f"Invalid json in {file_path}: {e} -- could NOT move it aside "
                    f"(left in place); callers with a backup will heal, loading empty"
                )
            return empty, True

    def save_file(self, file_path: str, data: Any) -> None:
        """Atomically write one JSON file, then invalidate its path-level cache entry.
        Path invalidation prevents stale readers even when callers bypass a surface."""
        # Temporary-file fsync and replace prevent truncation and create missing
        # parent directories.
        atomic_write_json(file_path, data)
        self.invalidate_path(file_path)

    def invalidate_path(self, file_path: str) -> None:
        """Forget the cached content of one file, if any is held."""
        resolved = _resolve(file_path)
        with self._cache_lock:
            self._cache.pop(resolved, None)
            # Resolution memoization serves reads only between writes; clear it
            # so a moved link is followed after its old cache entry is gone.
            self._resolved.clear()
            # Count even without a cached entry so a reader between miss and store
            # cannot cache content that this write replaced.
            self._invalidations[resolved] = self._invalidations.get(resolved, 0) + 1

    def _resolve_for_read(self, file_path: str) -> str:
        """Return the unlocked memoized cache key for a raw read path.
        Races compute the same value; writes, edits, and invalidations always resolve afresh."""
        try:
            return self._resolved[file_path]
        except KeyError:
            resolved = _resolve(file_path)
            self._resolved[file_path] = resolved
            return resolved

    @staticmethod
    def _handout(spec: SurfaceSpec, data: Any) -> Any:
        """Return the cached object for shared surfaces and a deep copy otherwise."""
        return data if spec.shared else copy.deepcopy(data)

    def _edit_lock(self, file_path: str) -> threading.Lock:
        resolved = _resolve(file_path)
        with self._edit_locks_guard:
            lock = self._edit_locks.get(resolved)
            if lock is None:
                lock = threading.Lock()
                self._edit_locks[resolved] = lock
            return lock


def _resolve(file_path: str) -> str:
    """Resolve one cache and lock key, matching the atomic writer's symlink handling.
    Managed settings must read, write, lock, and invalidate under one name."""
    return os.path.realpath(file_path)


# Keep the process store as a module singleton rather than expanding globals.
_store = SettingsStore()


def get() -> SettingsStore:
    """The process-wide settings store. Never None."""
    return _store

# Re-export typed views after their required specs and singleton exist;
# settings_views is this module's implementation back half, not a public entry point.
from src.backend.settings_views import (  # noqa: E402, F401
    UNNAMED_DECK as UNNAMED_DECK,
    AppSettings as AppSettings,
    DeckSettings as DeckSettings,
    PluginSettings as PluginSettings,
    SchemaView as SchemaView,
)
