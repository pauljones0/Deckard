
from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI
from GtkHelper.ColorButtonRow import ColorButtonRow as ColorDialog

from gi.repository import Gdk, Gtk

from typing import cast, TYPE_CHECKING, Callable, override


if TYPE_CHECKING:
    from src.backend.PluginManager.ActionCore import ActionCore


class ColorButtonRow(GenerativeUI[tuple[int, int, int, int]]):
    """
    A UI component for selecting and managing colors, extending GenerativeUI.

    Attributes:
        _widget (ColorDialog): The color selection dialog.
    """

    def __init__(self,
                 action_core: "ActionCore",
                 var_name: str,
                 default_value: tuple[int, int, int, int],
                 title: str | None = None,
                 subtitle: str | None = None,
                 on_change: Callable[[Gtk.Widget, tuple[int, int, int, int], tuple[int, int, int, int]], None] | None = None,
                 can_reset: bool = True,
                 auto_add: bool = True,
                 complex_var_name: bool = False
                 ):
        """
        Initializes the ColorButtonRow UI component.

        Args:
            action_core (ActionCore): The action this UI element is associated with.
            var_name (str): The key used to store the value in the action's settings.
            default_value (tuple[int, int, int, int]): The default RGBA color.
            title (str, optional): The title for the UI element.
            subtitle (str, optional): The subtitle for the UI element.
            on_change (Callable, optional): Function called when the color changes.
            can_reset (bool, optional): Whether the UI element can be reset. Defaults to True.
            auto_add (bool, optional): Whether the UI element is automatically added to the action. Defaults to True.
        """
        def build() -> None:
            self._widget: ColorDialog | None = ColorDialog(
                title=self.get_translation(title),
                subtitle=self.get_translation(subtitle),
                default_color=self._default_value
            )

            self._handle_reset_button_creation()
            self.connect_signals()
        super().__init__(action_core, var_name, default_value, can_reset, auto_add, complex_var_name, on_change, build=build)

    @override
    def connect_signals(self) -> None:
        """
        Connects the necessary signals for detecting color changes.
        """
        self._track_connect("color-set", self.widget.color_button, "color-set", self._value_changed)

    @override
    def disconnect_signals(self) -> None:
        """
        Disconnects signals to prevent unwanted behavior.
        """
        self._track_disconnect("color-set", self.widget.color_button)

    def set_color(self, color: tuple[int, int, int, int], update_setting: bool) -> None:
        """
        Sets the color in the UI and optionally updates the stored value.

        Args:
            color (tuple[int, int, int, int]): The new RGBA color.
            update_setting (bool): Whether to update the stored setting.
        """
        self.set_ui_value(color)

        if update_setting:
            self.set_value(color)

    def get_color(self) -> tuple[int, int, int, int]:
        """
        Return the selected color, or the stored value while unbuilt.
        A color read must not force a build.

        Returns:
            tuple[int, int, int, int]: The RGBA color tuple.
        """
        if self._widget is None:
            return self.get_value()
        return cast(tuple[int, int, int, int], self.widget.color)

    def _value_changed(self, button: Gtk.ColorButton) -> None:
        """
        Handles the event when the color is changed in the UI.

        Args:
            button (Gtk.ColorButton): The color button that triggered the event.
        """
        self._handle_value_changed(self.widget.color)

    @GenerativeUI.signal_manager
    @override
    def set_ui_value(self, value: tuple[int, int, int, int]) -> None:
        """
        Updates the UI with the given color.

        Args:
            value (tuple[int, int, int, int]): The new RGBA color value.
        """
        self.widget.color = value

    def convert_from_rgba(self, color: Gdk.RGBA) -> tuple[int, int, int, int]:
        return cast(tuple[int, int, int, int], self.widget.convert_from_rgba(color))

    def convert_to_rgba(self, color: tuple[int, int, int, int]) -> Gdk.RGBA:
        return cast("Gdk.RGBA", self.widget.convert_to_rgba(color))

    def normalize_to_255(self, color: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
        return cast(tuple[int, int, int, int], self.widget.normalize_to_255(color))

    def normalize_to_1(self, color: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
        return cast(tuple[float, float, float, float], self.widget.normalize_to_1(color))
