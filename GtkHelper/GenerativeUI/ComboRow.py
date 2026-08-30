from GtkHelper.ComboRow import ComboRow as Combo, BaseComboRowItem
from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI


from collections.abc import Callable
from typing import cast, TYPE_CHECKING, Any, override
if TYPE_CHECKING:
    from src.backend.PluginManager.ActionCore import ActionCore

from GtkHelper.GtkHelper import on_main


class ComboRow(GenerativeUI[BaseComboRowItem | str | None]):
    """Combo row whose widget uses items and value layer stores strings.
    None represents no selection."""

    def __init__(self,
                 action_core: "ActionCore",
                 var_name: str,
                 default_value: BaseComboRowItem | str,
                 items: list[BaseComboRowItem] | list[str],
                 title: str | None = None,
                 subtitle: str | None = None,
                 enable_search: bool = False,
                 on_change: Callable[..., Any] | None = None,
                 can_reset: bool = True,
                 auto_add: bool = True,
                 complex_var_name: bool = False
                 ):
        """
        Initializes the ComboRow UI element.

        Args:
            action_core (ActionCore): The associated action instance.
            var_name (str): The variable name for storing the selected value.
            default_value (BaseComboRowItem | str): The default selected item.
            items (list[BaseComboRowItem] | list[str]): The list of selectable items.
            title (str, optional): The title of the combo box. Defaults to None.
            subtitle (str, optional): The subtitle of the combo box. Defaults to None.
            enable_search (bool, optional): Enables search functionality. Defaults to False.
            on_change (callable, optional): Callback triggered when selection changes. Defaults to None.
            can_reset (bool, optional): Whether resetting is allowed. Defaults to True.
            auto_add (bool, optional): Whether to automatically add this UI element to the action. Defaults to True.
        """
        def build() -> None:
            self._widget: Combo | None = Combo(
                title=self.get_translation(title, title),
                subtitle=self.get_translation(subtitle, subtitle),
                items=items,
                enable_search=enable_search,
                default_selection=self._default_value
            )
            self._handle_reset_button_creation()
            self.connect_signals()
        super().__init__(action_core, var_name, default_value, can_reset, auto_add, complex_var_name, on_change, build=build)

    @on_main
    def set_sensitive(self, sensitive: bool) -> None:
        self.widget.set_sensitive(sensitive)

    def get_sensitive(self) -> bool:
        return cast(bool, self.widget.get_sensitive())

    @override
    def connect_signals(self) -> None:
        """Connects the signal to detect selection changes in the combo box."""
        self._track_connect("selected", self.widget, "notify::selected", self._value_changed)

    @override
    def disconnect_signals(self) -> None:
        """Disconnects the signal for selection changes."""
        self._track_disconnect("selected", self.widget)

    def _value_changed(self, combo_row: Combo, _: Any) -> None:
        """Handles the event when a new item is selected."""
        item = combo_row.get_selected_item()
        self._handle_value_changed(item)

    @override
    def _handle_value_changed(self, new_value: BaseComboRowItem | str | None, update_settings: bool = True, trigger_callback: bool = True) -> None:
        """Handles updating the stored value and triggering the change callback."""
        old_value = self.get_value(self._default_value)

        if update_settings:
            self.set_value(new_value)

        if trigger_callback and self.on_change:
            old_value = self.get_item(old_value)

            # Pass the raw widget reference so value changes do not force a build.
            self.on_change(self._widget, new_value, old_value)

    @override
    def reset_value(self) -> None:
        """Reset the selection to its default.
        An unbuilt row stores the default and skips the callback because it cannot resolve items.
        """
        if self._widget is None:
            self.set_value(self._default_value)
            return
        self._reset_value_on_widget()

    @GenerativeUI.signal_manager
    def _reset_value_on_widget(self) -> None:
        selected_item = self.widget.set_selected_item(self._default_value)
        self._handle_value_changed(selected_item)

    @override
    def load_initial_ui(self) -> None:
        value = self.get_value()
        selected_item = self.widget.set_selected_item(value)
        self._handle_value_changed(selected_item, False)

    @GenerativeUI.signal_manager
    @override
    def set_ui_value(self, value: BaseComboRowItem | str | None) -> None:
        """Sets the selected item in the UI."""
        self.widget.set_selected_item(value)

    @override
    def set_value(self, value: BaseComboRowItem | str | None) -> None:
        """Sets the selected item in the UI."""

        resolved: str | None
        if isinstance(value, BaseComboRowItem):
            resolved = value.get_value()
        else:
            resolved = value

        super().set_value(resolved)

    # Widget Wrappers

    @on_main
    def set_selected_item(self, item: BaseComboRowItem | str = "", update_setting: bool = False) -> "BaseComboRowItem | None":
        """Sets the selected item and optionally updates the stored value."""
        selected_item = cast("BaseComboRowItem | None", self.widget.set_selected_item(item))
        if update_setting:
            self.set_value(selected_item)
        return selected_item

    @GenerativeUI.signal_manager
    def add_item(self, combo_row_item: BaseComboRowItem | str) -> None:
        """Adds a single item to the combo box."""
        self.widget.add_item(combo_row_item)

    @GenerativeUI.signal_manager
    def add_items(self, items: list[BaseComboRowItem] | list[str]) -> None:
        """Adds multiple items to the combo box."""
        self.widget.add_items(items)

    @GenerativeUI.signal_manager
    def remove_item_at_index(self, index: int) -> None:
        """Removes an item from the combo box by its index."""
        self.widget.remove_item_at_index(index)

    @GenerativeUI.signal_manager
    def remove_item(self, item: BaseComboRowItem | str) -> None:
        """Removes an item from the combo box by its value."""
        self.widget.remove_item(item)

    @GenerativeUI.signal_manager
    def remove_items(self, start: int, amount: int) -> None:
        """Removes a range of items from the combo box."""
        self.widget.remove_items(start, amount)

    @GenerativeUI.signal_manager
    def remove_all_items(self) -> None:
        """Clears all items from the combo box."""
        self.widget.remove_all_items()

    def get_item_at(self, index: int) -> BaseComboRowItem | None:
        """Retrieves an item at a specific index."""
        return cast("BaseComboRowItem | None", self.widget.get_item_at(index))

    def get_item(self, name: BaseComboRowItem | str | None) -> BaseComboRowItem | None:
        """Retrieves an item by its name."""
        return cast("BaseComboRowItem | None", self.widget.get_item(name))

    def get_selected_item(self) -> BaseComboRowItem | None:
        """Returns the currently selected item."""
        return cast("BaseComboRowItem | None", self.widget.get_selected_item())

    def get_item_amount(self) -> int:
        return cast(int, self.widget.get_item_amount())

    @GenerativeUI.signal_manager
    def populate(self, items: list[BaseComboRowItem] | list[str], selected_item: BaseComboRowItem | str = "",
                 update_settings: bool = False,
                 trigger_callback: bool = True) -> None:
        """Repopulates the combo box with new items and optionally updates the selection."""
        self.widget.remove_all_items()
        self.widget.add_items(items)
        selected_item = self.widget.set_selected_item(selected_item)

        self._handle_value_changed(selected_item, update_settings, trigger_callback)
        self.widget.set_selected_item(selected_item)
