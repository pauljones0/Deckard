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
from gi.repository import Gtk

# Import python modules
import threading
from loguru import logger as log

# Import globals
import globals as gl

# Import own modules
from src.backend.DeckManagement.HelperMethods import is_video
from src.windows.AssetManager import asset_search
from src.windows.AssetManager.CustomAssets.AssetPreview import AssetPreview
from src.windows.AssetManager.DynamicFlowBox import DynamicFlowBox

# Import typing
from typing import Any, Callable, TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.AssetManager.CustomAssets.Chooser import CustomAssetChooser


class CustomAssetChooserFlowBox(DynamicFlowBox[AssetPreview, dict[str, Any]]):
    def __init__(self, asset_chooser: "CustomAssetChooser", *args: Any, **kwargs: Any) -> None:
        super().__init__(AssetPreview, *args, **kwargs)
        self.set_hexpand(True)

        self.asset_chooser:"CustomAssetChooser" = asset_chooser
        self.selected_asset: str = None  # ty: ignore[invalid-assignment]  # late-init: on_child_activated

        self.set_factory(self.preview_factory)
        # Hook names must differ from the base slots that hold installed callables
        self.set_filter_func(self._filter_asset)
        self.set_sort_func(self._sort_assets)

        self.flow_box.connect("child-activated", self.on_child_activated)

        # Custom assets use the whole backend list, so load after recycler creation
        self.load_assets()

    def load_assets(self) -> None:
        self.set_item_list(gl.asset_manager_backend.get_all())
        self.refresh()

    def refresh(self) -> None:
        self.show_range(0, self.N_ITEMS_PER_PAGE)

    def show_for_path(self, path: str) -> None:
        self.select_asset(path)
        self.refresh()

    def select_asset(self, path: str) -> None:
        self.selected_asset = path

    def preview_factory(self, preview: AssetPreview, asset: dict[str, Any]) -> None:
        # The recycler pools base_class instances, which this box sets to
        # AssetPreview, and the generic base delivers them as themselves.
        asset_preview = preview
        asset_preview.set_asset(self, asset)
        if self.selected_asset == asset.get("internal-path"):
            self.flow_box.select_child(asset_preview)

    def _filter_asset(self, asset: dict[str, Any]) -> bool:
        search_string = self.asset_chooser.search_entry.get_text()
        show_image = self.asset_chooser.image_button.get_active()
        show_video = self.asset_chooser.video_button.get_active()

        asset_is_video = is_video(asset["internal-path"])

        if asset_is_video and not show_video:
            return False
        if not asset_is_video and not show_image:
            return False

        # Custom assets carry a name of their own, so the search reads that
        # and not the file name. The ladder is the one the pack choosers use.
        return asset_search.matches(asset["name"], search_string)

    def _sort_assets(self, a: dict[str, Any], b: dict[str, Any]) -> int:
        search_string = self.asset_chooser.search_entry.get_text()

        if asset_search.is_empty_query(search_string):
            # Sort alphabetically
            if a["name"] < b["name"]:
                return -1
            if a["name"] > b["name"]:
                return 1
            return 0

        return asset_search.compare(a["name"], b["name"], search_string)

    def on_child_activated(self, flow_box: Gtk.FlowBox, child: Any) -> None:
        # Capture this session before the callback thread can observe a reopened window
        asset_path = child.asset["internal-path"]
        callback = self.asset_chooser.asset_manager.callback_func
        callback_args = self.asset_chooser.asset_manager.callback_args
        callback_kwargs = self.asset_chooser.asset_manager.callback_kwargs

        # Drop manager references so the hidden window pins no opener graph
        self.asset_chooser.asset_manager.callback_func = None
        self.asset_chooser.asset_manager.callback_args = ()
        self.asset_chooser.asset_manager.callback_kwargs = {}

        if callable(callback):
            callback_thread = threading.Thread(
                target=self.callback_thread,
                args=(asset_path, callback, callback_args, callback_kwargs),
                name="flow_box_callback_thread"
            )
            callback_thread.start()

        # Hide instead of close so GTK does not destroy the reusable window
        self.asset_chooser.asset_manager.hide()

    @log.catch
    def callback_thread(self, asset_path: str, callback: Callable[..., Any],
                        callback_args: tuple[Any, ...], callback_kwargs: dict[str, Any]) -> None:
        callback(asset_path, *callback_args, **callback_kwargs)

    def remove_asset(self, asset: dict[str, Any]) -> None:
        gl.asset_manager_backend.remove_asset_by_id(asset["id"])
        self.flow_box.unselect_all()
        self.refresh()
