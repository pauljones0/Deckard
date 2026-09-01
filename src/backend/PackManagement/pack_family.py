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

One discovery, one pack and one asset, shared by every family of installable art.

Icons, wallpapers and SD+ bar wallpapers all ship as packs. A pack is a folder
that holds a manifest.json, a thumbnail the manifest names, a folder of asset
files the manifest also names, and an optional attribution.json. A manager
scans one folder under the data path and returns the packs it can describe,
keyed by folder name.

The three families read that layout the same way and disagree in four places.
A family supplies each one as a class attribute:

- the folder under the data path that the manager scans (DATA_DIR)
- the manifest key that names the asset folder (ASSET_MANIFEST_KEY)
- the key an asset reads out of attribution.json (ATTRIBUTION_KEY)
- the word a rejection warning prints (LABEL)

A family also overrides two factories, make_pack and make_asset, so that a
manager builds packs of its own class and a pack builds assets of its own.
__init_subclass__ checks all six the moment a family class is written, so an
incomplete family fails at import rather than at the first pack it builds.

Two behaviours here are load-bearing and easy to lose.

The asset scan reads the asset folder and the folders one level below it, and
it skips a directory inside those, so a file two levels down stays invisible.
An os.walk in its place would list files the app has never shown.

The thumbnail check is lazy. A pack whose manifest names no thumbnail, or names
one that is not on disk, is still discovered as valid, because the constructor
never asks for the path. It turns invalid the first time a preview calls
get_thumbnail_path. An eager check in the constructor would drop those packs
out of the chooser instead.
"""
from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar, assert_never, cast

from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.HelperMethods import instance_cache


class AttributionKey(Enum):
    """Select how an asset is keyed in attribution.json.
    BASENAME shares entries across subfolders; RELPATH includes the asset folder and subfolder."""

    BASENAME = "basename"
    RELPATH = "relpath"


def pack_wide_entry(attribution: dict[str, Any]) -> dict[str, Any]:
    """Return the first pack-wide entry from default, general, then generic, or an empty mapping."""
    return cast("dict[str, Any]",
                attribution.get("default", attribution.get("general", attribution.get("generic", {}))))


def check_family_contract(cls: type, base: type, attributes: tuple[str, ...],
                          overrides: tuple[str, ...]) -> None:
    """Reject incomplete subclasses because type checking accepts annotations and factory stubs.
    Python calls __init_subclass__ only for subclasses, so the bases need no exemption."""
    missing = [name for name in attributes if not hasattr(cls, name)]
    missing += [name for name in overrides
                if getattr(cls, name) is getattr(base, name)]
    if missing:
        raise TypeError(
            f"{cls.__name__} is an incomplete pack family: it supplies no "
            f"{', '.join(missing)}. {base.__name__} says what a family owes."
        )


class PackAsset:
    """One asset file inside a pack."""

    ATTRIBUTION_KEY: ClassVar[AttributionKey]

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        check_family_contract(cls, PackAsset, ("ATTRIBUTION_KEY",), ())

    def __init__(self, pack: Pack[Any], path: str):
        self.pack = pack
        self.path = path

        self.name = os.path.splitext(os.path.basename(path))[0]

    def get_attribution_key(self) -> str:
        """The key this asset carries in attribution.json."""
        # Exhaustive on purpose: a third member added to AttributionKey fails
        # the type gate here rather than reading as a relative path.
        match self.ATTRIBUTION_KEY:
            case AttributionKey.BASENAME:
                return os.path.basename(self.path)
            case AttributionKey.RELPATH:
                return os.path.relpath(self.path, self.pack.path)
            case _:
                assert_never(self.ATTRIBUTION_KEY)

    def get_attribution(self) -> dict[str, Any]:
        attribution = self.pack.get_attribution_json()

        key = self.get_attribution_key()
        if key in attribution:
            return cast("dict[str, Any]", attribution[key])
        return pack_wide_entry(attribution)


AssetT = TypeVar("AssetT", bound=PackAsset)


class Pack(Generic[AssetT]):
    """Read one pack at construction and mark it invalid when required data is missing.
    Thumbnail validity remains lazy until get_thumbnail_path()."""

    # The family supplies this: the manifest key that names the asset folder.
    ASSET_MANIFEST_KEY: ClassVar[str]

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        check_family_contract(cls, Pack, ("ASSET_MANIFEST_KEY",), ("make_asset",))

    def __init__(self, path: str):
        self.path = path
        self.is_valid = True
        self.name = self.get_manifest().get("name") or os.path.basename(path)
        self.assets_by_folder: dict[str, list[AssetT]] = {}

        self.load_assets_by_folder(self.ASSET_MANIFEST_KEY)

    def make_asset(self, path: str) -> AssetT:
        """Build one asset of the family's own leaf class."""
        raise NotImplementedError

    @instance_cache
    def get_manifest(self) -> dict[str, Any]:
        path = Path(os.path.join(self.path, "manifest.json"))

        if not path.exists(follow_symlinks=True):
            self.is_valid = False
            return {}

        return self.get_json(path)

    @instance_cache
    def get_attribution_json(self) -> dict[str, Any]:
        path = Path(os.path.join(self.path, "attribution.json"))

        return self.get_json(path)

    @instance_cache
    def get_pack_attribution(self) -> dict[str, Any]:
        return pack_wide_entry(self.get_attribution_json())

    @instance_cache
    def get_json(self, json_path: Path) -> dict[str, Any]:
        if not json_path.exists(follow_symlinks=True):
            return {}

        with open(json_path) as f:
            return cast("dict[str, Any]", json.load(f))

    @instance_cache
    def get_thumbnail_path(self) -> Path | None:
        manifest = self.get_manifest()
        thumbnail = manifest.get("thumbnail")
        if thumbnail is None:
            # A manifest with no thumbnail key is as unusable as one whose
            # thumbnail is missing from disk, and joining None raises.
            self.is_valid = False
            return None
        path = Path(os.path.join(self.path, thumbnail))
        if path.exists(follow_symlinks=True):
            return path
        self.is_valid = False
        return None

    def get_assets(self) -> list[AssetT]:
        assets: list[AssetT] = []

        for folder_assets in self.assets_by_folder.values():
            assets.extend(folder_assets)

        return assets

    def load_assets_by_folder(self, asset_path: str) -> None:
        manifest = self.get_manifest()

        if self.is_valid is False:
            return

        # asset_path names the manifest key, not a path.
        # A missing value invalidates the pack before joining it.
        asset_folder = manifest.get(asset_path)
        if asset_folder is None:
            self.is_valid = False
            return

        pack_path = Path(os.path.join(self.path, asset_folder))

        if not pack_path.exists(follow_symlinks=True):
            self.is_valid = False
            return

        base_assets = self.load_assets(pack_path)
        if base_assets:
            self.assets_by_folder["Base"] = base_assets

        subfolders = [entry for entry in os.scandir(pack_path) if entry.is_dir()]

        for folder in subfolders:
            self.assets_by_folder[folder.name] = self.load_assets(folder.path)

    def load_assets(self, folder_path: str | os.PathLike[str]) -> list[AssetT]:
        assets: list[AssetT] = []

        for entry in os.scandir(folder_path):
            if os.path.isdir(entry.path):
                continue
            assets.append(self.make_asset(entry.path))

        return assets


PackT = TypeVar("PackT", bound=Pack[Any])


class PackDiscovery(Generic[PackT]):
    """Discovery for one family. It holds no pack between calls."""

    DATA_DIR: ClassVar[str]
    LABEL: ClassVar[str]

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        check_family_contract(cls, PackDiscovery, ("DATA_DIR", "LABEL"), ("make_pack",))

    def __init__(self) -> None:
        self.packs: dict[str, PackT] = {}

    def make_pack(self, path: str) -> PackT:
        """Build one pack of the family's own pack class."""
        raise NotImplementedError

    def get_packs(self) -> dict[str, PackT]:
        packs: dict[str, PackT] = {}
        root = os.path.join(gl.DATA_PATH, self.DATA_DIR)
        os.makedirs(root, exist_ok=True)
        for name in os.listdir(root):
            if name.startswith("."):
                # Transient install-swap trees (StoreBackend._swap_into_place)
                # and other hidden entries are not packs.
                continue
            pack = self.make_pack(os.path.join(root, name))
            if pack.is_valid:
                packs[name] = pack
            else:
                log.warning(f"{self.LABEL} pack {name} is not valid.")
        return packs
