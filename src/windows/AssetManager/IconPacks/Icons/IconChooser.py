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
from src.windows.AssetManager.GenericAssetChooser import (
    GenericAssetChooserPage,
    GenericAssetFlowBox,
    GenericAssetPreview,
)

# Import python modules

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.IconPackManagement.Icon import Icon
    from src.backend.IconPackManagement.IconPack import IconPack
    from src.windows.AssetManager.IconPacks.Stack import IconPackChooserStack


class IconChooserPage(GenericAssetChooserPage["IconPack", "Icon",
                                              "GenericAssetPreview[Icon]",
                                              "IconPackChooserStack"]):
    # The concrete stack, restated so the quoted name in the base subscript
    # has a checked in-file use.
    stack: "IconPackChooserStack"
    FLOW_BOX_CLASS = GenericAssetFlowBox
    PREVIEW_CLASS = GenericAssetPreview

    def get_assets(self, pack: "IconPack") -> "list[Icon]":
        return pack.get_icons()

    def bind_preview(self, preview: "GenericAssetPreview[Icon]", asset: "Icon") -> None:
        preview.set_asset(asset)

    def get_child_asset(self, child: "GenericAssetPreview[Icon]") -> "Icon":
        return child.asset

    def on_build_finished(self) -> None:
        # The icon stack gates a deferred show_for_path task on the
        # build_finished flag of each of its two pages.
        self.stack.on_load_finished()
