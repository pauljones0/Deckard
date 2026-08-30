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
from gi.repository import Gtk, GObject

# Import Python modules

# Import typing
from collections.abc import Callable
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.mainWindow.mainWindow import MainWindow

# Import globals
import globals as gl

# Import own modules
from src.windows.AssetManager.GenericAssetChooser import (
    PACK_CHOOSER_CHILD_NAME,
    GenericPackChooserStack,
)
from src.windows.AssetManager.InfoPage import InfoPage
from src.windows.AssetManager.CustomAssets.Chooser import CustomAssetChooser
from src.windows.AssetManager.IconPacks.Stack import IconPackChooserStack
from src.windows.AssetManager.WallpaperPacks.Stack import WallpaperPackChooserStack
from src.windows.AssetManager.SDPlusBarWallpaperPacks.Stack import SDPlusBarWallpaperPackChooserStack


class AssetManager(Gtk.ApplicationWindow):
    def __init__(self, main_window: "MainWindow", *args: Any, **kwargs: Any):
        super().__init__(  # GObject properties are keyword-only; *args forwards caller additions
            title="Asset Manager",
            default_width=1050,
            default_height=750,
            transient_for=main_window,
            *args, **kwargs
            )
        self.main_window = main_window

        # Callback func
        self.callback_func: Callable[..., Any] | None = None
        self.callback_args: tuple[Any, ...] = ()
        self.callback_kwargs: dict[str, Any] = {}

        self.build()

        self.connect("close-request", self.on_close)

    def on_close(self, *args: Any, **kwargs: Any) -> None:
        gl.asset_manager = None
        if gl.app is not None and getattr(gl.app, "asset_manager", None) is self:
            gl.app.asset_manager = None

    def build(self) -> None:
        self.main_stack = Gtk.Stack(transition_duration=200, transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT, hexpand=True, vexpand=True)
        self.set_child(self.main_stack)
        self.asset_chooser = AssetChooser(self)
        self.main_stack.add_titled(self.asset_chooser, "Asset Chooser", "Asset Chooser")

        self.asset_info = InfoPage(self)
        self.main_stack.add_titled(self.asset_info, "Asset Info", "Asset Info")

        # Header bar
        self.header_bar = Gtk.HeaderBar(css_classes=["flat"])
        self.set_titlebar(self.header_bar)

        self.stack_switcher = Gtk.StackSwitcher(stack=self.asset_chooser)
        self.header_bar.set_title_widget(self.stack_switcher)

        self.back_button = Gtk.Button(icon_name="go-previous-symbolic", visible=False)
        self.back_button.connect("clicked", self.on_back_button_click)
        self.header_bar.pack_start(self.back_button)

    def show_for_path(self, path: str | None, callback_func: Callable[..., Any] | None = None, *callback_args: Any, **callback_kwargs: Any) -> None:
        self.callback_func = callback_func
        self.callback_args = callback_args
        self.callback_kwargs = callback_kwargs

        # Each open reuses the window. See deliver_selection. A new session
        # must not inherit the drill-in position or the search filter.
        self._reset_session_state()

        self.asset_chooser.show_for_path(path)
        self.main_stack.set_visible_child(self.asset_chooser)
        self.present()

    def _reset_session_state(self) -> None:
        """Reset each pack stack and stale search before path selection."""
        chooser = self.asset_chooser

        for pack_stack in (chooser.icon_pack_chooser, chooser.wallpaper_pack_chooser,
                           chooser.sd_plus_bar_wallpaper_pack_chooser):
            pack_stack.set_visible_child_name(PACK_CHOOSER_CHILD_NAME)
        self.back_button.set_visible(False)

        pages = (
            chooser.custom_asset_chooser,
            chooser.icon_pack_chooser.pack_chooser,
            chooser.icon_pack_chooser.leaf_chooser,
            chooser.wallpaper_pack_chooser.pack_chooser,
            chooser.wallpaper_pack_chooser.leaf_chooser,
            chooser.sd_plus_bar_wallpaper_pack_chooser.pack_chooser,
            chooser.sd_plus_bar_wallpaper_pack_chooser.leaf_chooser,
        )
        for page in pages:
            # Retry pages whose main-loop build timed out; healthy pages ignore it
            retry_build = getattr(page, "retry_build", None)
            if callable(retry_build):
                retry_build()

            search_entry = getattr(page, "search_entry", None)
            if search_entry is None:
                continue
            # Avoid search-changed until a stale filter needs clearing because
            # a fresh page can still wait for its worker-built widgets
            if search_entry.get_text():
                search_entry.set_text("")

    def deliver_selection(self, path: str) -> None:
        """Run an available selection callback, then hide the reusable window."""
        callback_func = self.callback_func
        callback_args = self.callback_args
        callback_kwargs = self.callback_kwargs
        # Drop references before the call so the hidden window pins no opener graph
        self.callback_func = None
        self.callback_args = ()
        self.callback_kwargs = {}
        if callable(callback_func):
            callback_func(path, *callback_args, **callback_kwargs)
        self.hide()

    def show_info_for_asset(self, asset:dict[str, Any]) -> None:
        self.asset_info.show_for_asset(asset)
        self.main_stack.set_visible_child(self.asset_info)
        self.back_button.set_visible(True)
        self.present()

    def show_info(self, internal_path:str | None = None , licence_name: str | None = None, license_url: str | None = None, author: str | None = None, license_comment: str | None = None,
                  original_url: str | None = None) -> None:
        self.asset_info.show_info(internal_path, licence_name, license_url, author, license_comment, original_url)

        self.main_stack.set_visible_child(self.asset_info)
        self.back_button.set_visible(True)
        self.present()

    def on_back_button_click(self, button: Gtk.Button) -> None:
        if self.main_stack.get_visible_child() == self.asset_info:
            # Switch from info page to chooser page
            self.main_stack.set_visible_child(self.asset_chooser)

        elif self.main_stack.get_visible_child() == self.asset_chooser:
            # Return the visible pack family from its named asset grid
            pack_stack = self.asset_chooser.get_visible_child()
            if (isinstance(pack_stack, GenericPackChooserStack)
                    and pack_stack.get_visible_child_name() == pack_stack.leaf_child_name):
                pack_stack.set_visible_child_name(PACK_CHOOSER_CHILD_NAME)

        self.back_button.set_visible(False)


class AssetChooser(Gtk.Stack):
    def __init__(self, asset_manager: AssetManager, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.asset_manager = asset_manager

        self.build()

    def build(self) -> None:
        self.custom_asset_chooser = CustomAssetChooser(self.asset_manager)
        self.add_titled(self.custom_asset_chooser, "custom-assets", "Custom Assets")

        self.icon_pack_chooser = IconPackChooserStack(self.asset_manager)
        self.add_titled(self.icon_pack_chooser, "icon-packs", "Icon Packs")

        self.wallpaper_pack_chooser = WallpaperPackChooserStack(self.asset_manager)
        self.add_titled(self.wallpaper_pack_chooser, "wallpaper-packs", "Wallpaper Packs")

        self.sd_plus_bar_wallpaper_pack_chooser = SDPlusBarWallpaperPackChooserStack(self.asset_manager)
        self.add_titled(self.sd_plus_bar_wallpaper_pack_chooser, "sd-plus-bar-wallpaper-packs", "SD+ Bar Wallpapers")

        self.connect("notify::visible-child-name", self.on_switch)

    def show_for_path(self, path: str | None) -> None:
        # path is None when the caller has no current selection, e.g. a key
        # without a background image. Only a real path can be a custom asset.
        if path is not None and gl.asset_manager_backend.has_by_internal_path(path):
            # Switch the tab because a reopen after drill-in can show the old grid
            self.custom_asset_chooser.show_for_path(path)
            self.set_visible_child_name("custom-assets")
            self.asset_manager.back_button.set_visible(False)
        else:
            # Check if really is a icon pack
            # TODO
            self.icon_pack_chooser.show_for_path(path)


    def on_switch(self, stack: Gtk.Stack, name: GObject.ParamSpec) -> None:
        # Show the way back only for a pack family drilled into its asset grid
        pack_stack = self.get_visible_child()
        drilled_in = (isinstance(pack_stack, GenericPackChooserStack)
                      and pack_stack.get_visible_child_name() == pack_stack.leaf_child_name)
        self.asset_manager.back_button.set_visible(drilled_in)
