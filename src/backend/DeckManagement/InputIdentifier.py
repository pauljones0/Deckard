from collections.abc import Sequence
from typing import cast, Any, TYPE_CHECKING, TypedDict, override
from enum import Enum, StrEnum

if TYPE_CHECKING:
    from src.backend.PageManagement.Page import Page
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput


class InputType(StrEnum):
    """Top-level input families whose frozen values are page JSON keys.
    As StrEnum strings, members read, compare, hash, and serialize as their values."""
    KEYS = "keys"
    DIALS = "dials"
    TOUCHSCREENS = "touchscreens"


# Shape of a live entry in an input's page JSON "states" map.
# Functional syntax permits the hyphenated control keys.
StateDict = TypedDict("StateDict", {
    "actions": list[dict[str, Any]],
    "media": dict[str, Any],
    "labels": dict[str, Any],
    "background": dict[str, Any],
    "image-control-action": "int | None",
    "label-control-actions": list[Any],
    "background-control-action": "int | None",
}, total=False)


class InputIdentifier:
    # Concrete inputs define nested Events enums; this annotation exposes the type.
    # It creates no base attribute, so hasattr(InputIdentifier, "Events") stays false.
    Events: "type[InputEvent]"

    def __init__(self, input_type: InputType, json_identifier: str, controller_class_name: str):
        self.input_type = input_type
        self.json_identifier: str = json_identifier
        self.controller_class_name = controller_class_name

    def get_config(self, page: "Page") -> dict[str, Any]:
        return cast(dict[str, Any], page.dict.get(self.input_type, {}).get(self.json_identifier, {}))

    def get_dict(self, d: "dict[str, Any]") -> "dict[str, Any] | None":
        return cast("dict[str, Any] | None", d.get(self.input_type, {}).get(self.json_identifier))

    # Page JSON state keys are strings; accept integers and coerce them here.
    # Accessors return live nested values only when each requested nested value exists.

    def get_states(self, page: "Page") -> dict[str, Any]:
        return cast(dict[str, Any], self.get_config(page).get("states", {}))

    def get_state_dict(self, page: "Page", state: "int | str") -> dict[str, Any]:
        return cast(dict[str, Any], self.get_states(page).get(str(state), {}))

    def get_actions(self, page: "Page", state: "int | str") -> list[Any]:
        return cast(list[Any], self.get_state_dict(page, state).get("actions", []))

    def get_action_entry(self, page: "Page", state: "int | str", index: int) -> dict[str, Any] | None:
        actions = self.get_actions(page, state)
        if 0 <= index < len(actions):
            return cast(dict[str, Any] | None, actions[index])
        return None

    def ensure_state_dict(self, page: "Page", state: "int | str") -> dict[str, Any]:
        """Like get_state_dict, but creates the input, states and state chain,
        so the returned dict is part of the page."""
        input_config = page.dict.setdefault(self.input_type, {}).setdefault(self.json_identifier, {})
        return cast(dict[str, Any], input_config.setdefault("states", {}).setdefault(str(state), {}))

    # A wrong deck model or stale identifier is absent from the controller.
    def get_controller_input(self, controller: "DeckController") -> "ControllerInput[Any] | None":
        return controller.get_input(self)
    
    @override
    def __eq__(self, o: object) -> bool:
        if o is None:
            return False
        if not isinstance(o, InputIdentifier):
            raise ValueError(f"Invalid type {type(o)} for InputIdentifier")
        return self.input_type == o.input_type and self.json_identifier == o.json_identifier

    @override
    def __str__(self) -> str:
        return f"Input({self.input_type}, {self.json_identifier})"
    
    @override
    def __hash__(self) -> int:
        return hash((self.input_type, self.json_identifier))

class InputEvent(Enum):
    # Annotation only: __new__ sets this per member without creating an enum member.
    string_name: str

    def __new__(cls, string_name: str) -> "InputEvent":
        obj = object.__new__(cls)
        obj.string_name = string_name
        return obj
    
    @override
    def __str__(self) -> str:
        return self.string_name
    
    
class Input:
    class Key(InputIdentifier):
        input_type = InputType.KEYS
        controller_class_name = "ControllerKey"

        class Events(InputEvent):
            DOWN = "Key Down"
            UP = "Key Up"
            SHORT_UP = "Key Short Up"
            HOLD_START = "Key Hold Start"
            HOLD_STOP = "Key Hold Stop"

        def __init__(self, json_identifier: str):
            self.coords = Input.Key.Coords_From_PageCoords(json_identifier)
            self.json_identifier = Input.Key.Coords_To_PageCoords(self.coords)
            super().__init__(self.input_type, self.json_identifier, self.controller_class_name)

        @staticmethod
        def Coords_From_PageCoords(page_coords: str) -> "tuple[int, int]":
            split = page_coords.split("x")
            return (int(split[0]), int(split[1]))
        
        @staticmethod
        def Coords_To_PageCoords(coords: tuple[int, int]) -> str:
            return f"{coords[0]}x{coords[1]}"
        
        @staticmethod
        def Index_To_Coords(deck_controller: "DeckController", index: int) -> "tuple[int, int]":
            rows, cols = deck_controller.deck.key_layout()
            x = index % cols
            y = index // cols
            return (x, y)
        
        @staticmethod
        def Coords_To_Index(deck_controller: "DeckController", coords: "str | Sequence[Any]") -> int:
            parts: "Sequence[int] | Sequence[str]"
            if isinstance(coords, str):
                parts = coords.split("x")
            else:
                parts = coords
            x, y = map(int, parts)
            rows, cols = deck_controller.deck.key_layout()
            return y * cols + x
        
        def get_page_coords(self) -> str:
            return self.Coords_To_PageCoords(self.coords)
        
        def get_index(self, deck_controller: "DeckController") -> int:
            return self.Coords_To_Index(deck_controller, self.coords)
        
    class Dial(InputIdentifier):
        input_type = InputType.DIALS
        controller_class_name = "ControllerDial"

        class Events(InputEvent):
            DOWN = "Dial Down"
            UP = "Dial Up"
            SHORT_UP = "Dial Short Up"
            HOLD_START = "Dial Hold Start"
            HOLD_STOP = "Dial Hold Stop"
            TURN_CW = "Dial Turn CW"
            TURN_CCW = "Dial Turn CCW"
            SHORT_TOUCH_PRESS = "Dial Touchscreen Short Press"
            LONG_TOUCH_PRESS = "Dial Touchscreen Long Press"
  
        def __init__(self, json_identifier: str):
            self.index = int(json_identifier)
            super().__init__(self.input_type, json_identifier, self.controller_class_name)


    class Touchscreen(InputIdentifier):
        input_type = InputType.TOUCHSCREENS
        controller_class_name = "ControllerTouchScreen"

        class Events(InputEvent):
            DRAG_LEFT = "Touchscreen Drag Left"
            DRAG_RIGHT = "Touchscreen Drag Right"

        def __init__(self, json_identifier: str):
            self.index = str(json_identifier)
            super().__init__(self.input_type, json_identifier, self.controller_class_name)

    All = (Key, Dial, Touchscreen)
    KeyTypes = [key_type.input_type for key_type in All]
    
    @staticmethod
    def FromTypeIdentifier(input_type: str, json_identifier: str) -> "InputIdentifier":
        # Normalize page JSON strings to InputType; unknown values raise.
        input_map = {
            InputType.KEYS: Input.Key,
            InputType.DIALS: Input.Dial,
            InputType.TOUCHSCREENS: Input.Touchscreen
        }
        try:
            key = InputType(input_type)
        except ValueError:
            raise ValueError(f"Unknown input type {input_type}") from None
        return input_map[key](json_identifier)
    
    @staticmethod
    def AllEvents() -> list[InputEvent]:
        events: list[InputEvent] = []

        for t in Input.All:
            events.extend(list(t.Events))



        return events
    
    @staticmethod
    def EventFromStringName(string_name: str | None) -> InputEvent | None:
        if string_name in [None, str(None)]:
            return None
        for event in Input.AllEvents():
            if event.string_name == string_name:
                return event
        raise ValueError(f"Unknown string name {string_name}")
