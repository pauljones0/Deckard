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
from collections.abc import Callable, Iterable
from typing import Any

from typing_extensions import deprecated

import gi

from src.backend.DeckManagement.HelperMethods import open_web

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from loguru import logger as log

# Import the translation accessor
from src.backend.services import tr

# The thread kit lives in the toolkit-free src/backend/main_loop.py, so engine
# code marshals to the main loop without an import of the widget stack. These
# names re-export here unchanged, because every plugin imports them from
# GtkHelper. RUN_ON_MAIN_TIMEOUT_S does not re-bind here. run_on_main reads it
# from main_loop at call time, so a copy in this namespace is a dead write.
# Patch src.backend.main_loop.RUN_ON_MAIN_TIMEOUT_S instead.
from src.backend.main_loop import (  # noqa: F401  (re-export for plugins)
    background as background,
    on_main as on_main,
    run_in_background as run_in_background,
    run_on_main as run_on_main,
    shutdown_background_pool as shutdown_background_pool,
)


# Helper Functions
def get_focused_widgets(start: Gtk.Widget) -> list[Gtk.Widget]:
    widgets: list[Gtk.Widget] = []
    while True:
        child = start.get_focus_child()
        if child is None:
            return widgets
        widgets.append(child)
        start = child

def get_deepest_focused_widget(start: Gtk.Widget) -> Gtk.Widget:
    return get_focused_widgets(start)[-1]

def get_deepest_focused_widget_with_attr(start: Gtk.Widget, attr:str) -> Gtk.Widget | None:
    for widget in reversed(get_focused_widgets(start)):
        if hasattr(widget, attr):
            return widget
    return None

def better_unparent(widget: Gtk.Widget) -> None:
    if widget.get_parent() is not None:
        widget.unparent()

# Helper Classes
class BetterExpander(Adw.ExpanderRow):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Subclasses that track their rows replace this. Empty here, so
        # get_index_of_child raises the ValueError it documents rather than
        # an AttributeError on a subclass that never set it.
        self.actions: list[Any] = []

    def set_sort_func(self, *args: Any, **kwargs: Any) -> None:
        revealer_list_box = self.get_list_box()
        if revealer_list_box is None:
            return
        revealer_list_box.set_sort_func(*args, **kwargs)

    def set_filter_func(self, *args: Any, **kwargs: Any) -> None:
        revealer_list_box = self.get_list_box()
        if revealer_list_box is None:
            return
        revealer_list_box.set_filter_func(*args, **kwargs)

    def invalidate_filter(self) -> None:
        list_box = self.get_list_box()
        if list_box is None:
            return
        list_box.invalidate_filter()

    def invalidate_sort(self) -> None:
        list_box = self.get_list_box()
        if list_box is None:
            return
        list_box.invalidate_sort()

    def get_rows(self) -> Any:
        # The rows are per-subclass widgets that callers read duck-typed
        # attributes off, so the checker sees dynamic here, as with
        # BetterPreferencesGroup.get_list_box.
        revealer_list_box = self.get_list_box()
        if revealer_list_box is None:
            return

        rows = []
        child = revealer_list_box.get_first_child()
        while child is not None:
            rows.append(child)
            child = child.get_next_sibling()

        return rows

    def get_list_box(self) -> Gtk.ListBox | None:
        expander_box = self.get_first_child()
        if expander_box is None:
            return None

        expander_list_box = expander_box.get_first_child()
        if expander_list_box is None:
            return None

        revealer = expander_list_box.get_next_sibling()
        if revealer is None:
            return None

        revealer_list_box = revealer.get_first_child()
        if not isinstance(revealer_list_box, Gtk.ListBox):
            return None

        return revealer_list_box

    def clear(self) -> None:
        list_box = self.get_list_box()
        if list_box is None:
            # add_row() appends through libadwaita's own pointer and does not
            # use this walk, so a silent clear here lets a caller that clears
            # and refills duplicate every row. Say it rather than hide it.
            log.warning("Expander has no list box to clear; the Adw layout this walk expects has changed")
            return
        list_box.remove_all()

    def reorder_child_after(self, child: Gtk.Widget, after: Gtk.Widget) -> None:
        childs = self.get_rows()
        after_index = childs.index(after)

        if after_index is None:
            log.warning("After child could not be found. Please add it first")
            return

        # Remove child from list
        childs.remove(child)

        # Add child in new position
        childs.insert(after_index, child)

        # Remove all childs
        self.clear()

        # Add all childs in new order
        for child in childs:
            self.add_row(child)

    def remove_child(self, child:Gtk.Widget) -> None:
        list_box = self.get_list_box()
        if list_box is None:
            return
        list_box.remove(child)

    def get_index_of_child(self, child: Any) -> int:
        for i, action in enumerate(self.actions):
            if action == child:
                return i

        raise ValueError("Child not found")

    def get_arrow_image(self) -> Gtk.Image | None:
        box = self.get_child()
        if box is None:
            return None

        list_box = box.get_first_child()
        if list_box is None:
            return None

        adw_action_row = list_box.get_first_child()
        if not isinstance(adw_action_row, Gtk.ListBoxRow):
            return None

        row_box = adw_action_row.get_child()
        if row_box is None:
            return None

        suffix_box = row_box.get_last_child()
        if suffix_box is None:
            return None

        image = suffix_box.get_last_child()
        return image if isinstance(image, Gtk.Image) else None

class BetterPreferencesGroup(Adw.PreferencesGroup):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    def clear(self) -> None:
        list_box = self.get_list_box()
        list_box.remove_all()

    def set_sort_func(self, *args: Any, **kwargs: Any) -> None:
        list_box = self.get_list_box()
        list_box.set_sort_func(*args, **kwargs)

    def set_filter_func(self, *args: Any, **kwargs: Any) -> None:
        list_box = self.get_list_box()
        list_box.set_filter_func(*args, **kwargs)

    def invalidate_filter(self) -> None:
        list_box = self.get_list_box()
        list_box.invalidate_filter()

    def invalidate_sort(self) -> None:
        list_box = self.get_list_box()
        list_box.invalidate_sort()

    def get_rows(self) -> Any:
        # Dynamic for the same reason as get_list_box below: the rows are
        # per-subclass widgets read duck-typed by the callers.
        list_box = self.get_list_box()
        if list_box is None:
            return

        rows = []
        child = list_box.get_first_child()
        while child is not None:
            rows.append(child)
            child = child.get_next_sibling()

        return rows

    def get_list_box(self) -> Any:
        # Every step walks Adw's internal tree, so any of them can answer
        # None. Only get_rows below checks for that: the other callers
        # dereference the result, so a layout that does not match surfaces
        # there as an AttributeError. Do not add a guard to clear() without
        # reading the note on BetterExpander.clear. A silent clear that the
        # matching add_row does not skip duplicates every row.
        first_box = self.get_first_child()
        second_box = first_box.get_first_child() if first_box is not None else None
        third_box = second_box.get_next_sibling() if second_box is not None else None
        return third_box.get_first_child() if third_box is not None else None

class AttributeRow(Adw.PreferencesRow):
    # The row draws its own two labels, so the caption and the value live in
    # plain attributes and setters with names of their own. A name that the row
    # inherits (title, set_title) would write the GObject property instead, and
    # the property drives nothing here.
    def __init__(self, title:str, attr:str, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.title_str = title
        self.attr_str = attr
        self.build()

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.title_label = Gtk.Label(label=self.title_str, xalign=0, hexpand=True, margin_start=15)
        self.main_box.append(self.title_label)

        self.attribute_label = Gtk.Label(label=self.attr_str, halign=Gtk.Align.FILL, margin_end=15)
        self.main_box.append(self.attribute_label)

    def set_attribute_title(self, title:str) -> None:
        self.title_label.set_label(title)

    def set_attribute(self, attr: str | None) -> None:
        if attr is None:
            attr = "N/A"
        self.attribute_label.set_label(attr)

class EntryDialog(Gtk.ApplicationWindow):
    def __init__(self, parent_window: Gtk.Window, dialog_title:str, entry_heading:str = "Name:", default_text:str | None = None, confirm_label:str = "OK", forbid_answers:list[str] | None = None,
                 empty_warning:str = "The name cannot be empty", cancel_label:str = "Cancel", already_exists_warning:str = "This name already exists",
                 placeholder:str | None = None):
        if forbid_answers is None:
            forbid_answers = []

        self.default_text = default_text
        self.confirm_label = confirm_label
        self.entry_heading = entry_heading
        self.forbid_answers = forbid_answers
        self.empty_warning = empty_warning
        self.cancel_label = cancel_label
        self.placeholder_text = placeholder
        self.already_exists_warning = already_exists_warning
        super().__init__(transient_for=parent_window, modal=True, default_height=150, default_width=350, title = dialog_title)
        self.callback_func: Callable[[str], Any] | None = None
        self.build()

    def build(self) -> None:
        # Create title bar
        self.title_bar = Gtk.HeaderBar(show_title_buttons=False, css_classes=["flat"])
        # Cancel button
        self.cancel_button = Gtk.Button(label=self.cancel_label)
        self.cancel_button.connect('clicked', self.on_cancel)
        # Confirm button
        self.confirm_button = Gtk.Button(label=self.confirm_label, css_classes=['confirm-button'], sensitive=False)
        self.confirm_button.connect('clicked', self.on_confirm)
        # Main box
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True, margin_start=20, margin_end=20, margin_top=20, margin_bottom=20)
        # Label
        self.label = Gtk.Label(label=self.entry_heading)
        # Input box
        self.input_box = Gtk.Entry(hexpand=True, margin_top=10, text=self.default_text or "", placeholder_text=self.placeholder_text)
        self.input_box.connect('changed', self.on_name_change)
        # Warning label
        self.warning_label = Gtk.Label(label=self.empty_warning, css_classes=['warning-label'], margin_top=10)

        # Add objects
        self.set_titlebar(self.title_bar)
        self.title_bar.pack_start(self.cancel_button)
        self.title_bar.pack_end(self.confirm_button)
        self.set_child(self.main_box)
        self.main_box.append(self.label)
        self.main_box.append(self.input_box)
        self.main_box.append(self.warning_label)

        # Set status
        self.on_name_change(self.input_box)

        # Trigger on_confirm on return press
        self.input_box.connect("activate", self.on_confirm)

    def on_cancel(self, button: Gtk.Button) -> None:
        self.destroy()


    def on_name_change(self, entry: Gtk.Entry) -> None:
        if entry.get_text() == '':
            self.set_dialog_status(0)
        elif entry.get_text() not in self.forbid_answers:
            self.set_dialog_status(2)
        else:
            self.set_dialog_status(1)

    def set_dialog_status(self, status: int) -> None:
        """
        Sets the status of the dialog

        Args:
            status (int): 0 for no name, 1 for already in use, 2 for ok
        """
        if status == 0:
            # Label
            if self.main_box.get_last_child() is not self.warning_label:
                self.main_box.append(self.warning_label)
            self.warning_label.set_text(self.empty_warning)
            # Button
            self.confirm_button.set_sensitive(False)
            self.confirm_button.set_css_classes(['confirm-button'])
        if status == 1:
            # Label
            if self.main_box.get_last_child() is not self.warning_label:
                self.main_box.append(self.warning_label)
            self.warning_label.set_text(self.already_exists_warning)
            # Button
            self.confirm_button.set_sensitive(False)
            self.confirm_button.set_css_classes(['confirm-button-error'])
        if status == 2:
            # Label
            if self.main_box.get_last_child() is self.warning_label:
                self.main_box.remove(self.warning_label)
            # Button
            self.confirm_button.set_sensitive(True)
            self.confirm_button.set_css_classes(['confirm-button'])

    def show(self, callback_func: Callable[[str], Any] | None) -> None:  # ty: ignore[invalid-method-override]  # shadows Gtk.Widget.show with this dialog's callback form
        self.callback_func = callback_func
        self.present()

    def on_confirm(self, button: Gtk.Widget) -> None:
        if self.callback_func is None:
            return
        self.callback_func(self.input_box.get_text())
        self.destroy()

class ErrorPage(Gtk.Box):
    def __init__(self, reload_func: Callable[..., Any] | None = None,
                 error_text:str = "Error",
                 reload_args: "Iterable[Any] | None" = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL,
                         halign=Gtk.Align.CENTER,
                         valign=Gtk.Align.CENTER)

        if reload_args is None:
            reload_args = []

        self.reload_func = reload_func
        self.error_text = error_text
        self.reload_args = reload_args
        self.build()

    def build(self) -> None:
        self.error_label = Gtk.Label(label=self.error_text)
        self.append(self.error_label)

        self.retry_button = Gtk.Button(label="Retry")
        self.retry_button.connect("clicked", self.on_retry_button_click)

        if callable(self.reload_func):
            self.append(self.retry_button)

    def on_retry_button_click(self, button: Gtk.Button) -> None:
        if self.reload_func is None:
            return
        self.reload_func(*self.reload_args)

    def set_error_text(self, error_text: str) -> None:
        self.error_label.set_text(error_text)

    def set_reload_func(self, reload_func: "Callable[..., Any] | None") -> None:
        if callable(self.reload_func):
            if callable(reload_func):
                self.reload_func = reload_func
            else:
                self.remove(self.retry_button)
        else:
            self.append(self.retry_button)
            self.reload_func = reload_func

    def set_reload_args(self, reload_args: "Iterable[Any]") -> None:
        self.reload_args = reload_args

class OriginalURL(Adw.ActionRow):
    def __init__(self) -> None:
        super().__init__(title="Original URL:", subtitle="N/A")
        self.set_activatable(False)

        self.suffix_box = Gtk.Box(valign=Gtk.Align.CENTER)
        self.add_suffix(self.suffix_box)

        self.open_button = Gtk.Button(icon_name="web-browser-symbolic")
        self.open_button.connect("clicked", self.on_open_clicked)
        self.suffix_box.append(self.open_button)

    def set_url(self, url: str | None) -> None:
        if url is None:
            self.set_subtitle("N/A")
            self.open_button.set_sensitive(False)
            return
        self.set_subtitle(url)
        self.open_button.set_sensitive(True)

    def on_open_clicked(self, button:Gtk.Button) -> None:
        url = self.get_subtitle()
        if not url or url == "N/A":
            return
        open_web(url)

class EntryRowWithoutTitle(Adw.EntryRow):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        # Make title invisible
        # Walks Adw's internal tree to hide the title; any step can answer
        # None, and a layout that does not match leaves the title as it is.
        child = self.get_child()
        prefix_box = child.get_first_child() if child is not None else None
        gizmo = prefix_box.get_next_sibling() if prefix_box is not None else None
        empty_title = gizmo.get_first_child() if gizmo is not None else None
        title = empty_title.get_next_sibling() if empty_title is not None else None
        if empty_title is None or title is None:
            return

        empty_title.set_visible(False)
        title.set_visible(False)

class BackButton(Gtk.Button):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.set_child(self.box)

        self.box.append(Gtk.Image(icon_name="go-previous-symbolic"))
        self.box.append(Gtk.Label(label=tr("go-back")))

class RevertButton(Gtk.Button):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(icon_name="edit-undo-symbolic", **kwargs)
        self.set_tooltip_text("Revert to action defaults")

class LoadingScreen(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True,
                         valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)

        self.spinner = Gtk.Spinner(spinning=False)
        self.append(self.spinner)

        self.loading_label = Gtk.Label(label="Loading")
        self.append(self.loading_label)

        self.progress_bar = Gtk.ProgressBar(margin_top=20, show_text=True, text="", visible=False)
        self.append(self.progress_bar)

    def set_spinning(self, loading: bool) -> None:
        if loading:
            GLib.idle_add(self.spinner.start)
        else:
            GLib.idle_add(self.spinner.stop)


@deprecated("This has been deprecated in favor of GtkHelper.ComboRow.ComboRow.")
class ComboRow(Adw.PreferencesRow):
    def __init__(self, title: str, model: Gtk.ListStore, **kwargs: Any) -> None:
        super().__init__(title=title, **kwargs)
        self.model = model

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                margin_start=10, margin_end=10,
                                margin_top=10, margin_bottom=10)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label=title, hexpand=True, xalign=0)
        self.main_box.append(self.label)

        self.combo_box = Gtk.ComboBox.new_with_model(self.model)
        self.main_box.append(self.combo_box)


@deprecated("This has been deprecated in favor of GtkHelper.ScaleRow.ScaleRow.")
class ScaleRow(Adw.PreferencesRow):
    def __init__(self, title: str, value: float, min: float, max: float, step: float, text_right: str = "", text_left: str = "", **kwargs: Any) -> None:
        super().__init__(title=title, **kwargs)
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                margin_start=10, margin_end=10,
                                margin_top=10, margin_bottom=10)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label=title, hexpand=True, xalign=0)
        self.main_box.append(self.label)

        self.adjustment = Gtk.Adjustment.new(value, min, max, step, 1, 0)

        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adjustment)
        self.scale.set_size_request(200, -1)  # Adjust width as needed
        self.scale.set_tooltip_text(str(value))

        def correct_step_amount(adjustment: Gtk.Adjustment) -> None:
            value = adjustment.get_value()
            step = adjustment.get_step_increment()
            rounded_value = round(value / step) * step
            adjustment.set_value(rounded_value)

        self.adjustment.connect("value-changed", correct_step_amount)

        self.label_right = Gtk.Label(label=text_right, hexpand=False, xalign=0)

        self.label_left = Gtk.Label(label=text_left, hexpand=False, xalign=0)

        self.main_box.append(self.label_left)
        self.main_box.append(self.scale)
        self.main_box.append(self.label_right)
