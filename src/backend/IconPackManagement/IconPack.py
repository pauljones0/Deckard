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

from src.backend.IconPackManagement.Icon import Icon
from src.backend.PackManagement.pack_family import Pack

class IconPack(Pack[Icon]):
    """One icon pack folder. Its manifest names the asset folder under "icons"."""

    ASSET_MANIFEST_KEY = "icons"

    @override
    def make_asset(self, path: str) -> Icon:
        return Icon(icon_pack=self, path=path)

    def get_icons(self) -> list[Icon]:
        return self.get_content_from_structure()
