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
from src.backend.DeckManagement.deck_controller.background_media import background_canvas_size
from src.backend.DeckManagement.deck_controller.viewport import (
    is_default_view, media_entries, normalize_view,
)
from src.windows.mainWindow.elements.ViewportDialog import ViewportDialog
from src.windows.mainWindow.lazy_map import LazyMapTasks


def _view_as_setting(view: "tuple[float, float, float]") -> "dict[str, float] | None":
    """The stored shape of a view: a dict, or None for the default view so
    an untouched background keeps its pre-view settings file."""
    if is_default_view(view):
        return None
    return {"x": view[0], "y": view[1], "scale": view[2]}


def _slideshow_summary_text(image_count: int) -> str:
    """The label under the slideshow controls. Two or more images make a
    rotation; fewer means the single-image background is in effect. A free
    function, not a method, so the settings-page test can drive load_defaults
    without binding it onto its row stub.

    The label is cosmetic, so it reads empty when no locale manager is
    installed. Only the settings-row test reaches that state, because it drives
    load_defaults without the build() that reads every other localized string.
    """
    lm = gl.lm
    if lm is None:
        return ""
    if image_count >= 2:
        return lm.get("deck.background-group.slideshow-count").format(count=image_count)
    return lm.get("deck.background-group.slideshow-empty")


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

        # Pan and zoom the visible region of the selected media. A plain
        # clicked handler, outside the signal harness: load_defaults writes
        # no state into a button, so there is nothing to mute on reload.
        self.adjust_view_button = Gtk.Button(label=gl.lm.get("deck.background-group.adjust-view"),
                                             margin_top=10, halign=Gtk.Align.CENTER)
        self.adjust_view_button.connect("clicked", self.on_adjust_view)
        self.media_selector.append(self.adjust_view_button)

        # Slideshow controls. Add appends an image to the rotation, seeding the
        # list from the single media-path so the first added image joins the
        # existing background. Clear drops the list back to the single image.
        self.slideshow_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.CENTER,
                                         spacing=10, margin_top=10)
        self.config_box.append(self.slideshow_buttons)

        self.add_image_button = Gtk.Button(label=gl.lm.get("deck.background-group.slideshow-add"))
        self.slideshow_buttons.append(self.add_image_button)

        self.clear_slideshow_button = Gtk.Button(label=gl.lm.get("deck.background-group.slideshow-clear"))
        self.slideshow_buttons.append(self.clear_slideshow_button)

        self.slideshow_count_label = Gtk.Label(label=_slideshow_summary_text(0), halign=Gtk.Align.CENTER,
                                               margin_top=5, css_classes=["dim-label"])
        self.config_box.append(self.slideshow_count_label)

        self.interval_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, margin_top=15)
        self.config_box.append(self.interval_box)

        self.interval_label = Gtk.Label(label=gl.lm.get("deck.background-group.slideshow-interval"),
                                        hexpand=True, xalign=0)
        self.interval_box.append(self.interval_label)

        self.interval_spinner = Gtk.SpinButton.new_with_range(1, 3600, 1)
        self.interval_box.append(self.interval_spinner)

        self.shuffle_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, margin_top=15)
        self.config_box.append(self.shuffle_box)

        self.shuffle_label = Gtk.Label(label=gl.lm.get("deck.background-group.slideshow-shuffle"),
                                       hexpand=True, xalign=0)
        self.shuffle_box.append(self.shuffle_label)

        self.shuffle_switch = Gtk.Switch()
        self.shuffle_box.append(self.shuffle_switch)

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
            ("add-image", self.add_image_button, "clicked", self.on_add_image),
            ("clear-slideshow", self.clear_slideshow_button, "clicked", self.on_clear_slideshow),
            ("interval", self.interval_spinner, "value-changed", self.on_change_interval),
            ("shuffle", self.shuffle_switch, "state-set", self.on_toggle_shuffle),
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
            self.interval_spinner.set_value(config["slideshow-interval"])
            self.shuffle_switch.set_active(config["slideshow-order"] == "shuffle")
            image_paths = [p for p, _view in media_entries(config["media-paths"])]
            self.slideshow_count_label.set_label(_slideshow_summary_text(len(image_paths)))
            # Show the first slideshow image when no single media-path is set,
            # so a slideshow-only background still has a thumbnail.
            thumbnail = config["media-path"] or (image_paths[0] if image_paths else None)
            self.set_thumbnail(thumbnail)
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

    def _reload_background(self) -> None:
        controller = self.settings_page.deck_controller
        page = controller.active_page
        if page is not None:
            controller.load_background(page=page)

    def on_adjust_view(self, button: Gtk.Button) -> None:
        settings = gl.settings_manager.deck(self.deck_serial_number)
        config = settings.section("background")
        entries = media_entries(config["media-paths"])
        if len(entries) < 2:
            # No rotation: the single media-path with its own view is the one
            # entry. With nothing selected there is nothing to adjust.
            single = config["media-path"]
            if not single:
                return
            entries = [(single, normalize_view(config["view"]))]

        controller = self.settings_page.deck_controller
        dialog = ViewportDialog(
            entries,
            canvas_size=lambda: background_canvas_size(
                controller, controller.background.extend_to_touchscreen),
            on_live=self.on_view_live,
            on_commit=self.on_view_commit,
        )
        dialog.connect("closed", lambda d: d.close_cleanly())
        dialog.present(self)

    def on_view_live(self, path: str, view: "tuple[float, float, float]") -> None:
        """The drag preview: swap the showing image's view in place. A video
        or a slideshow image that is not the current frame skips the live
        push; the commit reloads it instead."""
        controller = self.settings_page.deck_controller
        image = controller.background.image
        if image is not None and image.path == path:
            controller.background.update_view(view)

    def on_view_commit(self, path: str, view: "tuple[float, float, float]") -> None:
        settings = gl.settings_manager.deck(self.deck_serial_number)
        entries = media_entries(settings.get("background", "media-paths"))
        if len(entries) >= 2:
            # Rewrite the list in order. A default-view entry stays a plain
            # string, so an untouched slideshow keeps its pre-view shape.
            rewritten: "list[Any]" = []
            for entry_path, entry_view in entries:
                use = view if entry_path == path else entry_view
                as_dict = _view_as_setting(use)
                rewritten.append(entry_path if as_dict is None
                                 else {"path": entry_path, "view": as_dict})
            settings.set("background", "media-paths", rewritten)
        else:
            settings.set("background", "view", _view_as_setting(view))
        settings.save()

        # A still that is showing already carries the new view through the
        # live path. Anything else, a video or GIF whose view bakes into its
        # frame cache, or a slideshow frame not on screen, needs the reload.
        controller = self.settings_page.deck_controller
        image = controller.background.image
        applied_live = (image is not None and image.path == path
                        and controller.background.update_view(view))
        if not applied_live:
            self._reload_background()

    def on_add_image(self, button: Gtk.Button) -> None:
        media_path = gl.settings_manager.deck(self.deck_serial_number).get("background", "media-path")
        services.require_app().let_user_select_asset(default_path=media_path, callback_func=self.append_image)

    def append_image(self, file_path: "str | None") -> None:
        if not file_path:
            return
        settings = gl.settings_manager.deck(self.deck_serial_number)
        paths = list(settings.get("background", "media-paths") or [])
        if not paths:
            # Seed the rotation from the single background already set, so the
            # first added image joins it rather than starting a fresh list of
            # one that hides the existing image.
            current = settings.get("background", "media-path")
            if current:
                paths.append(current)
        paths.append(file_path)
        settings.set("background", "media-paths", paths)
        settings.save()

        image_count = len(media_entries(paths))
        self.slideshow_count_label.set_label(_slideshow_summary_text(image_count))
        self._reload_background()

    def on_clear_slideshow(self, button: Gtk.Button) -> None:
        settings = gl.settings_manager.deck(self.deck_serial_number)
        settings.set("background", "media-paths", [])
        settings.save()
        self.slideshow_count_label.set_label(_slideshow_summary_text(0))
        self._reload_background()

    def on_change_interval(self, spinner: Gtk.SpinButton) -> None:
        settings = gl.settings_manager.deck(self.deck_serial_number)
        settings.set("background", "slideshow-interval", spinner.get_value_as_int())
        settings.save()
        self._reload_background()

    def on_toggle_shuffle(self, toggle_switch: Gtk.Switch, state: bool) -> None:
        settings = gl.settings_manager.deck(self.deck_serial_number)
        settings.set("background", "slideshow-order", "shuffle" if state else "in-order")
        settings.save()
        self._reload_background()

    def set_thumbnail(self, file_path: "str | None") -> None:
        if not file_path:
            return
        if not os.path.isfile(file_path):
            return
        image = gl.media_manager.get_thumbnail(file_path)
        pixbuf = image2pixbuf(image)
        self.media_selector_image.set_from_pixbuf(pixbuf)
        self.media_selector_button.set_child(self.media_selector_image)
