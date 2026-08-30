"""One resolver and adapter for libadwaita's private list containers.

Adw.ExpanderRow and Adw.PreferencesGroup keep their rows in a Gtk.ListBox
that their public API does not expose, so the helpers walk the private
widget tree. The walks and the operations over the resolved box live here
once; BetterExpander and BetterPreferencesGroup stay thin facades.

The two facades carry different missing-tree policies on purpose. The
expander skips a mismatched tree (its one warned operation is clear,
because a clear that silently skips while add_row still appends duplicates
every row on the next refill). The preferences group fails loud, so a
toolkit change surfaces at the call site: the adapter raises a LookupError
that names the walk. The adapter resolves through the owner's public
get_list_box, so a subclass that overrides it steers every operation.
"""
from collections.abc import Callable
from typing import Any, Literal

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from loguru import logger as log


def resolve_expander_list_box(expander: Gtk.Widget) -> Gtk.ListBox | None:
    """The row list box under an Adw.ExpanderRow, or None off-layout. The
    result is isinstance-checked: anything but a Gtk.ListBox answers
    None."""
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
    """The row list box under an Adw.PreferencesGroup, or None off-layout.
    The result is isinstance-checked: anything but a Gtk.ListBox answers
    None."""
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

    missing selects the policy for a tree that does not match: "skip"
    returns without touching anything (clear still warns, see the module
    docstring), and "raise" raises a LookupError naming the owner, which
    keeps a fail-loud facade loud under toolkit changes.
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
        """The current rows, or None when the tree does not match. Listing
        answers None under both policies: both facades guard their row
        reads, and only the mutating operations fail loud."""
        box = self._resolve()
        if box is None:
            return None
        return list_box_children(box)

    def clear(self) -> None:
        box = self.list_box()
        if box is None:
            # Reached under the skip policy only. add_row() appends through
            # libadwaita's own pointer and does not use this walk, so a
            # silent clear lets a caller that clears and refills duplicate
            # every row. Say it rather than hide it.
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
