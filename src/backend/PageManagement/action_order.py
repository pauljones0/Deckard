"""Move one action of an input state into another place on a page.

The sidebar reorders the actions of a key in two ways: the up and down buttons
on a row, and a drag of the row itself. Both end here, so both write the same
thing.

Two index spaces meet here, and a move that mixes them moves the wrong action.

The row space counts the actions the sidebar shows, from zero, with no gaps.
Every index a caller hands in is one of these, and so is every control index the
page holds. A permission names the position an action has in
Page.get_all_actions_for_input, which is what ActionCore.get_own_action_index
reports and what each permission getter compares against.

The slot space counts the entries of the page's actions list. It runs longer
than the row space whenever an entry carries no action object, and two entries
do that. An entry with no id is skipped by the loader, which leaves no registry
key. An entry whose action holder answers nothing takes a registry key whose
value is None, which happens for an action the app version is too old for and
for a plugin whose constructor raises. Neither draws a row.

The registry keys are the slots that carry an action, so the registry is the
bridge. move_action takes row indices, converts them to slots through the
registry, moves the page entry in slot space, re-keys the registry in slot
space, and remaps the permissions in row space. Each remap runs in the space
its readers use, and that is the rule to keep.

An action carries its own data inside its entry in the page's actions list.
That entry holds the action id, the settings, the comment and the event
assignments, and all of it travels with the entry across a move. A permission
does not travel: it sits beside the list and holds a plain number, so a move
that leaves it alone gives the permission of the moved action to whichever
action takes its place.

The write goes through Page.save, which routes to the atomic writer.
"""
from typing import Any, TYPE_CHECKING, TypeVar

from loguru import logger as log

if TYPE_CHECKING:
    from src.backend.DeckManagement.InputIdentifier import InputIdentifier
    from src.backend.PageManagement.Page import Page

# The element a move carries; the helpers never inspect it.
_ItemT = TypeVar("_ItemT")

# One control index per label position, as ActionPermissionManager reads them.
LABEL_CONTROL_COUNT = 3

# The control keys that name a single action by its row.
CONTROL_KEYS = ("image-control-action", "background-control-action")


def resolve_drop_index(source_index: int, target_index: int, drop_below: bool, count: int) -> "int | None":
    """Answer the row a dragged row lands on, or None for a drop that moves nothing.

    source_index is the row of the dragged action and target_index the row under
    the pointer. drop_below says the pointer sits in the lower half of the target
    row, which puts the drop after that row instead of before it.

    None covers every drop that leaves the order as it is: an index that names no
    row, which a list too short to reorder answers for every drop; a drop on the
    dragged row itself; and a drop on the edge the dragged row already occupies.
    """
    if not 0 <= source_index < count:
        return None
    if not 0 <= target_index < count:
        return None

    # The row the dragged row would take if it were still in the list.
    insert_before = target_index + 1 if drop_below else target_index
    # It leaves its own row first, so every row above it moves down one.
    dest_index = insert_before - 1 if source_index < insert_before else insert_before

    if dest_index == source_index:
        return None
    return dest_index


def move_item(items: "list[_ItemT]", source_index: int, dest_index: int) -> "list[_ItemT]":
    """Return a copy of items with the element at source_index moved to dest_index.

    Both indices must name an element. A negative index counts from the end for
    pop and insert alike, so an unchecked one moves a different element than the
    caller asked for, in silence.
    """
    if not 0 <= source_index < len(items):
        raise ValueError(f"Source index {source_index} is out of range for {len(items)} items.")
    if not 0 <= dest_index < len(items):
        raise ValueError(f"Destination index {dest_index} is out of range for {len(items)} items.")

    moved = list(items)
    moved.insert(dest_index, moved.pop(source_index))
    return moved


def build_order_map(source_index: int, dest_index: int, count: int) -> dict[int, int]:
    """Map each position before the move to the position it holds after it."""
    order = move_item(list(range(count)), source_index, dest_index)
    return {old_index: new_index for new_index, old_index in enumerate(order)}


def remap_control_index(value: Any, order_map: dict[int, int]) -> "int | None":
    """Follow one control index across the move.

    None means the permission belongs to no action, which is what an unset
    control key and a control key that names no row both mean.
    """
    if not isinstance(value, int):
        return None
    return order_map.get(value)


def row_slots(registry: "dict[int, Any] | None", count: int) -> list[int]:
    """The page slot behind each row, in the order the rows are shown.

    A registry value of None carries no row, and a page entry the loader skipped
    holds no key at all, so this is shorter than the actions list whenever the
    page holds an entry that draws nothing. Without a registry nothing is loaded
    and nothing is shown, and then each entry stands for itself.
    """
    if not registry:
        return list(range(count))
    return [slot for slot, action in registry.items() if action is not None]


def reorder_action_objects(action_objects: dict[int, _ItemT], order_map: dict[int, int]) -> dict[int, _ItemT]:
    """Re-key the loaded action objects onto their new slots.

    order_map names every key, because move_action refuses a move whose registry
    holds a slot the actions list does not.

    Page.get_all_actions_for_input reads these values in insertion order, and
    ActionCore.get_own_action_index reports a position in that list, so the
    result is built in slot order and not in the order the old keys had.
    """
    moved = [(order_map[slot], obj) for slot, obj in action_objects.items()]
    moved.sort(key=lambda entry: entry[0])
    return {slot: obj for slot, obj in moved}


def move_action(page: "Page", identifier: "InputIdentifier", state: int,
                source_index: int, dest_index: int) -> bool:
    """Move one action of an input state to another row and save the page.

    source_index and dest_index count the actions the sidebar shows. See the
    module docstring for why that is not the same as counting the page entries.

    Answers True once the page holds the new order, and False for a move that
    writes nothing. The page is left untouched in the False case, so a caller
    can skip the reload it would otherwise run.
    """
    state_dict = identifier.get_state_dict(page, state)
    actions = state_dict.get("actions")
    if not isinstance(actions, list):
        # A state with no actions list has nothing to reorder. get_state_dict
        # also answers a fresh dict for a state the page does not hold, and a
        # write into that one would reach no page at all.
        return False

    count = len(actions)
    loaded = page.action_objects.get(identifier.input_type, {}).get(identifier.json_identifier, {})
    # An empty registry stands for no registry: nothing loaded, nothing shown.
    registry = loaded.get(state) or None
    slots = row_slots(registry, count)

    if source_index == dest_index:
        return False
    if not (0 <= source_index < len(slots) and 0 <= dest_index < len(slots)):
        log.warning(f"Cannot move action {source_index} to {dest_index} of {len(slots)} on {identifier} state {state}")
        return False
    if registry is not None and not all(0 <= slot < count for slot in registry):
        # The loaded actions name a slot the page does not hold, so no move
        # keeps the two in step. Refuse rather than write half of one.
        log.warning(f"Loaded actions {sorted(registry)} do not fit the {count} actions on {identifier} state {state}")
        return False

    source_slot = slots[source_index]
    dest_slot = slots[dest_index]

    slot_map = build_order_map(source_slot, dest_slot, count)
    row_map = build_order_map(source_index, dest_index, len(slots))

    state_dict["actions"] = move_item(actions, source_slot, dest_slot)

    if registry is not None:
        loaded[state] = reorder_action_objects(registry, slot_map)

    # Remap a control key the state holds, and create none it does not. An
    # absent key means the page never assigned that permission, and a written
    # null says the same thing in more bytes.
    for key in CONTROL_KEYS:
        if key in state_dict:
            state_dict[key] = remap_control_index(state_dict[key], row_map)

    if "label-control-actions" in state_dict:
        label_controls = state_dict["label-control-actions"]
        if not isinstance(label_controls, list):
            # A corrupt value names no action. Use the same default as
            # ActionPermissionManager.get_label_control_indices, so the reorder
            # completes its write instead of raising with memory and disk out
            # of step.
            label_controls = [None] * LABEL_CONTROL_COUNT
        state_dict["label-control-actions"] = [remap_control_index(value, row_map) for value in label_controls]

    page.save()
    return True
