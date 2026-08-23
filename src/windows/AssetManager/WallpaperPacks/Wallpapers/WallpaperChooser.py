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
# Import gtk modules
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

# Import own modules
from src.windows.AssetManager.GenericAssetChooser import (
    GenericAssetChooserPage,
    GenericAssetFlowBox,
    GenericAssetPreview,
)

# Import python modules

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.AssetManager.WallpaperPacks.Stack import WallpaperPackChooserStack
    from src.backend.WallpaperPackManagement.Wallpaper import Wallpaper
    from src.backend.WallpaperPackManagement.WallpaperPack import WallpaperPack


class WallpaperChooserPage(GenericAssetChooserPage["WallpaperPack", "Wallpaper",
                                                   "GenericAssetPreview[Wallpaper]",
                                                   "WallpaperPackChooserStack"]):
    # The concrete stack, restated so the quoted name in the base subscript
    # has a checked in-file use.
    stack: "WallpaperPackChooserStack"
    FLOW_BOX_CLASS = GenericAssetFlowBox
    PREVIEW_CLASS = GenericAssetPreview

    def get_assets(self, pack: "WallpaperPack") -> "list[Wallpaper]":
        return pack.get_wallpapers()

    def bind_preview(self, preview: "GenericAssetPreview[Wallpaper]",
                     wallpaper: "Wallpaper") -> None:
        preview.set_asset(wallpaper)

    def get_child_asset(self, child: "GenericAssetPreview[Wallpaper]") -> "Wallpaper":
        return child.asset
