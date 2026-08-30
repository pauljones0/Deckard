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
import threading

import gi

from loguru import logger as log

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

# Import own modules
from GtkHelper.GtkHelper import run_on_main
from src.windows.AssetManager.GenericAssetChooser import GenericPackChooserStack
from src.windows.AssetManager.IconPacks.PackChooser import IconPackChooser
from src.windows.AssetManager.IconPacks.Icons.IconChooser import IconChooserPage

# Import globals
import globals as gl

# Import typing
from collections.abc import Callable
from typing import Any, TYPE_CHECKING, override

if TYPE_CHECKING:
    from src.backend.IconPackManagement.IconPack import IconPack


class IconPackChooserStack(GenericPackChooserStack[IconChooserPage]):
    """Defer non-custom path selection until both icon-pack pages are built."""

    PACK_CHOOSER_CLASS = IconPackChooser
    LEAF_CHOOSER_CLASS = IconChooserPage
    LEAF_CHILD_TITLE = "Icon Chooser"

    @override
    def prepare(self) -> None:
        self.on_loads_finished_tasks: list[Callable[[], Any]] = []
        # Serialize both worker completion flags with the deferred-task queue
        self._loads_lock = threading.Lock()

    def show_for_path(self, path: str | None) -> None:
        if path is None:
            # No pre-selection. The loop below would compare every icon
            # against None and select nothing, so return at once.
            return
        with self._loads_lock:
            if not self.get_is_build_finished():
                # The shared lock puts the task in the drain or after completion
                self.on_loads_finished_tasks.append(lambda: self.show_for_path(path))
                return
        if gl.icon_pack_manager is None:
            # Boot keeps this window closed until the optional manager exists
            return
        packs = gl.icon_pack_manager.get_icon_packs()
        for pack in packs.values():
            icons = pack.get_icons()
            for icon in icons:
                if icon.path == path:
                    # Marshal widget updates; run_on_main stays inline for main callers
                    run_on_main(self._show_pack_asset, pack, path)
                    return

    def _show_pack_asset(self, pack: "IconPack", path: str) -> None:
        """Turn the window to the icon of path inside pack. Main loop only."""
        self.leaf_chooser.load_for_pack(pack)
        self.leaf_chooser.select_asset(path=path)
        self.set_visible_child(self.leaf_chooser)
        self.asset_manager.asset_chooser.set_visible_child_name("icon-packs")
        self.asset_manager.back_button.set_visible(True)

    def get_is_build_finished(self) -> bool:
        return (hasattr(self, "pack_chooser") and self.pack_chooser.build_finished
                and hasattr(self, "leaf_chooser") and self.leaf_chooser.build_finished)

    def on_load_finished(self) -> None:
        """Drain deferred tasks once after both build workers finish.

        Snapshot under the lock, then run outside it because tasks re-enter the lock.
        """
        with self._loads_lock:
            if not self.get_is_build_finished():
                return
            tasks = list(self.on_loads_finished_tasks)
            self.on_loads_finished_tasks.clear()
        for task in tasks:
            try:
                task()
            except Exception as e:
                # Isolate a timed-out main-loop marshal from the build worker
                log.opt(exception=True).warning(f"Deferred icon-pack task failed: {e}")
