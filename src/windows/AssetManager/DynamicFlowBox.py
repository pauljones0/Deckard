"""
Year: 2024

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
from gi.repository import Gtk, GLib

# Import python modules
import functools

from collections.abc import Callable
from typing import Any, Generic, TypeVar, cast

T = TypeVar("T")
# Bound pooled widgets to FlowBoxChild so the binder receives them, not wrappers
WidgetT = TypeVar("WidgetT", bound=Gtk.FlowBoxChild)

from loguru import logger as log

from src.windows.AssetManager.thumbnail_loader import build_loader

# Optional filter and sort hooks preserve input; show_range requires a binder
FilterFunc = Callable[[T], bool]
SortFunc = Callable[[T, T], int]
ItemBinder = Callable[[WidgetT, T], None]

class DynamicFlowBox(Gtk.Box, Generic[WidgetT, T]):
    # Class default lets subclasses choose the first page before show_range owns it
    current_start_index: int = 0

    def __init__(self, base_class: "type[WidgetT]", *args: Any, **kwargs: Any):
        """
        base_class: The class of the items in the flow box. Its constructor is not allowed to require any arguments because empty
                    placeholder objects will be created in the flowbox.
                    You have to use the binder to configure the items.
        """
        super().__init__(*args, **kwargs)
        self.set_orientation(Gtk.Orientation.VERTICAL)

        self.N_ITEMS_PER_PAGE = 50

        self.base_class = base_class
        self.items: list[T] = []

        self.sort_func: SortFunc[T] | None = None
        self.filter_func: FilterFunc[T] | None = None
        self.item_binder: "ItemBinder[WidgetT, T] | None" = None

        # Per-grid epochs cancel local stale work; cache and workers stay shared
        self.thumbnail_loader = build_loader()

        self.build()

        self.generate_placeholders()

    def build(self) -> None:
        self.scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(self.scrolled_window)

        self.scrolled_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.scrolled_window.set_child(self.scrolled_box)

        self.flow_box = Gtk.FlowBox(hexpand=True, orientation=Gtk.Orientation.HORIZONTAL,
                                    selection_mode=Gtk.SelectionMode.SINGLE)
        self.scrolled_box.append(self.flow_box)

        # Fix stretch
        self.scrolled_box.append(Gtk.Box(vexpand=True))

        self.nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                               margin_top=15, margin_bottom=15, margin_start=15, margin_end=15)
        self.append(self.nav_box)

        # Both start insensitive. An empty pool has no page to step to, and
        # _apply_range sets the true sensitivity from the first range on.
        self.back_button = Gtk.Button(icon_name="go-previous-symbolic", sensitive=False)
        self.back_button.connect("clicked", self.on_back)
        self.nav_box.append(self.back_button)

        self.nav_box.append(Gtk.Box(hexpand=True))

        self.next_button = Gtk.Button(icon_name="go-next-symbolic", sensitive=False)
        self.next_button.connect("clicked", self.on_next)
        self.nav_box.append(self.next_button)

    def generate_placeholders(self) -> None:
        for i in range(self.N_ITEMS_PER_PAGE):
            placeholder = self.base_class()
            # setattr keeps this generic when a child ignores Preview's loader slot
            setattr(placeholder, "_thumbnail_loader", self.thumbnail_loader)
            self.flow_box.append(placeholder)


    def filter_items(self, items: list[T]) -> list[T]:
        if not callable(self.filter_func):
            return items
        
        filtered_items = []
        for item in items:
            if self.filter_func(item):
                filtered_items.append(item)
        return filtered_items

    def sort_items(self, items: list[T]) -> list[T]:
        if not callable(self.sort_func):
            return items
        
        return sorted(items, key=functools.cmp_to_key(self.sort_func))


    def get_items_to_show(self) -> list[T]:
        filtered_items = self.filter_items(self.items)
        return self.sort_items(filtered_items)
    

    def show_range(self, start: int, end: int) -> None:
        if not callable(self.item_binder):
            raise ValueError("item_binder must be callable")

        self.current_start_index = start

        # Rebind the full pool in one main-loop turn so no stale child is clickable
        # Filter and sort also stay here because they read GTK state
        GLib.idle_add(self._apply_range, start, end)

    def _apply_range(self, start: int, end: int) -> bool:
        item_binder = self.item_binder
        if item_binder is None:
            # show_range refuses to schedule without one, so a None here
            # means a clear between the schedule and this idle dispatch.
            return False
        items = self.get_items_to_show()
        page_items = items[start:end]

        # Cancel stale decodes before this page queues higher-priority requests
        self.thumbnail_loader.begin_generation()

        # Clear the selection of the earlier page or filter before the rebind
        # of the pool. The binder selects the matching child again.
        self.flow_box.unselect_all()

        for i in range(self.N_ITEMS_PER_PAGE):
            preview = self.flow_box.get_child_at_index(i)
            if preview is None:
                break
            # Only base_class instances enter this pool, so the child is WidgetT
            preview = cast("WidgetT", preview)
            if i < len(page_items):
                # Bind before showing; hide failures and continue the remaining pool
                try:
                    item_binder(preview, page_items[i])
                except Exception as e:
                    log.opt(exception=True).error(f"Asset binder failed for item {i}: {e}")
                    preview.set_visible(False)
                    continue
                preview.set_visible(True)
            else:
                # Hide left over placeholders
                preview.set_visible(False)

        self.back_button.set_sensitive(start > 0)
        self.next_button.set_sensitive(end < len(items))
        return False


    def on_next(self, *args: object) -> None:
        self.show_page_at(self.current_start_index + self.N_ITEMS_PER_PAGE)

    def on_back(self, *args: object) -> None:
        self.show_page_at(self.current_start_index - self.N_ITEMS_PER_PAGE)

    def show_page_at(self, start: int) -> None:
        """Show the page at start, or preserve the current page when out of range."""
        if start < 0 or start >= len(self.get_items_to_show()):
            return
        self.show_range(start, start + self.N_ITEMS_PER_PAGE)


    def set_item_list(self, items: list[T]) -> None:
        self.items = items

    def set_item_binder(self, item_binder: "ItemBinder[WidgetT, T]") -> None:
        self.item_binder = item_binder

    def set_sort_func(self, sort_func: SortFunc[T]) -> None:
        self.sort_func = sort_func

    def set_filter_func(self, filter_func: FilterFunc[T]) -> None:
        self.filter_func = filter_func
