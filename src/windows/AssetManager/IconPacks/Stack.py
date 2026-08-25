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
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.backend.IconPackManagement.IconPack import IconPack


class IconPackChooserStack(GenericPackChooserStack[IconChooserPage]):
    """The icon-pack stack, which is the one stack a pre-selection reaches.

    AssetChooser.show_for_path routes every non-custom path here, so this
    stack alone defers that request until both of its pages have built.
    """

    PACK_CHOOSER_CLASS = IconPackChooser
    LEAF_CHOOSER_CLASS = IconChooserPage
    LEAF_CHILD_TITLE = "Icon Chooser"

    def prepare(self) -> None:
        self.on_loads_finished_tasks: list[Callable[[], Any]] = []
        # Serializes the two build_finished flags with the deferred-task
        # queue. See on_load_finished and show_for_path. The pack chooser and
        # the icon chooser each build on their own worker thread, and both
        # call on_load_finished, so two threads can enter this drain at once.
        self._loads_lock = threading.Lock()

    def show_for_path(self, path: str | None) -> None:
        if path is None:
            # No pre-selection. The loop below would compare every icon
            # against None and select nothing, so return at once.
            return
        with self._loads_lock:
            if not self.get_is_build_finished():
                # Defer under the same lock that on_load_finished drains
                # with. The task either reaches a snapshot, or it reads the
                # flag as True here and dispatches at once.
                self.on_loads_finished_tasks.append(lambda: self.show_for_path(path))
                return
        if gl.icon_pack_manager is None:
            # Same reading as IconPackChooserPage.get_packs: the boot order
            # keeps the window shut until the manager exists, and the type
            # still allows None. With no packs there is nothing to select.
            return
        packs = gl.icon_pack_manager.get_icon_packs()
        for pack in packs.values():
            icons = pack.get_icons()
            for icon in icons:
                if icon.path == path:
                    # The scan above reads pack data and runs on whichever
                    # thread asked. The lines it hands over drive widgets, and
                    # GTK4 takes calls from the main thread only, so they run
                    # there. run_on_main runs inline when the caller already
                    # holds the main thread, so the direct call from the window
                    # and the deferred one from a build worker both work.
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
        """Run from both build worker threads, the pack one and the icon one.

        It snapshots and clears the deferred-task queue in one step under the
        lock. A show_for_path that read a flag as False then cannot add its
        task after this drain took the snapshot. Two callers cannot run or
        remove the same task twice. The tasks run outside the lock, because
        they re-enter show_for_path, which takes the same lock.
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
                # A task marshals to the main loop, which can time out. The
                # caller is the tail of a build worker, so a raise here would
                # end that thread instead of the one task.
                log.opt(exception=True).warning(f"Deferred icon-pack task failed: {e}")
