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
# The pooled child widget class. Gtk.FlowBox wraps a plain widget in its own
# FlowBoxChild, so a pool class below that bound would reach the factory as
# the wrapper and not as itself.
WidgetT = TypeVar("WidgetT", bound=Gtk.FlowBoxChild)

from loguru import logger as log

from src.windows.AssetManager.thumbnail_loader import build_loader

# The three hooks that a chooser installs on a flow box. All three are
# optional. A box without them shows its items unfiltered and unsorted, and
# show_range refuses to run without a factory.
FilterFunc = Callable[[T], bool]
SortFunc = Callable[[T, T], int]
FactoryFunc = Callable[[WidgetT, T], None]

class DynamicFlowBox(Gtk.Box, Generic[WidgetT, T]):
    # The page offset the nav buttons step. show_range owns it from the first
    # pack on. The class default covers the window before that, where a nav
    # click has no page to step from. It stays a class default rather than a
    # constructor assignment, so a subclass that wants another first page can
    # still declare one and have it read.
    current_start_index: int = 0

    def __init__(self, base_class: "type[WidgetT]", *args: Any, **kwargs: Any):
        """
        base_class: The class of the items in the flow box. Its constructor is not allowed to require any arguments because empty
                    placeholder objects will be created in the flowbox.
                    You have to use the factory to configure the items.
        """
        super().__init__(*args, **kwargs)
        self.set_orientation(Gtk.Orientation.VERTICAL)

        self.N_ITEMS_PER_PAGE = 50

        self.base_class = base_class
        self.items: list[T] = []

        self.sort_func: SortFunc[T] | None = None
        self.filter_func: FilterFunc[T] | None = None
        self.factory_func: "FactoryFunc[WidgetT, T] | None" = None

        # Decodes this grid's thumbnails off the main loop. Its epoch and its
        # pending stack are the grid's own, so a page flip here cancels only
        # this grid's stale decodes; the cache and the worker pool are shared.
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
            # A pooled preview hands its thumbnail decode to this grid's loader.
            # The attribute lives on Preview; setattr keeps this base generic
            # over any child class, and a child that never reads it just ignores
            # the value.
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
        if not callable(self.factory_func):
            raise ValueError("factory_func must be callable")

        self.current_start_index = start

        # The whole rebind runs as one main-loop callback. A show of the
        # recycled children here, with a separate idle per child for the bind,
        # leaves a gap in which a click activates the asset of the earlier
        # page, or the None asset of a fresh placeholder, which raises
        # TypeError in on_child_activated. A child that the old page selected
        # also keeps its GTK selection while it shows a different asset. The
        # same main loop dispatches the input events, so one callback leaves
        # no moment where a half-rebound pool is clickable. The filter and
        # sort functions read GTK state, such as the search entry, so
        # get_items_to_show runs on the main thread as well, at apply time.
        GLib.idle_add(self._apply_range, start, end)

    def _apply_range(self, start: int, end: int) -> bool:
        factory_func = self.factory_func
        if factory_func is None:
            # show_range refuses to schedule without one, so a None here
            # means a clear between the schedule and this idle dispatch.
            return False
        items = self.get_items_to_show()
        page_items = items[start:end]

        # A new page. Cancel the decodes the last page asked for and had not
        # finished, so a straggler from it neither runs nor paints over this
        # page, and this page's own requests below take priority on the pool.
        self.thumbnail_loader.begin_generation()

        # Clear the selection of the earlier page or filter before the rebind
        # of the pool. The factory selects the matching child again.
        self.flow_box.unselect_all()

        for i in range(self.N_ITEMS_PER_PAGE):
            preview = self.flow_box.get_child_at_index(i)
            if preview is None:
                break
            # The pool holds base_class instances only:
            # generate_placeholders built it from base_class and nothing else
            # appends to it, so the child is a WidgetT.
            preview = cast("WidgetT", preview)
            if i < len(page_items):
                # Bind before the show, so a child becomes clickable only
                # with its new asset. The guard keeps one bad item from
                # aborting the rest of the rebind. A child whose bind failed
                # stays hidden, or a click reaches the asset of the earlier
                # page.
                try:
                    factory_func(preview, page_items[i])
                except Exception as e:
                    log.opt(exception=True).error(f"Asset factory failed for item {i}: {e}")
                    preview.set_visible(False)
                    continue
                preview.set_visible(True)
            else:
                # Hide left over placeholders
                preview.set_visible(False)

        self.back_button.set_sensitive(start > 0)
        self.next_button.set_sensitive(end < len(items))
        return False  # one-shot idle


    def on_next(self, *args: object) -> None:
        self.step_to(self.current_start_index + self.N_ITEMS_PER_PAGE)

    def on_back(self, *args: object) -> None:
        self.step_to(self.current_start_index - self.N_ITEMS_PER_PAGE)

    def step_to(self, start: int) -> None:
        """Show the page that begins at start, or do nothing if there is none.

        The nav buttons reach show_range only through here. A start outside
        the item list stops, so a step off either end leaves the page that
        shows in place. A box that has loaded nothing yet has no items, so
        every start is outside and the step stops there too. The buttons of
        such a box are insensitive until _apply_range makes them meaningful,
        which is what keeps a user from reaching this at all; the guard covers
        a direct call.
        """
        if start < 0 or start >= len(self.get_items_to_show()):
            return
        self.show_range(start, start + self.N_ITEMS_PER_PAGE)


    def set_item_list(self, items: list[T]) -> None:
        self.items = items

    def set_factory(self, factory_func: "FactoryFunc[WidgetT, T]") -> None:
        self.factory_func = factory_func

    def set_sort_func(self, sort_func: SortFunc[T]) -> None:
        self.sort_func = sort_func

    def set_filter_func(self, filter_func: FilterFunc[T]) -> None:
        self.filter_func = filter_func