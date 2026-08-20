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
from src.windows.AssetManager.Preview import Preview

# Import python modules
import os

from loguru import logger as log

# Import globals
import globals as gl

# Import typing
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.IconPackManagement.Icon import Icon

class IconPreview(Preview):
    def __init__(self) -> None:
        super().__init__()

        self.icon: "Icon" = None  # type: ignore[assignment]  # late-init: set_icon

    def on_click_info(self, *args: Any) -> None:
        # The window that owns this preview nulls the slot as it closes, and a
        # recycled child can outlive that, so answer a closed window with a log
        # line rather than a traceback out of the click handler.
        asset_manager = gl.asset_manager
        if asset_manager is None:
            log.error("The asset manager window is gone; cannot show asset info")
            return
        asset_manager.show_info(
            internal_path = self.icon.path,
            licence_name = self.icon.get_attribution().get("license"),
            license_url = self.icon.get_attribution().get("license-url"),
            author = self.icon.get_attribution().get("copyright"),
            license_comment = self.icon.get_attribution().get("comment")
        )

    def set_icon(self, icon: "Icon") -> None:
        self.icon = icon

        # This runs inside the main-loop callback of
        # DynamicFlowBox._apply_range, and the factory function of the chooser
        # is the one caller, so set the text and the image here. A deferral
        # through idle_add leaves the recycled child visible for a frame with
        # the name and thumbnail of the earlier item.
        self.set_text(os.path.splitext(os.path.basename(self.icon.path))[0])
        self.set_image(self.icon.path)
