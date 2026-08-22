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

from src.backend.PackManagement.pack_family import AttributionKey, PackAsset

from typing import TYPE_CHECKING, cast
if TYPE_CHECKING:
    from src.backend.WallpaperPackManagement.WallpaperPack import WallpaperPack

class Wallpaper(PackAsset):
    """One wallpaper file inside a wallpaper pack."""

    ATTRIBUTION_KEY = AttributionKey.RELPATH

    def __init__(self, wallpaper_pack: "WallpaperPack", path: str):
        super().__init__(pack=wallpaper_pack, path=path)

    @property
    def wallpaper_pack(self) -> "WallpaperPack":
        """The pack this wallpaper came from, under the name the family publishes."""
        return cast("WallpaperPack", self.pack)
