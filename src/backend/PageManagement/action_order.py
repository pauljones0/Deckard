"""Reorder page actions by visible row while preserving sparse page slots.
Action data moves in slot space, while permission indices remap in row space."""
from typing import Any, TYPE_CHECKING, TypeVar

from loguru import logger as log

if TYPE_CHECKING:
    from src.backend.DeckManagement.InputIdentifier import InputIdentifier
    from src.backend.PageManagement.Page import Page

_ItemT = TypeVar("_ItemT")

# One control index per label position, as ActionPermissionManager reads them.
LABEL_CONTROL_COUNT = 3

# The control keys that name a single action by its row.
CONTROL_KEYS = ("image-control-action", "background-control-action")


def resolve_drop_index(source_index: int, target_index: int, drop_below: bool, count: int) -> "int | None":
    """Return the drop destination, placing the source after the target when drop_below.
    Return None for invalid rows, the source row, or its current edge."""
    if not 0 <= source_index < count:
        return None
    if not 0 <= target_index < count:
        return None

    insert_before = target_index + 1 if drop_below else target_index
    # It leaves its own row first, so every row above it moves down one.
    dest_index = insert_before - 1 if source_index < insert_before else insert_before

    if dest_index == source_index:
        return None
    return dest_index


def move_item(items: "list[_ItemT]", source_index: int, dest_index: int) -> "list[_ItemT]":
    """Return a copy with one element moved between valid indices.
    Reject negative indices because list operations silently count from the end."""
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
    """Remap one control index, or return None when it names no action."""
    if not isinstance(value, int):
        return None
    return order_map.get(value)


def row_slots(registry: "dict[int, Any] | None", count: int) -> list[int]:
    """Return the page slot behind each visible row.
    Skip None registry values; without a registry, map each entry to itself."""
    if not registry:
        return list(range(count))
    return [slot for slot, action in registry.items() if action is not None]


def reorder_action_objects(action_objects: dict[int, _ItemT], order_map: dict[int, int]) -> dict[int, _ItemT]:
    """Re-key loaded objects in slot order, which defines Page and ActionCore row indices.
    move_action first proves that order_map names every registry key."""
    moved = [(order_map[slot], obj) for slot, obj in action_objects.items()]
    moved.sort(key=lambda entry: entry[0])
    return {slot: obj for slot, obj in moved}


def move_action(page: "Page", identifier: "InputIdentifier", state: int,
                source_index: int, dest_index: int) -> bool:
    """Move one visible action row and save the page.
    Return False without mutation when the move is invalid or changes no order."""
    state_dict = identifier.get_state_dict(page, state)
    actions = state_dict.get("actions")
    if not isinstance(actions, list):
        # A missing actions list can belong to a fresh, detached state dict.
        # It has nothing to reorder and writes would not reach the page.
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

    # Remap only present control keys; absence means no assigned permission.
    for key in CONTROL_KEYS:
        if key in state_dict:
            state_dict[key] = remap_control_index(state_dict[key], row_map)

    if "label-control-actions" in state_dict:
        label_controls = state_dict["label-control-actions"]
        if not isinstance(label_controls, list):
            # A corrupt value names no action, so use ActionPermissionManager's default.
            # Complete the write instead of leaving memory and disk out of step.
            label_controls = [None] * LABEL_CONTROL_COUNT
        state_dict["label-control-actions"] = [remap_control_index(value, row_map) for value in label_controls]

    page.save()
    return True
