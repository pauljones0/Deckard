"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from gi.repository import Gtk, Adw, Gio

from src.backend.DeckManagement.ImageHelpers import image2pixbuf
from src.backend.DeckManagement.Media.Media import Media
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.PluginSettings.Asset import Icon,Color
import globals as gl
from .PluginAssetPreview import IconPreview, ColorPreview
from loguru import logger as log

from collections.abc import Callable
from typing import Any

class PluginSettingsWindow(Adw.PreferencesDialog):
    def __init__(self, plugin_base: PluginBase):
        super().__init__(title=f"{plugin_base.plugin_name} Settings")

        self.set_size_request(500, 500)

        self.settings_page = SettingsPage(self, plugin_base)
        self.assets_page = IconPage(self, plugin_base)
        self.color_page = ColorPage(self, plugin_base)

        self.add(self.settings_page)
        self.add(self.assets_page)
        self.add(self.color_page)

class PluginSettingsPage(Adw.PreferencesPage):
    def __init__(self, settings_window: PluginSettingsWindow, plugin_base: PluginBase, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.settings_window = settings_window
        self.plugin_base = plugin_base

        # The tile-click handler id, or None while it is disconnected. A
        # tracked id keeps connect and disconnect idempotent: a disconnect
        # while already off cannot raise, and a reconnect cannot stack a
        # second handler.
        self._flow_box_handler: int | None = None

        self.build()

    def build(self) -> None:
        group = Adw.PreferencesGroup()
        self.add(group)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        group.add(main_box)

        search_entry = Gtk.SearchEntry()
        search_entry.set_placeholder_text("Search Asset...")
        main_box.append(search_entry)

        scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        group.add(scrolled_window)

        self.flow_box = Gtk.FlowBox(hexpand=True, orientation=Gtk.Orientation.HORIZONTAL,
                               selection_mode=Gtk.SelectionMode.SINGLE, valign=Gtk.Align.START)
        self.flow_box.set_max_children_per_line(3)
        self.flow_box.set_row_spacing(5)
        self.flow_box.set_column_spacing(5)

        scrolled_window.set_child(self.flow_box)

    def connect_flow_box(self, callback: Callable[..., Any]) -> None:
        """Wire callback to a click on a tile, once.

        A second call while the handler is on does nothing, so a repeated
        wire cannot make one click open two dialogs.
        """
        if self._flow_box_handler is None:
            self._flow_box_handler = self.flow_box.connect("child-activated", callback)

    def disconnect_flow_box(self) -> None:
        """Drop the click handler. A call while it is already off does nothing."""
        if self._flow_box_handler is not None:
            self.flow_box.disconnect(self._flow_box_handler)
            self._flow_box_handler = None

    def reset_button_clicked(self, *args: Any) -> None:
        pass

class SettingsPage(Adw.PreferencesPage):
    def __init__(self, settings_window: PluginSettingsWindow, plugin_base: PluginBase):
        super().__init__(title="Settings", icon_name="preferences-system-symbolic")
        self.settings_window = settings_window
        self.plugin_base = plugin_base
        self.build()

    def build(self) -> None:
        area = self.plugin_base.get_settings_area()
        if area:
            self.add(area)

class IconEditDialog(Adw.PreferencesDialog):
    def __init__(self, icon: Media, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)

        self.icon = icon

        page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup()

        page.add(self.group)
        self.add(page)

        self.build()

    def build(self) -> None:
        # get_final_media answers None for an icon whose file is gone, and
        # new_from_pixbuf(None) builds the empty image that state should show.
        media = self.icon.get_final_media()
        pixbuf = image2pixbuf(media) if media is not None else None
        image = Gtk.Image.new_from_pixbuf(pixbuf)
        image.set_halign(Gtk.Align.CENTER)
        image.set_valign(Gtk.Align.CENTER)
        image.set_size_request(100, 100)

        self.group.add(image)

        h_scale_adjustment = Gtk.Adjustment(value=self.icon.halign, lower=-1.0, upper=1.0, step_increment=0.01)
        v_scale_adjustment = Gtk.Adjustment(value=self.icon.valign, lower=-1.0, upper=1.0, step_increment=0.01)
        s_scale_adjustment = Gtk.Adjustment(value=self.icon.size  , lower=0.1, upper=1.0, step_increment=0.01)

        h_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        v_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        s_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        h_label = Gtk.Label(label="Horizontal Align")
        v_label = Gtk.Label(label="Vertical Align")
        size_label = Gtk.Label(label="Size")

        h_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=h_scale_adjustment, draw_value=True, hexpand=True, digits=2)
        h_scale.connect("value-changed", self.h_scale_changed, image)

        v_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=v_scale_adjustment, draw_value=True, hexpand=True, digits=2)
        v_scale.connect("value-changed", self.v_scale_changed, image)

        s_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=s_scale_adjustment, draw_value=True, hexpand=True, digits=2)
        s_scale.connect("value-changed", self.s_scale_changed, image)

        h_box.append(h_label)
        h_box.append(h_scale)

        v_box.append(v_label)
        v_box.append(v_scale)

        s_box.append(size_label)
        s_box.append(s_scale)

        self.group.add(h_box)
        self.group.add(v_box)
        self.group.add(s_box)

    def h_scale_changed(self, scale: Gtk.Scale, image: Gtk.Image) -> None:
        self.icon.halign = scale.get_value()

        img = self.icon.get_final_media()
        pixbuf = image2pixbuf(img) if img is not None else None
        image.set_from_pixbuf(pixbuf)

    def v_scale_changed(self, scale: Gtk.Scale, image: Gtk.Image) -> None:

        self.icon.valign = scale.get_value()

        img = self.icon.get_final_media()
        pixbuf = image2pixbuf(img) if img is not None else None
        image.set_from_pixbuf(pixbuf)

    def s_scale_changed(self, scale: Gtk.Scale, image: Gtk.Image) -> None:

        self.icon.size = scale.get_value()

        img = self.icon.get_final_media()
        pixbuf = image2pixbuf(img) if img is not None else None
        image.set_from_pixbuf(pixbuf)

class IconPage(PluginSettingsPage):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(title="Assets", icon_name="image-x-generic-symbolic", *args, **kwargs)
        self.display_icons()
        self.connect_flow_box(self.on_icon_clicked)

    def on_icon_clicked(self, flow_box: Gtk.FlowBox, preview: IconPreview) -> None:
        icon_dialog = Gtk.FileDialog.new()
        icon_dialog.set_title("Icon")

        icon_dialog.open(None, None, self.on_icon_dialog_response, preview)

    def on_icon_dialog_response(self, dialog: Gtk.FileDialog, task: Gio.AsyncResult, preview: IconPreview) -> None:
        try:
            file = dialog.open_finish(task)

            file_path = file.get_path() if file is not None else None
            if file_path:
                self.plugin_base.asset_manager.icons.add_override(preview.name, Icon(path=file_path), override=True)

                _, render = self.plugin_base.asset_manager.icons.get_asset_values(preview.name)
                preview.set_image(render)
                self.plugin_base.asset_manager.save_assets()
        except Exception as e:
            log.warning(e)


    def display_icons(self) -> None:
        icons = self.plugin_base.asset_manager.icons.get_assets_merged()

        for name, icon in icons.items():
            media, render = icon.get_values()

            preview = IconPreview(window=self, name=name, media=media, image=render, size=(100, 100), vexpand=False, hexpand=False)
            preview.edit_button.connect("clicked", self.edit_button_clicked, preview)
            self.flow_box.append(preview)

    def reset_button_clicked(self, *args: Any) -> None:
        preview = args[1]
        if type(preview) == IconPreview:
            self.plugin_base.asset_manager.icons.remove_override(preview.name)
            asset = self.plugin_base.asset_manager.icons.get_asset(preview.name)
            if asset is None:
                # The override is gone and there is no base asset behind it,
                # so there is no image to put back on the preview.
                return
            _, render = asset.get_values()
            preview.set_image(render)
            self.plugin_base.asset_manager.save_assets()

    def edit_button_clicked(self, *args: Any) -> None:
        preview: IconPreview = args[1]

        if not preview:
            return

        icon_asset = self.plugin_base.asset_manager.icons.get_asset(preview.name)
        if icon_asset is None or icon_asset._path is None or icon_asset._icon is None:
            # A custom icon whose file the user deleted keeps its settings
            # entry with the path and the media both None, so there is
            # nothing for the editor to open.
            log.warning(f"No editable icon behind the asset {preview.name}")
            return

        self.plugin_base.asset_manager.icons.add_override(preview.name, Icon(path=icon_asset._path, size=icon_asset._icon.size, halign=icon_asset._icon.halign, valign=icon_asset._icon.valign))

        icon, _ = self.plugin_base.asset_manager.icons.get_asset_values(preview.name)

        dialog = IconEditDialog(icon=icon)
        dialog.connect("closed", self.edit_dialog_closed, preview)
        dialog.present(self)

    def edit_dialog_closed(self, _: IconEditDialog, preview: IconPreview) -> None:
        asset = self.plugin_base.asset_manager.icons.get_asset(preview.name)
        if asset is None or asset._icon is None:
            # See edit_button_clicked: a deleted file leaves every field None.
            return
        render = asset._rendered = asset._icon.get_final_media()

        preview.set_image(render)
        self.plugin_base.asset_manager.save_assets()

        self.plugin_base.asset_manager.icons.add_override(preview.name, asset, override=True)

class ColorPage(PluginSettingsPage):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(title="Colors", icon_name="color-select-symbolic", *args, **kwargs)
        self.display_colors()
        self.connect_flow_box(self.on_color_clicked)

    def on_color_clicked(self, flow_box: Gtk.FlowBox, preview: ColorPreview) -> None:
        color_dialog = Gtk.ColorDialog.new()
        color_dialog.set_title("Color")

        if gl.app is None:
            return
        # Open the dialog
        color_dialog.choose_rgba(gl.app.get_active_window(), preview.get_rgba(), None, self.on_color_dialog_response, preview)

    def on_color_dialog_response(self, dialog: Gtk.ColorDialog, task: Gio.Task, preview: ColorPreview) -> None:
        try:
            rgba = dialog.choose_rgba_finish(task)
            preview.set_color_rgba(rgba)
            self.plugin_base.asset_manager.colors.add_override(preview.name, Color(color=preview.color), override=True)
            self.plugin_base.asset_manager.save_assets()
        except Exception as e:
            log.warning(e)

    def display_colors(self) -> None:
        colors = self.plugin_base.asset_manager.colors.get_assets_merged()

        for name, color in colors.items():
            rgba = color.get_values()
            preview = ColorPreview(window=self, name=name, color=rgba, size=(100, 100), hexpand=False, vexpand=False)
            self.flow_box.append(preview)

    def reset_button_clicked(self, *args: Any) -> None:
        preview = args[1]
        if type(preview) == ColorPreview:
            self.plugin_base.asset_manager.colors.remove_override(preview.name)
            asset = self.plugin_base.asset_manager.colors.get_asset(preview.name)
            if asset is None:
                # No base colour behind the override that just went.
                return
            preview.set_color(asset.get_values())
            self.plugin_base.asset_manager.save_assets()