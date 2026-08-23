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
from src.windows.AssetManager.SDPlusBarWallpaperPacks.PackChooser import SDPlusBarWallpaperPackChooser
from src.windows.AssetManager.SDPlusBarWallpaperPacks.SDPlusBarWallpaper.SDPlusBarWallpaperChooser import SDPlusBarWallpaperChooserPage


class SDPlusBarWallpaperPackChooserStack(GenericPackChooserStack[SDPlusBarWallpaperChooserPage]):
    # This stack takes no show_for_path, for the reason WallpaperPacks.Stack
    # gives: a pre-selection reaches the icon-pack stack only.
    PACK_CHOOSER_CLASS = SDPlusBarWallpaperPackChooser
    LEAF_CHOOSER_CLASS = SDPlusBarWallpaperChooserPage
    LEAF_CHILD_TITLE = "SD+ Bar Wallpaper Chooser"
