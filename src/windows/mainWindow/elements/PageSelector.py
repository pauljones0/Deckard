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
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gtk, Pango

# Import Python modules
from functools import lru_cache
import os
from typing import TYPE_CHECKING, Any, cast

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


# Match tiers from best to worst; tier outranks score.
# A containing name must not lose to a name with only a higher fuzzy score.
TIER_PREFIX = 3
TIER_CONTAINS = 2
TIER_FUZZY = 1
TIER_NONE = 0

# The bar a name clears to stay on the fuzzy tier. rapidfuzz answers 0 to 100.
MATCH_THRESHOLD = 50.0

# How tall the list grows, in pixels, before it starts to scroll. The popover
# sizes itself to its content, so an unbounded list outgrows the window.
MAX_LIST_HEIGHT = 300

# How many frames the open waits for the list to take an allocation before it
# gives up on scrolling to the page the deck holds.
SCROLL_SETTLE_FRAMES = 60


def page_display_name(page_path: str) -> str:
    """The page name a user reads, which is the file name without .json."""
    return os.path.splitext(os.path.basename(page_path))[0]


@lru_cache(maxsize=1000)
def match_rank(name: str, search: str) -> tuple[int, float]:
    """Rank a page-name match by tier and then by a score from 0 to 100.
    Containment always survives the fuzzy threshold; module-level caching pins no widget."""
    if search == "":
        return TIER_FUZZY, 0.0

    name_folded = name.casefold()
    search_folded = search.casefold()
    # The ratio still orders a tier. For "volume" it puts volume_up (80.0)
    # over volume_down (70.6), which is the closer name first.
    ratio = float(fuzz.ratio(name_folded, search_folded))

    if name_folded.startswith(search_folded):
        return TIER_PREFIX, ratio
    if search_folded in name_folded:
        return TIER_CONTAINS, ratio

    # Use whole-string ratio for typo matches.
    # Windowed scores overrate long names that contain one matching run.
    if ratio > MATCH_THRESHOLD:
        return TIER_FUZZY, ratio
    return TIER_NONE, ratio


class PageSelector(Gtk.Box):
    def __init__(self, main_window: "MainWindow", page_manager: Any, **kwargs: Any) -> None:
        self.main_window = main_window
        self.page_manager = page_manager
        self.page_rows: list[PageRow] = []
        self.selected_page_path: str | None = None
        self.scroll_frames_left = 0
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
        self.search_entry.connect("activate", self.on_search_activate)
        # The entry consumes Up and Down, so directional focus cannot reach the rows.
        # Use a key controller to move list selection instead.
        self.search_key_controller = Gtk.EventControllerKey()
        self.search_key_controller.connect("key-pressed", self.on_search_key_pressed)
        self.search_entry.add_controller(self.search_key_controller)
        popover_box.append(self.search_entry)

        self.scrolled_window = Gtk.ScrolledWindow(propagate_natural_height=True,
                                                  max_content_height=MAX_LIST_HEIGHT,
                                                  hscrollbar_policy=Gtk.PolicyType.NEVER)
        # A focusable scrolled window takes the focus that leaves the search
        # entry and holds it, so Tab dead-ends there and never reaches a row.
        self.scrolled_window.set_focusable(False)
        popover_box.append(self.scrolled_window)

        self.list_box = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.SINGLE)
        # Shown when the list holds no row the filter kept, which covers both
        # a query that matches nothing and an install with no pages yet.
        self.list_placeholder = Gtk.Label(label=gl.lm.get("header-page-selector-empty-hint"),
                                          margin_top=12, margin_bottom=12,
                                          css_classes=["dim-label"])
        self.list_box.set_placeholder(self.list_placeholder)
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
        # remove_all() also unparents the placeholder, so attach it again.
        # Without it, an empty result appears as a blank list.
        self.list_box.set_placeholder(self.list_placeholder)
        # The header can be built before the page backend exists, and the
        # sidebar passes whatever the slot holds at that moment.
        if self.page_manager is not None:
            for order, page_path in enumerate(self.page_manager.get_pages()):
                row = PageRow(page_path=page_path, order=order)
                self.page_rows.append(row)
                self.list_box.append(row)

        self.update_selected()

    def lists_page(self, page_path: str) -> bool:
        return any(row.page_path == page_path for row in self.page_rows)

    def set_selected(self, page_path: str) -> None:
        for row in self.page_rows:
            if row.page_path == page_path:
                self.selected_page_path = page_path
                self.page_label.set_label(page_display_name(page_path))
                self.list_box.select_row(row)
                return

        # Clear pages absent from the backend, including a deleted page still held by the deck.
        # Do not leave the header or settings button pointing to a missing file.
        self.selected_page_path = None
        self.page_label.set_label("")
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
        tier, _score = match_rank(page_display_name(row.page_path), search)
        return tier > TIER_NONE

    def sort_func(self, row1: "PageRow", row2: "PageRow") -> int:
        """-1 when row1 comes first, 1 when row2 does, 0 when they tie."""
        search = self.search_entry.get_text()
        if search != "":
            rank1 = match_rank(page_display_name(row1.page_path), search)
            rank2 = match_rank(page_display_name(row2.page_path), search)
            if rank1 > rank2:
                return -1
            if rank1 < rank2:
                return 1

        # get_pages() provides natural name order, which also breaks score ties.
        # Keep equally ranked names in that backend order.
        if row1.order < row2.order:
            return -1
        if row1.order > row2.order:
            return 1
        return 0

    def visible_rows(self) -> "list[PageRow]":
        """Return visible rows in display order.
        Filtered rows remain in the box, so include only rows with child visibility."""
        rows: list[PageRow] = []
        index = 0
        while True:
            row = self.list_box.get_row_at_index(index)
            if row is None:
                return rows
            if row.get_child_visible():
                # update() is the only place that fills this list, and it
                # appends nothing but PageRow.
                rows.append(cast("PageRow", row))
            index += 1

    def on_search_changed(self, search_entry: Gtk.SearchEntry) -> None:
        self.list_box.invalidate_filter()
        self.list_box.invalidate_sort()

    def on_search_key_pressed(self, controller: Gtk.EventControllerKey, keyval: int,
                              keycode: int, state: Gdk.ModifierType) -> bool:
        """Walk the selection down and up the list from the search entry."""
        if keyval == Gdk.KEY_Down:
            step = 1
        elif keyval == Gdk.KEY_Up:
            step = -1
        else:
            return False

        rows = self.visible_rows()
        if not rows:
            return True

        selected = self.list_box.get_selected_row()
        if selected in rows:
            index = min(max(rows.index(cast("PageRow", selected)) + step, 0), len(rows) - 1)
        else:
            # Nothing in view is selected, so an arrow enters the list from
            # the end it points away from.
            index = 0 if step > 0 else len(rows) - 1

        self.list_box.select_row(rows[index])
        self.scroll_to_row(rows[index])
        return True

    def on_search_activate(self, search_entry: Gtk.SearchEntry) -> None:
        """Open the visible selected row, or the top match when none is selected.
        This lets Enter after typing choose the best match without arrow navigation."""
        rows = self.visible_rows()
        if not rows:
            return
        selected = self.list_box.get_selected_row()
        row = cast("PageRow", selected) if selected in rows else rows[0]
        self.activate_row(row)

    def on_popover_visible(self, popover: Gtk.Popover, param: GObject.ParamSpec) -> None:
        if not popover.get_visible():
            return
        # Start each open with the complete list.
        # A query retained from the prior open would hide pages before the user types.
        self.search_entry.set_text("")
        self.list_box.invalidate_filter()
        self.list_box.invalidate_sort()
        # The popover gives focus to its first focusable child on every popup.
        # Keep the search entry as that child so no explicit focus grab is needed.
        self.scroll_frames_left = SCROLL_SETTLE_FRAMES
        self.list_box.add_tick_callback(self.scroll_to_selected_row)

    def scroll_to_selected_row(self, widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        """Bring the deck's active page into view after list allocation.
        Wait on frame ticks because an idle can run before rows have a scrollable height."""
        selected = self.list_box.get_selected_row()
        adjustment = self.scrolled_window.get_vadjustment()
        if selected is None or adjustment is None or not self.popover.get_visible():
            return GLib.SOURCE_REMOVE

        if selected.get_height() <= 0 or adjustment.get_upper() <= 0:
            # No allocation yet. Wait for one, but not for ever: a popover
            # closed before its first frame would otherwise keep this alive.
            self.scroll_frames_left -= 1
            if self.scroll_frames_left > 0:
                return GLib.SOURCE_CONTINUE
            return GLib.SOURCE_REMOVE

        self.scroll_to_row(cast("PageRow", selected))
        return GLib.SOURCE_REMOVE

    def scroll_to_row(self, row: "PageRow") -> None:
        adjustment = self.scrolled_window.get_vadjustment()
        if adjustment is None:
            return
        found, point = row.compute_point(self.list_box, Graphene.Point().init(0, 0))
        if not found:
            return
        # Centre the row where the view is taller than it is. set_value()
        # clamps to the scrollable range itself.
        adjustment.set_value(point.y - max(0.0, adjustment.get_page_size() - row.get_height()) / 2)

    def on_row_activated(self, list_box: Gtk.ListBox, row: "PageRow") -> None:
        self.activate_row(row)

    def activate_row(self, row: "PageRow") -> None:
        self.popover.popdown()
        self.change_page(row.page_path)

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
        if page is None:
            # A listed page can disappear or fail to build before selection.
            # Keep the current deck image and notify the user instead of loading None.
            log.error(f"Page {page_path} did not load; the deck keeps its page")
            gl.notify.error(gl.lm.get("page-selector-load-failed"))
            return
        log.info(f"Load page: {page}")
        window_grabber = gl.window_grabber
        if window_grabber is None:
            active_controller.load_page(page)
            return
        # Mark this user choice as a manual load in the window grabber.
        # Otherwise a later restore can return to the automatic page that the user left.
        with window_grabber.manual_page_load(active_controller, page_path):
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
        if page_path is None or not self.lists_page(page_path):
            # Open the manager without a page when selection is empty or no longer listed.
            # Do not pass a missing path.
            return
        page_manager_window = gl.page_manager_window
        if page_manager_window is None:
            # The call above binds this global or raises.
            # Keep the guard because its optional type is not narrowed across that call.
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
