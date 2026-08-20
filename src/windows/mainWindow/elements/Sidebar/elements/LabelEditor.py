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

from src.backend.DeckManagement.HelperMethods import color_values_to_gdk, gdk_color_to_values, get_pango_font_description, get_values_from_pango_font_description
from src.backend.DeckManagement.InputIdentifier import InputIdentifier

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.mainWindow.elements.Sidebar.Sidebar import Sidebar
    from src.backend.PageManagement.Page import Page

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

# Import Python modules
from loguru import logger as log

# Import own modules
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from GtkHelper.GtkHelper import RevertButton

# Import globals
from src.backend import services

import globals as gl
from typing import Any

class LabelEditor(Gtk.Box):
    def __init__(self, sidebar: "Sidebar", **kwargs: Any) -> None:
        self.sidebar = sidebar
        super().__init__(**kwargs)
        self.build()

    def build(self) -> None:
        self.clamp = Adw.Clamp()
        self.append(self.clamp)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.clamp.set_child(self.main_box)

        self.label_group = LabelGroup(self.sidebar)
        self.main_box.append(self.label_group)

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.label_group.load_for_identifier(identifier, state)


class LabelGroup(Adw.PreferencesGroup):
    def __init__(self, sidebar: "Sidebar", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sidebar = sidebar

        self.build()

    def build(self) -> None:
        self.expander = LabelExpanderRow(self)
        self.add(self.expander)

        return

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.expander.load_for_identifier(identifier, state)

class LabelExpanderRow(Adw.ExpanderRow):
    def __init__(self, label_group: LabelGroup) -> None:
        super().__init__(title=gl.lm.get("label-editor-header"), subtitle=gl.lm.get("label-editor-expander-subtitle"))
        self.label_group = label_group
        # Unset until load_for_identifier binds this row to an input.
        self.active_identifier: InputIdentifier | None = None
        self.build()

    def build(self) -> None:
        self.top_row = LabelRow(gl.lm.get("label-editor-top-name"), 0, self.label_group.sidebar, key_name="top")
        self.center_row = LabelRow(gl.lm.get("label-editor-center-name"), 1, self.label_group.sidebar, key_name="center")
        self.bottom_row = LabelRow(gl.lm.get("label-editor-bottom-name"), 2, self.label_group.sidebar, key_name="bottom")

        self.add_row(self.top_row)
        self.add_row(self.center_row)
        self.add_row(self.bottom_row)

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        if not isinstance(identifier, InputIdentifier):
            raise TypeError
        self.active_identifier = identifier

        self.top_row.load_for_identifier(identifier, state)
        self.center_row.load_for_identifier(identifier, state)
        self.bottom_row.load_for_identifier(identifier, state)

class LabelRow(Adw.PreferencesRow):
    def __init__(self, label_text: str, label_index: int, sidebar: "Sidebar", key_name: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.label_text = label_text
        self.sidebar = sidebar
        # Unset until load_for_identifier binds this row to an input.
        self.active_identifier: InputIdentifier | None = None
        self.state: int = 0
        self.label_index = label_index
        self.key_name = key_name
        self.build()

        self.lock = threading.Lock()

        self.block = False

        # Connect set signals
        self.connect_signals()

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label=self.label_text, xalign=0, margin_bottom=3, css_classes=["bold"])
        self.main_box.append(self.label)

        self.controlled_by_action_label = Gtk.Label(label=gl.lm.get("label-editor-warning-controlled-by-action"), css_classes=["bold", "red-color"], xalign=0,
                                                    margin_bottom=3, visible=False)
        self.main_box.append(self.controlled_by_action_label)

        self.text_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        self.main_box.append(self.text_box)

        self.text_entry = TextEntry()
        self.text_box.append(self.text_entry)

        self.color_chooser_button = ColorChooserButton()
        self.text_box.append(self.color_chooser_button)

        self.font_chooser_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, margin_top=6)
        self.main_box.append(self.font_chooser_box)

        self.font_chooser_label = Gtk.Label(label=gl.lm.get("label-editor-font-chooser-label"), xalign=0, hexpand=True, margin_start=2)
        self.font_chooser_box.append(self.font_chooser_label)

        self.font_chooser_button = FontChooserButton()
        self.font_chooser_box.append(self.font_chooser_button)

        # Alignment box
        self.alignment_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, margin_top=6)
        self.main_box.append(self.alignment_box)

        self.alignment_label = Gtk.Label(label=gl.lm.get("label-editor-alignment-label", "Align:"), xalign=0, hexpand=True, margin_start=2)
        self.alignment_box.append(self.alignment_label)

        self.alignment_buttons = AlignmentButtons()
        self.alignment_box.append(self.alignment_buttons)

        self.outline_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, margin_top=6)
        self.main_box.append(self.outline_box)

        self.outline_width_label = Gtk.Label(label=gl.lm.get("label-editor-outline-width-label"), xalign=0, hexpand=True, margin_start=2)
        self.outline_width_label.set_hexpand(False)
        self.outline_width_label.set_margin_end(5)
        self.outline_box.append(self.outline_width_label)

        self.outline_width = SpinButton(0, 10, 1)
        self.outline_width.set_hexpand(False)
        self.outline_box.append(self.outline_width)

        self.outline_color_label = Gtk.Label(label=gl.lm.get("label-editor-outline-color-label"), xalign=0, hexpand=True, margin_start=2)
        self.outline_color_label.set_hexpand(True)
        self.outline_color_label.set_margin_end(5)
        self.outline_color_label.set_halign(Gtk.Align.END)
        self.outline_box.append(self.outline_color_label)

        self.outline_color_chooser_button = ColorChooserButton()
        self.outline_color_chooser_button.set_hexpand(False)
        self.outline_box.append(self.outline_color_chooser_button)

        ## Connect reset buttons
        self.text_entry.revert_button.connect("clicked", self.on_reset_text)
        self.color_chooser_button.revert_button.connect("clicked", self.on_reset_color)
        self.font_chooser_button.revert_button.connect("clicked", self.on_reset_font)
        self.outline_width.revert_button.connect("clicked", self.on_reset_outline_width)
        self.outline_color_chooser_button.revert_button.connect("clicked", self.on_reset_outline_color)
        self.alignment_buttons.revert_button.connect("clicked", self.on_reset_alignment)

    def connect_signals(self) -> None:
        self.text_entry.entry.connect("changed", self.on_change_text)
        self.color_chooser_button.button.connect("color-set", self.on_change_color)
        self.font_chooser_button.button.connect("font-set", self.on_change_font)
        self.outline_width.button.connect("value-changed", self.on_change_outline_width)
        self.outline_color_chooser_button.button.connect("color-set", self.on_change_outline_color)
        self.alignment_buttons.left_button.connect("toggled", self.on_change_alignment)
        self.alignment_buttons.center_button.connect("toggled", self.on_change_alignment)
        self.alignment_buttons.right_button.connect("toggled", self.on_change_alignment)

    def disconnect_signals(self) -> None:
        try:
            self.text_entry.entry.disconnect_by_func(self.on_change_text)
        except Exception as e:
            log.error(f"Failed to disconnect signals. Error: {e}")

        try:
            self.color_chooser_button.button.disconnect_by_func(self.on_change_color)
        except Exception as e:
            log.error(f"Failed to disconnect signals. Error: {e}")

        try:
            self.font_chooser_button.button.disconnect_by_func(self.on_change_font)
        except Exception as e:
            log.error(f"Failed to disconnect signals. Error: {e}")

        try:
            self.outline_width.button.disconnect_by_func(self.on_change_outline_width)
        except Exception as e:
            log.error(f"Failed to disconnect signals. Error: {e}")

        try:
            self.outline_color_chooser_button.button.disconnect_by_func(self.on_change_outline_color)
        except Exception as e:
            log.error(f"Failed to disconnect signals. Error: {e}")

        try:
            self.alignment_buttons.left_button.disconnect_by_func(self.on_change_alignment)
            self.alignment_buttons.center_button.disconnect_by_func(self.on_change_alignment)
            self.alignment_buttons.right_button.disconnect_by_func(self.on_change_alignment)
        except Exception as e:
            log.error(f"Failed to disconnect signals. Error: {e}")

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        if not isinstance(identifier, InputIdentifier):
            raise ValueError
        self.active_identifier = identifier
        self.state = state

        if gl.app is None:
            return
        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return
        page = controller.active_page

        if page is None:
            #TODO: Show error
            return
        
        controller_input = controller.get_input(identifier)
        if controller_input is None:
            return
        use_page_label_properties = controller_input.get_active_state().label_manager.get_use_page_label_properties(position=self.key_name)

        ## Set visibility of revert buttons
        self.text_entry.revert_button.set_visible(use_page_label_properties.get("text", False))
        self.color_chooser_button.revert_button.set_visible(use_page_label_properties.get("color", False))
        self.outline_width.revert_button.set_visible(use_page_label_properties.get("outline_width", False))
        self.outline_color_chooser_button.revert_button.set_visible(use_page_label_properties.get("outline_color", False))
        self.alignment_buttons.revert_button.set_visible(use_page_label_properties.get("alignment", False))

        font_combined = use_page_label_properties.get("font-family", False) and use_page_label_properties.get("font-size", False)
        self.font_chooser_button.revert_button.set_visible(font_combined)

        # Set properties
        self.update_values()

    def update_values(self, composed_label: KeyLabel | None = None) -> None:
        with self.lock:
            self._update_values_locked(composed_label)

    def _update_values_locked(self, composed_label: KeyLabel | None = None) -> None:
        self.disconnect_signals()
        if composed_label is None:
            if gl.app is None:
                return
            controller = gl.app.main_win.get_active_controller()
            if controller is None:
                return
            if self.active_identifier is None:
                return
            controller_input = controller.get_input(self.active_identifier)
            if controller_input is None:
                return
            composed_label = controller_input.get_active_state().label_manager.get_composed_label(position=self.key_name)

        # Every KeyLabel property is optional, and an unset one means that
        # nothing is configured here, which for the text is the empty entry.
        text = composed_label.text or ""

        if self.text_entry.entry.get_text() != text:
            pos = self.text_entry.entry.get_position()
            
            self.text_entry.entry.set_text(text)

            pos = min(pos, len(text))
            self.text_entry.entry.set_position(pos)

        hide_details = text.strip() == ""
        self.font_chooser_box.set_visible(not hide_details)
        self.outline_box.set_visible(not hide_details)
        self.alignment_box.set_visible(not hide_details)

        self.set_color(composed_label.color)
        self.set_outline_width(composed_label.outline_width)
        self.set_outline_color(composed_label.outline_color)
        self.set_alignment(composed_label.alignment)

        # self.font_chooser_button.button.set_font_desc(Pango.FontDescription.from_string(f"{composed_label.font_name} {composed_label.style} {composed_label.font_size}px"))
        # A font description needs all four; an incompletely composed label
        # leaves the chooser showing whatever it had.
        font_name = composed_label.font_name
        font_size = composed_label.font_size
        style = composed_label.style
        font_weight = composed_label.font_weight
        if (font_name is not None and font_size is not None
                and style is not None and font_weight is not None):
            desc = get_pango_font_description(
                font_family=font_name,
                font_size=font_size,
                font_style=style,
                font_weight=font_weight
            )
            self.font_chooser_button.button.set_font_desc(desc)

        self.connect_signals()

    # None means the label sets no value for this property, so the widget
    # keeps its current value.
    def set_color(self, color_values: list[Any] | None) -> None:
        if color_values is None:
            return
        color = color_values_to_gdk(color_values)
        self.color_chooser_button.button.set_rgba(color)

    def set_outline_width(self, outline_width: int | None) -> None:
        if outline_width is None:
            return
        self.outline_width.button.set_value(outline_width)

    def set_outline_color(self, color_values: list[Any] | None) -> None:
        if color_values is None:
            return
        color = color_values_to_gdk(color_values)
        self.outline_color_chooser_button.button.set_rgba(color)

    def set_alignment(self, alignment: str | None) -> None:
        if alignment is None:
            return
        self.alignment_buttons.set_alignment(alignment)

    def _page_and_identifier(self) -> "tuple[Page, InputIdentifier] | None":
        """The page and the input this row writes to, or None when there is
        no pair to write.

        MainWindow.get_active_page answers None between the deck selection
        and the first page load, which its own docstring calls the normal
        state, and active_identifier stays None until load_for_identifier
        binds one. Every setter below keys its write by both, and Page keys
        its dict path off identifier.input_type, so neither may be None.
        """
        page = services.require_main_window().get_active_page()
        identifier = self.active_identifier
        if page is None or identifier is None:
            return None
        return page, identifier

    def on_change_color(self, _: Any) -> None:
        color = list(gdk_color_to_values(self.color_chooser_button.button.get_rgba()))

        target = self._page_and_identifier()
        if target is None:
            return
        active_page, identifier = target
        active_page.set_label_font_color(identifier=identifier, state=self.state, label_position=self.key_name, font_color=color)

        self.color_chooser_button.revert_button.set_visible(True)

    def on_change_outline_width(self, _: Any) -> None:
        width = int(self.outline_width.button.get_value())

        target = self._page_and_identifier()
        if target is None:
            return
        active_page, identifier = target
        active_page.set_label_outline_width(identifier=identifier, state=self.state, label_position=self.key_name, outline_width=width)

        self.outline_width.revert_button.set_visible(True)

    def on_change_outline_color(self, _: Any) -> None:
        color = list(gdk_color_to_values(self.outline_color_chooser_button.button.get_rgba()))

        target = self._page_and_identifier()
        if target is None:
            return
        active_page, identifier = target
        active_page.set_label_outline_color(identifier=identifier, state=self.state, label_position=self.key_name, outline_color=color)

        self.outline_color_chooser_button.revert_button.set_visible(True)

    def on_change_font(self, button: Gtk.Button) -> None:
        # font = self.font_chooser_button.button.get_font()
        # name, size = self.parse_font_description(font)

        font_desc = self.font_chooser_button.button.get_font_desc()
        if font_desc is None:
            # The chooser has no font selected, so there is nothing to apply.
            return
        name, size, weight, style = get_values_from_pango_font_description(font_desc)

        target = self._page_and_identifier()
        if target is None:
            return
        active_page, identifier = target
        active_page.set_label_font_family(identifier=identifier, state=self.state, label_position=self.key_name, font_family=name, update=False)
        active_page.set_label_font_style(identifier=identifier, state=self.state, label_position=self.key_name, font_style=style, update=False)
        active_page.set_label_font_size(identifier=identifier, state=self.state, label_position=self.key_name, font_size=size, update=False)
        active_page.set_label_font_weight(identifier=identifier, state=self.state, label_position=self.key_name, font_weight=weight, update=True)

        self.font_chooser_button.revert_button.set_visible(True)

    def on_reset_font(self, button: Gtk.Button) -> None:
        #FIXME: gets called multiple times
        target = self._page_and_identifier()
        if target is None:
            return
        active_page, identifier = target
        #TODO
        active_page.set_label_font_family(identifier=identifier, state=self.state, label_position=self.key_name, font_family=None, update=False)
        active_page.set_label_font_size(identifier=identifier, state=self.state, label_position=self.key_name, font_size=None, update=True)

        button.set_visible(False)

    def on_reset_text(self, button: Gtk.Button) -> None:
        target = self._page_and_identifier()
        if target is None:
            return
        active_page, identifier = target
        active_page.set_label_text(identifier=identifier, state=self.state, label_position=self.key_name, text=None)

        self.update_values()

        button.set_visible(False)

    def on_reset_color(self, button: Gtk.Button) -> None:
        target = self._page_and_identifier()
        if target is None:
            return
        active_page, identifier = target
        active_page.set_label_font_color(identifier=identifier, state=self.state, label_position=self.key_name, font_color=None)

        button.set_visible(False)

    def on_reset_outline_width(self, button: Gtk.Button) -> None:
        target = self._page_and_identifier()
        if target is None:
            return
        active_page, identifier = target
        active_page.set_label_outline_width(identifier=identifier, state=self.state, label_position=self.key_name, outline_width=None)

        button.set_visible(False)

    def on_reset_outline_color(self, button: Gtk.Button) -> None:
        target = self._page_and_identifier()
        if target is None:
            return
        active_page, identifier = target
        active_page.set_label_outline_color(identifier=identifier, state=self.state, label_position=self.key_name, outline_color=None)

        button.set_visible(False)

    def on_change_alignment(self, button: Gtk.ToggleButton) -> None:
        if not button.get_active():
            return  # Only respond to the button being activated

        alignment = self.alignment_buttons.get_alignment()

        target = self._page_and_identifier()
        if target is None:
            return
        active_page, identifier = target
        active_page.set_label_alignment(identifier=identifier, state=self.state, label_position=self.key_name, alignment=alignment)

        self.alignment_buttons.revert_button.set_visible(True)

    def on_reset_alignment(self, button: Gtk.Button) -> None:
        target = self._page_and_identifier()
        if target is None:
            return
        active_page, identifier = target
        active_page.set_label_alignment(identifier=identifier, state=self.state, label_position=self.key_name, alignment=None)

        button.set_visible(False)

    def on_change_text(self, entry: Gtk.Editable) -> None:
        text = entry.get_text()

        target = self._page_and_identifier()
        if target is None:
            return
        active_page, identifier = target
        active_page.set_label_text(identifier=identifier, state=self.state, label_position=self.key_name, text=text)

        self.text_entry.revert_button.set_visible(True)

        hide_details = text.strip() == ""
        self.font_chooser_box.set_visible(not hide_details)
        self.outline_box.set_visible(not hide_details)
        self.alignment_box.set_visible(not hide_details)


class TextEntry(Gtk.Box):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(css_classes=["linked"], margin_end=5,  **kwargs)

        self.entry = Gtk.Entry(hexpand=True,placeholder_text=gl.lm.get("label-editor-placeholder-text"))
        self.revert_button = RevertButton()

        self.append(self.entry)
        self.append(self.revert_button)

class ColorChooserButton(Gtk.Box):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(css_classes=["linked"], **kwargs)

        self.button = Gtk.ColorButton()
        self.revert_button = RevertButton()

        self.append(self.button)
        self.append(self.revert_button)

class FontChooserButton(Gtk.Box):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(css_classes=["linked"], **kwargs)

        self.button = Gtk.FontButton()
        self.revert_button = RevertButton()

        self.append(self.button)
        self.append(self.revert_button)


class SpinButton(Gtk.Box):
    def __init__(self, start: float, end: float, step: float, **kwargs: Any) -> None:
        super().__init__(css_classes=["linked"], **kwargs)

        self.button = Gtk.SpinButton.new_with_range(start, end, step)
        self.revert_button = RevertButton()

        self.append(self.button)
        self.append(self.revert_button)


class AlignmentButtons(Gtk.Box):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(css_classes=["linked"], **kwargs)

        self.left_button = Gtk.ToggleButton(icon_name="format-justify-left-symbolic", tooltip_text="Left")
        self.center_button = Gtk.ToggleButton(icon_name="format-justify-center-symbolic", tooltip_text="Center")
        self.right_button = Gtk.ToggleButton(icon_name="format-justify-right-symbolic", tooltip_text="Right")

        # Group the toggle buttons so only one can be active
        self.center_button.set_group(self.left_button)
        self.right_button.set_group(self.left_button)

        self.revert_button = RevertButton()

        self.append(self.left_button)
        self.append(self.center_button)
        self.append(self.right_button)
        self.append(self.revert_button)

    def get_alignment(self) -> str:
        if self.left_button.get_active():
            return "left"
        elif self.right_button.get_active():
            return "right"
        else:
            return "center"

    def set_alignment(self, alignment: str) -> None:
        if alignment == "left":
            self.left_button.set_active(True)
        elif alignment == "right":
            self.right_button.set_active(True)
        else:
            self.center_button.set_active(True)
