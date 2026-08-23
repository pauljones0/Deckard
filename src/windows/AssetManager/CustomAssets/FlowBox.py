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
from rapidfuzz import fuzz
import threading
from loguru import logger as log

# Import globals
import globals as gl

# Import own modules
from src.backend.DeckManagement.HelperMethods import is_video
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
        # These two hook methods carry names the base does not assign.
        # DynamicFlowBox.__init__ writes self.filter_func and self.sort_func,
        # so a hook method under either name is shadowed by the instance
        # attribute before these lines read them, and the box installs None
        # over its own hook: the search entry and the kind toggles then change
        # nothing and the grid keeps backend order. The base keeps its slot
        # names. Renaming them would end the hazard for every subclass at
        # once, but it changes every consumer of the flow box, so a test
        # refuses any subclass that supplies one of those names instead.
        self.set_filter_func(self._filter_asset)
        self.set_sort_func(self._sort_assets)

        self.flow_box.connect("child-activated", self.on_child_activated)

        # Custom assets have one pack, the whole backend list, so the load
        # runs as soon as the recycler exists. See the DynamicFlowBox
        # docstring.
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

        if search_string == "":
            return True

        fuzz_score = fuzz.ratio(search_string.lower(), asset["name"].lower())
        if fuzz_score < 40:
            return False

        return True

    def _sort_assets(self, a: dict[str, Any], b: dict[str, Any]) -> int:
        search_string = self.asset_chooser.search_entry.get_text()

        if search_string == "":
            # Sort alphabetically
            if a["name"] < b["name"]:
                return -1
            if a["name"] > b["name"]:
                return 1
            return 0

        a_fuzz = fuzz.ratio(search_string.lower(), a["name"].lower())
        b_fuzz = fuzz.ratio(search_string.lower(), b["name"].lower())

        if a_fuzz > b_fuzz:
            return -1
        elif a_fuzz < b_fuzz:
            return 1

        return 0

    def on_child_activated(self, flow_box: Gtk.FlowBox, child: Any) -> None:
        # Capture the selection and the callback before the thread starts.
        # Each open reuses the window, so a thread that reads
        # self.asset_chooser.asset_manager from its own body can call a new
        # callback with the state of a new window, when the user reopens the
        # Asset Manager while the thread runs.
        asset_path = child.asset["internal-path"]
        callback = self.asset_chooser.asset_manager.callback_func
        callback_args = self.asset_chooser.asset_manager.callback_args
        callback_kwargs = self.asset_chooser.asset_manager.callback_kwargs

        # The capture is done, so drop the references of the manager. The
        # hidden window must not pin the bound callback of the opener, and
        # through it the action and page graph, until the next show_for_path.
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

        # Call hide, not close, so the window survives for the next open.
        # close() reaches the default GTK4 close-request handling, which
        # destroys the window on the next main-loop iteration, with no
        # explicit destroy() call.
        self.asset_chooser.asset_manager.hide()

    @log.catch
    def callback_thread(self, asset_path: str, callback: Callable[..., Any],
                        callback_args: tuple[Any, ...], callback_kwargs: dict[str, Any]) -> None:
        callback(asset_path, *callback_args, **callback_kwargs)

    def remove_asset(self, asset: dict[str, Any]) -> None:
        gl.asset_manager_backend.remove_asset_by_id(asset["id"])
        self.flow_box.unselect_all()
        self.refresh()