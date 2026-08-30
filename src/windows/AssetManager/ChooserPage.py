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
from gi.repository import Gtk, Gdk, GLib

from typing import Any

import globals as gl

from src.windows.AssetManager import asset_search

# Wait longer than GTK's 150 ms default because a pass scores thousands of names
# GTK still reports a cleared entry immediately
SEARCH_DELAY_MS = 300


class ChooserPage(Gtk.Stack):
    # _build can emit before a subclass initializes, so these defaults are class-level
    _search_generation = 0
    _search_showing = True
    # Last rendered query, used to catch up only grids that changed while hidden
    _searched_text = ""

    def __init__(self) -> None:
        super().__init__(margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        # Unmap invalidates delayed emissions before they render into a hidden page
        self.connect("map", self._on_map)
        self.connect("unmap", self.invalidate_search)
        self._build()

        self.init_dnd()

    def _build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.add_titled(self.main_box, "main", "main")

        self.nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, vexpand=False, margin_bottom=15)
        self.main_box.append(self.nav_box)

        self.search_entry = Gtk.SearchEntry(placeholder_text="Search", hexpand=True)
        self.search_entry.set_search_delay(SEARCH_DELAY_MS)
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.nav_box.append(self.search_entry)

        self.type_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, css_classes=["linked"], margin_start=15)
        self.nav_box.append(self.type_box)

        self.video_button = Gtk.ToggleButton(icon_name="camera-video-symbolic", css_classes=["blue-toggle-button"])
        self.video_button.connect("toggled", self.on_video_toggled)
        self.type_box.append(self.video_button)

        self.image_button = Gtk.ToggleButton(icon_name="camera-photo-symbolic", css_classes=["blue-toggle-button"])
        self.image_button.connect("toggled", self.on_image_toggled)
        self.type_box.append(self.image_button)

        self.scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.main_box.append(self.scrolled_window)

        self.scrolled_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=False,
                                margin_top=5, margin_bottom=5)
        self.scrolled_window.set_child(self.scrolled_box)

        self.inside_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=False)
        self.scrolled_box.append(self.inside_box)


        # Add vexpand box to the bottom to avoid unwanted stretching of the children
        self.fix_box = Gtk.Box(vexpand=True, hexpand=True)
        self.scrolled_box.append(self.fix_box)


        ## Loading box
        self.loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True,
                                   valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.add_titled(self.loading_box, "loading", "loading")

        self.spinner = Gtk.Spinner(spinning=False)
        self.loading_box.append(self.spinner)

        self.loading_label = Gtk.Label(label=gl.lm.get("store.page.loading-spinner.label"))
        self.loading_box.append(self.loading_label)

        self.set_loading(True)

    def set_loading(self, loading: bool) -> None:
        if loading:
            GLib.idle_add(self.set_visible_child_name, "loading")
            GLib.idle_add(self.spinner.start)
        else:
            GLib.idle_add(self.set_visible_child_name, "main")
            GLib.idle_add(self.spinner.stop)

    def init_dnd(self) -> None:
        self.dnd_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        self.dnd_target.connect("drop", self.on_dnd_drop)
        self.dnd_target.connect("accept", self.on_dnd_accept)

        self.main_box.add_controller(self.dnd_target)

    def on_dnd_accept(self, drop: Gtk.DropTarget, user_data: Gdk.Drop) -> bool | None:
        pass
    
    def on_dnd_drop(self, drop_target: Gtk.DropTarget, value: Any, x: float, y: float) -> bool | None:
        pass

    def show_for_path(self, path: str) -> None:
        pass

    def on_video_toggled(self, button: Gtk.ToggleButton) -> None:
        pass

    def on_image_toggled(self, button: Gtk.ToggleButton) -> None:
        pass

    def on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        """Queue the current query after the entry delay.

        Pages override apply_search so this shared staleness guard stays active.
        """
        if not self._search_showing:
            # The entry held this emission for its delay, and the page stopped
            # showing in between. It renders again when it is shown.
            return
        self._search_generation += 1
        # One loop turn lets the generation guard merge immediate and delayed emissions
        GLib.idle_add(self.run_search, self._search_generation)

    def run_search(self, generation: int) -> bool:
        """Render the query unless a later pass has overtaken this one."""
        if generation != self._search_generation:
            # A newer pass, or invalidate_search, moved the generation on. The
            # newer pass renders the text the user has now.
            return False
        query = self.search_entry.get_text()
        self.apply_search(query)
        return False

    def search_rendered(self, query: str) -> None:
        """Record a query only after its results are visible.

        A dropped worker pass must leave this stale so the next map catches up.
        """
        self._searched_text = query

    def search_is_current(self, generation: int) -> bool:
        """Whether this visible page still owns the specified search generation."""
        return generation == self._search_generation and self._search_showing

    def focus_search_entry(self) -> bool:
        """Focus the entry and move its cursor to the end to preserve the query."""
        self.search_entry.grab_focus()
        self.search_entry.set_position(-1)
        return False

    def invalidate_search(self, *args: Any) -> None:
        """Invalidate pending searches and release their scoring cache until remap."""
        self._search_showing = False
        self._search_generation += 1
        asset_search.release_cache()

    def _on_map(self, *args: Any) -> None:
        """Render only an entry that changed while the page was hidden."""
        self._search_showing = True
        # Settle the entry before deciding whether the visible query needs catch-up
        self.on_shown()
        if self.search_entry.get_text() == self._searched_text:
            return
        self._search_generation += 1
        self.run_search(self._search_generation)

    def on_shown(self) -> None:
        """Settle the entry on the main thread before the map catch-up pass."""

    def apply_search(self, query: str) -> None:
        """Show the query on the main thread and record it only after rendering."""
