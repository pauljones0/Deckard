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

# Import python modules

# Import own modules
from src.windows.AssetManager.GenericAssetChooser import (
    GenericPackChooserPage,
    GenericPackFlowBox,
    GenericPackPreview,
)

# Import globals
import globals as gl

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.IconPackManagement.IconPack import IconPack
    from src.windows.AssetManager.IconPacks.Icons.IconChooser import IconChooserPage
    from src.windows.AssetManager.IconPacks.Stack import IconPackChooserStack


class IconPackChooser(GenericPackChooserPage["IconPack", "IconPackChooserStack"]):
    # The concrete stack, restated so the quoted name in the base subscript
    # has a checked in-file use.
    stack: "IconPackChooserStack"
    PACK_FLOW_BOX_CLASS = GenericPackFlowBox
    PACK_PREVIEW_CLASS = GenericPackPreview
    LEAF_CHILD_NAME = "icon-chooser"

    def get_packs(self) -> "dict[str, IconPack]":
        if gl.icon_pack_manager is None:
            # The boot order keeps the window shut until the manager exists,
            # and the type still allows None.
            return {}
        return gl.icon_pack_manager.get_icon_packs()

    def get_leaf_chooser(self) -> "IconChooserPage":
        return self.stack.leaf_chooser

    def on_build_finished(self) -> None:
        # The icon stack gates a deferred show_for_path task on the
        # build_finished flag of each of its two pages.
        self.stack.on_load_finished()
