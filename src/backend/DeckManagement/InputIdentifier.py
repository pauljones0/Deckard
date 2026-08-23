from collections.abc import Sequence
from typing import cast, Any, TYPE_CHECKING, TypedDict
from enum import Enum

if TYPE_CHECKING:
    from src.backend.PageManagement.Page import Page
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput


# Shape of one entry under an input's "states" map in a page json.
# This is an annotation only. The accessors below hand back the live dicts.
# The functional syntax is necessary, because the control keys are hyphenated.
StateDict = TypedDict("StateDict", {
    "actions": list[dict[str, Any]],
    "media": dict[str, Any],
    "labels": dict[str, Any],
    "background": dict[str, Any],
    "image-control-action": "int | None",
    "label-control-actions": list[Any],
    "background-control-action": "int | None",
}, total=False)


# Which of an input's states it shows, stored beside that input's "states" map.
# The key is absent while the input shows state 0, which is the state an input
# opens on when its page names none. A page that never leaves the first state
# therefore keeps the bytes it always had, and a build that predates the key
# reads a page that carries it unchanged.
ACTIVE_STATE_KEY = "active-state"


def stored_active_state(input_dict: "dict[str, Any]") -> int | None:
    """Give the state number an input dict stores, or None when it stores none.

    A page file is editable by hand, so anything that is not a state number
    counts as no number at all.
    """
    value = input_dict.get(ACTIVE_STATE_KEY)
    # A bool is an int, and True next to state 1 would read as that state.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def read_active_state(input_dict: "dict[str, Any]", n_states: int) -> int:
    """Give the state an input dict selects, of the n_states the input has.

    State 0 covers every page that names no state and every number the input
    cannot show. A page can name a state that is gone: one edited by hand, or
    an input a plugin rebuilt with fewer states than the page was written
    with. Such a number selects nothing, so the input opens on its first
    state instead of on none.
    """
    value = stored_active_state(input_dict)
    if value is None or value >= n_states:
        return 0
    return value


class InputIdentifier:
    # Every concrete input below (Input.Key, Dial, Touchscreen) defines its
    # own nested Events enum, so code holding the base type can reach it.
    # This is an annotation only, like InputEvent.string_name. It declares
    # the attribute without creating one, so hasattr(InputIdentifier,
    # "Events") stays False.
    Events: "type[InputEvent]"

    def __init__(self, input_type: str, json_identifier: str, controller_class_name: str):
        self.input_type = input_type
        self.json_identifier: str = json_identifier
        self.controller_class_name = controller_class_name

    def get_config(self, page: "Page") -> dict[str, Any]:
        return cast(dict[str, Any], page.dict.get(self.input_type, {}).get(self.json_identifier, {}))

    def get_dict(self, d: "dict[str, Any]") -> "dict[str, Any] | None":
        return cast("dict[str, Any] | None", d.get(self.input_type, {}).get(self.json_identifier))

    # Page state accessors.
    # State keys in a page json are strings, because Page.save writes
    # self.dict verbatim. Int keys are legitimate only in the in-memory
    # action_objects registry. str(state) below is the one place that coerces
    # them, so callers can pass either. Each accessor returns the live nested
    # dict or list. Callers mutate in place, and page.save() writes self.dict
    # wholesale.

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

    def persist_active_state(self, page: "Page | None", state: int) -> None:
        """Record in page which of its states this input shows.

        The page carries the number, so a reload, a page switch and the next
        launch all open the input on the state it was left on. The edit rides
        the page's own write, so a burst of state changes costs one file
        write and a page switch takes the last one with it.
        """
        if page is None:
            # No page is loaded, at boot or during teardown, so nothing can
            # carry the number.
            return
        config = self.get_dict(page.dict)
        if config is None:
            # The page holds no entry for this input, so it holds no second
            # state either. An entry minted here would put this key on every
            # page whose inputs are touched.
            return
        # State 0 is what an input opens on when the page names no state, so
        # the first state is the absent key rather than a second spelling of
        # it. A page the user never takes off state 0 keeps the bytes it has.
        wanted = state if state > 0 else None
        if stored_active_state(config) == wanted:
            return
        with page.edit() as data:
            live = self.get_dict(data)
            if live is None:
                return
            if wanted is None:
                live.pop(ACTIVE_STATE_KEY, None)
            else:
                live[ACTIVE_STATE_KEY] = wanted

    # DeckController.get_input answers None when this identifier is not among
    # the controller's inputs, e.g. a wrong deck model or a stale identifier.
    # The optional return type states that.
    def get_controller_input(self, controller: "DeckController") -> "ControllerInput[Any] | None":
        return controller.get_input(self)
    
    def __eq__(self, o: object) -> bool:
        if o is None:
            return False
        if not isinstance(o, InputIdentifier):
            raise ValueError(f"Invalid type {type(o)} for InputIdentifier")
        return self.input_type == o.input_type and self.json_identifier == o.json_identifier

    def __str__(self) -> str:
        return f"Input({self.input_type}, {self.json_identifier})"
    
    def __hash__(self) -> int:
        return hash((self.input_type, self.json_identifier))

class InputEvent(Enum):
    # This is an annotation only. An Enum body turns assignments into
    # members, so it declares the per-member attribute that __new__ sets below
    # without creating a member of its own.
    string_name: str

    def __new__(cls, string_name: str) -> "InputEvent":
        obj = object.__new__(cls)
        obj.string_name = string_name
        return obj
    
    def __str__(self) -> str:
        return self.string_name
    
    
class Input:
    class Key(InputIdentifier):
        input_type = "keys"
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
        input_type = "dials"
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
        input_type = "touchscreens"
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
        input_map = {
            "keys": Input.Key,
            "dials": Input.Dial,
            "touchscreens": Input.Touchscreen
        }
        if input_type in input_map:
            return input_map[input_type](json_identifier)
        raise ValueError(f"Unknown input type {input_type}")
    
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