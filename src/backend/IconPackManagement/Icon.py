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
    from src.backend.IconPackManagement.IconPack import IconPack

class Icon(PackAsset):
    """One icon file inside an icon pack."""

    ATTRIBUTION_KEY = AttributionKey.BASENAME

    def __init__(self, icon_pack: "IconPack", path: str):
        super().__init__(pack=icon_pack, path=path)

    @property
    def icon_pack(self) -> "IconPack":
        """The pack this icon came from, under the name the family publishes."""
        return cast("IconPack", self.pack)
