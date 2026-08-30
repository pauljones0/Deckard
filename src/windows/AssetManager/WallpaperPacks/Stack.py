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
from src.windows.AssetManager.GenericAssetChooser import GenericPackChooserStack
from src.windows.AssetManager.WallpaperPacks.PackChooser import WallpaperPackChooser
from src.windows.AssetManager.WallpaperPacks.Wallpapers.WallpaperChooser import WallpaperChooserPage


class WallpaperPackChooserStack(GenericPackChooserStack[WallpaperChooserPage]):
    # Pre-selection routes only to custom assets or icon packs, not this stack
    PACK_CHOOSER_CLASS = WallpaperPackChooser
    LEAF_CHOOSER_CLASS = WallpaperChooserPage
    LEAF_CHILD_TITLE = "Wallpaper Chooser"
