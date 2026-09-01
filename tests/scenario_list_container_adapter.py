"""Resolve, list, sort, filter, clear/refill, and remove rows in both private lists.
Missing expanders skip/warn, groups raise, and both return no rows; widget checks need a display."""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

from loguru import logger

from GtkHelper.list_container import ListContainerAdapter, list_box_children


def check_policies_without_display() -> None:
    # The policies need no real Adw tree: the stand-in resolver answers None.
    def resolve_none():
        return None

    skipping = ListContainerAdapter(
        resolve_none, missing="skip", owner_label="Expander")
    assert skipping.list_box() is None, "skip policy must answer None"
    skipping.set_sort_func(lambda a, b: 0)
    skipping.invalidate_sort()
    assert skipping.rows() is None, (
        "a mismatched tree must keep the long-standing None rows answer")

    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        skipping.clear()
    finally:
        logger.remove(sink)
    assert any("no list box to clear" in w for w in warnings), (
        "a skipped clear must warn: a silent clear before a refill "
        "duplicates every row")

    raising = ListContainerAdapter(
        resolve_none, missing="raise", owner_label="PreferencesGroup")
    for op in (raising.clear, raising.invalidate_sort, raising.invalidate_filter):
        try:
            op()
        except LookupError as e:
            assert "PreferencesGroup" in str(e), (
                "the raise policy must name the failed walk")
        else:
            raise AssertionError(
                f"{op.__name__} on a mismatched tree must raise LookupError")
    print("PASS: both missing-tree policies hold")


def check_widgets() -> None:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    from GtkHelper.GtkHelper import BetterExpander, BetterPreferencesGroup

    expander = BetterExpander(title="rows")
    group = BetterPreferencesGroup(title="rows")

    for owner in (expander, group):
        # The expander appends with add_row, the group with add; both go
        # through libadwaita's own pointer, never through the walk.
        append = owner.add_row if hasattr(owner, "add_row") else owner.add
        box = owner.get_list_box()
        assert isinstance(box, Gtk.ListBox), (
            f"{type(owner).__name__} did not resolve a real Gtk.ListBox; "
            f"the private Adw walk no longer matches this toolkit")

        rows = [Adw.ActionRow(title=f"row {i}") for i in range(3)]
        for row in rows:
            append(row)
        listed = owner.get_rows()
        assert [r.get_title() for r in listed] == ["row 0", "row 1", "row 2"], (
            f"{type(owner).__name__} listed {len(listed or [])} rows, "
            f"expected the three added, in order")

        # Sorting through the facade reorders what get_rows sees.
        owner.set_sort_func(
            lambda a, b: -1 if a.get_title() > b.get_title() else 1)
        owner.invalidate_sort()
        titles = [r.get_title() for r in owner.get_rows()]
        assert titles == ["row 2", "row 1", "row 0"], (
            f"sorting through the facade did not reorder: {titles}")
        owner.set_sort_func(None)

        # Clear then refill must not duplicate: clear goes through the same
        # box that add_row appends to.
        owner.clear()
        assert owner.get_rows() == [], (
            f"{type(owner).__name__} still lists rows after clear")
        append(rows[0])
        listed = owner.get_rows()
        assert len(listed) == 1, (
            f"a clear-and-refill duplicated rows: {len(listed)}")
        owner.clear()

    # Filtering through the facade hides non-matching rows from the box,
    # on both widgets.
    for owner in (expander, group):
        append = owner.add_row if hasattr(owner, "add_row") else owner.add
        append(Adw.ActionRow(title="keep"))
        append(Adw.ActionRow(title="drop"))
        owner.set_filter_func(lambda row: row.get_title() == "keep")
        owner.invalidate_filter()
        box = owner.get_list_box()
        visible = [c for c in list_box_children(box) if c.get_child_visible()]
        assert len(visible) == 1 and visible[0].get_title() == "keep", (
            f"filtering {type(owner).__name__} left {len(visible)} visible "
            f"rows")
        owner.set_filter_func(None)

    # Removal through the facade takes the row out of the listing.
    target = expander.get_rows()[0]
    expander.remove_child(target)
    assert target not in (expander.get_rows() or []), (
        "remove_child left the row in the listing")

    # The adapter resolves through the public get_list_box override.
    # A subclass-reported missing tree must raise with the named walk.
    class _BlindGroup(BetterPreferencesGroup):
        def get_list_box(self):
            return None

    blind = _BlindGroup(title="blind")
    try:
        blind.clear()
    except LookupError as e:
        assert "PreferencesGroup" in str(e), (
            "the raise policy must name the failed walk")
    else:
        raise AssertionError(
            "clear on a group whose get_list_box answers None must raise")
    assert blind.get_rows() is None, (
        "row listing must keep the guarded None answer on a mismatched tree")
    print("PASS: both widgets keep their contracts through the adapter")


fixtures.start_watchdog(60, "list container adapter")
check_policies_without_display()
if fixtures.has_usable_display():
    check_widgets()
else:
    print("SKIP: no usable display; widget-construction checks skipped")
print("SCENARIO PASS")
