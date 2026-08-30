"""Resolve private list boxes for expander and preferences-group facades.
Expanders skip but warn before refill; groups raise LookupError; owner overrides steer calls."""
from collections.abc import Callable
from typing import Any, Literal

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from loguru import logger as log


def resolve_expander_list_box(expander: Gtk.Widget) -> Gtk.ListBox | None:
    """Return the Adw.ExpanderRow list box, or None for an off-layout or wrong-type result."""
    expander_box = expander.get_first_child()
    if expander_box is None:
        return None
    expander_list_box = expander_box.get_first_child()
    if expander_list_box is None:
        return None
    revealer = expander_list_box.get_next_sibling()
    if revealer is None:
        return None
    revealer_list_box = revealer.get_first_child()
    if not isinstance(revealer_list_box, Gtk.ListBox):
        return None
    return revealer_list_box


def resolve_preferences_group_list_box(group: Gtk.Widget) -> Gtk.ListBox | None:
    """Return the Adw.PreferencesGroup list box, or None for an off-layout or wrong-type result."""
    first_box = group.get_first_child()
    second_box = first_box.get_first_child() if first_box is not None else None
    third_box = second_box.get_next_sibling() if second_box is not None else None
    candidate = third_box.get_first_child() if third_box is not None else None
    if not isinstance(candidate, Gtk.ListBox):
        return None
    return candidate


def list_box_children(list_box: Gtk.ListBox) -> list[Gtk.Widget]:
    """Every direct child of the box, in order. The rows are per-subclass
    widgets that callers read duck-typed attributes off."""
    rows: list[Gtk.Widget] = []
    child = list_box.get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    return rows


class ListContainerAdapter:
    """The shared operations over one widget's privately-resolved list box.
    missing="skip" leaves mismatches untouched, while "raise" reports a named LookupError.
    """

    def __init__(self, resolve: Callable[[], Gtk.ListBox | None], *,
                 missing: Literal["skip", "raise"], owner_label: str) -> None:
        # resolve is the owner's public get_list_box, so a subclass that
        # overrides that method steers every operation here.
        self._resolve = resolve
        self._missing = missing
        self._owner_label = owner_label

    def list_box(self) -> Gtk.ListBox | None:
        box = self._resolve()
        if box is None and self._missing == "raise":
            raise LookupError(
                f"{self._owner_label} has no list box; the private Adw "
                f"layout this walk expects has changed")
        return box

    def set_sort_func(self, *args: Any, **kwargs: Any) -> None:
        box = self.list_box()
        if box is None:
            return
        box.set_sort_func(*args, **kwargs)

    def set_filter_func(self, *args: Any, **kwargs: Any) -> None:
        box = self.list_box()
        if box is None:
            return
        box.set_filter_func(*args, **kwargs)

    def invalidate_filter(self) -> None:
        box = self.list_box()
        if box is None:
            return
        box.invalidate_filter()

    def invalidate_sort(self) -> None:
        box = self.list_box()
        if box is None:
            return
        box.invalidate_sort()

    def rows(self) -> list[Gtk.Widget] | None:
        """Return current rows, or None for a mismatch under either policy.
        Only mutating operations use the fail-loud policy."""
        box = self._resolve()
        if box is None:
            return None
        return list_box_children(box)

    def clear(self) -> None:
        box = self.list_box()
        if box is None:
            # Only the skip policy reaches this branch.
            # Warn because add_row bypasses this walk and a silent refill duplicates rows.
            log.warning(
                f"{self._owner_label} has no list box to clear; the Adw "
                f"layout this walk expects has changed")
            return
        box.remove_all()

    def remove(self, child: Gtk.Widget) -> None:
        box = self.list_box()
        if box is None:
            return
        box.remove(child)
