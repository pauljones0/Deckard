"""Verify discovery and attribution for icon, wallpaper, and SD+ bar packs.
Invalid manifests are dropped; fallback order and family-specific leaf keys are preserved."""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import json
import os
import shutil
import types

import globals as gl
from fixtures import start_watchdog
from src.backend.IconPackManagement.IconPackManager import IconPackManager
from src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaperPackManager import (
    SDPlusBarWallpaperPackManager,
)
from src.backend.WallpaperPackManagement.WallpaperPackManager import WallpaperPackManager

# The folder that holds the assets of a built pack. Its name is free; the
# manifest key that points at it is what the three families disagree on.
ASSET_FOLDER = "assets"

FAMILIES = (
    types.SimpleNamespace(
        label="icon",
        data_dir="icons",
        manifest_key="icons",
        discover=IconPackManager().get_icon_packs,
        assets=lambda pack: pack.get_icons(),
        # Icon.get_attribution keys on the basename, so a subfolder drops out.
        leaf_key=lambda asset_rel_path: os.path.basename(asset_rel_path),
        leaf_key_label="basename",
    ),
    types.SimpleNamespace(
        label="wallpaper",
        data_dir="wallpapers",
        manifest_key="images",
        discover=WallpaperPackManager().get_wallpaper_packs,
        assets=lambda pack: pack.get_wallpapers(),
        # Wallpaper.get_attribution keys on the path relative to the pack root,
        # so the asset folder and any subfolder stay in the key.
        leaf_key=lambda asset_rel_path: os.path.join(ASSET_FOLDER, asset_rel_path),
        leaf_key_label="relpath",
    ),
    types.SimpleNamespace(
        label="sd+bar wallpaper",
        data_dir="sd_plus_bar_wallpapers",
        manifest_key="images",
        discover=SDPlusBarWallpaperPackManager().get_wallpaper_packs,
        assets=lambda pack: pack.get_wallpapers(),
        leaf_key=lambda asset_rel_path: os.path.join(ASSET_FOLDER, asset_rel_path),
        leaf_key_label="relpath",
    ),
)


def family_root(family) -> str:
    return os.path.join(gl.DATA_PATH, family.data_dir)


def write_json(path: str, content: dict) -> None:
    with open(path, "w") as f:
        json.dump(content, f)


def build_pack(family, pack_name: str, assets=(), attribution=None, thumbnail=True) -> str:
    """Write a complete pack with marker assets and an optional marker thumbnail.
    Asset names are relative; None omits attribution and false thumbnail omits the preview."""
    pack_path = os.path.join(family_root(family), pack_name)
    asset_root = os.path.join(pack_path, ASSET_FOLDER)
    os.makedirs(asset_root, exist_ok=True)

    for rel_path in assets:
        full_path = os.path.join(asset_root, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(b"asset")

    manifest = {"name": f"{pack_name} display name", family.manifest_key: ASSET_FOLDER}
    if thumbnail:
        with open(os.path.join(pack_path, "thumb.png"), "wb") as f:
            f.write(b"thumb")
        manifest["thumbnail"] = "thumb.png"
    write_json(os.path.join(pack_path, "manifest.json"), manifest)

    if attribution is not None:
        write_json(os.path.join(pack_path, "attribution.json"), attribution)

    return pack_path


def find_asset(family, pack, asset_rel_path: str):
    """Return the leaf object for asset_rel_path, or None."""
    wanted = os.path.join(pack.path, ASSET_FOLDER, asset_rel_path)
    for asset in family.assets(pack):
        if os.path.abspath(asset.path) == os.path.abspath(wanted):
            return asset
    return None


DEFAULT_ENTRY = {"copyright": "by-default"}
GENERAL_ENTRY = {"copyright": "by-general"}
GENERIC_ENTRY = {"copyright": "by-generic"}

# Each case gets its own pack because attribution data is cached per instance.
# Tuple fields: pack name, attribution content or None, expected result.
FALLBACK_CASES = (
    ("all-three", {"default": DEFAULT_ENTRY, "general": GENERAL_ENTRY,
                   "generic": GENERIC_ENTRY}, DEFAULT_ENTRY),
    ("no-default", {"general": GENERAL_ENTRY, "generic": GENERIC_ENTRY}, GENERAL_ENTRY),
    ("generic-only", {"generic": GENERIC_ENTRY}, GENERIC_ENTRY),
    ("per-asset-only", {"logo.png": DEFAULT_ENTRY}, {}),
    ("no-file", None, {}),
)


def check_empty_family_dir() -> int:
    """Verify discovery creates an absent family folder and returns no packs."""
    rc = 0
    for family in FAMILIES:
        root = family_root(family)
        if os.path.exists(root):
            print(f"FAIL(1): {family.label} folder already exists -- "
                  f"check_empty_family_dir must run before the packs are built")
            rc = 1
            continue
        packs = family.discover()
        if packs != {}:
            print(f"FAIL(1): {family.label} discovery on an empty data dir "
                  f"returned {sorted(packs)}")
            rc = 1
            continue
        if not os.path.isdir(root):
            print(f"FAIL(1): {family.label} discovery did not create {root}")
            rc = 1
    if rc == 0:
        print("PASS: discovery on an empty data dir returns no packs and creates the folder")
    return rc


def check_discovery() -> int:
    """Discovery keys valid packs by folder name and drops the rest."""
    rc = 0
    rejected = (".install-swap", "no-manifest", "no-asset-key", "missing-folder")
    deeper_asset = os.path.join("sub", "deeper", "deep.png")
    for family in FAMILIES:
        # One accumulator per family. A shared one lets the first failure hide
        # the same defect in the families behind it.
        family_rc = 0
        root = family_root(family)
        try:
            build_pack(family, "alpha", assets=["logo.png"])
            build_pack(family, "beta", assets=[os.path.join("sub", "wide.png"), deeper_asset])
            # The thumbnail check runs when a preview asks for the path, and the
            # constructor never asks, so a pack with no thumbnail is discovered.
            build_pack(family, "no-thumbnail", assets=["logo.png"], thumbnail=False)
            # Hidden entries are transient install-swap trees, not packs.
            build_pack(family, ".install-swap", assets=["logo.png"])

            os.makedirs(os.path.join(root, "no-manifest"), exist_ok=True)

            path = os.path.join(root, "no-asset-key")
            os.makedirs(path, exist_ok=True)
            write_json(os.path.join(path, "manifest.json"), {"name": "no-asset-key"})

            path = os.path.join(root, "missing-folder")
            os.makedirs(path, exist_ok=True)
            write_json(os.path.join(path, "manifest.json"),
                       {"name": "missing-folder", family.manifest_key: ASSET_FOLDER})

            packs = family.discover()

            for name in ("alpha", "beta", "no-thumbnail"):
                if name not in packs:
                    print(f"FAIL(2): {family.label} discovery dropped the valid pack {name}")
                    family_rc = 1
            for name in rejected:
                if name in packs:
                    print(f"FAIL(2): {family.label} discovery kept {name}")
                    family_rc = 1

            if family_rc == 0:
                if packs["alpha"].name != "alpha display name":
                    print(f"FAIL(2): {family.label} pack name came from the folder, not "
                          f"the manifest: {packs['alpha'].name!r}")
                    family_rc = 1

                alpha_found = sorted(os.path.basename(a.path) for a in family.assets(packs["alpha"]))
                if alpha_found != ["logo.png"]:
                    print(f"FAIL(2): {family.label} pack alpha listed {alpha_found}")
                    family_rc = 1

                # The scan descends one folder; the folder reader skips nested
                # directories, so files two levels down stay out of the list.
                beta_found = sorted(os.path.basename(a.path) for a in family.assets(packs["beta"]))
                if beta_found != ["wide.png"]:
                    print(f"FAIL(2): {family.label} pack beta listed {beta_found}, expected "
                          f"the file one level down and nothing from below it")
                    family_rc = 1

                # Asking for the path is what turns the pack invalid, and the
                # pack was already discovered as valid above.
                no_thumbnail = packs["no-thumbnail"]
                if no_thumbnail.get_thumbnail_path() is not None:
                    print(f"FAIL(2): {family.label} thumbnail-less pack returned a "
                          f"thumbnail path")
                    family_rc = 1
                if no_thumbnail.is_valid:
                    print(f"FAIL(2): {family.label} thumbnail-less pack stayed valid after "
                          f"its thumbnail path was asked for")
                    family_rc = 1
        finally:
            # Remove rejected folders after failures to prevent repeated warnings
            # from obscuring later failure output.
            for name in rejected:
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
        rc |= family_rc
    if rc == 0:
        print("PASS: discovery keys valid packs by folder name, drops hidden and "
              "undescribed ones, reads one level down, and keeps a thumbnail-less pack")
    return rc


def check_pack_attribution_fallback() -> int:
    """A pack default falls back default, then general, then generic."""
    rc = 0
    for family in FAMILIES:
        for case_name, attribution, expected in FALLBACK_CASES:
            pack_name = f"pack-attr-{case_name}"
            build_pack(family, pack_name, assets=["logo.png"], attribution=attribution)
            pack = family.discover()[pack_name]
            got = pack.get_pack_attribution()
            if got != expected:
                print(f"FAIL(3): {family.label} pack attribution for {case_name} "
                      f"returned {got}, expected {expected}")
                rc = 1
    if rc == 0:
        print("PASS: pack attribution falls back default, general, generic")
    return rc


def check_leaf_attribution_fallback() -> int:
    """A leaf with no entry of its own falls back on the same order."""
    rc = 0
    for family in FAMILIES:
        for case_name, attribution, expected in FALLBACK_CASES:
            pack_name = f"leaf-attr-{case_name}"
            build_pack(family, pack_name, assets=["unlisted.png"], attribution=attribution)
            pack = family.discover()[pack_name]
            asset = find_asset(family, pack, "unlisted.png")
            if asset is None:
                print(f"FAIL(4): {family.label} pack {pack_name} listed no asset")
                rc = 1
                continue
            got = asset.get_attribution()
            if got != expected:
                print(f"FAIL(4): {family.label} leaf attribution for {case_name} "
                      f"returned {got}, expected {expected}")
                rc = 1
    if rc == 0:
        print("PASS: leaf attribution falls back default, general, generic")
    return rc


def check_leaf_attribution_key() -> int:
    """Verify icons use basenames and wallpapers use pack-relative paths.
    One attribution file carries both keys and a default to reveal the selected key."""
    rc = 0
    asset_rel_path = os.path.join("sub", "logo.png")
    basename_key = os.path.basename(asset_rel_path)
    relpath_key = os.path.join(ASSET_FOLDER, asset_rel_path)

    for family in FAMILIES:
        pack_name = "leaf-key"
        build_pack(family, pack_name, assets=[asset_rel_path], attribution={
            basename_key: {"copyright": "by-basename"},
            relpath_key: {"copyright": "by-relpath"},
            "default": DEFAULT_ENTRY,
        })
        pack = family.discover()[pack_name]
        asset = find_asset(family, pack, asset_rel_path)
        if asset is None:
            print(f"FAIL(5): {family.label} pack {pack_name} listed no asset")
            rc = 1
            continue

        expected = {"copyright": f"by-{family.leaf_key_label}"}
        got = asset.get_attribution()
        if got != expected:
            print(f"FAIL(5): {family.label} leaf read the key "
                  f"{family.leaf_key(asset_rel_path)!r} as {got}, expected {expected}")
            rc = 1
    if rc == 0:
        print("PASS: an icon keys attribution on its basename, a wallpaper on its relpath")
    return rc


def main() -> int:
    start_watchdog(30, "pack_manager_contract")
    rc = 0
    for check in (
        check_empty_family_dir,
        check_discovery,
        check_pack_attribution_fallback,
        check_leaf_attribution_fallback,
        check_leaf_attribution_key,
    ):
        try:
            rc |= check()
        except Exception as e:  # a check itself raising is a failure too
            print(f"FAIL({check.__name__}): raised {type(e).__name__}: {e}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
