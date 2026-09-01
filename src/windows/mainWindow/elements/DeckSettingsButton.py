import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

# Import globals

# Import typing
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.mainWindow.mainWindow import MainWindow

class DeckSettingsButton(Gtk.Button):
    def __init__(self, main_window: "MainWindow", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.main_window: "MainWindow" = main_window
        self.deck_settings_icon = "drive-removable-media-symbolic"
        self.page_settings_icon = "input-dialpad-symbolic"

        self.build()

        self.connect("clicked", self.toggle)

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.set_child(self.main_box)

        self.icon = Gtk.Image(icon_name=self.deck_settings_icon)
        self.main_box.append(self.icon)

        self.label = Gtk.Label(label="Deck Settings")
        self.main_box.append(self.label)

    def set_state(self, state: str) -> None:
        if state == "page-settings":
            self.icon.set_from_icon_name(self.deck_settings_icon)
            self.label.set_label("Deck Settings")
        else:
            self.icon.set_from_icon_name(self.page_settings_icon)
            self.label.set_label("Key Grid")

    def refresh_state(self) -> None:
        # Do not use update_state because Gtk.Accessible already defines it.
        # Overriding it would swallow accessibility states and values.
        deck_stack_child = self.main_window.leftArea.deck_stack.get_visible_child()
        if deck_stack_child is None:
            return
        if deck_stack_child.stack.get_visible_child_name() == "page-settings":
            self.set_state("page-settings")
        else:
            self.set_state("deck-settings")

    def toggle(self, button: "DeckSettingsButton") -> None:
        deck_stack_child = self.main_window.leftArea.deck_stack.get_visible_child()
        if deck_stack_child is None:
            # The last deck went while the button was still up: set_main_error
            # hides it through an idle, so a click can land before that runs.
            return
        if deck_stack_child.stack.get_visible_child_name() == "page-settings":
            deck_stack_child.stack.set_visible_child_name("deck-settings")
        else:
            deck_stack_child.stack.set_visible_child_name("page-settings")

        self.refresh_state()
