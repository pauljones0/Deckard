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
    # This stack takes no show_for_path. AssetChooser.show_for_path is the one
    # entry point for a pre-selection, and it routes to the custom-asset
    # chooser or to the icon-pack stack. A wallpaper pre-selection needs that
    # routing first, and the tab switch that goes with it.
    PACK_CHOOSER_CLASS = WallpaperPackChooser
    LEAF_CHOOSER_CLASS = WallpaperChooserPage
    LEAF_CHILD_TITLE = "Wallpaper Chooser"
