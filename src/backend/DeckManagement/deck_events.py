"""Typed hardware events with one frozen payload shape per input kind.
InputEvent names action-facing enums; device enums stay type-only imports."""
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
