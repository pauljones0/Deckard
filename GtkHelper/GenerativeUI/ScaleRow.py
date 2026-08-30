from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI

from gi.repository import Gtk

from collections.abc import Callable
from typing import cast, TYPE_CHECKING, Any, override

from GtkHelper.GtkHelper import on_main

if TYPE_CHECKING:
    from src.backend.PluginManager.ActionCore import ActionCore

from GtkHelper.ScaleRow import ScaleRow as Scale

class ScaleRow(GenerativeUI[float]):
    """Slider row with optional text entry and configurable range, step, and precision."""

    def __init__(self, action_core: "ActionCore",
                 var_name: str,
                 default_value: float,
                 min: float,
                 max: float,
                 title: str | None = None,
                 subtitle: str | None = None,
                 step: float = 0.1,
                 digits: int = 2,
                 draw_value: bool = True,
                 round_digits: int = 1,
                 add_text_entry: bool = False,
                 text_entry_max_length: int = 6,
                 on_change: Callable[..., Any] | None = None,
                 can_reset: bool = True,
                 auto_add: bool = True,
                 complex_var_name: bool = False
                 ):
        """Initialize the slider range, step, precision, value display, and optional text entry.
        A round_digits value of -1 disables rounding."""
        def build() -> None:
            self._widget: Scale | None = Scale(
                title=self.get_translation(title, title),
                subtitle=self.get_translation(subtitle, subtitle),
                value=self._default_value,
                min=min,
                max=max,
                add_text_entry=add_text_entry,
                step=step,
                digits=digits,
                draw_value=draw_value,
                round_digits=round_digits,
                text_entry_max_length=text_entry_max_length,
            )
            self._handle_reset_button_creation()
            self.connect_signals()
        super().__init__(action_core, var_name, default_value, can_reset, auto_add, complex_var_name, on_change, build=build)

    @override
    def connect_signals(self) -> None:
        """
        Connects the signal handler for the 'value-changed' signal to track changes in the scale's value.

        This ensures that when the scale value is changed, the appropriate callback is called to handle the change.
        """
        self._track_connect("value-changed", self.widget.scale, "value-changed", self._value_changed)

    @override
    def disconnect_signals(self) -> None:
        self._track_disconnect("value-changed", self.widget.scale)

    def set_number(self, number: float, update_setting: bool = False) -> None:
        """
        Sets the scale value and optionally updates the associated setting.

        Args:
            number (float): The new value for the scale.
            update_setting (bool, optional): If True, updates the setting with the new scale value. Defaults to False.
        """
        self.set_ui_value(number)

        if update_setting:
            self.set_value(number)

    def get_number(self) -> float:
        """Return the scale or settings value without forcing a build."""
        if self._widget is None:
            return self.get_value()
        return cast(float, self.widget.scale.get_value())

    def _value_changed(self, scale: Gtk.Scale) -> None:
        """
        Handles the change in scale value.

        This method is triggered when the user adjusts the scale, updating the associated value.

        Args:
            scale (Gtk.Scale): The scale widget whose value changed.
        """
        self._handle_value_changed(scale.get_value())

    @GenerativeUI.signal_manager
    @override
    def set_ui_value(self, value: float) -> None:
        """
        Sets the value of the scale widget in the UI.

        Args:
            value (float): The value to set in the scale widget.
        """
        self.widget.scale.set_value(value)

    @on_main
    def set_min(self, min: float) -> None:
        """
        Sets the minimum value for the scale.

        Args:
            min (float): The minimum value for the scale.
        """
        self.widget.set_min(min)

    @on_main
    def set_max(self, max: float) -> None:
        """
        Sets the maximum value for the scale.

        Args:
            max (float): The maximum value for the scale.
        """
        self.widget.set_max(max)

    @on_main
    def set_step(self, step: float) -> None:
        """
        Sets the step size for adjusting the scale value.

        Args:
            step (float): The step size for the scale.
        """
        self.widget.set_step(step)

    @property
    def min(self) -> float:
        """
        Gets the minimum value for the scale.

        Returns:
            float: The minimum value for the scale.
        """
        return cast(float, self.widget.min)

    @min.setter
    @on_main
    def min(self, value: float) -> None:
        """
        Sets the minimum value for the scale.

        Args:
            value (float): The new minimum value for the scale.
        """
        self.widget.min = value

    @property
    def max(self) -> float:
        """
        Gets the maximum value for the scale.

        Returns:
            float: The maximum value for the scale.
        """
        return cast(float, self.widget.max)

    @max.setter
    @on_main
    def max(self, value: float) -> None:
        """
        Sets the maximum value for the scale.

        Args:
            value (float): The new maximum value for the scale.
        """
        self.widget.max = value

    @property
    def step(self) -> float:
        """
        Gets the step size for adjusting the scale value.

        Returns:
            float: The step size for the scale.
        """
        return cast(float, self.widget.step)

    @step.setter
    @on_main
    def step(self, value: float) -> None:
        """
        Sets the step size for adjusting the scale value.

        Args:
            value (float): The new step size for the scale.
        """
        self.widget.step = value

    @property
    def digits(self) -> int:
        """
        Gets the number of digits to display for the scale value.

        Returns:
            int: The number of digits for the scale value.
        """
        # digits is widget-construction config, and the value layer holds no
        # equivalent, so this read builds the widget.
        return cast(int, self.widget.digits)

    @digits.setter
    @on_main
    def digits(self, digits: int) -> None:
        """
        Sets the number of digits to display for the scale value.

        Args:
            digits (int): The number of digits for the scale value.
        """
        self.widget.digits = digits
