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

from typing import override

from src.backend.PackManagement.pack_family import Pack
from src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaper import SDPlusBarWallpaper

class SDPlusBarWallpaperPack(Pack[SDPlusBarWallpaper]):
    """One SD+ bar wallpaper pack folder. Its manifest names the asset folder under "images"."""

    ASSET_MANIFEST_KEY = "images"

    @override
    def make_asset(self, path: str) -> SDPlusBarWallpaper:
        return SDPlusBarWallpaper(wallpaper_pack=self, path=path)

    def get_wallpapers(self) -> list[SDPlusBarWallpaper]:
        return self.get_assets()
