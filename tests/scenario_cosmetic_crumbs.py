"""Three small, independent cosmetic fixes, one leg each.

The onboarding icon page builds its icon at the size its sibling carousel pages
use. The plugin settings list sorts case-insensitively by name and keeps both of
its None guards. The action comment update refuses an index past the rows
instead of raising.

Each leg drives production code over duck-typed stand-ins, so no window and no
device is needed. The onboarding leg builds a real Gtk.Image, so it needs a
display and is skipped without one.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import sys
import types

import globals as gl

from src.backend.DeckManagement.InputIdentifier import Input

fixtures.start_watchdog(60, label="scenario_cosmetic_crumbs")

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def call(handler, *args):
    """Invoke a handler and return the exception instead of raising it."""
    try:
        handler(*args)
        return None
    except Exception as e:
        return e


# ------------------------------------------------------ onboarding icon size

def check_onboarding_icon_size() -> None:
    print("(1) the onboarding icon page builds its icon at the sibling size")
    if not fixtures.has_usable_display():
        print("  SKIP: no usable display; the icon widget is not built")
        return

    import gi
    gi.require_version("Adw", "1")
    from gi.repository import Adw
    Adw.init()
    from src.windows.Onboarding.OnboardingWindow import IconOnboardingScreen

    screen = IconOnboardingScreen("go-home-symbolic", "header", "detail")
    size = screen.image.get_pixel_size()
    # The three icon pages share a carousel with the extension and udev pages,
    # whose icons build at 250. A page that draws its icon at another size reads
    # as a size jump on the swipe between them.
    check("the icon page builds its icon at 250px", size == 250,
          f"built at {size}px, not the 250px its sibling pages use")


# ------------------------------------------------------- plugin settings sort

def check_plugin_settings_sorted() -> None:
    print("(2) the plugin settings list sorts by name and keeps its guards")
    from src.windows.Settings import PluginSettingsPage as pssp

    load = pssp.PluginSettingsGroup.load

    class RecordingExpander:
        def __init__(self, settings_group, plugin_base):
            self.plugin_base = plugin_base

    class FakeGroup:
        def __init__(self):
            self.cleared = 0
            self.added: list = []

        def clear(self):
            self.cleared += 1

        def add(self, expander):
            self.added.append(expander.plugin_base)

    class FakeBase:
        def __init__(self, name):
            self.plugin_name = name

    class FakeManager:
        def __init__(self, order, table):
            self._order = order
            self._table = table

        def get_plugins(self):
            return list(self._order)

        def get_plugin_by_id(self, plugin_id):
            return self._table.get(plugin_id)

    def added_names(order, table):
        group = FakeGroup()
        gl.plugin_manager = FakeManager(order, table)
        load(group)
        return group, [base.plugin_name for base in group.added]

    real_expander = pssp.PluginExpander
    real_manager = gl.plugin_manager
    pssp.PluginExpander = RecordingExpander
    try:
        # Discovery order is deliberately not alphabetical and the names mix
        # case, so only a case-insensitive sort gives the order below.
        table = {
            "id_b": FakeBase("banana"),
            "id_a": FakeBase("Apple"),
            "id_c": FakeBase("cherry"),
            "id_d": FakeBase("apricot"),
        }
        group, names = added_names(["id_b", "id_a", "id_c", "id_d"], table)
        check("the rows sort case-insensitively by name",
              names == ["Apple", "apricot", "banana", "cherry"], str(names))
        check("load clears the group before it rebuilds", group.cleared == 1, str(group.cleared))

        # The per-plugin None guard: an id get_plugin_by_id no longer answers is
        # skipped, not sorted as a None that raises on .lower().
        table_hole = {"id_a": FakeBase("Apple"), "id_x": None, "id_c": FakeBase("cherry")}
        _group, names = added_names(["id_c", "id_x", "id_a"], table_hole)
        check("a removed plugin is skipped, not sorted as None",
              names == ["Apple", "cherry"], str(names))

        # A manifest with no name sorts as empty and does not raise.
        table_noname = {"id_a": FakeBase("Apple"), "id_n": FakeBase(None)}
        _group, names = added_names(["id_a", "id_n"], table_noname)
        check("a nameless plugin sorts first as empty", names == [None, "Apple"], str(names))

        # The plugin-manager None guard: the page can open before the manager
        # exists, and load must return without touching the rows.
        gl.plugin_manager = None
        group = FakeGroup()
        raised = call(load, group)
        check("load with no plugin manager does not raise", raised is None, repr(raised))
        check("load with no plugin manager clears and adds nothing",
              group.added == [] and group.cleared == 1,
              f"{group.added} / {group.cleared}")
    finally:
        pssp.PluginExpander = real_expander
        gl.plugin_manager = real_manager


# ------------------------------------------------ action comment index guard

def check_comment_index_guard() -> None:
    print("(3) a comment update past the action rows moves nothing")
    from src.windows.mainWindow.elements.Sidebar.elements import ActionManager as am

    class FakeRow:
        def __init__(self):
            self.comment = None

        def set_comment(self, comment):
            self.comment = comment

    class FakeExpander:
        update_comment_for_index = am.ActionExpanderRow.update_comment_for_index

        def __init__(self, rows, identifier, state):
            self.rows = rows
            self.active_identifier = identifier
            self.active_state = state

        def get_rows(self):
            return list(self.rows)

    # The window path the update reads, and a page whose get_action_comment
    # answers, so control reaches the index and the guard is what is tested.
    page = types.SimpleNamespace(
        get_action_comment=lambda index, state, identifier: f"comment-{index}",
    )
    visible_child = types.SimpleNamespace(
        deck_controller=types.SimpleNamespace(active_page=page))
    main_win = types.SimpleNamespace(
        leftArea=types.SimpleNamespace(
            deck_stack=types.SimpleNamespace(get_visible_child=lambda: visible_child)))

    identifier = Input.Key("0x0")
    real_services = am.services
    am.services = types.SimpleNamespace(require_main_window=lambda: main_win)
    try:
        # An index into an empty row list must move nothing, not raise.
        expander = FakeExpander([], identifier, 0)
        raised = call(expander.update_comment_for_index, 0)
        check("a comment update on no rows does not raise", raised is None, repr(raised))

        # An index past a short row list: the same.
        rows = [FakeRow(), FakeRow()]
        expander = FakeExpander(rows, identifier, 0)
        raised = call(expander.update_comment_for_index, 5)
        check("a comment update past the rows does not raise", raised is None, repr(raised))
        check("an out-of-range update writes no comment",
              all(r.comment is None for r in rows), str([r.comment for r in rows]))

        # A valid index reaches the row it names. The stand-in supplies the
        # set_comment the row is asked for, so the guard's own logic is what
        # this checks, in both directions.
        rows = [FakeRow(), FakeRow(), FakeRow()]
        expander = FakeExpander(rows, identifier, 0)
        raised = call(expander.update_comment_for_index, 1)
        check("a comment update on a valid index does not raise", raised is None, repr(raised))
        check("the comment reaches the row it names", rows[1].comment == "comment-1",
              str(rows[1].comment))
        check("only the named row gets the comment",
              rows[0].comment is None and rows[2].comment is None,
              str([r.comment for r in rows]))
    finally:
        am.services = real_services


check_onboarding_icon_size()
check_plugin_settings_sorted()
check_comment_index_guard()

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
    sys.exit(1)
print("all checks passed")
sys.exit(0)
