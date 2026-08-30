# The state classes live in input_state_classes.py.
# This file and its module docstring define their persistence rules.
"""Cold loads use stored state; warm same-page loads keep each deck's live state.
State 0/invalid keys are removed; out-of-range values remain; weak page identity owns writes."""
from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.backend.DeckManagement.InputIdentifier import InputIdentifier
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput
    from src.backend.PageManagement.Page import Page


#: Where a page keeps the state number, beside the input's "states" map.
ACTIVE_STATE_KEY = "active-state"


def stored_active_state(input_dict: "dict[str, Any]") -> int | None:
    """
    The state number an input dict stores, or None when it stores none.
    """
    value = input_dict.get(ACTIVE_STATE_KEY)
    # A bool is an int, and True beside state 1 would read as that state.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def has_unusable_state(input_dict: "dict[str, Any]") -> bool:
    """
    Whether the dict stores something that is no state number.
    """
    return ACTIVE_STATE_KEY in input_dict and stored_active_state(input_dict) is None


def read_active_state(input_dict: "dict[str, Any]", n_states: int) -> int:
    """The state an input dict selects, of the n_states the input has."""
    value = stored_active_state(input_dict)
    if value is None or value >= n_states:
        return 0
    return value


class PersistedState:
    """
    One input's state, as its page keeps it. It answers every load and owns every write, so an input
    keeps one attribute and three calls and the rules above hold in one place.
    """

    def __init__(self, identifier: "InputIdentifier") -> None:
        self.identifier = identifier
        # The page whose states the input holds. It decides whether the next
        # load is warm, and which page a write may reach.
        self._loaded_from: "weakref.ref[Page] | None" = None
        # Whether the last load moved the input off the state it was on. Only
        # a move is worth a sidebar refresh.
        self.moved: bool = False

    def on_load(self, controller_input: "ControllerInput[Any]",
                input_dict: "dict[str, Any]", page: "Page | None") -> int:
        """
        The state to open on, once a load has built the states. The page the deck shows is not the
        same thing while a switch is under way.
        """
        live = controller_input.state
        if page is not None and page is self.page() and live < len(controller_input.states):
            # Warm: this deck already shows the page, so its own state stands.
            state = live
        else:
            state = read_active_state(input_dict, len(controller_input.states))
            self._drop_unusable(page, input_dict)
        self._loaded_from = None if page is None else weakref.ref(page)
        self.moved = state != live
        return state

    def page(self) -> "Page | None":
        """The page the input last loaded from, while it still stands."""
        return None if self._loaded_from is None else self._loaded_from()

    def sync_sidebar(self, controller_input: "ControllerInput[Any]") -> None:
        """
        Let the sidebar follow the load, but only after a move.
        """
        if self.moved:
            controller_input.reload_sidebar()

    def write(self, controller_input: "ControllerInput[Any]", state: int) -> None:
        """
        Record a state the input just moved to, in the page it belongs to.
        """
        page = controller_input.deck_controller.active_page
        if page is None or page is not self.page():
            # The deck is between pages, or has moved on to another one. The
            # number belongs to the page whose states this input holds.
            return
        config = self.identifier.get_dict(page.dict)
        if config is None:
            # The page holds no entry for this input, so it holds no second state either.
            # An entry minted here would put this key on every page whose inputs are touched.
            return
        # The first state is the absent key and never a second spelling of it.
        wanted = state if state > 0 else None
        # A stored value that is no state number counts as different, so a
        # write past it replaces it.
        if stored_active_state(config) == wanted and not has_unusable_state(config):
            return
        with page.edit() as data:
            live = self.identifier.get_dict(data)
            # A refresh can replace the tree between the read above and this lock.
            if live is not None:
                if wanted is None:
                    live.pop(ACTIVE_STATE_KEY, None)
                else:
                    live[ACTIVE_STATE_KEY] = wanted

    def _drop_unusable(self, page: "Page | None", input_dict: "dict[str, Any]") -> None:
        """Take a stored value that is no state number out of the page."""
        if page is None or not has_unusable_state(input_dict):
            return
        if self.identifier.get_dict(page.dict) is not input_dict:
            # The load is not of the page the deck shows, so a write from here
            # would land on another page.
            return
        with page.edit() as data:
            live = self.identifier.get_dict(data)
            if live is not None and has_unusable_state(live):
                live.pop(ACTIVE_STATE_KEY, None)
