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

# Import python modules

# Import own modules
from src.windows.AssetManager.GenericAssetChooser import (
    GenericPackChooserPage,
    GenericPackFlowBox,
    GenericPackPreview,
)

# Import globals
import globals as gl

# Import typing
from typing import TYPE_CHECKING, override
if TYPE_CHECKING:
    from src.backend.WallpaperPackManagement.WallpaperPack import WallpaperPack
    from src.windows.AssetManager.WallpaperPacks.Stack import WallpaperPackChooserStack
    from src.windows.AssetManager.WallpaperPacks.Wallpapers.WallpaperChooser import WallpaperChooserPage


class WallpaperPackChooser(GenericPackChooserPage["WallpaperPack", "WallpaperPackChooserStack"]):
    # The concrete stack, restated so the quoted name in the base subscript
    # has a checked in-file use.
    stack: "WallpaperPackChooserStack"
    PACK_FLOW_BOX_CLASS = GenericPackFlowBox
    PACK_PREVIEW_CLASS = GenericPackPreview
    LEAF_CHILD_NAME = "wallpaper-chooser"

    @override
    def get_packs(self) -> "dict[str, WallpaperPack]":
        return gl.wallpaper_pack_manager.get_wallpaper_packs()

    @override
    def get_leaf_chooser(self) -> "WallpaperChooserPage":
        return self.stack.leaf_chooser
