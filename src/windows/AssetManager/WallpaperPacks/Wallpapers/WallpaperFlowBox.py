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
from src.windows.AssetManager.DynamicFlowBox import DynamicFlowBox
from src.backend.WallpaperPackManagement.Wallpaper import Wallpaper
from src.windows.AssetManager.WallpaperPacks.Wallpapers.WallpaperPreview import WallpaperPreview

# Import typing
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.AssetManager.WallpaperPacks.Wallpapers.WallpaperChooser import WallpaperChooserPage

class WallpaperFlowBox(DynamicFlowBox[WallpaperPreview, Wallpaper]):
    def __init__(self, base_class: type, wallpaper_chooser: "WallpaperChooserPage", *args: Any, **kwargs: Any) -> None:
        super().__init__(base_class, *args, **kwargs)
        self.CHILDREN_PER_PAGE = 150
        self.set_hexpand(True)

        self.callback_func = None
        self.callback_args: tuple[Any, ...] = ()
        self.callback_kwargs: dict[str, Any] = {}

        self.wallpaper_chooser = wallpaper_chooser