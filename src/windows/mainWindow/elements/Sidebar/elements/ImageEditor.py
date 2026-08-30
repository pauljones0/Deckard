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
# Import gtk modules
import gi
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.windows.mainWindow.elements.Sidebar.Sidebar import Sidebar


from src.backend.DeckManagement.InputIdentifier import InputIdentifier

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

# Import Python modules

# Import globals
from src.backend import services

import globals as gl

# Import own modules
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout
from GtkHelper.GtkHelper import RevertButton


class ImageEditor(Gtk.Box):
    def __init__(self, sidebar: "Sidebar", **kwargs: Any) -> None:
        self.sidebar = sidebar
        super().__init__(**kwargs)
        self.build()

    def build(self) -> None:
        self.clamp = Adw.Clamp()
        self.append(self.clamp)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.clamp.set_child(self.main_box)

        self.image_group = ImageGroup(self.sidebar)
        self.main_box.append(self.image_group)

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.image_group.load_for_identifier(identifier, state)


class ImageGroup(Adw.PreferencesGroup):
    def __init__(self, sidebar: "Sidebar", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sidebar = sidebar

        self.build()

    def build(self) -> None:
        self.expander = Layout(self)
        self.add(self.expander)

        return

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.expander.load_for_identifier(identifier, state)


class Layout(Adw.ExpanderRow):
    def __init__(self, margin_group: "ImageGroup") -> None:
        super().__init__(title=gl.lm.get("right-area.image-editor.layout.header"), subtitle=gl.lm.get("right-area.image-editor.layout.subtitle"))
        self.margin_group = margin_group
        self.identifier: InputIdentifier = None  # ty: ignore[invalid-assignment]  # late-init: load_for_identifier
        self.active_state: int = None  # ty: ignore[invalid-assignment]  # late-init: load_for_identifier
        self.build()

    def build(self) -> None:
        self.size_row = SizeRow(sidebar=self.margin_group.sidebar)
        self.add_row(self.size_row)

        self.valign_row = ValignRow(sidebar=self.margin_group.sidebar)
        self.add_row(self.valign_row)

        self.halign_row = HalignRow(sidebar=self.margin_group.sidebar)
        self.add_row(self.halign_row)

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.active_identifier = identifier
        self.active_state = state

        self.size_row.load_for_identifier(identifier, state)
        self.valign_row.load_for_identifier(identifier, state)
        self.halign_row.load_for_identifier(identifier, state)


class SizeRow(Adw.PreferencesRow):
    def __init__(self, sidebar: "Sidebar", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.active_identifier: InputIdentifier = None  # ty: ignore[invalid-assignment]  # late-init: load_for_identifier
        # Value-changed handler ID, or None while disconnected.
        # Tracking prevents duplicate connections and invalid disconnects.
        self._value_handler: int | None = None
        self.build()

        self.connect_signals()

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label=gl.lm.get("right-area.image-editor.layout.size.label"), hexpand=True, xalign=0)
        self.main_box.append(self.label)

        self.size_spinner = SpinButton(0, 200, 1)
        self.main_box.append(self.size_spinner)

        self.size_spinner.revert_button.connect("clicked", self.on_size_reset)

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.disconnect_signals()
        try:
            self.active_identifier = identifier
            self.active_state = state

            if gl.app is None:
                return
            controller = services.require_main_window().get_active_controller()
            if controller is None:
                return

            controller_input = controller.get_input(identifier)
            if controller_input is None:
                return
            use_page_properties = controller_input.get_active_state().layout_manager.get_use_page_layout_properties()
            self.size_spinner.revert_button.set_visible(use_page_properties.get("size", False))

            self.update_values()
        finally:
            # Reconnect after every early return or exception.
            # Otherwise the spinner cannot save again for the life of the window.
            self.connect_signals()

    def update_values(self, composed_label: ImageLayout | None = None) -> None:
        self.disconnect_signals()
        try:
            if composed_label is None:
                if gl.app is None:
                    return
                visible_child = services.require_main_window().leftArea.deck_stack.get_visible_child()
                if visible_child is None:
                    return
                controller = visible_child.deck_controller
                controller_input = controller.get_input(self.active_identifier)
                if controller_input is None:
                    return
                composed_label = controller_input.get_active_state().layout_manager.get_composed_layout()

            # ImageLayout.size is optional, and an unset one leaves the spinner
            # on the value it shows.
            size = composed_label.size
            if size is not None:
                self.size_spinner.button.set_value(size*100)
        finally:
            # Every arm above must reconnect. A return in between leaves the
            # spinner silently unable to save for the life of the window.
            self.connect_signals()

    def on_size_changed(self, widget: Gtk.SpinButton) -> None:
        active_page = services.require_main_window().get_active_page()
        if active_page is None:
            return
        active_page.set_media_size(identifier=self.active_identifier, state=self.active_state, size=widget.get_value()/100)

        self.size_spinner.revert_button.set_visible(True)

    def on_size_reset(self, widget: Gtk.Button) -> None:
        active_page = services.require_main_window().get_active_page()
        if active_page is None:
            return
        active_page.set_media_size(identifier=self.active_identifier, state=self.active_state, size=None)

        self.size_spinner.revert_button.set_visible(False)
        self.update_values()

    def connect_signals(self) -> None:
        if self._value_handler is None:
            self._value_handler = self.size_spinner.button.connect("value-changed", self.on_size_changed)

    def disconnect_signals(self) -> None:
        if self._value_handler is not None:
            self.size_spinner.button.disconnect(self._value_handler)
            self._value_handler = None


class AlignmentRow(Adw.PreferencesRow):
    def __init__(self, sidebar: "Sidebar", label_text: str, property_name: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.property_name = property_name
        self.active_identifier: InputIdentifier = None  # ty: ignore[invalid-assignment]  # late-init: load_for_identifier
        self.active_state: int = None  # ty: ignore[invalid-assignment]  # late-init: load_for_identifier
        # Value-changed handler ID, or None while disconnected.
        # Tracking prevents an invalid disconnect after a transient load failure.
        self._value_handler: int | None = None
        self.build(label_text)

        self.connect_signals()

    def build(self, label_text: str) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label=label_text, hexpand=True, xalign=0)
        self.main_box.append(self.label)

        self.alignment_spinner = SpinButton(-1, 1, 0.1)
        self.main_box.append(self.alignment_spinner)

        self.alignment_spinner.revert_button.connect("clicked", self.on_alignment_reset)

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.active_identifier = identifier
        self.active_state = state
        self.disconnect_signals()
        try:
            if gl.app is None:
                return
            controller = services.require_main_window().get_active_controller()
            if controller is None:
                return

            controller_input = controller.get_input(identifier)
            if controller_input is None:
                return
            use_page_properties = controller_input.get_active_state().layout_manager.get_use_page_layout_properties()
            self.alignment_spinner.revert_button.set_visible(use_page_properties.get(self.property_name, False))

            self.update_values()
        finally:
            # A lookup that returns early must still leave the spinner wired, or
            # every later edit is dropped silently.
            self.connect_signals()

    def update_values(self, composed_label: ImageLayout | None = None) -> None:
        self.disconnect_signals()
        try:
            if composed_label is None:
                if gl.app is None:
                    return
                controller = services.require_main_window().get_active_controller()
                if controller is None:
                    return
                controller_input = controller.get_input(self.active_identifier)
                if controller_input is None:
                    return
                composed_label = controller_input.get_active_state().layout_manager.get_composed_layout()

            self.alignment_spinner.button.set_value(getattr(composed_label, self.property_name))
        finally:
            self.connect_signals()

    def on_alignment_changed(self, widget: Gtk.SpinButton) -> None:
        active_page = services.require_main_window().get_active_page()

        page_method = getattr(active_page, f"set_media_{self.property_name}")
        page_method(self.active_identifier, self.active_state, widget.get_value())

        self.alignment_spinner.revert_button.set_visible(True)

    def on_alignment_reset(self, widget: Gtk.Button) -> None:
        active_page = services.require_main_window().get_active_page()

        page_method = getattr(active_page, f"set_media_{self.property_name}")
        page_method(self.active_identifier, self.active_state, None)

        self.alignment_spinner.revert_button.set_visible(False)
        self.update_values()

    def connect_signals(self) -> None:
        if self._value_handler is None:
            self._value_handler = self.alignment_spinner.button.connect("value-changed", self.on_alignment_changed)

    def disconnect_signals(self) -> None:
        if self._value_handler is not None:
            self.alignment_spinner.button.disconnect(self._value_handler)
            self._value_handler = None

class ValignRow(AlignmentRow):
    def __init__(self, sidebar: "Sidebar", **kwargs: Any) -> None:
        super().__init__(sidebar, label_text=gl.lm.get("right-area.image-editor.layout.valign.label"), property_name="valign", **kwargs)

class HalignRow(AlignmentRow):
    def __init__(self, sidebar: "Sidebar", **kwargs: Any) -> None:
        super().__init__(sidebar, label_text=gl.lm.get("right-area.image-editor.layout.halign.label"), property_name="halign", **kwargs)


class SpinButton(Gtk.Box):
    def __init__(self, start: float, end: float, step: float, **kwargs: Any) -> None:
        super().__init__(css_classes=["linked"], **kwargs)

        self.button = Gtk.SpinButton.new_with_range(start, end, step)
        self.revert_button = RevertButton()

        self.append(self.button)
        self.append(self.revert_button)
