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
    from src.windows.AssetManager.SDPlusBarWallpaperPacks.Stack import SDPlusBarWallpaperPackChooserStack
    from src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaper import SDPlusBarWallpaper
    from src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaperPack import SDPlusBarWallpaperPack


class SDPlusBarWallpaperChooserPage(
        GenericAssetChooserPage["SDPlusBarWallpaperPack", "SDPlusBarWallpaper",
                                "GenericAssetPreview[SDPlusBarWallpaper]",
                                "SDPlusBarWallpaperPackChooserStack"]):
    # The concrete stack, restated so the quoted name in the base subscript
    # has a checked in-file use.
    stack: "SDPlusBarWallpaperPackChooserStack"
    FLOW_BOX_CLASS = GenericAssetFlowBox
    PREVIEW_CLASS = GenericAssetPreview

    def get_assets(self, pack: "SDPlusBarWallpaperPack") -> "list[SDPlusBarWallpaper]":
        return pack.get_wallpapers()

    def bind_preview(self, preview: "GenericAssetPreview[SDPlusBarWallpaper]",
                     asset: "SDPlusBarWallpaper") -> None:
        preview.set_asset(asset)

    def get_child_asset(self,
                        child: "GenericAssetPreview[SDPlusBarWallpaper]") -> "SDPlusBarWallpaper":
        return child.asset
