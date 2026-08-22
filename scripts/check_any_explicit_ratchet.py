#!/usr/bin/env python3
"""disallow_any_explicit exemption ratchet. It stops the list from growing.

Run it the same way CI does, from anywhere:

    python scripts/check_any_explicit_ratchet.py

Exit 0 means the exemption list in pyproject.toml is a subset of the baseline
below. Exit 1 prints what changed and names the fix.

mypy's disallow_any_explicit is on for the whole tree. An explicit Any is
visible dynamism: honest at a boundary (JSON payloads, plugin callbacks, gi
values, *args forwarding), lazy everywhere else. The modules that still carry
one are exempted one by one in pyproject.toml. Giving the lazy ones real types
is the work. The boundary ones stay until the boundary spelling itself
changes. So this list shrinks, but unlike the retired signature ratchets it
does not reach zero.

The list names modules. It names no packages. A package pattern would
un-cover every module inside it that is already clean.

An exemption list with nothing watching it grows. A module that starts failing
gets added, the addition reads as a one-line diff, and the ratchet quietly runs
backwards. BASELINE pins the list as it stood when this guard landed. An entry
may leave it and never join.

A guard that fails open reads as green and covers nothing, so this check also
fails when its own footing moves: a missing pyproject.toml, a missing or
malformed override block, a disabled flag, an entry in the file that BASELINE
does not describe, the option written under mypy's inverse alias
(allow_any_explicit) or as a non-boolean value mypy coerces, and an
"explicit-any" entry in any disable_error_code list. Each is a loud failure
that names the fix, never a silent skip.

To clean a module: give every explicit Any in it a real type, delete its entry
from pyproject.toml, and delete the same entry here.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

# The exemption list as it stood when this guard landed. Entries leave, never join.
BASELINE = {
    "GtkHelper.ComboRow",
    "GtkHelper.ConfirmationDialog",
    "GtkHelper.DynamicFlowBox",
    "GtkHelper.GenerativeUI.ComboRow",
    "GtkHelper.GenerativeUI.EntryRow",
    "GtkHelper.GenerativeUI.ExpanderRow",
    "GtkHelper.GenerativeUI.FileDialogRow",
    "GtkHelper.GenerativeUI.GenerativeUI",
    "GtkHelper.GenerativeUI.PasswordEntryRow",
    "GtkHelper.GenerativeUI.ScaleRow",
    "GtkHelper.GenerativeUI.SpinRow",
    "GtkHelper.GenerativeUI.SwitchRow",
    "GtkHelper.GenerativeUI.ToggleRow",
    "GtkHelper.GtkHelper",
    "GtkHelper.ItemListComboRow",
    "GtkHelper.NetworkRows",
    "GtkHelper.ScaleRow",
    "GtkHelper.SearchComboRow",
    "autostart",
    "globals",
    "locales.LegacyLocaleManager",
    "src.Signals.SignalManager",
    "src.Signals.weak_callbacks",
    "src.api",
    "src.app",
    "src.backend.AssetManagerBackend",
    "src.backend.DeckManagement.DeckManager",
    "src.backend.DeckManagement.HelperMethods",
    "src.backend.DeckManagement.InputIdentifier",
    "src.backend.DeckManagement.Media.MediaConfig",
    "src.backend.DeckManagement.Subclasses.ActionPermissionManager",
    "src.backend.DeckManagement.Subclasses.FakeDeck",
    "src.backend.DeckManagement.Subclasses.KeyImage",
    "src.backend.DeckManagement.Subclasses.KeyLabel",
    "src.backend.DeckManagement.Subclasses.KeyVideo",
    "src.backend.DeckManagement.Subclasses.RemoteDeck",
    "src.backend.DeckManagement.Subclasses.RemoteDeckManager",
    "src.backend.DeckManagement.Subclasses.RemoteDecksLocalServerHandler",
    "src.backend.DeckManagement.Subclasses.ScreenSaver",
    "src.backend.DeckManagement.Subclasses.SingleKeyAsset",
    "src.backend.DeckManagement.Subclasses.encoded_image_cache",
    "src.backend.DeckManagement.Subclasses.video_cache_sweeper",
    "src.backend.DeckManagement.deck_controller.controller",
    "src.backend.DeckManagement.deck_controller.inputs",
    "src.backend.DeckManagement.deck_controller.label_engine",
    "src.backend.DeckManagement.deck_controller.media_writer",
    "src.backend.DeckManagement.font_resolver",
    "src.backend.IconPackManagement.Icon",
    "src.backend.IconPackManagement.IconPack",
    "src.backend.LockScreenManager.LockScreenDetector",
    "src.backend.Logger",
    "src.backend.Migration.Migrator",
    "src.backend.PageManagement.Page",
    "src.backend.PageManagement.PageManagerBackend",
    "src.backend.PageManagement.page_document",
    "src.backend.PageManagement.page_flush",
    "src.backend.PermissionManagement.FlatpakPermissionManager",
    "src.backend.PluginManager.ActionBase",
    "src.backend.PluginManager.ActionCore",
    "src.backend.PluginManager.ActionHolder",
    "src.backend.PluginManager.EventAssigner",
    "src.backend.PluginManager.EventHolder",
    "src.backend.PluginManager.InputBases",
    "src.backend.PluginManager.PluginBase",
    "src.backend.PluginManager.PluginManager",
    "src.backend.PluginManager.PluginSettings.Asset",
    "src.backend.PluginManager.PluginSettings.Manager",
    "src.backend.PluginManager.PluginSettings.Observer",
    "src.backend.PluginManager.PluginSettings.PluginAssetManager",
    "src.backend.PluginManager.backend_guard.deckard_rpyc_guard",
    "src.backend.PluginManager.event_dispatch",
    "src.backend.PresenceMonitor.PresenceMonitor",
    "src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaper",
    "src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaperPack",
    "src.backend.SettingsManager",
    "src.backend.Store.StoreBackend",
    "src.backend.Store.StoreCache",
    "src.backend.WallpaperPackManagement.Wallpaper",
    "src.backend.WallpaperPackManagement.WallpaperPack",
    "src.backend.WindowGrabber.Integrations.Sway",
    "src.backend.atomic_json",
    "src.backend.cli_forward",
    "src.backend.log_hooks",
    "src.backend.main_loop",
    "src.backend.services",
    "src.backend.settings_store",
    "src.backend.settings_views",
    "src.backend.startup_queue",
    "src.backend.trayicon",
    "src.tray",
    "src.windows.AssetManager.AssetManager",
    "src.windows.AssetManager.ChooserPage",
    "src.windows.AssetManager.CustomAssets.AssetPreview",
    "src.windows.AssetManager.CustomAssets.Chooser",
    "src.windows.AssetManager.CustomAssets.FlowBox",
    "src.windows.AssetManager.DynamicFlowBox",
    "src.windows.AssetManager.GenericAssetChooser",
    "src.windows.AssetManager.IconPacks.FlowBox",
    "src.windows.AssetManager.IconPacks.Icons.IconFlowBox",
    "src.windows.AssetManager.IconPacks.Stack",
    "src.windows.AssetManager.InfoPage",
    "src.windows.AssetManager.SDPlusBarWallpaperPacks.FlowBox",
    "src.windows.AssetManager.SDPlusBarWallpaperPacks.SDPlusBarWallpaper.SDPlusBarWallpaperFlowBox",
    "src.windows.AssetManager.SDPlusBarWallpaperPacks.Stack",
    "src.windows.AssetManager.WallpaperPacks.FlowBox",
    "src.windows.AssetManager.WallpaperPacks.Stack",
    "src.windows.AssetManager.WallpaperPacks.Wallpapers.WallpaperFlowBox",
    "src.windows.MultiDeckSelector.MultiDeckSelector",
    "src.windows.MultiDeckSelector.MultiDeckSelectorRow",
    "src.windows.Onboarding.PluginRecommendations",
    "src.windows.PageManager.Importer.Importer",
    "src.windows.PageManager.Importer.StreamController.StreamController",
    "src.windows.PageManager.Importer.StreamDeckUI.StreamDeckUI",
    "src.windows.PageManager.PageManager",
    "src.windows.PageManager.elements.PageEditor",
    "src.windows.PageManager.elements.PageSelector",
    "src.windows.Settings.PluginSettingsPage",
    "src.windows.Settings.PluginSettingsWindow.PluginAssetPreview",
    "src.windows.Settings.PluginSettingsWindow.PluginSettingsWindow",
    "src.windows.Settings.Settings",
    "src.windows.Shortcuts.Shortcuts",
    "src.windows.Store.Badges",
    "src.windows.Store.InfoPage",
    "src.windows.Store.Store",
    "src.windows.Store.StorePage",
    "src.windows.Store.StorePageSection",
    "src.windows.mainWindow.DeckPlus.DialBox",
    "src.windows.mainWindow.DeckPlus.ScreenBar",
    "src.windows.mainWindow.deckSwitcher",
    "src.windows.mainWindow.elements.DeckConfig",
    "src.windows.mainWindow.elements.DeckSettings.BackgroundGroup",
    "src.windows.mainWindow.elements.DeckSettings.DeckSettingsPage",
    "src.windows.mainWindow.elements.DeckSettings.FakeDeckGroup",
    "src.windows.mainWindow.elements.DeckSettingsButton",
    "src.windows.mainWindow.elements.DeckStack",
    "src.windows.mainWindow.elements.DeckStackChild",
    "src.windows.mainWindow.elements.HeaderHamburgerMenuButton",
    "src.windows.mainWindow.elements.KeepRunningDialog",
    "src.windows.mainWindow.elements.KeyGrid",
    "src.windows.mainWindow.elements.NoPagesError",
    "src.windows.mainWindow.elements.PageSelector",
    "src.windows.mainWindow.elements.PageSettingsPage",
    "src.windows.mainWindow.elements.Sidebar.Sidebar",
    "src.windows.mainWindow.elements.Sidebar.elements.ActionChooser",
    "src.windows.mainWindow.elements.Sidebar.elements.ActionConfigurator",
    "src.windows.mainWindow.elements.Sidebar.elements.ActionManager",
    "src.windows.mainWindow.elements.Sidebar.elements.BackgroundEditor",
    "src.windows.mainWindow.elements.Sidebar.elements.IconSelector",
    "src.windows.mainWindow.elements.Sidebar.elements.ImageEditor",
    "src.windows.mainWindow.elements.Sidebar.elements.LabelEditor",
    "src.windows.mainWindow.elements.Sidebar.elements.StateSwitcher",
    "src.windows.mainWindow.elements.leftArea",
    "src.windows.mainWindow.mainWindow",
    "src.windows.ui_adapter",
}

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def fail(*lines: str) -> None:
    for line in lines:
        print(line, file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    if not PYPROJECT.is_file():
        fail(f"disallow_any_explicit ratchet: {PYPROJECT} is missing.",
             "This guard reads the exemption list from it and cannot verify anything without it.")

    with PYPROJECT.open("rb") as f:
        data = tomllib.load(f)

    mypy_config = data.get("tool", {}).get("mypy", {})
    if "allow_any_explicit" in mypy_config:
        fail("disallow_any_explicit ratchet: [tool.mypy] uses mypy's inverse alias allow_any_explicit.",
             "The guard pins the canonical spelling only. Write disallow_any_explicit = true.")
    if mypy_config.get("disallow_any_explicit") is not True:
        fail("disallow_any_explicit ratchet: [tool.mypy] does not set disallow_any_explicit = true",
             "(the literal boolean; mypy coerces other spellings, this guard rejects them).",
             "The exemption list only means anything while the flag is on for everything else.",
             "Turning the flag off makes mypy report fewer errors, so lint:types stays green",
             "and the whole ratchet reverts unnoticed. That is what this check is for.")

    overrides = mypy_config.get("overrides", [])
    # Select every block that touches the option at all, under either of the
    # spellings mypy honours. Filtering on `is False` alone would skip a block
    # written as allow_any_explicit = true or disallow_any_explicit = 0, both
    # of which mypy applies while the pinned list stays untouched.
    blocks = [o for o in overrides
              if "disallow_any_explicit" in o or "allow_any_explicit" in o]
    if len(blocks) != 1:
        fail("disallow_any_explicit ratchet: expected exactly one mypy override block that touches",
             f"disallow_any_explicit, found {len(blocks)}.",
             "Keep the exemptions in one block, so this guard and a reader see the same list.")

    if "allow_any_explicit" in blocks[0]:
        fail("disallow_any_explicit ratchet: the override block uses mypy's inverse alias",
             "allow_any_explicit. Write disallow_any_explicit = false.")
    if blocks[0].get("disallow_any_explicit") is not False:
        fail("disallow_any_explicit ratchet: the override block must set disallow_any_explicit = false",
             "(the literal boolean; mypy coerces other spellings, this guard rejects them).")

    for scope, cfg in [("[tool.mypy]", mypy_config)] + [
            (f"override block {i}", o) for i, o in enumerate(overrides)]:
        disabled = cfg.get("disable_error_code", [])
        if isinstance(disabled, str):
            disabled = [disabled]
        if "explicit-any" in disabled:
            fail(f"disallow_any_explicit ratchet: {scope} lists explicit-any in disable_error_code.",
                 "That silences the flag everywhere the scope reaches while the exemption list",
                 "stays green. Remove it; exemptions go through the override list only.")

    modules = blocks[0].get("module")
    if isinstance(modules, str):
        modules = [modules]
    if not isinstance(modules, list) or not all(isinstance(m, str) for m in modules):
        fail("disallow_any_explicit ratchet: the override block's `module` is not a list of strings.")

    current = set(modules)
    if len(current) != len(modules):
        dupes = sorted({m for m in modules if modules.count(m) > 1})
        fail("disallow_any_explicit ratchet: the exemption list repeats an entry: " + ", ".join(dupes))

    added = current - BASELINE
    if added:
        fail("disallow_any_explicit ratchet: the exemption list grew.",
             "These entries are in pyproject.toml but not in this guard's BASELINE:",
             *(f"    {m}" for m in sorted(added)),
             "",
             "The list only shrinks. Type the module rather than adding it here.",
             "If an exemption is genuinely required, that is a deliberate decision: add it",
             "to BASELINE in the same commit and say why.")

    cleaned = BASELINE - current
    if cleaned:
        fail("disallow_any_explicit ratchet: these entries left pyproject.toml but are still in",
             "this guard's BASELINE:",
             *(f"    {m}" for m in sorted(cleaned)),
             "",
             "Delete them from BASELINE in the same commit, so the guard tracks the real list.")

    print(f"disallow_any_explicit ratchet: {len(current)} module(s) still exempt.")


if __name__ == "__main__":
    main()
