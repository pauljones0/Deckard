"""Move one action of an input state into another slot on a page.

The sidebar reorders the actions of a key in two ways: the up and down buttons
on a row, and a drag of the row itself. Both end here, so both write the same
thing.

An action carries its own data inside its entry in the page's actions list.
That entry holds the action id, the settings, the comment and the event
assignments, and all of it travels with the entry across a move. What does not
travel is a reference that names an action by slot. The image, background and
label control indices sit beside the list and hold plain integers, so a move
that leaves them alone gives the permission of the moved action to whichever
action takes its slot. The remap here keeps each permission with the action
that holds it.

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


def resolve_drop_index(source_index: int, target_index: int, drop_below: bool, count: int) -> "int | None":
    """Answer the slot a dragged row lands in, or None for a drop that moves nothing.

    source_index is the slot of the dragged row and target_index the slot of
    the row under the pointer, both counted over the action rows alone.
    drop_below says the pointer sits in the lower half of the target row, which
    puts the drop after that row instead of before it.

    None covers every drop that leaves the order as it is: a list too short to
    reorder, an index that names no row, a drop on the dragged row itself, and
    a drop on the edge the dragged row already occupies.
    """
    if count < 2:
        return None
    if not 0 <= source_index < count:
        return None
    if not 0 <= target_index < count:
        return None

    # The slot the row would take if it were still in the list.
    insert_before = target_index + 1 if drop_below else target_index
    # The row leaves its own slot first, so every slot above it moves down one.
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
    """Map each slot before the move to the slot it holds after it."""
    order = move_item(list(range(count)), source_index, dest_index)
    return {old_index: new_index for new_index, old_index in enumerate(order)}


def remap_control_index(value: Any, order_map: dict[int, int]) -> "int | None":
    """Follow one control index across the move.

    None means the permission belongs to no action, which is what an unset
    control key and a control key that names a slot outside the list both mean.
    """
    if not isinstance(value, int):
        return None
    return order_map.get(value)


def reorder_action_objects(action_objects: dict[int, _ItemT], order_map: dict[int, int]) -> dict[int, _ItemT]:
    """Re-key the loaded action objects onto their new slots.

    Page.get_all_actions_for_input reads these values in insertion order and
    ActionCore.get_own_action_index reports a position in that list, so the
    result is built in slot order and not in the order the old keys had. A key
    the map does not name keeps its own, which happens for a page whose actions
    list holds an entry with no id, because the loader skips such an entry and
    leaves the registry sparse.
    """
    moved = [(order_map.get(index, index), obj) for index, obj in action_objects.items()]
    moved.sort(key=lambda entry: entry[0])
    return {index: obj for index, obj in moved}


def move_action(page: "Page", identifier: "InputIdentifier", state: int,
                source_index: int, dest_index: int) -> bool:
    """Move one action of an input state to another slot and save the page.

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
    if source_index == dest_index:
        return False
    if not (0 <= source_index < count and 0 <= dest_index < count):
        log.warning(f"Cannot move action {source_index} to {dest_index} of {count} on {identifier} state {state}")
        return False

    order_map = build_order_map(source_index, dest_index, count)

    state_dict["actions"] = move_item(actions, source_index, dest_index)

    loaded = page.action_objects.get(identifier.input_type, {}).get(identifier.json_identifier, {})
    if state in loaded:
        loaded[state] = reorder_action_objects(loaded[state], order_map)

    state_dict["image-control-action"] = remap_control_index(state_dict.get("image-control-action"), order_map)
    state_dict["background-control-action"] = remap_control_index(state_dict.get("background-control-action"), order_map)

    # The key is absent on a page that add_action never touched, which covers a
    # hand-edited, an imported and an old page. Use the same default as
    # ActionPermissionManager.get_label_control_indices, so the reorder
    # completes its write instead of raising with memory and disk out of step.
    label_controls = state_dict.get("label-control-actions")
    if not isinstance(label_controls, list):
        label_controls = [None] * LABEL_CONTROL_COUNT
    state_dict["label-control-actions"] = [remap_control_index(value, order_map) for value in label_controls]

    page.save()
    return True
