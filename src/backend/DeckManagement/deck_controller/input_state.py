"""
Which state an input opens on, and what its page keeps of it.

An input with several states shows one of them at a time. That choice belongs
to the page, so it outlives the app: the input opens on the state the user
left it on after a page switch and after the next start.

The page keeps the number beside the input's "states" map. The rules are
these.

The key is absent while the input shows its first state. That is the state an
input opens on when its page names none, so a page nobody takes off the first
state keeps the bytes it always had, and a build that predates the key reads a
page carrying it unchanged.

A cold load opens the state the page names. Cold means the deck arrives on
this page: a page switch, a start of the app, a deck that never showed it.

A warm reload keeps the state the input is on. Warm means the deck already
shows this page and something reloaded it, such as an edit made through
another deck on the same page. The page carries one number while two decks can
show one page on different states, so a reload must never drag a deck off its
state.

A number the input cannot show opens the first state. A number out of range
that is still a state number stays in the page, because the states it names
can come back, from a plugin that rebuilds the input with all of them. The
next cold load then opens that state again. A warm reload does not: it keeps
the state the input is on, so the number comes back at the next page switch or
start of the app and never under the user's hands. A stored value that is no
state number at all, such as text, a bool or a negative number, goes out of
the page at the load that rejects it, because nothing can ever make it mean a
state.

A write reaches only the page the input last loaded from, and a load records
the page it read, never the page the deck happens to show. The two differ
while a page switch is under way: a load that finishes late carries the
leaving page's states, and a state change that lands in that window would
otherwise put this input's number under the arriving page, which never had
one.

The page is held by identity and weakly. A rename re-points a page's file in
place, so a name recorded at load time names the wrong file afterwards, while
the page itself stays the page this input holds. The weak hold lets the page
cache evict a page this input no longer shows, and an evicted page comes back
as another object, which is a cold load.
"""
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
    """The state number an input dict stores, or None when it stores none.

    None covers an absent key and a value that is no state number.
    has_unusable_state() tells those two apart.
    """
    value = input_dict.get(ACTIVE_STATE_KEY)
    # A bool is an int, and True beside state 1 would read as that state.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def has_unusable_state(input_dict: "dict[str, Any]") -> bool:
    """Whether the dict stores something that is no state number.

    A page file is editable by hand, and a version of this app that stored
    something else would leave it here too.
    """
    return ACTIVE_STATE_KEY in input_dict and stored_active_state(input_dict) is None


def read_active_state(input_dict: "dict[str, Any]", n_states: int) -> int:
    """The state an input dict selects, of the n_states the input has."""
    value = stored_active_state(input_dict)
    if value is None or value >= n_states:
        return 0
    return value


class PersistedState:
    """One input's state, as its page keeps it.

    One per input, for the life of the input. It answers every load and owns
    every write, so an input keeps one attribute and three calls and the rules
    above hold in one place.
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
        """The state to open on, once a load has built the states.

        page is the page the states came from, which the caller holds. The
        page the deck shows is not the same thing while a switch is under way.
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
        """Let the sidebar follow the load, but only after a move.

        The sidebar's input editor puts the main stack back on itself, so a
        refresh with no move to show takes a user out of the action chooser or
        the action configurator in the middle of an edit.
        """
        if self.moved:
            controller_input.reload_sidebar()

    def write(self, controller_input: "ControllerInput[Any]", state: int) -> None:
        """Record a state the input just moved to, in the page it belongs to.

        The edit rides the page's own write, so a burst of state changes costs
        one file write and a page switch takes the last of them with it.
        """
        page = controller_input.deck_controller.active_page
        if page is None or page is not self.page():
            # The deck is between pages, or has moved on to another one. The
            # number belongs to the page whose states this input holds.
            return
        config = self.identifier.get_dict(page.dict)
        if config is None:
            # The page holds no entry for this input, so it holds no second
            # state either. An entry minted here would put this key on every
            # page whose inputs are touched.
            return
        # The first state is the absent key and never a second spelling of it.
        wanted = state if state > 0 else None
        # A stored value that is no state number counts as different, so a
        # write past it replaces it.
        if stored_active_state(config) == wanted and not has_unusable_state(config):
            return
        with page.edit() as data:
            live = self.identifier.get_dict(data)
            # A refresh can replace the tree between the read above and this
            # lock. There is nothing left to write then, and the mark the
            # block leaves costs one write of content that did not change.
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
