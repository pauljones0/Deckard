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
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.windows.mainWindow.mainWindow import MainWindow

# Import Python modules

# Import globals
import globals as gl

class DeckSwitcher(Gtk.Box):
    def __init__(self, main_window: "MainWindow", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.main_window = main_window
        self.build()

        no_decks = len(main_window.deck_manager.deck_controller) == 0
        self.set_show_switcher(not no_decks)


    def build(self) -> None:
        self.switcher = Gtk.StackSwitcher(hexpand=False, margin_start=75, margin_end=75)
        self.append(self.switcher)

        self.label = Gtk.Label(label=gl.lm.get("deck-switcher-no-decks"), css_classes=["bold"])
        self.append(self.label)

    def set_show_switcher(self, show: bool) -> None:
        if show:
            self.switcher.set_visible(True)
            self.label.set_visible(False)
        else:
            self.switcher.set_visible(False)
            self.label.set_visible(True)

    def set_label_text(self, text: str) -> None:
        self.label.set_text(text)