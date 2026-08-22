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

# Import python modules

# Import own modules
from src.windows.AssetManager.IconPacks.Preview import IconPackPreview
# Import typing

from typing import Any, Callable, TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.AssetManager.IconPacks.PackChooser import IconPackChooser

class IconPackFlowBox(Gtk.Box):
    def __init__(self, pack_chooser: "IconPackChooser", *args: Any, **kwargs: Any) -> None:
        # The chooser passes itself first and positionally. Nothing in this
        # widget reads it back, so it is not stored.
        super().__init__(*args, **kwargs)
        self.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.set_hexpand(True)

        self.callback_func: Callable[..., Any] | None = None
        self.callback_args: tuple[Any, ...] = ()
        self.callback_kwargs: dict[str, Any] = {}

        self.all_assets:list["IconPackPreview"] = []

        self.build()

    def build(self) -> None:
        self.flow_box = Gtk.FlowBox(hexpand=True, orientation=Gtk.Orientation.HORIZONTAL, selection_mode=Gtk.SelectionMode.NONE)
        self.append(self.flow_box)