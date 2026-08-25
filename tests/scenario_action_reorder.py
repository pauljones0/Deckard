"""Regression scenario for reordering the actions of a key.

Three layers, in order.

The decision layer is src.backend.PageManagement.action_order, which answers
where a dragged row lands and moves the slots. It is pure, so every edge case
of a drag runs here: a drop on the dragged row, a drop past the end, a list too
short to reorder, and an index that names no row.

The page layer is action_order.move_action over a real Page on disk. It proves
what a reorder must never lose: the settings, the comment and the event
assignments of an action follow that action, and the control indices beside the
list follow the action they name.

The sidebar layer is ActionRow and ActionExpanderRow, driven over duck-typed
stand-ins without GTK. The up and down buttons and the drop handler both end in
the same move, so both are checked against the same page.

A real drag needs a pointer, so the gesture itself and the drop indicator it
draws are outside what a headless run can reach.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import copy
import json
import sys
from types import SimpleNamespace

from fixtures import FaultyFakeDeck, seed_page, start_watchdog

import globals as gl
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PageManagement import action_order
from src.backend.PageManagement.Page import Page
from src.windows.mainWindow.elements.Sidebar.elements import ActionManager as action_manager
from src.windows.mainWindow.elements.Sidebar.elements.ActionManager import (
    ActionExpanderRow,
    ActionRow,
)

# The checks run at module top level, with no main(). Start the watchdog here,
# so a hang in the code under test fails fast.
start_watchdog(60, label="scenario_action_reorder")

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def call(handler, *args):
    """Invoke a handler and return the exception instead of raising.

    PyGObject swallows handler exceptions, and a run against broken code must
    fail in order instead of dying mid-scenario.
    """
    try:
        handler(*args)
        return None
    except Exception as e:
        return e


# ---------------------------------------------------------------- decision

print("(1) resolve_drop_index answers where a dragged row lands")

# Three rows. The pointer half decides the edge, and the dragged row leaves its
# own slot before it lands, so a drop below row 2 lands in slot 2 and not 3.
check("drag row 0 onto the lower half of row 2 lands last",
      action_order.resolve_drop_index(0, 2, True, 3) == 2)
check("drag row 0 onto the upper half of row 2 lands in the middle",
      action_order.resolve_drop_index(0, 2, False, 3) == 1)
check("drag row 2 onto the upper half of row 0 lands first",
      action_order.resolve_drop_index(2, 0, False, 3) == 0)
check("drag row 2 onto the lower half of row 0 lands in the middle",
      action_order.resolve_drop_index(2, 0, True, 3) == 1)

check("a drop on the dragged row moves nothing (upper half)",
      action_order.resolve_drop_index(1, 1, False, 3) is None)
check("a drop on the dragged row moves nothing (lower half)",
      action_order.resolve_drop_index(1, 1, True, 3) is None)
check("a drop above the row below moves nothing",
      action_order.resolve_drop_index(0, 1, False, 3) is None)
check("a drop below the row above moves nothing",
      action_order.resolve_drop_index(2, 1, True, 3) is None)
check("a drop below the last row, dragged from last, moves nothing",
      action_order.resolve_drop_index(2, 2, True, 3) is None)

check("a single action cannot be reordered",
      action_order.resolve_drop_index(0, 0, True, 1) is None)
check("an empty list cannot be reordered",
      action_order.resolve_drop_index(0, 0, False, 0) is None)
check("a source index past the end is refused",
      action_order.resolve_drop_index(3, 0, False, 3) is None)
check("a negative source index is refused",
      action_order.resolve_drop_index(-1, 0, False, 3) is None)
check("a target index past the end is refused",
      action_order.resolve_drop_index(0, 3, False, 3) is None)
check("a negative target index is refused",
      action_order.resolve_drop_index(0, -1, False, 3) is None)

print("(1) move_item, build_order_map and remap_control_index")
check("move_item moves down", action_order.move_item(["A", "B", "C"], 0, 2) == ["B", "C", "A"])
check("move_item moves up", action_order.move_item(["A", "B", "C"], 2, 0) == ["C", "A", "B"])
source_list = ["A", "B", "C"]
action_order.move_item(source_list, 0, 2)
check("move_item leaves its argument alone", source_list == ["A", "B", "C"], str(source_list))
check("move_item refuses a source index out of range",
      isinstance(call(action_order.move_item, ["A", "B"], 2, 0), ValueError))
check("move_item refuses a destination index out of range",
      isinstance(call(action_order.move_item, ["A", "B"], 0, -1), ValueError))

order_map = action_order.build_order_map(2, 0, 3)
check("build_order_map sends the moved slot to its destination", order_map[2] == 0, str(order_map))
check("build_order_map pushes the slots it passes down", order_map == {2: 0, 0: 1, 1: 2}, str(order_map))
check("remap_control_index follows a named slot",
      action_order.remap_control_index(2, order_map) == 0)
check("remap_control_index leaves an unset index unset",
      action_order.remap_control_index(None, order_map) is None)
check("remap_control_index answers None for a slot outside the list",
      action_order.remap_control_index(7, order_map) is None)

objects = action_order.reorder_action_objects({0: "obj_A", 1: "obj_B", 2: "obj_C"}, order_map)
check("reorder_action_objects re-keys onto the new slots",
      objects == {0: "obj_C", 1: "obj_A", 2: "obj_B"}, str(objects))
check("reorder_action_objects builds the map in slot order, which is what "
      "get_own_action_index reads",
      list(objects.values()) == ["obj_C", "obj_A", "obj_B"], str(list(objects.values())))
sparse = action_order.reorder_action_objects({0: "obj_A", 2: "obj_C"}, order_map)
check("reorder_action_objects keeps an action the loader skipped",
      sparse == {0: "obj_C", 1: "obj_A"}, str(sparse))


# ------------------------------------------------------------------- page

print("(2) a move on a real page, written to disk and read back")


class StubController:
    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None

    def serial_number(self) -> str:
        return self.deck.get_serial_number()


fixtures._install_integration_globals()

page_path = seed_page("ActionReorder")
real_page = Page(json_path=page_path, deck_controller=StubController("action-reorder-1"))
real_identifier = Input.Key("0x0")

real_state_dict = real_identifier.ensure_state_dict(real_page, 0)
real_state_dict["actions"] = [
    {"id": "plugin::A", "settings": {"marker": "a"}, "comment": "comment-a",
     "event-assignments": {"Key Down": "assign-a"}},
    {"id": "plugin::B", "settings": {"marker": "b"}, "comment": "comment-b",
     "event-assignments": {"Key Down": "assign-b"}},
    {"id": "plugin::C", "settings": {"marker": "c"}, "comment": "comment-c",
     "event-assignments": {"Key Down": "assign-c"}},
]
real_state_dict["image-control-action"] = 2
real_state_dict["background-control-action"] = 0
real_state_dict["label-control-actions"] = [0, 1, 2]
real_page.action_objects.setdefault("keys", {}).setdefault("0x0", {})[0] = {
    0: "obj_A", 1: "obj_B", 2: "obj_C",
}

moved = action_order.move_action(real_page, real_identifier, 0, source_index=2, dest_index=0)
check("move_action reports the page changed", moved is True, str(moved))

real_page.flush()
with open(page_path) as f:
    on_disk = json.load(f)
# Read the file defensively. A move that never marks the page leaves the seeded
# file behind, and that must show as a failed check and not as a KeyError.
disk_state = on_disk.get("keys", {}).get("0x0", {}).get("states", {}).get("0", {})
disk_actions = disk_state.get("actions", [])

check("the move reached the file", len(disk_actions) == 3, str(on_disk))
check("the new order is on disk",
      [a.get("id") for a in disk_actions] == ["plugin::C", "plugin::A", "plugin::B"],
      str([a.get("id") for a in disk_actions]))
check("settings follow their action and not their slot",
      [a.get("settings", {}).get("marker") for a in disk_actions] == ["c", "a", "b"],
      str([a.get("settings") for a in disk_actions]))
check("comments follow their action",
      [a.get("comment") for a in disk_actions] == ["comment-c", "comment-a", "comment-b"],
      str([a.get("comment") for a in disk_actions]))
check("event assignments follow their action",
      [a.get("event-assignments", {}).get("Key Down") for a in disk_actions]
      == ["assign-c", "assign-a", "assign-b"],
      str([a.get("event-assignments") for a in disk_actions]))
check("the media permission follows its action (2 -> 0)",
      disk_state.get("image-control-action") == 0, str(disk_state.get("image-control-action")))
check("the background permission follows its action (0 -> 1)",
      disk_state.get("background-control-action") == 1, str(disk_state.get("background-control-action")))
check("the label permissions follow their actions ([0, 1, 2] -> [1, 2, 0])",
      disk_state.get("label-control-actions") == [1, 2, 0], str(disk_state.get("label-control-actions")))
check("the loaded action objects follow the same order",
      list(real_page.action_objects["keys"]["0x0"][0].values()) == ["obj_C", "obj_A", "obj_B"],
      str(real_page.action_objects["keys"]["0x0"][0]))

# A page reloaded from its file must read the same order back.
real_page.update_dict()
reloaded_actions = real_identifier.get_actions(real_page, 0)
check("a page read back from the file holds the new order",
      [a.get("id") for a in reloaded_actions] == ["plugin::C", "plugin::A", "plugin::B"],
      str([a.get("id") for a in reloaded_actions]))
check("a page read back from the file holds the moved settings",
      [a.get("settings", {}).get("marker") for a in reloaded_actions] == ["c", "a", "b"],
      str([a.get("settings") for a in reloaded_actions]))

print("(2) a move that changes nothing writes nothing")
before = copy.deepcopy(real_identifier.get_state_dict(real_page, 0))
check("a move onto the same slot is refused",
      action_order.move_action(real_page, real_identifier, 0, 1, 1) is False)
check("a source index past the end is refused",
      action_order.move_action(real_page, real_identifier, 0, 3, 0) is False)
check("a negative destination is refused",
      action_order.move_action(real_page, real_identifier, 0, 0, -1) is False)
check("a state the page does not hold is refused",
      action_order.move_action(real_page, real_identifier, 4, 0, 1) is False)
check("a refused move leaves the state alone",
      real_identifier.get_state_dict(real_page, 0) == before,
      str(real_identifier.get_state_dict(real_page, 0)))


# ---------------------------------------------------------------- sidebar

# Duck-typed stand-ins. FakeExpander borrows the real methods from
# ActionExpanderRow, which are plain functions in the class dict, so the data
# path under test is production code. Lists emulate the widget-tree plumbing.

class FakeExpander:
    action_rows = ActionExpanderRow.action_rows
    plan_drop = ActionExpanderRow.plan_drop
    show_drop_indicator = ActionExpanderRow.show_drop_indicator
    clear_drop_indicators = ActionExpanderRow.clear_drop_indicators
    apply_drop = ActionExpanderRow.apply_drop
    move_row = ActionExpanderRow.move_row
    move_row_by = ActionExpanderRow.move_row_by
    reorder_actions = ActionExpanderRow.reorder_actions
    update_indices = ActionExpanderRow.update_indices

    def __init__(self, rows, add_action_button, identifier, state):
        self.rows = list(rows)  # add button last, like the real expander
        self.add_action_button = add_action_button
        self.active_identifier = identifier
        self.active_state = state
        self.dragged_row = None
        self.reorder_child_after_calls = []

    def get_rows(self):
        return list(self.rows)

    def reorder_child_after(self, child, after):
        # Emulates BetterExpander.reorder_child_after, which drops the child in
        # at the index the neighbour held before the removal and rebuilds the
        # list box from the result.
        self.reorder_child_after_calls.append((child, after))
        after_index = self.rows.index(after)
        self.rows.remove(child)
        self.rows.insert(after_index, child)


class FakeRow:
    """Carries exactly what the button and drop handlers dereference."""

    def __init__(self, name, index, expander):
        self.name = name
        self.index = index
        self.expander = expander
        self.css = set()

    def add_css_class(self, name):
        self.css.add(name)

    def remove_css_class(self, name):
        self.css.discard(name)

    def get_height(self):
        return 100

    def __repr__(self):
        return f"<FakeRow {self.name}@{self.index}>"


def make_world(action_ids, image_control=0, background_control=0,
               label_controls=(0, 0, 0), include_control_keys=True):
    """Build a fake controller and page in gl.app, plus a FakeExpander.

    The expander holds one FakeRow per action and the add button last.
    """
    state = {"actions": [{"id": a, "settings": {"marker": a}} for a in action_ids]}
    if include_control_keys:
        state["image-control-action"] = image_control
        state["background-control-action"] = background_control
        state["label-control-actions"] = list(label_controls)

    page = SimpleNamespace(
        dict={"keys": {"0x0": {"states": {"0": copy.deepcopy(state)}}}},
        action_objects={
            "keys": {"0x0": {0: {i: f"obj_{a}" for i, a in enumerate(action_ids)}}}
        },
        save_calls=0,
    )
    page.save = lambda: setattr(page, "save_calls", page.save_calls + 1)

    controller = SimpleNamespace(active_page=page, load_page_calls=[])
    controller.load_page = lambda p: controller.load_page_calls.append(p)

    gl.app = SimpleNamespace(
        main_win=SimpleNamespace(get_active_controller=lambda: controller)
    )

    # A real identifier, not a stand-in, because the move reaches the page
    # state through the InputIdentifier accessors.
    identifier = Input.Key("0x0")
    # Stands in for the Adw.ButtonRow, which is a widget and takes CSS classes.
    add_button = SimpleNamespace(name="add-button", css=set())
    add_button.add_css_class = add_button.css.add
    add_button.remove_css_class = add_button.css.discard
    expander = FakeExpander([], add_button, identifier, state=0)
    rows = [FakeRow(a, i, expander) for i, a in enumerate(action_ids)]
    expander.rows = rows + [add_button]
    return controller, page, expander, rows


def state_dict(page):
    return page.dict["keys"]["0x0"]["states"]["0"]


def action_order_of(page):
    return [a["id"] for a in state_dict(page)["actions"]]


def settings_order(page):
    return [a["settings"]["marker"] for a in state_dict(page)["actions"]]


def object_order(page):
    return list(page.action_objects["keys"]["0x0"][0].values())


print("(3) the up button on the middle row")
controller, page, expander, rows = make_world(["A", "B", "C"], image_control=1,
                                              background_control=0,
                                              label_controls=[0, 1, 2])
raised = call(ActionRow.on_click_up, rows[1], None)  # B moves up

check("on_click_up does not raise", raised is None, repr(raised))
if raised is None:
    check("the visual reorder names the row widgets themselves",
          expander.reorder_child_after_calls == [(rows[1], rows[0])],
          str(expander.reorder_child_after_calls))
    check("the visual order is B, A, C, add",
          [getattr(r, "name", None) for r in expander.rows] == ["B", "A", "C", "add-button"],
          str(expander.rows))
    check("the page dict holds the new order", action_order_of(page) == ["B", "A", "C"],
          str(action_order_of(page)))
    check("settings follow their action", settings_order(page) == ["B", "A", "C"],
          str(settings_order(page)))
    check("the action objects hold the new order", object_order(page) == ["obj_B", "obj_A", "obj_C"],
          str(object_order(page)))
    check("the media permission follows its action (1 -> 0)",
          state_dict(page)["image-control-action"] == 0,
          str(state_dict(page)["image-control-action"]))
    check("the background permission follows its action (0 -> 1)",
          state_dict(page).get("background-control-action") == 1,
          str(state_dict(page).get("background-control-action")))
    check("the label permissions follow their actions ([0, 1, 2] -> [1, 0, 2])",
          state_dict(page)["label-control-actions"] == [1, 0, 2],
          str(state_dict(page)["label-control-actions"]))
    check("the page is saved", page.save_calls == 1, str(page.save_calls))
    check("the page is loaded onto the deck",
          controller.load_page_calls == [page], str(controller.load_page_calls))
    check("the row indices are refreshed", [r.index for r in rows] == [1, 0, 2],
          str([r.index for r in rows]))

print("(3) the down button on the middle row")
controller, page, expander, rows = make_world(["A", "B", "C"], image_control=1,
                                              background_control=2,
                                              label_controls=[2, 2, 2])
raised = call(ActionRow.on_click_down, rows[1], None)  # B moves down

check("on_click_down does not raise", raised is None, repr(raised))
if raised is None:
    check("the visual order is A, C, B, add",
          [getattr(r, "name", None) for r in expander.rows] == ["A", "C", "B", "add-button"],
          str(expander.rows))
    check("the page dict holds the new order", action_order_of(page) == ["A", "C", "B"],
          str(action_order_of(page)))
    check("settings follow their action", settings_order(page) == ["A", "C", "B"],
          str(settings_order(page)))
    check("the media permission follows its action (1 -> 2)",
          state_dict(page)["image-control-action"] == 2,
          str(state_dict(page)["image-control-action"]))
    check("the background permission follows its action (2 -> 1)",
          state_dict(page).get("background-control-action") == 1,
          str(state_dict(page).get("background-control-action")))
    check("the label permissions follow their actions ([2, 2, 2] -> [1, 1, 1])",
          state_dict(page)["label-control-actions"] == [1, 1, 1],
          str(state_dict(page)["label-control-actions"]))

print("(3) the buttons at the ends of the list")
controller, page, expander, rows = make_world(["A", "B"])
raised = call(ActionRow.on_click_up, rows[0], None)  # nothing sits above the first row
check("the up button on the first row does not raise", raised is None, repr(raised))
check("the up button on the first row moves nothing",
      expander.reorder_child_after_calls == [] and action_order_of(page) == ["A", "B"],
      f"{expander.reorder_child_after_calls} / {action_order_of(page)}")
check("the up button on the first row saves nothing", page.save_calls == 0, str(page.save_calls))
check("the up button on the first row does not touch the add button",
      [getattr(r, "name", None) for r in expander.rows] == ["A", "B", "add-button"],
      str(expander.rows))

raised = call(ActionRow.on_click_down, rows[1], None)  # the add button is not an action
check("the down button on the last row does not raise", raised is None, repr(raised))
check("the down button on the last row moves nothing",
      expander.reorder_child_after_calls == [] and action_order_of(page) == ["A", "B"],
      f"{expander.reorder_child_after_calls} / {action_order_of(page)}")
check("the down button on the last row saves nothing", page.save_calls == 0, str(page.save_calls))

print("(3) two clicks between sidebar rebuilds")
controller, page, expander, rows = make_world(["A", "B", "C"], image_control=2,
                                              background_control=0,
                                              label_controls=[2, 0, 1])
c_row = rows[2]
raised = call(ActionRow.on_click_up, c_row, None)
raised = raised or call(ActionRow.on_click_up, c_row, None)
check("two ups do not raise", raised is None, repr(raised))
check("two ups move C to the top", action_order_of(page) == ["C", "A", "B"],
      str(action_order_of(page)))
check("the action objects follow", object_order(page) == ["obj_C", "obj_A", "obj_B"],
      str(object_order(page)))
check("settings follow across both moves", settings_order(page) == ["C", "A", "B"],
      str(settings_order(page)))
check("the media permission follows across both moves (2 -> 0)",
      state_dict(page)["image-control-action"] == 0,
      str(state_dict(page)["image-control-action"]))
check("the background permission follows across both moves (0 -> 1)",
      state_dict(page).get("background-control-action") == 1,
      str(state_dict(page).get("background-control-action")))
raised = call(ActionRow.on_click_up, c_row, None)
check("a third up moves nothing, because C is at the top",
      raised is None and action_order_of(page) == ["C", "A", "B"],
      f"{raised!r} / {action_order_of(page)}")

print("(3) a page without the control keys")
controller, page, expander, rows = make_world(["A", "B", "C"],
                                              include_control_keys=False)
raised = call(ActionRow.on_click_up, rows[1], None)  # B moves up
check("a move without the control keys does not raise", raised is None, repr(raised))
check("the actions are still reordered", action_order_of(page) == ["B", "A", "C"],
      str(action_order_of(page)))
check("the action objects are still reordered", object_order(page) == ["obj_B", "obj_A", "obj_C"],
      str(object_order(page)))
check("the write completes: the page is saved", page.save_calls == 1, str(page.save_calls))
check("the write completes: the page is loaded", controller.load_page_calls == [page],
      str(controller.load_page_calls))
check("an absent media permission stays unset",
      state_dict(page).get("image-control-action") is None,
      str(state_dict(page).get("image-control-action")))
check("an absent background permission stays unset",
      state_dict(page).get("background-control-action") is None,
      str(state_dict(page).get("background-control-action")))
check("absent label permissions take the ActionPermissionManager default",
      state_dict(page).get("label-control-actions") == [None, None, None],
      str(state_dict(page).get("label-control-actions")))

print("(3) a move the page refuses moves no row either")
controller, page, expander, rows = make_world(["A", "B", "C"])
controller.active_page = None
raised = call(ActionRow.on_click_down, rows[0], None)
check("a move with no page loaded does not raise", raised is None, repr(raised))
check("a move with no page loaded moves no row",
      [getattr(r, "name", None) for r in expander.rows] == ["A", "B", "C", "add-button"],
      str(expander.rows))
check("a move with no page loaded saves nothing", page.save_calls == 0, str(page.save_calls))

# The rows and the page can disagree, because a row is built per action entry
# and the page can lose one under a rebuild. The row must follow the page.
controller, page, expander, rows = make_world(["A", "B", "C"])
state_dict(page)["actions"] = state_dict(page)["actions"][:2]
raised = call(ActionRow.on_click_down, rows[1], None)
check("a move onto a slot the page does not hold does not raise", raised is None, repr(raised))
check("a move onto a slot the page does not hold moves no row",
      [getattr(r, "name", None) for r in expander.rows] == ["A", "B", "C", "add-button"],
      str(expander.rows))
check("a move onto a slot the page does not hold saves nothing",
      page.save_calls == 0, str(page.save_calls))


# ------------------------------------------------------------------- drag

print("(4) a drag marks where it lands")
controller, page, expander, rows = make_world(["A", "B", "C"])
call(ActionRow.on_dnd_begin, rows[0], None, None)
check("the drag records its row", expander.dragged_row is rows[0], repr(expander.dragged_row))
check("the dragged row is marked", action_manager.DRAGGED_CLASS in rows[0].css, str(rows[0].css))

action = ActionRow.on_dnd_motion(rows[2], None, 0.0, 90.0)  # lower half of row C
check("the lower half of a row marks that row's lower edge",
      action_manager.DROP_BELOW_CLASS in rows[2].css, str(rows[2].css))
check("the drag is accepted", action != 0, str(action))

action = ActionRow.on_dnd_motion(rows[2], None, 0.0, 10.0)  # upper half of row C
check("the upper half of a row marks that row's upper edge",
      action_manager.DROP_ABOVE_CLASS in rows[2].css, str(rows[2].css))
check("only one edge is marked at a time",
      action_manager.DROP_BELOW_CLASS not in rows[2].css, str(rows[2].css))

action = ActionRow.on_dnd_motion(rows[0], None, 0.0, 10.0)  # upper half of the dragged row
check("a drop that moves nothing is refused", action == 0, str(action))
check("a drop that moves nothing marks no edge",
      all(action_manager.DROP_ABOVE_CLASS not in r.css and action_manager.DROP_BELOW_CLASS not in r.css
          for r in rows),
      str([r.css for r in rows]))

call(ActionRow.on_dnd_motion, rows[2], None, 0.0, 90.0)
call(ActionRow.on_dnd_leave, rows[2], None)
check("leaving a row clears its edge",
      not rows[2].css & {action_manager.DROP_ABOVE_CLASS, action_manager.DROP_BELOW_CLASS},
      str(rows[2].css))

call(ActionRow.on_dnd_motion, rows[2], None, 0.0, 90.0)
call(ActionRow.on_dnd_end, rows[0], None, None, True)
check("the end of a drag clears its row mark",
      action_manager.DRAGGED_CLASS not in rows[0].css, str(rows[0].css))
check("the end of a drag forgets the row", expander.dragged_row is None, repr(expander.dragged_row))
check("the end of a drag clears every edge",
      all(not r.css for r in rows), str([r.css for r in rows]))


class RecordingIdle:
    """Stands in for GLib, so the deferred move can be run here."""

    SOURCE_REMOVE = False

    def __init__(self):
        self.calls = []

    def idle_add(self, func, *args):
        self.calls.append((func, args))
        return 1


print("(4) a drop moves the action, once the drag is over")
controller, page, expander, rows = make_world(["A", "B", "C"], image_control=0,
                                              background_control=1,
                                              label_controls=[0, 1, 2])
real_glib = action_manager.GLib
real_action_row = action_manager.ActionRow
action_manager.GLib = RecordingIdle()
# The drop handler refuses a value that is not an action row, and a stand-in is
# not one. Point the name the handler reads at the stand-in class, so the check
# passes for the rows this scenario builds and still refuses everything else.
action_manager.ActionRow = FakeRow
try:
    # Drag A onto the lower half of C.
    call(ActionRow.on_dnd_begin, rows[0], None, None)
    accepted = ActionRow.on_dnd_drop(rows[2], None, rows[0], 0.0, 90.0)
    check("the drop is accepted", accepted is True, str(accepted))
    check("the drop clears the indicator",
          all(not (r.css - {action_manager.DRAGGED_CLASS}) for r in rows), str([r.css for r in rows]))
    check("the drop moves nothing while the drag is still running",
          action_order_of(page) == ["A", "B", "C"] and page.save_calls == 0,
          f"{action_order_of(page)} / {page.save_calls}")
    check("the drop queues the move it planned",
          [args for _func, args in action_manager.GLib.calls] == [(0, 2)],
          str(action_manager.GLib.calls))

    for func, args in action_manager.GLib.calls:
        func(*args)

    check("the queued move reorders the page", action_order_of(page) == ["B", "C", "A"],
          str(action_order_of(page)))
    check("the queued move reorders the rows",
          [getattr(r, "name", None) for r in expander.rows] == ["B", "C", "A", "add-button"],
          str(expander.rows))
    check("settings follow their action", settings_order(page) == ["B", "C", "A"],
          str(settings_order(page)))
    check("the action objects follow", object_order(page) == ["obj_B", "obj_C", "obj_A"],
          str(object_order(page)))
    check("the media permission follows its action (0 -> 2)",
          state_dict(page)["image-control-action"] == 2,
          str(state_dict(page)["image-control-action"]))
    check("the background permission follows its action (1 -> 0)",
          state_dict(page)["background-control-action"] == 0,
          str(state_dict(page)["background-control-action"]))
    check("the label permissions follow their actions ([0, 1, 2] -> [2, 0, 1])",
          state_dict(page)["label-control-actions"] == [2, 0, 1],
          str(state_dict(page)["label-control-actions"]))
    check("the page is saved once", page.save_calls == 1, str(page.save_calls))
    check("the page is loaded onto the deck",
          controller.load_page_calls == [page], str(controller.load_page_calls))
    check("the row indices are refreshed", [r.index for r in rows] == [2, 0, 1],
          str([r.index for r in rows]))

    print("(4) a drop that moves nothing")
    controller, page, expander, rows = make_world(["A", "B", "C"])
    action_manager.GLib = RecordingIdle()
    call(ActionRow.on_dnd_begin, rows[1], None, None)
    accepted = ActionRow.on_dnd_drop(rows[1], None, rows[1], 0.0, 10.0)
    check("a drop on the dragged row is refused", accepted is False, str(accepted))
    check("a refused drop queues nothing", action_manager.GLib.calls == [],
          str(action_manager.GLib.calls))
    check("a refused drop saves nothing", page.save_calls == 0, str(page.save_calls))

    accepted = ActionRow.on_dnd_drop(rows[1], None, "not a row", 0.0, 10.0)
    check("a drop of something that is not an action row is refused",
          accepted is False, str(accepted))
    check("a drop of something else queues nothing", action_manager.GLib.calls == [],
          str(action_manager.GLib.calls))

    print("(4) a drop onto a list of one action")
    controller, page, expander, rows = make_world(["A"])
    action_manager.GLib = RecordingIdle()
    call(ActionRow.on_dnd_begin, rows[0], None, None)
    accepted = ActionRow.on_dnd_drop(rows[0], None, rows[0], 0.0, 90.0)
    check("a single action cannot be dropped onto itself", accepted is False, str(accepted))
    check("a single action stays where it is",
          action_order_of(page) == ["A"] and page.save_calls == 0,
          f"{action_order_of(page)} / {page.save_calls}")
finally:
    action_manager.GLib = real_glib
    action_manager.ActionRow = real_action_row

print("(4) the add button is not a drop slot")
controller, page, expander, rows = make_world(["A", "B"])
check("the add button is not an action row",
      expander.action_rows() == rows, str(expander.action_rows()))
check("a drag past the last action row is refused",
      expander.plan_drop(rows[0], expander.add_action_button, True) is None)
check("a move onto the add button's slot is refused",
      call(expander.move_row, rows[0], 2) is None and action_order_of(page) == ["A", "B"],
      str(action_order_of(page)))
check("a move onto the add button's slot saves nothing", page.save_calls == 0, str(page.save_calls))

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
    sys.exit(1)
print("all checks passed")
sys.exit(0)
