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

from src.backend.DeckManagement.InputIdentifier import InputIdentifier
from src.backend.PluginManager.ActionHolderGroup import ActionHolderGroup
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

# Import Python modules
from loguru import logger as log
from rapidfuzz import fuzz

# Import own modules
from GtkHelper.GtkHelper import BackButton, BetterExpander, BetterPreferencesGroup
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.PluginBase import PluginRegistration

# Import typing
from typing import Any, TYPE_CHECKING, cast, override
if TYPE_CHECKING:
    from collections.abc import Callable
    from src.windows.mainWindow.elements.Sidebar.Sidebar import Sidebar

# Import globals
from src.backend import services

import globals as gl

class ActionChooser(Gtk.Box):
    def __init__(self, sidebar: "Sidebar", **kwargs: Any) -> None:
        super().__init__(hexpand=True, vexpand=True, **kwargs)
        self.sidebar: "Sidebar" = sidebar

        self.callback_function: "Callable[..., Any] | None" = None
        self.callback_args: "tuple[Any, ...] | None" = None
        self.callback_kwargs: "dict[str, Any] | None" = None
        self.current_stack_page: "Gtk.Widget | None" = None
        # Cleared again whenever show() is handed an invalid callback.
        self.identifier: InputIdentifier | None = None

        self.build()

    def build(self) -> None:
        self.scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(self.scrolled_window)

        self.clamp = Adw.Clamp()
        self.scrolled_window.set_child(self.clamp)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True, margin_top=4)
        self.clamp.set_child(self.main_box)

        self.nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        self.main_box.append(self.nav_box)

        self.back_button = BackButton()
        self.back_button.connect("clicked", self.on_back_button_click)
        self.nav_box.append(self.back_button)

        self.header = Gtk.Label(label=gl.lm.get("action-chooser.header"), xalign=0, css_classes=["page-header"], margin_top=30)
        self.main_box.append(self.header)

        self.search_entry = Gtk.SearchEntry(margin_top=10,
                                            placeholder_text=gl.lm.get("action-chooser.search-entry.placeholder"),
                                            hexpand=True)
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.main_box.append(self.search_entry)

        # Create this before PluginGroup because its constructor updates the empty state.
        # Store install and uninstall updates use the same label.
        self.empty_state_label = Gtk.Label(
            wrap=True,
            justify=Gtk.Justification.CENTER,
            margin_top=40,
            css_classes=["dim-label"],
            visible=False,
        )

        self.plugin_group = PluginGroup(self, margin_top=40)
        self.main_box.append(self.plugin_group)
        self.main_box.append(self.empty_state_label)

        self.open_store_button = OpenStoreButton(margin_top=40, margin_bottom=40)
        self.main_box.append(self.open_store_button)

    def update_empty_state(self, n_plugins: int) -> None:
        """Explain an empty list as failed, version-disabled, or absent plugins.
        Main-thread only; store code dispatches PluginGroup.update through GLib.idle_add."""
        if n_plugins > 0:
            self.empty_state_label.set_visible(False)
            return

        n_failed, n_disabled = 0, 0
        if gl.plugin_manager is not None:
            n_failed, n_disabled = gl.plugin_manager.get_load_issue_counts()

        if n_failed > 0:
            text = (f"No actions available -- {n_failed} plugin{'s' if n_failed != 1 else ''} "
                    f"failed to load (check the logs)")
        elif n_disabled > 0:
            text = (f"No actions available -- {n_disabled} plugin{'s' if n_disabled != 1 else ''} "
                    f"{'are' if n_disabled != 1 else 'is'} disabled because of an app version mismatch")
        else:
            text = "No plugins installed -- use the button below to browse the store"

        self.empty_state_label.set_label(text)
        self.empty_state_label.set_visible(True)

    @override
    def show(self, callback_function: "Callable[..., Any] | None", current_stack_page: "Gtk.Widget | None", identifier: InputIdentifier, callback_args: "tuple[Any, ...]", callback_kwargs: "dict[str, Any]") -> None:  # ty: ignore[invalid-method-override]  # gi stub: shadows Gtk.Widget.show() with the show-for-this-action-slot entry point of the chooser; its one caller is Sidebar.let_user_select_action
        # current_stack_page matters when a plugin action in the
        # action_configurator calls let_user_select_action.

        # Validate the callback function
        if not callable(callback_function):
            log.error(f"Invalid callback function: {callback_function}")
            self.callback_function = None
            self.callback_args = None
            self.callback_kwargs = None
            self.current_stack_page = None
            self.identifier = None
            return
        
        self.callback_function = callback_function
        self.current_stack_page = current_stack_page
        self.callback_args = callback_args
        self.callback_kwargs = callback_kwargs
        self.identifier = identifier
        self.plugin_group.set_identifier(identifier)

        self.sidebar.main_stack.set_visible_child(self)

    def on_back_button_click(self, button: Gtk.Button) -> None:
        self.sidebar.main_stack.set_visible_child_name("configurator_stack")

    def on_search_changed(self, search_entry: Gtk.SearchEntry) -> None:
        self.plugin_group.search()

class OpenStoreButton(Gtk.Button):
    def __init__(self, **kwargs: Any) -> None:
        # No *args. The one caller passes margins by keyword, and a forwarded
        # positional would bind label a second time.
        super().__init__(label=gl.lm.get("asset-chooser.add-more-button.label"),
                         css_classes=["suggested-action"], **kwargs)
        self.connect("clicked", self.on_click)

    def on_click(self, button: Gtk.Button) -> None:
        services.require_app().open_store()

class PluginGroup(BetterPreferencesGroup):
    def __init__(self, action_chooser: "ActionChooser", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.action_chooser = action_chooser

        self.expander: "list[PluginExpander]" = []

        self.update()

        self.set_sort_func(self.sort_func, None)
        self.set_filter_func(self.filter_func, None)

    def update(self) -> None:
        self.clear()
        self.expander = []
        plugin_manager = gl.plugin_manager
        if plugin_manager is None:
            # Nothing to offer before main.create_global_objects() builds it.
            return
        for plugin_id, plugin_registration in dict(plugin_manager.get_plugins()).items():
            plugin_name = plugin_registration["object"].plugin_name
            expander = PluginExpander(self, plugin_name, plugin_registration)
            self.add(expander)
            self.expander.append(expander)

        self.action_chooser.update_empty_state(len(self.expander))

    def search(self) -> None:
        # Let the expanders search
        for expander in self.expander:
            expander.search()

        self.invalidate_sort()
        self.invalidate_filter()

    def sort_func(self, expander1: "PluginExpander", expander2: "PluginExpander", user_data: None) -> int:
        search_string = self.action_chooser.search_entry.get_text()

        if search_string == "":
            # sort alphabetically
            if expander1.get_title() < expander2.get_title():
                return -1
            if expander1.get_title() > expander2.get_title():
                return 1
            return 0

        highest_fuzz_1 = expander1.highest_fuzz_score
        highest_fuzz_2 = expander2.highest_fuzz_score

        title_fuzz_1 = fuzz.ratio(search_string.lower(), expander1.get_title().lower())
        title_fuzz_2 = fuzz.ratio(search_string.lower(), expander2.get_title().lower())

        # Sort by highest fuzzy score and title fuzz score
        max_expander_1 = max(highest_fuzz_1, title_fuzz_1)
        max_expander_2 = max(highest_fuzz_2, title_fuzz_2)

        if max_expander_1 > max_expander_2:
            return -1
        elif max_expander_1 < max_expander_2:
            return 1
        
        return 0
    
    def filter_func(self, expander: "PluginExpander", user_data: None) -> bool:
        MIN_ACTION_FUZZY_SCORE = 20
        MIN_TITLE_FUZZY_SCORE = 20

        search_string = self.action_chooser.search_entry.get_text()
        if search_string == "":
            # Show all
            return True

        # Round for the threshold because rapidfuzz can return 19.999999999999996 for 20.
        # Sorting keeps the unrounded score for finer ranking.
        if round(expander.highest_fuzz_score) >= MIN_ACTION_FUZZY_SCORE:
            return True

        title_fuzzy = round(fuzz.ratio(search_string.lower(), expander.get_title().lower()))
        if title_fuzzy >= MIN_TITLE_FUZZY_SCORE:
            return True
        return False
    
    def set_identifier(self, identifier: InputIdentifier) -> None:
        for expander in self.expander:
            expander.set_identifier(identifier)
            expander.invalidate_filter()

class ActionChooserExpander(BetterExpander):
    def __init__(self, plugin_group: PluginGroup, plugin_name: str, plugin_registration: "PluginRegistration") -> None:
        super().__init__()
        self.plugin_group = plugin_group
        self.plugin_name = plugin_name
        self.plugin_registration = plugin_registration

        self.input_type: InputIdentifier = None  # ty: ignore[invalid-assignment]  # late-init: show

        self.highest_fuzz_score: float = 0

        self.set_sort_func(self.sort_func, None)
        self.set_filter_func(self.filter_func, None)

    def build(self) -> None:
        pass

    def sort_func(self, row1: Gtk.ListBoxRow, row2: Gtk.ListBoxRow, user_data: None) -> "int | None":
        pass

    def filter_func(self, row: "PluginActionRow", user_data: None) -> "bool | None":
        pass

    def calculate_fuzz_ratio_sort(self, search_string: str, action1_label: str, action2_label: str) -> int:
        if search_string == "":
            self.highest_fuzz_score = 0
            # sort alphabetically
            if action1_label < action2_label:
                return -1
            if action1_label > action2_label:
                return 1
            return 0

        fuzz_score_1 = fuzz.ratio(search_string.lower(), action1_label.lower())
        fuzz_score_2 = fuzz.ratio(search_string.lower(), action2_label.lower())

        if fuzz_score_1 > self.highest_fuzz_score:
            self.highest_fuzz_score = fuzz_score_1
        if fuzz_score_2 > self.highest_fuzz_score:
            self.highest_fuzz_score = fuzz_score_2

        if fuzz_score_1 > fuzz_score_2:
            return -1
        if fuzz_score_1 < fuzz_score_2:
            return 1
        return 0

    def calculate_fuzz_ratio_filter(self, search_string: str, label: str) -> bool:
        if search_string == "":
            # Collapse all
            self.set_expanded(False)
            # Show all
            return True

        # Rounded for the same reason as PluginGroup.filter_func.
        fuzz_score = round(fuzz.ratio(search_string.lower(), label.lower()))

        MIN_FUZZY_SCORE = 20
        if fuzz_score >= MIN_FUZZY_SCORE:
            # Expand
            self.set_expanded(True)
            return True
        return False

    def search(self) -> None:
        self.invalidate_filter()
        self.invalidate_sort()

class PluginExpander(ActionChooserExpander):
    def __init__(self, plugin_group: PluginGroup, plugin_name: str, plugin_registration: "PluginRegistration") -> None:
        super().__init__(plugin_group, plugin_name, plugin_registration)
        self.build()
        self.add_action_holders()

    @override
    def build(self) -> None:
        # Texts
        self.set_title(self.plugin_name)
        self.set_subtitle(self.plugin_registration["object"].plugin_id)

        self.add_prefix(self.plugin_registration["object"].get_selector_icon())

    def add_action_holders(self) -> None:
        action_holders: set[ActionHolder] = set(self.plugin_registration["object"].action_holders.values())
        action_holder_groups: set[ActionHolderGroup] = self.plugin_registration["object"].action_holder_groups

        added_holders: set[ActionHolder] = set()

        # Add Groups
        for group in action_holder_groups:
            action_group = ActionGroupExpander(group, self.plugin_group, self.plugin_name, self.plugin_registration)
            action_group.add_css_class("action-chooser-item")
            action_group.add_css_class("action-chooser-group")

            self.add_row(action_group)
            added_holders.update(group.get_action_holders())

        not_added_holders = action_holders - added_holders

        # Add leftovers
        for holder in not_added_holders:
            action_row = PluginActionRow(self, holder)
            action_row.add_css_class("action-chooser-item")

            self.add_row(action_row)

    @override
    def sort_func(self, row1: Gtk.ListBoxRow, row2: Gtk.ListBoxRow, user_data: None) -> int:
        # Returns -1 if row1 should be brefore row2, 0 if they are equal, and 1 otherwise
        search_string = self.plugin_group.action_chooser.search_entry.get_text()

        if type(row1) is Gtk.ListBoxRow or type(row2) is Gtk.ListBoxRow:
            return 0

        # This expander adds only group expanders and action rows, so a row
        # that is not the one is the other.
        if isinstance(row1, ActionGroupExpander):
            action1_label = row1.get_title()
        else:
            action1_label = cast("PluginActionRow", row1).label.get_label()

        if isinstance(row2, ActionGroupExpander):
            action2_label = row2.get_title()
        else:
            action2_label = cast("PluginActionRow", row2).label.get_label()

        return self.calculate_fuzz_ratio_sort(search_string, action1_label, action2_label)
    
    @override
    def filter_func(self, row: "PluginActionRow | ActionGroupExpander", user_data: None) -> bool:
        search_string = self.plugin_group.action_chooser.search_entry.get_text()

        if isinstance(row, ActionGroupExpander):
            label = row.get_title()
        else:
            label = row.label.get_label()

        return self.calculate_fuzz_ratio_filter(search_string, label)
    
    def set_identifier(self, input_type: InputIdentifier) -> None:
        self.input_type = input_type
        for row in self.get_rows():
            if isinstance(row, ActionGroupExpander):
                if not self.set_group_identifier(input_type, row.holder_group, row):
                    continue
            row.set_identifier(input_type)
        self.invalidate_filter()

    def set_group_identifier(self, input_type: InputIdentifier, group: ActionHolderGroup, row: "ActionGroupExpander") -> bool:
        return True

class ActionGroupExpander(ActionChooserExpander):
    def __init__(self, holder_group: ActionHolderGroup, plugin_group: PluginGroup, plugin_name: str, plugin_registration: "PluginRegistration") -> None:
        super().__init__(plugin_group, plugin_name, plugin_registration)
        self.holder_group: ActionHolderGroup = holder_group
        self.build()
        self.add_action_holders()

    @override
    def build(self) -> None:
        # Texts
        self.set_title(self.holder_group.get_group_name())

        folder_icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        self.add_prefix(folder_icon)

        # set icon to not activated
        image = self.get_arrow_image()
        if image is not None:
            image.set_css_classes(["expander-arrow-not-activated"])

        self.connect("notify::expanded", self.on_expanded)

        self.warning_icon = Gtk.Image(icon_name="dialog-warning-symbolic",
                                      hexpand=True, halign=Gtk.Align.END, margin_end=3, visible=False)
        self.add_suffix(self.warning_icon)

    def add_action_holders(self) -> None:
        for holder in self.holder_group.get_action_holders():
            action_row = PluginActionRow(self, holder)
            action_row.add_css_class("action-chooser-group-item")

            self.add_row(action_row)

    def on_expanded(self, *args: object) -> None:
        # This expander sits inside another expander, which sticks the icon
        # in the expanded state. The code below sets the icon.
        image = self.get_arrow_image()
        if image is None:
            # libadwaita's internal tree can differ from the shape get_arrow_image walks.
            # Keep the default arrow state when the image is unavailable.
            return
        if self.get_expanded():
            image.set_css_classes(["expander-arrow-activated"])
        else:
            image.set_css_classes(["expander-arrow-not-activated"])

    @override
    def sort_func(self, row1: Gtk.ListBoxRow, row2: Gtk.ListBoxRow, user_data: None) -> int:
        # Returns -1 if row1 should be brefore row2, 0 if they are equal, and 1 otherwise
        search_string = self.plugin_group.action_chooser.search_entry.get_text()

        # This expander adds action rows only.
        action1_label = cast("PluginActionRow", row1).label.get_label()
        action2_label = cast("PluginActionRow", row2).label.get_label()

        return self.calculate_fuzz_ratio_sort(search_string, action1_label, action2_label)

    @override
    def filter_func(self, row: "PluginActionRow", user_data: None) -> bool:
        search_string = self.plugin_group.action_chooser.search_entry.get_text()

        label = row.label.get_label()

        return self.calculate_fuzz_ratio_filter(search_string, label)

    def set_identifier(self, input_type: InputIdentifier) -> None:
        compatibility = self.holder_group.get_min_input_compatibility(input_type)

        def show_compatibility(show: bool = False, tooltip: str | None = None, icon_name: str | None = None) -> None:
            self.warning_icon.set_visible(show)

            if icon_name:
                self.warning_icon.set_from_icon_name(icon_name)

            if tooltip:
                self.set_tooltip_text(tooltip)
                if show:
                    self.warning_icon.set_tooltip_text(tooltip)

        if self.holder_group.get_min_input_compatibility(input_type) < ActionInputSupport.UNTESTED:
            warning_icon = "dialog-error-symbolic"
            tooltip_text = f"Some actions in this group are not compatible with {input_type.input_type}"
            show_warning = True
        elif self.holder_group.get_min_input_compatibility(input_type) == ActionInputSupport.UNTESTED:
            warning_icon = "dialog-warning-symbolic"
            tooltip_text = f"Some actions in this group might not be compatible with {input_type.input_type}"
            show_warning = True
        else:
            warning_icon = None
            tooltip_text = ""
            show_warning = False

        show_compatibility(show=show_warning, tooltip=tooltip_text, icon_name=warning_icon)

        self.input_type = input_type
        for row in self.get_rows():
            row.set_identifier(input_type)
        self.invalidate_filter()

class PluginActionRow(Adw.ActionRow):
    def __init__(self, expander: "ActionChooserExpander", action_holder: ActionHolder, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.expander = expander
        self.action_holder = action_holder

        self.button = Gtk.Button(hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN,
                                 css_classes=["no-margin", "invisible"])
        self.button.connect("clicked", self.on_click)
        self.set_child(self.button)
        
        self.main_box = Gtk.Box(hexpand=True, vexpand=True, orientation=Gtk.Orientation.HORIZONTAL,
                                margin_top=10, margin_bottom=10)
        self.button.set_child(self.main_box)

        # self.icon = Gtk.Image(icon_name="insert-image", icon_size=Gtk.IconSize.LARGE, margin_start=5)
        self.icon = action_holder.icon
        icon_parent = action_holder.icon.get_parent()
        if icon_parent is not None:
            icon_parent.remove(self.action_holder.icon)  # ty: ignore[unresolved-attribute]  # gi stub: remove() lives on the container subclasses (Gtk.Box here), not on Gtk.Widget, which is all get_parent() promises
        self.main_box.append(self.icon)

        self.label = Gtk.Label(label=self.action_holder.action_name, margin_start=10, css_classes=["bold", "large-text"])
        self.main_box.append(self.label)

        self.warning_icon = Gtk.Image(icon_name="dialog-warning-symbolic",
                                      hexpand=True, halign=Gtk.Align.END, margin_end=3, visible=False)
        self.main_box.append(self.warning_icon)

    def on_click(self, button: Gtk.Button) -> None:
        if self.action_holder.action_core is None:
            return
        
        # Go back to old page. show() binds the page beside the callback;
        # with none bound there is nothing to switch back to.
        current_stack_page = self.expander.plugin_group.action_chooser.current_stack_page
        if current_stack_page is not None:
            self.expander.plugin_group.action_chooser.sidebar.main_stack.set_visible_child(current_stack_page)

        # Verify the callback function
        if not callable(self.expander.plugin_group.action_chooser.callback_function):
            log.warning(f"Invalid callback function: {self.expander.plugin_group.action_chooser.callback_function}")
            return
        
        # Call the callback function
        callback = self.expander.plugin_group.action_chooser.callback_function
        # show() sets the three fields together; the fallbacks restate that
        # for the checker.
        args = self.expander.plugin_group.action_chooser.callback_args or ()
        kwargs = self.expander.plugin_group.action_chooser.callback_kwargs or {}

        callback(self.action_holder, *args, **kwargs)

    def show_warning(self, show: bool, tooltip: str | None = None) -> None:
        self.warning_icon.set_visible(show)

        if show and tooltip is not None:
            self.warning_icon.set_tooltip_text(tooltip)

    def set_identifier(self, identifier: InputIdentifier) -> None:
        action_input_compatibility = self.action_holder.get_input_compatibility(identifier)

        if action_input_compatibility <= ActionInputSupport.UNSUPPORTED:
            self.warning_icon.set_from_icon_name("dialog-error-symbolic")
            self.set_tooltip_text(f"Action is not compatible with {identifier.input_type}")
            self.show_warning(True)
            self.set_sensitive(False)
            
        elif action_input_compatibility == ActionInputSupport.UNTESTED:
            self.warning_icon.set_from_icon_name("dialog-warning-symbolic")
            self.warning_icon.set_tooltip_text(f"Action might not be compatible with {identifier.input_type}")
            self.set_tooltip_text("")
            self.show_warning(True)
            self.set_sensitive(True)

        elif action_input_compatibility >= ActionInputSupport.SUPPORTED:
            self.set_tooltip_text("")
            self.show_warning(False)
            self.set_sensitive(True)
