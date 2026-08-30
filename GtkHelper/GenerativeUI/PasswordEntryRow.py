from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI

import base64
from gi.repository import Adw

from collections.abc import Callable
from typing import cast, TYPE_CHECKING, Any, override


if TYPE_CHECKING:
    from src.backend.PluginManager.ActionCore import ActionCore


class PasswordEntryRow(GenerativeUI[str]):
    """Password entry row that stores values as base64 text."""

    def __init__(self, action_core: "ActionCore",
                 var_name: str,
                 default_value: str,
                 title: str | None = None,
                 on_change: Callable[..., Any] | None = None,
                 can_reset: bool = True,
                 auto_add: bool = True,
                 complex_var_name: bool = False
                 ):
        """
        Initializes the PasswordEntryRow widget, setting up the password entry UI component.

        Args:
            action_core (ActionCore): The base action that provides context for this password entry row.
            var_name (str): The variable name to associate with this password entry row.
            default_value (str): The default password value to display in the entry field.
            title (str, optional): The title to display for the password entry row.
            on_change (callable, optional): A callback function to call when the password changes.
            can_reset (bool, optional): Whether the password can be reset. Defaults to True.
            auto_add (bool, optional): Whether to automatically add this entry to the UI. Defaults to True.
        """
        def build() -> None:
            self._widget: Adw.PasswordEntryRow | None = Adw.PasswordEntryRow(
                title=self.get_translation(title, title),
                text=self._default_value
            )

            self._handle_reset_button_creation()
            self.connect_signals()
        super().__init__(action_core, var_name, default_value, can_reset, auto_add, complex_var_name, on_change, build=build)

    @override
    def connect_signals(self) -> None:
        """
        Connects the signal handler for the 'changed' signal to track changes in the password entry.

        This ensures that when the password input changes, the value is handled accordingly.
        """
        self._track_connect("changed", self.widget, "changed", self._value_changed)

    @override
    def disconnect_signals(self) -> None:
        self._track_disconnect("changed", self.widget)

    def set_password(self, password: str, update_setting: bool = False) -> None:
        """
        Sets the password in the password entry widget and optionally updates the associated setting.

        Args:
            password (str): The password to set in the entry field.
            update_setting (bool, optional): If True, updates the setting with the new password. Defaults to False.
        """
        self.set_ui_value(password)

        if update_setting:
            self.set_value(password)

    def get_password(self) -> str:
        """Return the widget password or the decoded setting without forcing a build."""
        if self._widget is None:
            return self.get_value()
        return cast(str, self.widget.get_text())

    def _value_changed(self, entry_row: Adw.EntryRow) -> None:
        """
        Handles the change in password input in the password entry row.

        This method is triggered when the user changes the password in the entry field,
        updating the associated value accordingly.

        Args:
            entry_row (Adw.EntryRow): The password entry row widget whose value changed.
        """
        self._handle_value_changed(entry_row.get_text())

    @override
    def get_value(self, fallback: str | None = None) -> str:
        """
        Retrieves the stored password value, decoding it from base64.

        This method retrieves the encoded password from settings and decodes it to the original string value.

        Args:
            fallback (str, optional): A fallback value to return if no stored value is found. Defaults to None.

        Returns:
            str: The decoded password value.
        """
        value = super().get_value(fallback)
        return base64.b64decode(value).decode("utf-8")

    @override
    def set_value(self, value: str) -> None:
        """Store the password as base64 text in settings."""
        # A local annotation, not a cast. ActionCore.get_settings declares a
        # return type of dir, a typo for dict, so this file cannot use it.
        settings: dict[str, Any] = self._action_core.get_settings()

        encoded = base64.b64encode(value.encode("utf-8")).decode("utf-8")
        settings[self._var_name] = encoded
        self._action_core.set_settings(settings)

    @GenerativeUI.signal_manager
    @override
    def set_ui_value(self, value: str) -> None:
        """
        Sets the password value in the UI password entry widget.

        Args:
            value (str): The password value to set in the UI widget.
        """
        self.widget.set_text(value)
