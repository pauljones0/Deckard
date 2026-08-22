"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from src.backend.PackManagement.pack_family import PackManager
from src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaperPack import SDPlusBarWallpaperPack

class SDPlusBarWallpaperPackManager(PackManager[SDPlusBarWallpaperPack]):
    """Discovery for the SD+ bar wallpaper packs under the data path."""

    DATA_DIR = "sd_plus_bar_wallpapers"
    LABEL = "SD+ Bar Wallpaper"

    def make_pack(self, path: str) -> SDPlusBarWallpaperPack:
        return SDPlusBarWallpaperPack(path)

    def get_wallpaper_packs(self) -> dict[str, SDPlusBarWallpaperPack]:
        return self.get_packs()
