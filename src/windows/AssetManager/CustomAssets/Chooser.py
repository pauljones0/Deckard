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

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GLib, Gio

# Import python modules
from loguru import logger as log

# Import own modules
from GtkHelper.GtkHelper import run_in_background, run_on_main
from src.backend import settings_store
from src.windows.AssetManager.ChooserPage import ChooserPage
from src.windows.AssetManager.CustomAssets.FlowBox import CustomAssetChooserFlowBox

# Import globals
import globals as gl

# Import typing modules
from collections.abc import Callable
from collections.abc import Iterable
from typing import Any, TYPE_CHECKING, override
if TYPE_CHECKING:
    from src.windows.AssetManager.AssetManager import AssetManager

class CustomAssetChooser(ChooserPage):
    def __init__(self, asset_manager: "AssetManager"):
        super().__init__()
        self.asset_manager = asset_manager

        self.asset_chooser: CustomAssetChooserFlowBox | None = None
        self.browse_files_button: Gtk.Button | None = None
        self.build_finished = False
        self.build_task_finished_tasks: list[Callable[[], Any]] = []
        # Serializes build_finished with the deferred-task queue. See
        # _finish_build and show_for_path.
        self._build_tasks_lock = threading.Lock()

        run_in_background(self.build)

    @log.catch
    def build(self) -> None:
        self.build_finished = False
        try:
            # Build every GTK object on the main loop; only bookkeeping stays here
            def _build_ui() -> None:
                self.asset_chooser = CustomAssetChooserFlowBox(self)
                # Use the flow's own scroller directly so it fills the page
                # The default expanding scroller would compete for height
                self.main_box.remove(self.scrolled_window)
                self.main_box.append(self.asset_chooser)

                self.browse_files_button = Gtk.Button(label=gl.lm.get("asset-chooser.custom.browse-files"), margin_top=15)
                self.browse_files_button.connect("clicked", self.on_browse_files_clicked)
                self.main_box.append(self.browse_files_button)

            run_on_main(_build_ui)

            self.load_defaults()
        finally:
            # Always stop loading before log.catch records a build failure
            self.set_loading(False)

            self._finish_build()

    def _finish_build(self) -> None:
        """Set completion and drain deferred tasks under the same lock."""
        with self._build_tasks_lock:
            self.build_finished = True
            tasks = list(self.build_task_finished_tasks)
            self.build_task_finished_tasks.clear()
        # Run the tasks outside the lock, because they call back into code
        # near show_for_path, which must not hold it.
        for task in tasks:
            try:
                task()
            except Exception as e:
                log.opt(exception=True).warning(f"Deferred asset-chooser task failed: {e}")

    @override
    def on_dnd_accept(self, drop: Gtk.DropTarget, user_data: Gdk.Drop) -> bool:
        return True
    
    @override
    def on_dnd_drop(self, drop_target: Gtk.DropTarget, value: Gdk.FileList, x: float, y: float) -> bool:
        paths = value.get_files()
        self.add_files(paths)
        return True
    
    def add_asset(self, asset: dict[str, Any]) -> None:
        # The shared live list already holds the asset; marshal only its re-render
        chooser = self.asset_chooser
        if chooser is None:
            return
        GLib.idle_add(chooser.refresh)

    def add_files(self, files: Iterable[Any]) -> None:
        # A late drop or dialog callback still adds files after the window closes
        asset_manager = gl.asset_manager
        if asset_manager is not None:
            asset_manager.set_cursor_from_name("wait")
        for path in files:

            url = path.get_uri()
            path = path.get_path()

            # gl.asset_manager_backend.add_custom_media_set_by_ui(url=url, path=path)
            threading.Thread(target=gl.asset_manager_backend.add_custom_media_set_by_ui, args=(url, path), name="add_custom_media_set_by_ui").start()

        if asset_manager is not None:
            asset_manager.set_cursor_from_name("default")

    @override
    def show_for_path(self, path: str) -> None:
        def show_now() -> None:
            chooser = self.asset_chooser
            if chooser is None:
                # build() failed before the flow box existed. The failure is
                # already in the log, so do not raise into the caller too.
                return
            chooser.show_for_path(path)

        with self._build_tasks_lock:
            if not self.build_finished:
                # The shared lock puts the task in the drain or after completion
                self.build_task_finished_tasks.append(show_now)
                return
        show_now()

    @override
    def on_video_toggled(self, button: Gtk.ToggleButton) -> None:
        # Read-modify-write serialized against the other toggle, so flipping
        # both in quick succession cannot lose one.
        with settings_store.get().edit(settings_store.UI_ASSET_MANAGER) as settings:
            settings["video-toggle"] = button.get_active()

        # Update ui
        if self.asset_chooser is not None:
            self.asset_chooser.refresh()

    @override
    def on_image_toggled(self, button: Gtk.ToggleButton) -> None:
        with settings_store.get().edit(settings_store.UI_ASSET_MANAGER) as settings:
            settings["image-toggle"] = button.get_active()

        # Update ui
        if self.asset_chooser is not None:
            self.asset_chooser.refresh()

    def load_defaults(self) -> None:
        # Both toggles start on. The True defaults live in the surface schema.
        settings = settings_store.get().view(settings_store.UI_ASSET_MANAGER)
        # This runs on the build worker, and a toggle write is a GTK call.
        run_on_main(self.video_button.set_active, settings.get("video-toggle"))
        run_on_main(self.image_button.set_active, settings.get("image-toggle"))

    @override
    def apply_search(self, query: str) -> None:
        if self.asset_chooser is not None:
            self.asset_chooser.refresh()
            # The refresh is the render, so this page is current with the
            # entry and needs no catch-up the next time it shows.
            self.record_rendered_query(query)

    def on_browse_files_clicked(self, button: Gtk.Button) -> None:
        ChooseFileDialog(self) #TODO: Change to Xdp Portal call


class ChooseFileDialog(Gtk.FileDialog):
    def __init__(self, custom_asset_chooser: CustomAssetChooser):
        super().__init__(title=gl.lm.get("asset-chooser.custom.browse-files.dialog.title"),
                         accept_label=gl.lm.get("asset-chooser.custom.browse-files.dialog.select-button"))
        self.custom_asset_chooser = custom_asset_chooser
        self.open_multiple(callback=self.callback)

    def callback(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            selected_files = self.open_multiple_finish(result)
        except GLib.Error as err:
            log.error(err)
            return
        
        self.custom_asset_chooser.add_files(selected_files)
