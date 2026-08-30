"""Typed events from the deck hardware to a controller input.

The deck reports key, dial, and touchscreen changes with three mutually
incompatible argument shapes, which forced the dispatch seam into a star
signature no checker could verify. One frozen event object per input kind
carries the payload instead, so the base seam takes exactly one argument
and each input binds its own kind.

The name family is "deck event" on purpose: InputEvent already names the
action-facing enums (Input.Key.Events.DOWN, ...), and this seam feeds
those, it does not replace them.

This module stays import-light: the enum types come in under TYPE_CHECKING
only, so the CLI fast path and the control plane can construct a KeyEvent
without pulling the device library.
"""
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType


@dataclass(frozen=True, slots=True)
class KeyEvent:
    """One key edge. pressed is True on the down edge."""

    pressed: bool


@dataclass(frozen=True, slots=True)
class TouchscreenEvent:
    """One touchscreen gesture the device classified."""

    kind: "TouchscreenEventType"
    value: dict[str, int]


@dataclass(frozen=True, slots=True)
class DialEvent:
    """One dial change: a turn carries signed steps, a push its edge."""

    kind: "DialEventType"
    value: int


DeckEvent = KeyEvent | TouchscreenEvent | DialEvent