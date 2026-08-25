from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI

from gi.repository import Adw, Gtk

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast


from GtkHelper.ToggleRow import ToggleRow as Toggle

if TYPE_CHECKING:
    from src.backend.PluginManager.ActionCore import ActionCore

class ToggleRow(GenerativeUI[int]):
    """The stored value is the *index* of the active toggle, not a flag.
    That is why this is GenerativeUI[int] (see default_value/set_ui_value
    below)."""

    def __init__(self, action_core: "ActionCore",
                 var_name: str,
                 default_value: int,
                 toggles: list[Adw.Toggle] | None = None,
                 title: str | None = None,
                 subtitle: str | None = None,
                 can_shrink: bool = True,
                 homogeneous: bool = True,
                 active: bool = True,
                 on_change: Callable[..., Any] | None = None,
                 can_reset: bool = True,
                 auto_add: bool = True,
                 complex_var_name: bool = False
                 ):
        def build() -> None:
            self._widget: Toggle | None = Toggle(
                toggles = toggles or [],
                active_toggle = self._default_value,
                title=self.get_translation(title),
                subtitle=self.get_translation(subtitle),
                can_shrink=can_shrink,
                homogeneous=homogeneous,
                active=active
            )

            self._handle_reset_button_creation()
            self.connect_signals()
        super().__init__(action_core, var_name, default_value, can_reset, auto_add, complex_var_name, on_change, build=build)

    def _handle_value_changed(self, new_value: int, update_settings: bool = True, trigger_callback: bool = True) -> None:
        old_value = self.get_value()

        if update_settings:
            self.set_value(new_value)

        if trigger_callback and self.on_change:
            # A toggle object needs the widget, so this branch runs only
            # after the widget exists. reset_value and _value_changed both
            # guarantee a built widget before this point. build() makes a
            # Toggle, while the base declares the plain Gtk.Widget.
            assert isinstance(self._widget, Toggle)
            toggle_row = self._widget
            new_toggle = toggle_row.get_toggle_at(new_value)
            old_toggle = toggle_row.get_toggle_at(old_value)

            # This row deviates from the base contract: on_change receives
            # the Toggle objects, not the stored index values.
            on_change = cast("Callable[[Any, Any, Any], None]", self.on_change)
            on_change(toggle_row, new_toggle, old_toggle)

    def connect_signals(self) -> None:
        self._track_connect("active", self.widget.toggle_group, "notify::active", self._value_changed)

    def disconnect_signals(self) -> None:
        self._track_disconnect("active", self.widget.toggle_group)

    def _value_changed(self, toggle_group: Adw.ToggleGroup, _: Any) -> None:
        index = self.widget.get_active_index()
        self._handle_value_changed(index)

    @GenerativeUI.signal_manager
    def set_ui_value(self, value: int) -> None:
        self.widget.set_active_toggle(value)

    def reset_value(self) -> None:
        """Reset the active toggle to its default.

        An unbuilt row has no toggle objects to resolve the old and new values
        against, so it persists the default and skips the on_change callback.
        A reset must not force a build.
        """
        if self._widget is None:
            self.set_value(self._default_value)
            return
        self.widget.set_active_toggle(self._default_value)
        self._handle_value_changed(self._default_value)

    # Wrapper

    def get_toggles(self) -> Gtk.SelectionModel:
        return cast(Gtk.SelectionModel, self.widget.get_toggles())

    def get_n_toggles(self) -> int:
        return cast(int, self.widget.get_n_toggles())

    def get_toggle_by_name(self, name: str) -> "Adw.Toggle | None":
        return cast("Adw.Toggle | None", self.widget.get_toggle_by_name(name))

    def get_toggle_at(self, index: int) -> "Adw.Toggle | None":
        return cast("Adw.Toggle | None", self.widget.get_toggle_at(index))

    @GenerativeUI.signal_manager
    def add_toggle(self, label: str | None = None, tooltip: str | None = None, icon_name: str | None = None, name: str | None = None, enabled: bool = True) -> None:
        self.widget.add_toggle(label, tooltip, icon_name, name, enabled)

    @GenerativeUI.signal_manager
    def add_toggles(self, toggles: list[Adw.Toggle]) -> None:
        self.widget.add_toggles(toggles)

    @GenerativeUI.signal_manager
    def add_custom_toggle(self, toggle: Adw.Toggle) -> None:
        self.widget.add_custom_toggle(toggle)

    @GenerativeUI.signal_manager
    def set_active_toggle(self, index: int) -> None:
        self.widget.set_active_toggle(index)

    @GenerativeUI.signal_manager
    def set_active_by_name(self, name: str) -> None:
        self.widget.set_active_by_name(name)

    @GenerativeUI.signal_manager
    def populate(self, toggles: list[Adw.Toggle], active_index: int) -> None:
        self.widget.populate(toggles, active_index)

    @GenerativeUI.signal_manager
    def remove_toggle(self, toggle: Adw.Toggle) -> None:
        self.widget.remove_toggle(toggle)

    @GenerativeUI.signal_manager
    def remove_at(self, index: int) -> None:
        # The widget guards an out-of-range index, where get_toggle_at
        # answers None and the toggle group rejects a None remove.
        self.widget.remove_at(index)

    @GenerativeUI.signal_manager
    def remove_with_name(self, name: str) -> None:
        # The widget guards an unknown name the same way remove_at guards
        # an out-of-range index.
        self.widget.remove_with_name(name)

    @GenerativeUI.signal_manager
    def remove_all(self) -> None:
        self.widget.remove_all()