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
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, GObject

# Import Python modules

# Import globals
from src.backend import services

import globals as gl

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    # A runtime import cycles, because DeckSettingsPage imports this module.
    from src.windows.mainWindow.elements.DeckSettings.DeckSettingsPage import DeckSettingsPage

# Import own modules
from src.backend.DeckManagement.ImageHelpers import image2pixbuf
from src.backend.settings_store import DECK_NAME_MAX_LENGTH
from src.windows.mainWindow.lazy_map import LazyMapTasks

class DeckGroup(Adw.PreferencesGroup):
    def __init__(self, settings_page: "DeckSettingsPage") -> None:
        super().__init__(title=gl.lm.get("deck.deck-group.title"), description=gl.lm.get("deck.deck-group.description"))
        self.deck_serial_number = settings_page.deck_serial_number

        self.name_row = DeckName(settings_page, self.deck_serial_number)
        self.brightness = Brightness(settings_page, self.deck_serial_number)
        self.saturation = Saturation(settings_page, self.deck_serial_number)
        self.screensaver = Screensaver(settings_page, self.deck_serial_number)
        self.rotation = Rotation(settings_page, self.deck_serial_number)

        # The name first. It says which deck the rest of the group applies to.
        self.add(self.name_row)
        self.add(self.brightness)
        self.add(self.saturation)
        self.add(self.screensaver)
        self.add(self.rotation)


class DeckName(Adw.EntryRow):
    """Set the deck name shown in the header switcher; an empty value uses the model name.
    Apply or Enter writes a trimmed value immediately without a pending timeout."""

    def __init__(self, settings_page: "DeckSettingsPage", deck_serial_number: str) -> None:
        super().__init__(title=gl.lm.get("deck.deck-group.name"), show_apply_button=True)
        self.settings_page = settings_page
        self.deck_serial_number = deck_serial_number

        # Apply-handler ID, or None while disconnected.
        # Tracking keeps connect and disconnect idempotent.
        self._apply_handler_id: int | None = None

        # The switcher does not shorten a long label, so the row refuses what
        # the switcher cannot show. See DECK_NAME_MAX_LENGTH.
        self.set_max_length(DECK_NAME_MAX_LENGTH)

        self.connect_signal()

        self.load_default()
        self.connect("map", self.load_default)

    def connect_signal(self) -> None:
        if self._apply_handler_id is None:
            self._apply_handler_id = self.connect("apply", self.on_apply)

    def disconnect_signal(self) -> None:
        if self._apply_handler_id is not None:
            self.disconnect(self._apply_handler_id)
            self._apply_handler_id = None

    def deck_stack(self) -> Any:
        """Return this settings page's deck stack, or None when it is detached.
        A missing stack skips only the live retitle because the name is already saved."""
        stack_child = getattr(self.settings_page, "deck_stack_child", None)
        return getattr(stack_child, "deck_stack", None)

    def on_apply(self, _: Adw.EntryRow) -> None:
        name = self.get_text().strip()

        settings = gl.settings_manager.deck(self.deck_serial_number)
        settings.set_top_level_value("name", name)
        settings.save()

        # Show what was stored. Space the user typed around the name is not in
        # the file, and a row that keeps showing it describes the file wrongly.
        if name != self.get_text():
            self.set_text(name)

        deck_stack = self.deck_stack()
        if deck_stack is not None:
            deck_stack.refresh_page_title(self.settings_page.deck_controller)

    def load_default(self, *args: object) -> None:
        # Load without the handler so opening the page cannot write an unchanged name.
        # The read must not persist a value that the user did not choose.
        self.disconnect_signal()
        try:
            name = gl.settings_manager.deck(self.deck_serial_number).get("name")
            self.set_text(name if isinstance(name, str) else "")
        finally:
            # A read that returns early or raises must still leave the row
            # wired, or every later name change is dropped silently.
            self.connect_signal()


class Rotation(Adw.PreferencesRow):
    def __init__(self, settings_page: "DeckSettingsPage", deck_serial_number: str) -> None:
        super().__init__()
        self.settings_page = settings_page
        self.deck_serial_number = deck_serial_number

        # Active-handler ID, or None while disconnected.
        # Tracking keeps connect and disconnect idempotent.
        self._rotation_handler_id: int | None = None

        self.build()

        self.load_default()
        self.connect("map", self.load_default)

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.rotation_label = Gtk.Label(label=gl.lm.get("deck.deck-group.rotation"), hexpand=True, xalign=0)
        self.main_box.append(self.rotation_label)

        self.toggle_group = Adw.ToggleGroup()
        self.main_box.append(self.toggle_group)

        self.toggle_0 = Adw.Toggle(label="0°", name="0")
        self.toggle_group.add(self.toggle_0)

        self.toggle_90 = Adw.Toggle(label="90°", name="90")
        self.toggle_group.add(self.toggle_90)

        self.toggle_180 = Adw.Toggle(label="180°", name="180")
        self.toggle_group.add(self.toggle_180)

        self.toggle_270 = Adw.Toggle(label="270°", name="270")
        self.toggle_group.add(self.toggle_270)


        self.connect_signal()

    def connect_signal(self) -> None:
        if self._rotation_handler_id is None:
            self._rotation_handler_id = self.toggle_group.connect("notify::active", self.on_value_changed)

    def disconnect_signal(self) -> None:
        if self._rotation_handler_id is not None:
            self.toggle_group.disconnect(self._rotation_handler_id)
            self._rotation_handler_id = None

    def on_value_changed(self, _: Adw.ToggleGroup, __: GObject.ParamSpec) -> None:
        GLib.idle_add(self.on_value_changed_idle)

    def on_value_changed_idle(self) -> None:
        active_name = self.toggle_group.get_active_name()
        if active_name is None:
            # No button of the group is active, so there is no rotation to
            # read and nothing to save.
            return
        rot = int(active_name)

        deck_settings = gl.settings_manager.deck(self.deck_serial_number)
        deck_settings.set_top_level_value("rotation", rot)
        deck_settings.save()

        self.settings_page.deck_controller.set_rotation(rot)

    def load_default(self, *args: object) -> None:
        # Keep the handler off across the read and widget update.
        # Opening the page must not save and apply an unchanged rotation.
        self.disconnect_signal()
        try:
            rot = gl.settings_manager.deck(self.deck_serial_number).get("rotation")
            self.toggle_group.set_active_name(str(rot))
        finally:
            # A read that returns early or raises must still leave the group
            # wired, or every later rotation choice is dropped silently.
            self.connect_signal()


class Brightness(LazyMapTasks, Adw.PreferencesRow):
    def __init__(self, settings_page: "DeckSettingsPage", deck_serial_number: str) -> None:
        super().__init__()
        self.settings_page = settings_page
        self.deck_serial_number = deck_serial_number

        # Value-changed handler ID, or None while disconnected.
        # Tracking keeps connect and disconnect idempotent.
        self._scale_handler_id: int | None = None

        self.build()

        """
        To save performance and memory, we only load the thumbnail when the user sees the row
        """
        self.on_map_tasks = []
        self.connect("map", self.on_map)

        # Keep one handler; construction defers load_default until the row maps.
        # The tracked ID makes an earlier connection a no-op.
        self.load_default()
        self.connect_signal()

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label=gl.lm.get("deck.deck-group.brightness"), hexpand=True, xalign=0)
        self.main_box.append(self.label)

        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, min=0, max=100, step=1)
        self.scale.set_draw_value(True)
        self.main_box.append(self.scale)

    def on_value_changed(self, scale: Gtk.Scale) -> None:
        GLib.idle_add(self.on_value_changed_idle, scale)

    def on_value_changed_idle(self, scale: Gtk.Scale) -> None:
        value = round(scale.get_value())

        # Update and save brightness in deck settings
        deck_settings = gl.settings_manager.deck(self.deck_serial_number)
        deck_settings.set_section_value("brightness", "value", value)
        deck_settings.save()

        # Check if brightness is overwritten by the current page (there may
        # be no active page, e.g. right after connect or with zero pages)
        active_page = self.settings_page.deck_controller.active_page
        page_dict = active_page.dict if active_page is not None else {}
        overwrite = page_dict.get("settings", {}).get("brightness", {}).get("overwrite", False)

        # Apply brightness if not overwritten
        if not overwrite:
            self.settings_page.deck_controller.set_brightness(value)

    def load_default(self) -> None:
        if not self.get_mapped():
            self.on_map_tasks.clear()
            self.on_map_tasks.append(lambda: self.load_default())
            return

        # Load with the handler off so opening the page does not persist a missing default.
        # It must not send an unchanged brightness to the physical deck.
        self.disconnect_signal()
        try:
            self.scale.set_value(gl.settings_manager.deck(self.deck_serial_number).get("brightness", "value"))
        finally:
            # A read that returns early or raises must still leave the scale
            # wired, or every later brightness change is dropped silently.
            self.connect_signal()

    def connect_signal(self) -> None:
        if self._scale_handler_id is None:
            self._scale_handler_id = self.scale.connect("value-changed", self.on_value_changed)

    def disconnect_signal(self) -> None:
        if self._scale_handler_id is not None:
            self.scale.disconnect(self._scale_handler_id)
            self._scale_handler_id = None


class Saturation(LazyMapTasks, Adw.PreferencesRow):
    """Set the per-deck PIL ImageEnhance.Color factor; 1.0 changes nothing."""
    # Saturation is read at load and cache-build time, so changes reload the page.
    # Static media updates immediately; video cache rebuilds on its next playthrough.
    def __init__(self, settings_page: "DeckSettingsPage", deck_serial_number: str) -> None:
        super().__init__()
        self.settings_page = settings_page
        self.deck_serial_number = deck_serial_number

        # Value-changed handler ID, or None while disconnected.
        # Tracking keeps connect and disconnect idempotent.
        self._scale_handler_id: int | None = None

        self.build()

        self.on_map_tasks = []
        self.connect("map", self.on_map)

        # The pending id of the trailing debounce below, None between runs.
        self._apply_source: int | None = None

        self.load_default()  # defers at construction; see Brightness above
        self.connect_signal()

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label=gl.lm.get("deck.deck-group.saturation"), hexpand=True, xalign=0)
        self.main_box.append(self.label)

        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, min=1.0, max=1.5, step=0.05)
        self.scale.set_draw_value(True)
        self.scale.set_digits(2)
        self.main_box.append(self.scale)

    def on_value_changed(self, scale: Gtk.Scale) -> None:
        # Debounce drag updates for 300 ms because each apply reloads the page.
        # A video background also rebuilds its cache.
        if self._apply_source is not None:
            GLib.source_remove(self._apply_source)
        self._apply_source = GLib.timeout_add(300, self._apply_value)

    def _apply_value(self) -> bool:
        self._apply_source = None
        value = round(self.scale.get_value(), 2)

        # Persist the setting, refresh the controller cache, and reload the active page.
        self.settings_page.deck_controller.set_display_saturation(value)
        return GLib.SOURCE_REMOVE

    def load_default(self) -> None:
        if not self.get_mapped():
            self.on_map_tasks.clear()
            self.on_map_tasks.append(lambda: self.load_default())
            return

        # Load with the handler off so opening the page does not write the file.
        # It must not reload the page for an unchanged factor.
        self.disconnect_signal()
        try:
            self.scale.set_value(gl.settings_manager.deck(self.deck_serial_number).get("display", "saturation"))
        finally:
            # A read that returns early or raises must still leave the scale
            # wired, or every later saturation change is dropped silently.
            self.connect_signal()

    def connect_signal(self) -> None:
        if self._scale_handler_id is None:
            self._scale_handler_id = self.scale.connect("value-changed", self.on_value_changed)

    def disconnect_signal(self) -> None:
        if self._scale_handler_id is not None:
            self.scale.disconnect(self._scale_handler_id)
            self._scale_handler_id = None


class Screensaver(LazyMapTasks, Adw.PreferencesRow):
    def __init__(self, settings_page: "DeckSettingsPage", deck_serial_number: str) -> None:
        super().__init__()
        self.settings_page = settings_page
        self.deck_serial_number = deck_serial_number

        # Handler ID by widget key, absent while disconnected.
        # Tracking keeps connect and disconnect idempotent.
        self._handler_ids: dict[str, int] = {}

        self.build()

        """
        To save performance and memory, we only load the thumbnail when the user sees the row
        """
        self.on_map_tasks = []
        self.connect("map", self.on_map)

        self.load_defaults()

    def _signal_bindings(self) -> list[tuple[str, GObject.Object, str, Callable[..., Any]]]:
        return [
            ("enable", self.enable_switch, "state-set", self.on_toggle_enable),
            ("time", self.time_spinner, "value-changed", self.on_change_time),
            ("media", self.media_selector_button, "clicked", self.on_choose_image),
            ("loop", self.loop_switch, "state-set", self.on_toggle_loop),
            ("fps", self.fps_spinner, "value-changed", self.on_change_fps),
            ("brightness", self.scale, "value-changed", self.on_change_brightness),
        ]

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.enable_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        self.main_box.append(self.enable_box)

        self.enable_label = Gtk.Label(label=gl.lm.get("deck.deck-group.enable-screensaver"), hexpand=True, xalign=0)
        self.enable_box.append(self.enable_label)

        self.enable_switch = Gtk.Switch()
        self.enable_box.append(self.enable_switch)

        self.config_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, visible=False)
        self.main_box.append(self.config_box)

        self.config_box.append(Gtk.Separator(hexpand=True, margin_top=10, margin_bottom=10))

        self.time_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        self.config_box.append(self.time_box)

        self.time_label = Gtk.Label(label=gl.lm.get("screensaver-delay"), hexpand=True, xalign=0)
        self.time_box.append(self.time_label)

        self.time_spinner = Gtk.SpinButton.new_with_range(1, 24*60, 1)
        self.time_box.append(self.time_spinner)

        self.media_selector_label = Gtk.Label(label=gl.lm.get("deck.deck-group.media-to-show"), hexpand=True, xalign=0)
        self.config_box.append(self.media_selector_label)

        self.media_selector_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.CENTER)
        self.config_box.append(self.media_selector_box)

        self.media_selector_button = Gtk.Button(label=gl.lm.get("deck.deck-group.media-select-label"), css_classes=["page-settings-media-selector"])
        self.media_selector_box.append(self.media_selector_button)

        self.progress_bar = Gtk.ProgressBar(hexpand=True, margin_top=10, text=gl.lm.get("background.processing"), fraction=0, show_text=True, visible=False)
        self.config_box.append(self.progress_bar)

        self.media_selector_image = Gtk.Image() # Will be bound to the button by self.set_thumbnail()

        self.loop_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, margin_bottom=15)
        self.config_box.append(self.loop_box)

        self.loop_label = Gtk.Label(label=gl.lm.get("deck.deck-group.media-loop"), hexpand=True, xalign=0)
        self.loop_box.append(self.loop_label)

        self.loop_switch = Gtk.Switch()
        self.loop_box.append(self.loop_switch)

        self.fps_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        self.config_box.append(self.fps_box)

        self.fps_label = Gtk.Label(label=gl.lm.get("deck.deck-group.media-fps"), hexpand=True, xalign=0)
        self.fps_box.append(self.fps_label)

        self.fps_spinner = Gtk.SpinButton.new_with_range(1, 30, 1)
        self.fps_box.append(self.fps_spinner)

        self.brightness_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.config_box.append(self.brightness_box)

        self.brightness_label = Gtk.Label(label=gl.lm.get("deck.deck-group.brightness"), hexpand=True, xalign=0)
        self.brightness_box.append(self.brightness_label)

        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, min=0, max=100, step=1)
        self.brightness_box.append(self.scale)

        self.connect_signals()

    def connect_signals(self) -> None:
        for key, widget, signal, callback in self._signal_bindings():
            if self._handler_ids.get(key) is None:
                self._handler_ids[key] = widget.connect(signal, callback)

    def disconnect_signals(self) -> None:
        for key, widget, _signal, _callback in self._signal_bindings():
            handler_id = self._handler_ids.pop(key, None)
            if handler_id is not None:
                widget.disconnect(handler_id)

    def load_defaults(self) -> None:
        self.disconnect_signals()
        try:
            # Read one section without persisting missing schema defaults.
            # Opening a settings page must not pin current defaults to the deck.
            config = gl.settings_manager.deck(self.deck_serial_number).section("screensaver")

            self.enable_switch.set_active(config["enable"])
            self.config_box.set_visible(config["enable"])
            self.time_spinner.set_value(config["time-delay"])
            self.loop_switch.set_active(config["loop"])
            self.fps_spinner.set_value(config["fps"])
            self.scale.set_value(config["brightness"])

            path = config["media-path"]
            if path is not None:
                if os.path.isfile(path):
                    self.set_thumbnail(path)
        finally:
            # A read that returns early or raises must still leave the widgets
            # wired, or every later change to this row is dropped silently.
            self.connect_signals()

    def page_overwrites_screensaver(self) -> bool:
        # Missing page or missing "screensaver"/"overwrite" keys mean
        # "not overwritten".
        active_page = self.settings_page.deck_controller.active_page
        if active_page is None:
            return False
        return bool(active_page.dict.get("screensaver", {}).get("overwrite", False))

    def on_toggle_enable(self, toggle_switch: Gtk.Switch, state: bool) -> None:
        config = gl.settings_manager.deck(self.deck_serial_number)
        config.set_section_value("screensaver", "enable", state)
        # Save
        config.save()
        # Update enable if not overwritten by the active page
        if not self.page_overwrites_screensaver():
            self.settings_page.deck_controller.screen_saver.set_enable(state)

        self.config_box.set_visible(state)

    def on_toggle_loop(self, toggle_switch: Gtk.Switch, state: bool) -> None:
        config = gl.settings_manager.deck(self.deck_serial_number)
        config.set_section_value("screensaver", "loop", state)
        # Save
        config.save()

        # Update loop if not overwritten by the active page
        if not self.page_overwrites_screensaver():
            self.settings_page.deck_controller.screen_saver.set_loop(state)

    def on_change_fps(self, spinner: Gtk.SpinButton) -> None:
        config = gl.settings_manager.deck(self.deck_serial_number)
        config.set_section_value("screensaver", "fps", spinner.get_value_as_int())
        # Save
        config.save()
        # Update fps if not overwritten by the active page
        if not self.page_overwrites_screensaver():
            self.settings_page.deck_controller.screen_saver.set_fps(spinner.get_value_as_int())

    def on_change_time(self, spinner: Gtk.SpinButton) -> None:
        config = gl.settings_manager.deck(self.deck_serial_number)
        config.set_section_value("screensaver", "time-delay", round(spinner.get_value_as_int()))
        # Save
        config.save()
        # Update time if not overwritten by the active page
        if not self.page_overwrites_screensaver():
            self.settings_page.deck_controller.screen_saver.set_time(round(spinner.get_value_as_int()))

    def on_change_brightness(self, scale: Gtk.Scale) -> None:
        config = gl.settings_manager.deck(self.deck_serial_number)
        config.set_section_value("screensaver", "brightness", scale.get_value())
        # Save
        config.save()
        # Update brightness if not overwritten by the active page
        if not self.page_overwrites_screensaver():
            self.settings_page.deck_controller.screen_saver.set_brightness(scale.get_value())

    def set_thumbnail(self, file_path: "str | None") -> None:
        if file_path is None:
            return
        if not os.path.isfile(file_path):
            return
        image = gl.media_manager.get_thumbnail(file_path)
        pixbuf = image2pixbuf(image)
        self.media_selector_image.set_from_pixbuf(pixbuf)
        self.media_selector_button.set_child(self.media_selector_image)

    def on_choose_image(self, button: Gtk.Button) -> None:
        media_path = gl.settings_manager.deck(self.deck_serial_number).get("screensaver", "media-path")

        services.require_app().let_user_select_asset(default_path=media_path, callback_func=self.update_image)

    def update_image(self, image_path: "str | None") -> None:
        self.set_thumbnail(image_path)
        settings = gl.settings_manager.deck(self.deck_serial_number)
        settings.set_section_value("screensaver", "media-path", image_path)
        settings.save()

        deck_controller = self.settings_page.deck_controller
        # A deck can have no active page just after connection or when it has zero pages.
        # Do not call load_screensaver without a page because it reads page.dict.
        if deck_controller.active_page is not None:
            deck_controller.load_screensaver(deck_controller.active_page)
