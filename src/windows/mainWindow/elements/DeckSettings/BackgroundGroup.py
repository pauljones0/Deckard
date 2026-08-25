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
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.windows.mainWindow.elements.DeckSettings.DeckSettingsPage import DeckSettingsPage


import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GObject

# Import Python modules

# Import globals
from src.backend import services

import globals as gl

# Import own modules
from src.backend.DeckManagement.ImageHelpers import image2pixbuf
from src.windows.mainWindow.lazy_map import LazyMapTasks

class BackgroundGroup(Adw.PreferencesGroup):
    def __init__(self, settings_page: "DeckSettingsPage") -> None:
        super().__init__(title=gl.lm.get("deck.background-group.title"), description=gl.lm.get("deck.background-group.description"))
        self.set_margin_top(50)
        self.deck_serial_number = settings_page.deck_serial_number
        self.media_row = BackgroundMediaRow(settings_page, self.deck_serial_number)
        self.add(self.media_row)


class BackgroundMediaRow(LazyMapTasks, Adw.PreferencesRow):
    def __init__(self, settings_page: "DeckSettingsPage", deck_serial_number: str, **kwargs: Any) -> None:
        super().__init__()
        self.settings_page = settings_page
        self.deck_serial_number = deck_serial_number

        """
        To save performance and memory, we only load the thumbnail when the user sees the row
        """
        self.on_map_tasks = []
        self.connect("map", self.on_map)

        # The handler id per widget key, absent while that widget is
        # disconnected. Tracked ids keep connect and disconnect idempotent: a
        # disconnect while already off cannot raise, and a reconnect cannot
        # stack a second handler.
        self._handlers: dict[str, int] = {}

        self.build()

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.enable_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        self.main_box.append(self.enable_box)
        
        self.enable_label = Gtk.Label(label=gl.lm.get("deck.background-group.enable"), hexpand=True, xalign=0)
        self.enable_box.append(self.enable_label)

        self.enable_switch = Gtk.Switch()
        self.enable_box.append(self.enable_switch)

        self.config_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, visible=False)
        self.main_box.append(self.config_box)

        self.config_box.append(Gtk.Separator(hexpand=True, margin_top=10, margin_bottom=10))

        self.media_selector = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER)
        self.config_box.append(self.media_selector)

        self.media_selector_image = Gtk.Image() # Will be bound to the button by self.set_thumbnail()

        self.media_selector_button = Gtk.Button(label=gl.lm.get("deck.background-group.media-select-label"), css_classes=["page-settings-media-selector"])
        self.media_selector.append(self.media_selector_button)

        self.loop_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, margin_bottom=15)
        self.config_box.append(self.loop_box)

        self.loop_label = Gtk.Label(label=gl.lm.get("media-loop"), hexpand=True, xalign=0)
        self.loop_box.append(self.loop_label)

        self.loop_switch = Gtk.Switch()
        self.loop_box.append(self.loop_switch)

        self.fps_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        self.config_box.append(self.fps_box)

        self.fps_label = Gtk.Label(label=gl.lm.get("fps"), hexpand=True, xalign=0)
        self.fps_box.append(self.fps_label)

        self.fps_spinner = Gtk.SpinButton.new_with_range(1, 30, 1)
        self.fps_box.append(self.fps_spinner)

        self.extend_touchscreen_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, margin_top=15)
        self.config_box.append(self.extend_touchscreen_box)

        self.extend_touchscreen_label = Gtk.Label(
            label=gl.lm.get("deck.background-group.extend-background-to-touchscreen"),
            hexpand=True, xalign=0
        )
        self.extend_touchscreen_box.append(self.extend_touchscreen_label)

        self.extend_touchscreen_switch = Gtk.Switch()
        self.extend_touchscreen_box.append(self.extend_touchscreen_switch)

        self.connect_signals()
        self.load_defaults()

    def _signal_bindings(self) -> list[tuple[str, GObject.Object, str, Callable[..., Any]]]:
        return [
            ("enable", self.enable_switch, "state-set", self.on_toggle_enable),
            ("media", self.media_selector_button, "clicked", self.on_choose_image),
            ("loop", self.loop_switch, "state-set", self.on_toggle_loop),
            ("fps", self.fps_spinner, "value-changed", self.on_change_fps),
            ("extend", self.extend_touchscreen_switch, "state-set", self.on_toggle_extend_touchscreen),
        ]

    def connect_signals(self) -> None:
        for key, widget, signal, callback in self._signal_bindings():
            if self._handlers.get(key) is None:
                self._handlers[key] = widget.connect(signal, callback)


    def disconnect_signals(self) -> None:
        for key, widget, _signal, _callback in self._signal_bindings():
            handler = self._handlers.pop(key, None)
            if handler is not None:
                widget.disconnect(handler)


    def load_defaults(self) -> None:
        if not self.get_mapped():
            self.on_map_tasks.clear()
            self.on_map_tasks.append(lambda: self.load_defaults())
            return
        self.disconnect_signals()
        try:
            # One read, and read-only: the missing keys show the deck-settings
            # schema's defaults without being written into the file. Persisting
            # them here is what made a deck background loop or not depending on
            # whether anyone had ever opened this page.
            config = gl.settings_manager.deck(self.deck_serial_number).section("background")

            # Update ui
            self.enable_switch.set_active(config["enable"])
            self.config_box.set_visible(config["enable"])
            self.loop_switch.set_active(config["loop"])
            self.fps_spinner.set_value(config["fps"])
            self.extend_touchscreen_switch.set_active(config["extend-to-touchscreen"])
            self.extend_touchscreen_box.set_visible(self.settings_page.deck_controller.deck.is_touch())
            self.set_thumbnail(config["media-path"])
        finally:
            # A read that returns early or raises must still leave the widgets
            # wired, or every later change to this row is dropped silently.
            self.connect_signals()

    def load_defaults_from_page(self) -> None:
        # The early return below disables this method, so the unguarded
        # active_page.dict["background"] reads that follow never run. Guard
        # them before anything calls this method again. The dead body stays
        # as the record of what that guard has to cover.
        return
        if not hasattr(self.settings_page.deck_page.deck_controller, "active_page"):
            return
        if self.settings_page.deck_page.deck_controller.active_page is None:
            return
        
        original_values = None
        if "background" in self.settings_page.deck_page.deck_controller.active_page:
            original_values = self.settings_page.deck_page.deck_controller.active_page.dict["background"]

        overwrite = self.settings_page.deck_page.deck_controller.active_page.dict["background"].setdefault("overwrite", False)
        show = self.settings_page.deck_page.deck_controller.active_page.dict["background"].setdefault("show", False)
        file_path = self.settings_page.deck_page.deck_controller.active_page.dict["background"].setdefault("media-path", None)

        # Save if changed
        if original_values != self.settings_page.deck_page.deck_controller.active_page:
            self.settings_page.deck_page.deck_controller.active_page.save()

        self.overwrite_switch.set_active(overwrite)
        self.enable_switch.set_active(show)

        # Set config box state
        self.config_box.set_visible(overwrite)

        self.set_thumbnail(file_path)

    def on_toggle_enable(self, toggle_switch: Gtk.Switch, state: bool) -> None:
        config = gl.settings_manager.deck(self.deck_serial_number)
        config.set("background", "enable", state)
        # Save
        config.save()
        # Update
        self.config_box.set_visible(state)
        # Update
        controller = self.settings_page.deck_controller
        page = controller.active_page
        if page is not None:
            controller.load_background(page=page)

    def on_toggle_loop(self, toggle_switch: Gtk.Switch, state: bool) -> None:
        settings = gl.settings_manager.deck(self.deck_serial_number)
        settings.set("background", "loop", state)

        # Save
        settings.save()

        # Update
        controller = self.settings_page.deck_controller
        page = controller.active_page
        if page is not None:
            controller.load_background(page=page)

    def on_toggle_extend_touchscreen(self, toggle_switch: Gtk.Switch, state: bool) -> None:
        settings = gl.settings_manager.deck(self.deck_serial_number)
        settings.set("background", "extend-to-touchscreen", state)

        # Save
        settings.save()

        # Update
        controller = self.settings_page.deck_controller
        page = controller.active_page
        if page is not None:
            controller.load_background(page=page)

    def on_change_fps(self, spinner: Gtk.SpinButton) -> None:
        settings = gl.settings_manager.deck(self.deck_serial_number)
        settings.set("background", "fps", spinner.get_value_as_int())

        # Save
        settings.save()

        # Update
        controller = self.settings_page.deck_controller
        page = controller.active_page
        if page is not None:
            controller.load_background(page=page)

    def on_choose_image(self, button: Gtk.Button) -> None:
        media_path = gl.settings_manager.deck(self.deck_serial_number).get("background", "media-path")

        services.require_app().let_user_select_asset(default_path=media_path, callback_func=self.update_image)

    def update_image(self, file_path: "str | None") -> None:
        self.set_thumbnail(file_path)   
        settings = gl.settings_manager.deck(self.deck_serial_number)
        settings.set("background", "media-path", file_path)
        settings.save()

        controller = self.settings_page.deck_controller
        page = controller.active_page
        if page is not None:
            controller.load_background(page=page)

    def set_thumbnail(self, file_path: "str | None") -> None:
        if not file_path:
            return
        if not os.path.isfile(file_path):
            return
        image = gl.media_manager.get_thumbnail(file_path)
        pixbuf = image2pixbuf(image)
        self.media_selector_image.set_from_pixbuf(pixbuf)
        self.media_selector_button.set_child(self.media_selector_image)
