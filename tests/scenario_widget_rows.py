"""Headless coverage for GtkHelper widget rows and their GenerativeUI wrappers.

Three plugin-facing defects sit under this scenario:

  * ComboRow.get_selected_item tested the wrong no-selection sentinel (-1).
    GTK4 reports "nothing selected" as Gtk.INVALID_LIST_POSITION, so the guard
    never fired and only the get_item_at fallback returned None by accident.

  * The GenerativeUI ToggleRow wrapper called widget methods that do not exist
    (get_toggle, add, set_active, set_active_name) or routed a remove through
    the inherited Adw.ActionRow.remove, which raises TypeError on a None toggle
    and leaves a real toggle in the group.

  * ScaleRow.round_digits round-trips a decimal-place count, not a bool.

The ComboRow guard runs through the unbound method with a duck-typed self, so
it needs no widget and no display. The widget checks build real Adw rows, which
aborts a headless host with no display, so they run only when a display exists
(hugo provides one through xvfb).
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

from src.backend.PluginManager.ActionCore import ActionCore


class _FakeAction(ActionCore):
    """Stand-in action whose settings live in a plain dict.

    Follows scenario_genui_lazy: get_settings/set_settings back onto a dict, so
    the GenerativeUI value layer needs no real page entry.
    """

    def __init__(self, page):
        super().__init__(
            action_id="test::widget_rows",
            action_name="WidgetRows",
            deck_controller=page.deck_controller,
            page=page,
            plugin_base=None,
            state=0,
            input_ident=None,
        )
        self._fake_settings: dict = {}

    def get_settings(self):
        return self._fake_settings

    def set_settings(self, settings: dict):
        self._fake_settings = settings


def check_combo_row_no_selection_sentinel() -> None:
    """get_selected_item maps the no-selection sentinel to None via its guard.

    The method reads self.get_selected() and self.get_item_at() only, so a
    duck-typed self drives it with no widget. get_item_at answers a non-None
    sentinel, so the guard is the only path that can return None for the
    no-selection case. A guard that tests -1 falls through to get_item_at and
    returns the sentinel instead.
    """
    from gi.repository import Gtk

    from GtkHelper.ComboRow import ComboRow, ComboRowItem

    sentinel = ComboRowItem("sentinel")

    class _Fake:
        def __init__(self, selected):
            self._selected = selected

        def get_selected(self):
            return self._selected

        def get_item_at(self, index):
            return sentinel

    no_selection = _Fake(Gtk.INVALID_LIST_POSITION)
    result = ComboRow.get_selected_item(no_selection)
    assert result is None, (
        "get_selected_item must return None for INVALID_LIST_POSITION, "
        f"got {result!r}"
    )

    selected = _Fake(1)
    result = ComboRow.get_selected_item(selected)
    assert result is sentinel, (
        "get_selected_item must return the item at a valid selection index"
    )
    print("PASS: ComboRow.get_selected_item guards on INVALID_LIST_POSITION")


def check_toggle_row_widget_remove_guards() -> None:
    """The widget ToggleRow tolerates an out-of-range index and unknown name.

    get_toggle(out_of_range) answers None and the toggle group rejects a None
    remove with TypeError, so remove_at and remove_with_name must guard None.
    """
    from gi.repository import Adw

    from GtkHelper.ToggleRow import ToggleRow

    row = ToggleRow(
        toggles=[Adw.Toggle(label="a", name="a"), Adw.Toggle(label="b", name="b")],
        active_toggle=0,
        title="", subtitle="",
        can_shrink=True, homogeneous=True, active=True,
    )
    assert row.get_n_toggles() == 2

    row.remove_at(999)            # out of range: a no-op, never a TypeError
    row.remove_with_name("nope")  # unknown name: a no-op, never a TypeError
    assert row.get_n_toggles() == 2

    row.remove_at(1)
    assert row.get_n_toggles() == 1
    row.remove_with_name("a")
    assert row.get_n_toggles() == 0
    print("PASS: widget ToggleRow.remove_at/remove_with_name guard the None toggle")


def check_genui_togglerow_wrapper(page) -> None:
    """The GenerativeUI ToggleRow wrapper drives the real widget methods.

    Every wrapper method below hit a name the widget does not carry, or the
    inherited Adw.ActionRow.remove, before the fix.
    """
    from gi.repository import Adw

    from GtkHelper.GenerativeUI.ToggleRow import ToggleRow

    action = _FakeAction(page)
    row = ToggleRow(
        action, "toggles_var", 0,
        toggles=[Adw.Toggle(label="a", name="a"), Adw.Toggle(label="b", name="b")],
        can_reset=False,
    )
    # First .widget access builds the underlying GtkHelper.ToggleRow.
    assert row.widget is not None

    # get_toggle_at must call the widget's get_toggle_at, not a missing
    # get_toggle.
    assert row.get_toggle_at(0) is not None, "get_toggle_at returned None for a valid index"

    # set_active_toggle / set_active_by_name are the real widget methods, not
    # set_active / set_active_name.
    row.set_active_toggle(1)
    assert row.widget.get_active_index() == 1
    row.set_active_by_name("a")
    assert row.widget.get_active_index() == 0

    # add_custom_toggle must add to the toggle group, not call ActionRow.add.
    before = row.get_n_toggles()
    row.add_custom_toggle(Adw.Toggle(label="c", name="c"))
    assert row.get_n_toggles() == before + 1

    # populate rebuilds the group and sets the active index through a real
    # widget method.
    row.populate([Adw.Toggle(label="x", name="x"), Adw.Toggle(label="y", name="y")], 1)
    assert row.get_n_toggles() == 2
    assert row.widget.get_active_index() == 1

    # remove_toggle must remove from the toggle group. The inherited
    # ActionRow.remove leaves the toggle in the group instead.
    row.remove_toggle(row.get_toggle_at(1))
    assert row.get_n_toggles() == 1, "remove_toggle did not remove from the toggle group"

    # remove_at out of range and remove_with_name unknown are no-ops, never a
    # TypeError from a None passed into remove.
    row.remove_at(999)
    row.remove_with_name("does-not-exist")
    assert row.get_n_toggles() == 1

    # remove_at at a valid index removes from the group.
    row.remove_at(0)
    assert row.get_n_toggles() == 0
    print("PASS: GenerativeUI ToggleRow wrapper drives the real widget methods")


def check_scalerow_round_digits_int() -> None:
    """round_digits is an int decimal-place count that round-trips, -1 disables.

    It mirrors Gtk.Scale.get_round_digits/set_round_digits, so a plugin can
    read round_digits and assign it back without a type mismatch.
    """
    from GtkHelper.ScaleRow import ScaleRow

    row = ScaleRow(value=0.0, min=0.0, max=10.0, round_digits=3)
    assert row.round_digits == 3, f"ctor round_digits not applied, got {row.round_digits!r}"

    row.round_digits = -1
    assert row.round_digits == -1, f"round_digits did not accept -1, got {row.round_digits!r}"

    row.round_digits = 2
    assert row.round_digits == 2
    assert isinstance(row.round_digits, int) and not isinstance(row.round_digits, bool)

    # The property round-trips: read it and assign it straight back.
    row.round_digits = row.round_digits
    assert row.round_digits == 2
    print("PASS: ScaleRow.round_digits round-trips as an int decimal-place count")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_widget_rows")

    # Needs neither a controller nor a display.
    check_combo_row_no_selection_sentinel()

    if not fixtures.has_usable_display():
        print("SKIP: no usable display; widget-construction checks skipped")
        print("PASS: scenario_widget_rows")
        return

    controller = fixtures.make_headless_controller(serial="widget-rows-1")
    page = controller.active_page

    check_toggle_row_widget_remove_guards()
    check_genui_togglerow_wrapper(page)
    check_scalerow_round_digits_int()

    fixtures.teardown(controller)
    print("PASS: scenario_widget_rows")


if __name__ == "__main__":
    main()
