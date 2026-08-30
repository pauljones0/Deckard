
from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI

from gi.repository import Gtk

from collections.abc import Callable
from typing import cast, TYPE_CHECKING, Any, override
from GtkHelper.GtkHelper import on_main

if TYPE_CHECKING:
    from src.backend.PluginManager.ActionCore import ActionCore

from GtkHelper.GtkHelper import BetterExpander

class ExpanderRow(GenerativeUI[bool]):
    """Expander row that manages expansion state and child widgets."""

    def __init__(self, action_core: "ActionCore",
                 var_name: str,
                 default_value: bool,
                 title: str | None = None,
                 subtitle: str | None = None,
                 show_enable_switch: bool = False,
                 start_expanded: bool = False,
                 on_change: Callable[..., Any] | None = None,
                 can_reset: bool = False,
                 auto_add: bool = True,
                 complex_var_name: bool = False
                 ):
        """
        Initializes the ExpanderRow widget.

        Args:
            action_core (ActionCore): The base action that provides context for this expander row.
            var_name (str): The variable name to associate with this expander row.
            default_value (bool): The default expanded/collapsed state of the expander.
            title (str, optional): The title to display for the expander row.
            subtitle (str, optional): The subtitle to display for the expander row.
            show_enable_switch (bool, optional): Whether to show the enable switch. Defaults to False.
            start_expanded (bool, optional): Whether the expander should start in an expanded state. Defaults to False.
            on_change (callable, optional): A callback function to call when the value changes.
            can_reset (bool, optional): Whether the value can be reset. Defaults to False.
            auto_add (bool, optional): Whether to automatically add this entry to the UI. Defaults to True.
        """
        # GenerativeUI children added via add_row; destroyed in clear_rows().
        self._child_generative_ui: list[Any] = []

        self._switch_enabled = show_enable_switch

        def build() -> None:
            self._widget: BetterExpander | None = BetterExpander(
                title=self.get_translation(title),
                subtitle=self.get_translation(subtitle),
                expanded=start_expanded,
                show_enable_switch=show_enable_switch
            )

            self._handle_reset_button_creation()
            self.connect_signals()
        super().__init__(action_core, var_name, default_value, can_reset, auto_add, complex_var_name, on_change, build=build)

    @override
    def connect_signals(self) -> None:
        """
        Connects the signal handler for the 'notify::enable-expansion' signal to track changes
        in the expansion state of the expander widget.

        This ensures that when the expander's enabled state changes, the value is handled accordingly.
        """
        self._track_connect("enable-expansion", self.widget, "notify::enable-expansion", self._value_changed)

    @override
    def disconnect_signals(self) -> None:
        self._track_disconnect("enable-expansion", self.widget)

    def set_enable_expansion(self, enable_expansion: bool, update_setting: bool = False) -> None:
        """
        Sets the expansion state for the expander and optionally updates the associated setting.

        Args:
            enable_expansion (bool): Whether to enable expansion for the expander.
            update_setting (bool, optional): If True, updates the setting with the new state. Defaults to False.
        """
        self.set_ui_value(enable_expansion)

        if update_setting:
            self.set_value(enable_expansion)

    def get_enable_expansion(self) -> bool:
        """Return expansion state from the widget or settings without forcing a build."""
        if self._widget is None:
            return self.get_value()
        return cast(bool, self.widget.get_enable_expansion())

    @on_main
    def add_row(self, widget: Gtk.Widget) -> None:
        """
        Adds a widget as a row to the expander. If the widget already has a parent,
        it is removed before being added to the expander.

        Args:
            widget (Gtk.Widget): The widget to add as a row in the expander.
        """
        # Track the GenerativeUI owner (if any) so clear_rows() can tear it down.
        owner = getattr(widget, "_generative_ui_owner", None)
        if owner is not None and owner not in self._child_generative_ui:
            self._child_generative_ui.append(owner)
        if widget.get_parent() is not None:
            self.widget.remove_child(widget)
            widget.unparent()
        self.widget.add_row(widget)

    @on_main
    def clear_rows(self) -> None:
        # child.destroy() unregisters the children from the action; the
        # _widget guard keeps a repeat destroy() a no-op.
        if self._widget is not None:
            self.widget.clear()
        children, self._child_generative_ui = self._child_generative_ui, []
        for child in children:
            child.destroy()

    @override
    def destroy(self) -> None:
        # Tear down tracked children first so nested expanders don't leak.
        self.clear_rows()
        super().destroy()

    def _value_changed(self, expander_row: BetterExpander, _: Any) -> None:
        """
        Handles the change in the expander's expansion state.

        This method is triggered when the expansion state of the expander widget changes,
        and it handles updating the associated value accordingly.

        Args:
            expander_row (BetterExpander): The expander row widget whose expansion state changed.
            _: Placeholder for additional unused parameters.
        """
        self._handle_value_changed(expander_row.get_enable_expansion())

    @override
    def _handle_value_changed(self, new_value: bool, update_settings: bool = True, trigger_callback: bool = True) -> None:
        if not self._switch_enabled:
            return
        super()._handle_value_changed(new_value, update_settings, trigger_callback)

    @GenerativeUI.signal_manager
    @override
    def set_ui_value(self, value: bool) -> None:
        """
        Sets the expansion state of the expander in the UI widget.

        Args:
            value (bool): The expansion state to set for the UI widget (expanded or collapsed).
        """
        if not self._switch_enabled:
            return

        self.widget.set_enable_expansion(value)

    @on_main
    def set_expanded(self, value: bool) -> None:
        self.widget.set_expanded(value)

    def get_expanded(self) -> bool:
        return cast(bool, self.widget.get_expanded())
