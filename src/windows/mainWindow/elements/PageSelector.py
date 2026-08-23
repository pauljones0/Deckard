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
from gi.repository import GLib, GObject, Gtk, Pango

# Import Python modules
from functools import lru_cache
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.windows.mainWindow.mainWindow import MainWindow


from loguru import logger as log
from rapidfuzz import fuzz

# Import globas
from src.backend import services

import globals as gl

# Import own modules
from src.windows.PageManager.PageManager import PageManager
from src.Signals import Signals

# A page whose name scores at or below this against the query is dropped from
# the list. The page-manager selector uses the same bar, so a query keeps the
# same pages in both places.
MATCH_THRESHOLD = 50.0

# Rows the list shows before it scrolls. The popover sizes itself to its
# content, so an unbounded list would grow taller than the window.
MAX_LIST_HEIGHT = 300


def page_display_name(page_path: str) -> str:
    """The page name a user reads, which is the file name without .json."""
    return os.path.splitext(os.path.basename(page_path))[0]


@lru_cache(maxsize=1000)
def match_ratio(name: str, search: str) -> float:
    """How well a page name matches a query, from 0 to 100.

    A module-level function so the cache keys on the two strings alone and
    never pins a widget.
    """
    return float(fuzz.ratio(name.lower(), search.lower()))


class PageSelector(Gtk.Box):
    def __init__(self, main_window: "MainWindow", page_manager: Any, **kwargs: Any) -> None:
        self.main_window = main_window
        self.page_manager = page_manager
        self.page_rows: list[PageRow] = []
        self.selected_page_path: str | None = None
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, **kwargs)
        self.build()

    def build(self) -> None:
        # Label
        self.label = Gtk.Label(label=gl.lm.get("header-page-selector-page-label"), margin_start=3, margin_end=7, css_classes=["bold"])
        self.append(self.label)

        # Right area
        self.sidebar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, css_classes=["linked"])
        self.append(self.sidebar)

        # The button shows the page the active deck holds and opens the list.
        self.page_label = Gtk.Label(label="", xalign=0, hexpand=True,
                                    ellipsize=Pango.EllipsizeMode.END, max_width_chars=20)
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.append(self.page_label)
        button_box.append(Gtk.Image(icon_name="pan-down-symbolic"))

        self.page_button = Gtk.MenuButton(css_classes=["header-page-dropdown"], hexpand=False,
                                          tooltip_text=gl.lm.get("header-page-selector-drop-down-hint"))
        self.page_button.set_child(button_box)
        self.sidebar.append(self.page_button)

        self.popover = Gtk.Popover()
        self.popover.connect("notify::visible", self.on_popover_visible)
        self.page_button.set_popover(self.popover)

        popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.popover.set_child(popover_box)

        self.search_entry = Gtk.SearchEntry(placeholder_text=gl.lm.get("header-page-selector-search-hint"), hexpand=True)
        self.search_entry.connect("search-changed", self.on_search_changed)
        popover_box.append(self.search_entry)

        self.scrolled_window = Gtk.ScrolledWindow(propagate_natural_height=True,
                                                  max_content_height=MAX_LIST_HEIGHT,
                                                  hscrollbar_policy=Gtk.PolicyType.NEVER)
        popover_box.append(self.scrolled_window)

        self.list_box = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.SINGLE)
        self.list_box.set_filter_func(self.filter_func)
        self.list_box.set_sort_func(self.sort_func)
        self.list_box.connect("row-activated", self.on_row_activated)
        self.scrolled_window.set_child(self.list_box)

        self.update()

        # Settings button
        self.open_settings_button = Gtk.Button(icon_name="folder-documents-symbolic", tooltip_text=gl.lm.get("header-page-selector-page-settings-hint"))
        self.open_settings_button.connect("clicked", self.on_click_open_page_settings)
        self.sidebar.append(self.open_settings_button)

        # Manager button
        self.open_manager_button = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text=gl.lm.get("header-page-selector-page-manager-hint"))
        self.open_manager_button.connect("clicked", self.on_click_open_page_manager)
        self.sidebar.append(self.open_manager_button)

        gl.signal_manager.connect_signal(signal=Signals.ChangePage, callback=self.update_selected)
        gl.signal_manager.connect_signal(signal=Signals.PageRename, callback=self.update)
        gl.signal_manager.connect_signal(signal=Signals.PageAdd, callback=self.update)
        gl.signal_manager.connect_signal(signal=Signals.PageDelete, callback=self.update)

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Rebuild the list from the backend, then mark the active page."""
        self.page_rows.clear()
        self.list_box.remove_all()
        for order, page_path in enumerate(self.page_manager.get_pages()):
            row = PageRow(page_path=page_path, order=order)
            self.page_rows.append(row)
            self.list_box.append(row)

        self.update_selected()

    def set_selected(self, page_path: str) -> None:
        self.selected_page_path = page_path
        self.page_label.set_label(page_display_name(page_path))
        for row in self.page_rows:
            if row.page_path == page_path:
                self.list_box.select_row(row)
                return
        # The active page carries a path the backend no longer lists, such as
        # a page a plugin holds. Name it on the button, and mark no row.
        self.list_box.select_row(None)

    def update_selected(self, *args: Any, **kwargs: Any) -> None:
        child = self.main_window.leftArea.deck_stack.get_visible_child()
        if child is None:
            self.page_button.set_sensitive(False)
            return
        else:
            self.page_button.set_sensitive(True)
        active_controller = child.deck_controller
        page = active_controller.active_page
        if page is None:
            return
        page_path = page.json_path
        self.set_selected(page_path)

    def filter_func(self, row: "PageRow") -> bool:
        search = self.search_entry.get_text()
        if search == "":
            return True
        return match_ratio(page_display_name(row.page_path), search) > MATCH_THRESHOLD

    def sort_func(self, row1: "PageRow", row2: "PageRow") -> int:
        """-1 when row1 comes first, 1 when row2 does, 0 when they tie."""
        search = self.search_entry.get_text()
        if search != "":
            score1 = match_ratio(page_display_name(row1.page_path), search)
            score2 = match_ratio(page_display_name(row2.page_path), search)
            if score1 > score2:
                return -1
            if score1 < score2:
                return 1

        # get_pages() returns the paths in natural name order, so the position
        # each row took in that list is the alphabetical order already. It also
        # breaks a score tie, which keeps equally scored names in name order.
        if row1.order < row2.order:
            return -1
        if row1.order > row2.order:
            return 1
        return 0

    def on_search_changed(self, search_entry: Gtk.SearchEntry) -> None:
        self.list_box.invalidate_filter()
        self.list_box.invalidate_sort()

    def on_popover_visible(self, popover: Gtk.Popover, param: GObject.ParamSpec) -> None:
        if not popover.get_visible():
            return
        # Every open starts on the whole list, and typing narrows it from
        # there. A query left over from the last open would hide pages the
        # user never filtered out.
        self.search_entry.set_text("")
        self.list_box.invalidate_filter()
        self.list_box.invalidate_sort()
        self.search_entry.grab_focus()

    def on_row_activated(self, list_box: Gtk.ListBox, row: "PageRow") -> None:
        self.popover.popdown()
        self.rearm_focus()
        self.change_page(row.page_path)

    def rearm_focus(self) -> None:
        """Give the header button the keyboard focus back after a pick.

        The popover holds the focus while it is open and hands back nothing
        when it closes, so the header ends up with no focused widget at all.
        The grab waits for the next main-loop turn because the popdown has not
        finished yet at this point.
        """
        GLib.idle_add(self.grab_page_button_focus)

    def grab_page_button_focus(self) -> bool:
        self.page_button.grab_focus()
        # grab_focus() answers True when it worked, and an idle callback that
        # returns True runs again forever. Say so explicitly instead.
        return GLib.SOURCE_REMOVE

    def change_page(self, page_path: str) -> None:
        active_child = self.main_window.leftArea.deck_stack.get_visible_child()
        if active_child is None:
            return

        active_controller = active_child.deck_controller

        if active_controller.active_page is not None and active_controller.active_page.json_path == page_path:
            # The selector matches a switch that the deck triggered, so the
            # page already loads or is loaded. Do not start a second load.
            return
        page = services.require_page_manager().get_page(path=page_path, deck_controller = active_controller)
        log.info(f"Load page: {page}")
        active_controller.load_page(page)

    def on_click_open_page_manager(self, button: Gtk.Button) -> None:
        if gl.page_manager_window is not None:
            gl.page_manager_window.present()
            return
        gl.page_manager_window = PageManager(main_win=services.require_main_window())
        gl.page_manager_window.present()

    def on_click_open_page_settings(self, button: Gtk.Button) -> None:
        self.on_click_open_page_manager(button)

        page_path = self.selected_page_path
        if page_path is None:
            # Nothing is selected, so open the manager and activate no page.
            return
        page_manager_window = gl.page_manager_window
        if page_manager_window is None:
            # The call above binds the global or raises out of this method,
            # so this arm cannot run. It stands because the slot is typed
            # optional and nothing narrows it across the call.
            return
        page_manager_window.page_selector.activate_page(page_path)


class PageRow(Gtk.ListBoxRow):
    """One page in the list. `order` is its place in the backend's own order."""

    def __init__(self, page_path: str, order: int) -> None:
        super().__init__()
        self.page_path = page_path
        self.order = order

        self.label = Gtk.Label(label=page_display_name(page_path), xalign=0, hexpand=True,
                               ellipsize=Pango.EllipsizeMode.END, max_width_chars=30)
        self.set_child(self.label)
