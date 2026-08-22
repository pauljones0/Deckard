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

One manager, one pack and one asset, shared by every family of installable art.

Icons, wallpapers and SD+ bar wallpapers all ship as packs. A pack is a folder
that holds a manifest.json, a thumbnail the manifest names, a folder of asset
files the manifest also names, and an optional attribution.json. A manager
scans one folder under the data path and returns the packs it can describe,
keyed by folder name.

The three families read that layout the same way and disagree in four places,
each one a class attribute or an override that a family module supplies:

- the folder under the data path that the manager scans (DATA_DIR)
- the manifest key that names the asset folder (ASSET_MANIFEST_KEY)
- the key an asset reads out of attribution.json (ATTRIBUTION_KEY)
- the word a rejection warning prints (LABEL)

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
from typing import Any, ClassVar, Generic, TypeVar, cast

from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.HelperMethods import instance_cache


class AttributionKey(Enum):
    """How an asset names itself in its pack's attribution.json.

    The one place the families disagree on meaning rather than on spelling.
    BASENAME reads the file name alone, so two files of that name in different
    subfolders share one entry. RELPATH reads the path of the file relative to
    the pack root, so the asset folder and any subfolder stay in the key.
    """

    BASENAME = "basename"
    RELPATH = "relpath"


def pack_wide_entry(attribution: dict[str, Any]) -> dict[str, Any]:
    """The entry that covers a whole pack: default, then general, then generic.

    Three spellings of one idea, from three generations of pack author. A pack
    with none of them attributes nothing.
    """
    return cast("dict[str, Any]",
                attribution.get("default", attribution.get("general", attribution.get("generic", {}))))


class PackAsset:
    """One asset file inside a pack."""

    # The family supplies this. See AttributionKey.
    ATTRIBUTION_KEY: ClassVar[AttributionKey]

    def __init__(self, pack: Pack[Any], path: str):
        self.pack = pack
        self.path = path

        self.name = os.path.splitext(os.path.basename(path))[0]

    def get_attribution_key(self) -> str:
        """The key this asset carries in attribution.json."""
        if self.ATTRIBUTION_KEY is AttributionKey.BASENAME:
            return os.path.basename(self.path)
        return os.path.relpath(self.path, self.pack.path)

    def get_attribution(self) -> dict[str, Any]:
        attribution = self.pack.get_attribution_json()

        key = self.get_attribution_key()
        if key in attribution:
            return cast("dict[str, Any]", attribution[key])
        return pack_wide_entry(attribution)


AssetT = TypeVar("AssetT", bound=PackAsset)


class Pack(Generic[AssetT]):
    """One pack folder, read once at construction.

    is_valid starts true and falls the moment a read finds the pack
    undescribed. A manager drops an invalid pack; see the module docstring for
    the one check that runs later than the constructor.
    """

    # The family supplies this: the manifest key that names the asset folder.
    ASSET_MANIFEST_KEY: ClassVar[str]

    def __init__(self, path: str):
        self.path = path
        self.is_valid = True
        self.name = self.get_manifest().get("name") or os.path.basename(path)
        self.pack_structure: dict[str, list[AssetT]] = {}

        self.generate_folder_structure(self.ASSET_MANIFEST_KEY)

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

    def get_content_from_structure(self) -> list[AssetT]:
        content: list[AssetT] = []

        for folder_content in self.pack_structure.values():
            content.extend(folder_content)

        return content

    def generate_folder_structure(self, asset_path: str) -> None:
        manifest = self.get_manifest()

        if self.is_valid is False:
            return

        # Not asset_path again: the parameter names the manifest key, and
        # what comes back is the folder it points at. A manifest that omits
        # the key leaves the pack unusable, and joining None raises.
        asset_folder = manifest.get(asset_path)
        if asset_folder is None:
            self.is_valid = False
            return

        pack_path = Path(os.path.join(self.path, asset_folder))

        if not pack_path.exists(follow_symlinks=True):
            self.is_valid = False
            return

        base_dir_content = self.load_content(pack_path)
        if base_dir_content:
            self.pack_structure["Base"] = base_dir_content

        subfolders = [entry for entry in os.scandir(pack_path) if entry.is_dir()]

        for folder in subfolders:
            self.pack_structure[folder.name] = self.load_content(folder.path)

    def load_content(self, folder_path: str | os.PathLike[str]) -> list[AssetT]:
        content: list[AssetT] = []

        for entry in os.scandir(folder_path):
            if os.path.isdir(entry.path):
                continue
            content.append(self.make_asset(entry.path))

        return content


PackT = TypeVar("PackT", bound=Pack[Any])


class PackManager(Generic[PackT]):
    """Discovery for one family. It holds no pack between calls."""

    # The family supplies both of these.
    DATA_DIR: ClassVar[str]
    LABEL: ClassVar[str]

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
