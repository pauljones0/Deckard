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

import os
from loguru import logger as log

from src.backend.IconPackManagement.IconPack import IconPack

import globals as gl

class IconPackManager:
    def __init__(self) -> None:
        self.packs: dict[str, IconPack] = {}

    def get_icon_packs(self) -> dict[str, IconPack]:
        packs: dict[str, IconPack] = {}
        os.makedirs(os.path.join(gl.DATA_PATH, "icons"), exist_ok=True)
        for pack in os.listdir(os.path.join(gl.DATA_PATH, "icons")):
            if pack.startswith("."):
                # Transient install-swap trees (StoreBackend._swap_into_place)
                # and other hidden entries are not packs.
                continue
            icon_pack = IconPack(os.path.join(gl.DATA_PATH, "icons", pack))
            if icon_pack.is_valid:
                packs[pack] = icon_pack
            else:
                log.warning(f"Icon pack {pack} is not valid.")
        return packs
