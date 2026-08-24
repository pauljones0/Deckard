"""
An input opens on the state it was last left on.

The page carries which of its states an input shows, so the number survives a
page switch and the next start of the app. A reload of a page a deck already
shows keeps that deck's own state instead, because one page serves several
decks and each is on its own state. A page that never leaves the first state
carries no number, and a number the input cannot show opens the first state.
"""

# Timers stay disarmed, so every write here is one a check asks for by name.
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import os
from types import SimpleNamespace

import globals as gl
from fixtures import make_headless_controller, start_watchdog, teardown, wait_until

from src.backend import services, ui_port
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.deck_controller.input_state import ACTIVE_STATE_KEY
from src.backend.PageManagement import page_flush

WATCHDOG_SECONDS = 120

# The key every check drives. A page seeded below gives it several states.
IDENT = Input.Key("0x0")

# How long a load dispatched onto the media thread may take before the deck is
# declared wedged rather than slow.
SETTLE_TIMEOUT_S = 10.0


class NoTimers:
    """A timer source that arms nothing.

    Every write in this scenario is one a check asks for, so a line in the
    test fixes the moment a page reaches its file.
    """

    def schedule(self, delay_s, callback):
        return object()

    def cancel(self, handle):
        pass


class RecordingPort(ui_port.UIPort):
    """Records the sidebar calls the engine makes, and nothing else.

    Every other port method keeps the base no-op, so nothing here needs a
    widget.
    """

    def __init__(self):
        self.state_selected = []

    def on_input_state_selected(self, controller, identifier, state):
        self.state_selected.append((controller.serial_number(), identifier, state))


def fresh_flush() -> None:
    """A flush seam that writes only when told, installed process-wide.

    Every production caller reaches the seam through page_flush.get(), so
    replacing the singleton is the injection point for the whole process.
    """
    page_flush._flush = page_flush.PageFlush(scheduler=NoTimers())


def seed_states_page(name: str, n_states: int, extra: dict | None = None) -> str:
    """Write a page whose key 0x0 carries n_states action-free states.

    extra goes beside the states map, which is where the state number lives,
    so a check can plant one the way another build would leave it.
    """
    path = os.path.join(gl.page_manager.PAGE_PATH, f"{name}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key_config: dict = {"states": {str(i): {} for i in range(n_states)}}
    if extra is not None:
        key_config.update(extra)
    with open(path, "w") as f:
        json.dump({
            "keys": {"0x0": key_config},
            "dials": {},
            "touchscreens": {},
            "settings": {"screensaver": {"enable": False, "time-delay": 60}},
        }, f)
    return path


def read_file(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def key_of(content: dict) -> dict:
    """The 0x0 entry of a page's content, from the file or from memory."""
    return content["keys"]["0x0"]


def stored_number(content: dict):
    """The state number a page's content carries for 0x0, or None."""
    return key_of(content).get(ACTIVE_STATE_KEY)


def load_barrier(c_input) -> dict:
    """Give a counter of the loads c_input has finished.

    A page switch loads the inputs on the deck's own thread, so a check waits
    for the load to end rather than for a time to pass. A count of the states
    is no barrier: the load builds them first and picks the state last, so the
    count is right already when the page before had as many states.
    """
    box = getattr(c_input, "_test_loads", None)
    if box is not None:
        return box
    box = {"n": 0}
    real = c_input.load_from_input_dict

    def counting(input_dict, *args, **kwargs):
        real(input_dict, *args, **kwargs)
        box["n"] += 1

    c_input.load_from_input_dict = counting
    c_input._test_loads = box
    return box


def show_page(controller, path: str, n_states: int):
    """Put the deck on the page at path, and wait for its inputs to load."""
    page = gl.page_manager.get_page(path, controller)
    c_input = controller.get_input(IDENT)
    assert c_input is not None, "the fake deck has no key 0x0"
    box = load_barrier(c_input)
    loads = box["n"]
    controller.load_page(page)
    assert wait_until(lambda: controller.active_page is page, timeout=SETTLE_TIMEOUT_S), (
        f"the deck never reached {os.path.basename(path)}")
    assert wait_until(lambda: box["n"] > loads, timeout=SETTLE_TIMEOUT_S), (
        f"key 0x0 never finished loading from {os.path.basename(path)}")
    assert len(c_input.states) == n_states, (
        f"key 0x0 has {len(c_input.states)} states after loading "
        f"{os.path.basename(path)}, not the {n_states} the page carries -- "
        "the state checks below would prove nothing")
    return page, c_input


def check_a_state_change_reaches_the_page_and_its_file(controller) -> int:
    """A state change lands in the page, and reaches the file at a flush."""
    fresh_flush()
    path = seed_states_page("StateWrite", 3)
    page, c_input = show_page(controller, path, 3)

    c_input.set_state(1)
    if c_input.state != 1:
        print(f"FAIL: the input did not switch state, it reports {c_input.state}")
        return 1
    if stored_number(page.dict) != 1:
        print(f"FAIL: the page did not record the state: {key_of(page.dict)}")
        return 1
    if ACTIVE_STATE_KEY in key_of(read_file(path)):
        print("FAIL: the state reached the file with no flush -- the write "
              "goes around the page's own write instead of riding it")
        return 1

    page_flush.get().flush_path(path)
    if stored_number(read_file(path)) != 1:
        print(f"FAIL: the flush did not put the state in the file: "
              f"{key_of(read_file(path))}")
        return 1

    # Back on the first state the number goes, because an input with no
    # number opens on the first state anyway.
    c_input.set_state(0)
    page_flush.get().flush_path(path)
    if ACTIVE_STATE_KEY in key_of(read_file(path)):
        print(f"FAIL: the first state left a number in the file: "
              f"{key_of(read_file(path))}")
        return 1

    print("PASS: a state change lands in the page and reaches the file at a flush")
    return 0


def check_a_cold_load_opens_the_kept_state(controller) -> int:
    """A deck that arrives on a page opens the state the page names, whether
    it comes from another page or from a fresh start."""
    fresh_flush()
    path = seed_states_page("StateRestore", 3)
    elsewhere = seed_states_page("StateElsewhere", 2)
    page, c_input = show_page(controller, path, 3)

    c_input.set_state(2)
    page_flush.get().flush_all()

    # Away and back. The deck arrives on the page again, which is the page
    # switch a user makes.
    show_page(controller, elsewhere, 2)
    if c_input.state != 0:
        print(f"FAIL: the page the deck moved to names no state and the input "
              f"opened state {c_input.state}")
        return 1
    show_page(controller, path, 3)
    if c_input.state != 2:
        print(f"FAIL: a page switch back opened state {c_input.state}, not "
              "the state the page names")
        return 1

    # The next start of the app. Poison the number the page holds in memory,
    # without marking the page, so the file is the only place the state
    # survives. A second deck mints its own Page for the file, and that read
    # replaces what every Page of this file holds.
    key_of(page.dict)[ACTIVE_STATE_KEY] = 0
    second = make_headless_controller(serial="input-state-2")
    try:
        page_two, input_two = show_page(second, path, 3)
        if stored_number(page_two.dict) != 2:
            print(f"FAIL: the fresh read did not take the number from the file: "
                  f"{key_of(page_two.dict)} -- this leg proves nothing")
            return 1
        if input_two.state != 2:
            print(f"FAIL: a deck that loads the page fresh opened state "
                  f"{input_two.state}, not the state the file carries")
            return 1
    finally:
        teardown(second)

    print("PASS: a page switch and a fresh load both open the state the page names")
    return 0


def check_a_warm_reload_keeps_each_deck_on_its_own_state(controller) -> int:
    """A reload of a page a deck already shows keeps that deck's state.

    One page carries one number while two decks can show it on different
    states, so an edit made through one deck must not drag the other one.
    """
    fresh_flush()
    path = seed_states_page("StateShared", 3)
    page_one, input_one = show_page(controller, path, 3)
    input_one.set_state(2)

    second = make_headless_controller(serial="input-state-shared-2")
    try:
        page_two, input_two = show_page(second, path, 3)
        if input_two.state != 2:
            print(f"FAIL: the second deck arrived on state {input_two.state}, "
                  "not the state the page names -- this leg would prove nothing")
            return 1
        # The second deck goes its own way. The page follows it, because the
        # page keeps one number and this was the last state change.
        input_two.set_state(1)
        if input_one.state != 2:
            print(f"FAIL: a state change on one deck moved the other deck to "
                  f"state {input_one.state}")
            return 1
        if stored_number(page_one.dict) != 1:
            print(f"FAIL: the page does not name the second deck's state: "
                  f"{key_of(page_one.dict)} -- this leg would prove nothing")
            return 1

        # The reload an edit triggers: it reloads this input on every deck
        # showing the page.
        page_one.reload_similar_pages(identifier=IDENT, reload_self=True)

        if input_one.state != 2:
            print(f"FAIL: a reload moved the first deck from state 2 to "
                  f"{input_one.state} -- the page's number won over the "
                  "state that deck is on")
            return 1
        if input_two.state != 1:
            print(f"FAIL: a reload moved the second deck from state 1 to "
                  f"{input_two.state}")
            return 1
        if stored_number(page_one.dict) != 1:
            print(f"FAIL: the reload rewrote the page's number: "
                  f"{key_of(page_one.dict)}")
            return 1
    finally:
        teardown(second)

    print("PASS: a reload keeps every deck on the state it is on")
    return 0


def check_a_page_that_names_no_state_opens_the_first(controller) -> int:
    """A page carrying no number opens its inputs on the first state, whatever
    the deck showed before."""
    fresh_flush()
    from_path = seed_states_page("StateFrom", 3)
    to_path = seed_states_page("StateTo", 2)

    page_from, c_input = show_page(controller, from_path, 3)
    c_input.set_state(2)
    page_flush.get().flush_all()

    show_page(controller, to_path, 2)
    if c_input.state != 0:
        print(f"FAIL: the page the deck arrived on names no state, and the "
              f"input opened state {c_input.state} -- the state of the page "
              "the deck left carried over")
        return 1
    if stored_number(read_file(to_path)) is not None:
        print(f"FAIL: a page nobody took off the first state gained a number: "
              f"{key_of(read_file(to_path))}")
        return 1
    if stored_number(read_file(from_path)) != 2:
        print(f"FAIL: the page the deck left lost its number: "
              f"{key_of(read_file(from_path))}")
        return 1

    print("PASS: a page that names no state opens the first one, and the page "
          "the deck left keeps its number")
    return 0


def check_a_number_the_input_cannot_show_opens_the_first(controller) -> int:
    """A number outside the states the input has, and a value that is no state
    number, both open the first state. Only the second is dropped."""
    fresh_flush()
    # 3 is what a page written with four states carries after a plugin
    # rebuilt the input with two. It stays, because the missing states can
    # come back. The rest are what a hand edit leaves, and they go.
    for name, planted, keep in (("StateTooHigh", 3, True),
                                ("StateNegative", -1, False),
                                ("StateText", "1", False),
                                ("StateBool", True, False)):
        path = seed_states_page(name, 2, extra={ACTIVE_STATE_KEY: planted})
        page, c_input = show_page(controller, path, 2)
        if c_input.state != 0:
            print(f"FAIL: a page naming {planted!r} opened state "
                  f"{c_input.state} on an input with 2 states")
            return 1

        page_flush.get().flush_all()
        left = stored_number(read_file(path))
        if keep and left != planted:
            print(f"FAIL: the page lost the number {planted!r} it named, which "
                  f"the states it points at can come back to: it now says {left!r}")
            return 1
        if not keep and ACTIVE_STATE_KEY in key_of(read_file(path)):
            print(f"FAIL: {planted!r} is no state number and the load left it "
                  f"in the page: {key_of(read_file(path))}")
            return 1

        # The input still works, and the next state change replaces what the
        # page carried.
        c_input.set_state(1)
        page_flush.get().flush_path(path)
        if stored_number(read_file(path)) != 1:
            print(f"FAIL: after a page naming {planted!r}, a state change did "
                  f"not reach the file: {key_of(read_file(path))}")
            return 1

    print("PASS: a number the input cannot show opens the first state, and only "
          "a value that is no state number is dropped")
    return 0


def check_a_write_reaches_only_the_page_the_input_loaded(controller) -> int:
    """A state change during a page switch stays off the arriving page."""
    fresh_flush()
    from_path = seed_states_page("StateRaceFrom", 3)
    to_path = seed_states_page("StateRaceTo", 3)
    page_from, c_input = show_page(controller, from_path, 3)
    c_input.set_state(1)
    page_to = gl.page_manager.get_page(to_path, controller)

    # The window a page switch opens: the deck has taken the new page, and
    # this input still holds the states of the old one. A plugin's state
    # change lands right there.
    controller.active_page = page_to
    try:
        c_input.set_state(2)
    finally:
        controller.active_page = page_from

    page_flush.get().flush_all()
    if ACTIVE_STATE_KEY in key_of(page_to.dict) or ACTIVE_STATE_KEY in key_of(read_file(to_path)):
        print(f"FAIL: the page the deck was arriving on gained a number it "
              f"never had: {key_of(page_to.dict)}")
        return 1
    if stored_number(read_file(from_path)) != 1:
        print(f"FAIL: the page the input belongs to no longer carries the "
              f"state it was left on: {key_of(read_file(from_path))}")
        return 1

    # Put the input back on the state its page names, so the change below is
    # one, and a write that never happens cannot read as one that did.
    c_input.set_state(1)

    # The other half of the same window: a load of the leaving page that
    # finishes after the deck has taken the new one. It carries the leaving
    # page's states, so it must record that page and not the arriving one.
    controller.active_page = page_to
    try:
        controller.load_input(c_input, page_from)
    finally:
        controller.active_page = page_from
    c_input.set_state(2)
    page_flush.get().flush_all()
    if stored_number(read_file(to_path)) is not None:
        print(f"FAIL: a late load of the leaving page tagged the arriving one, "
              f"and the next state change wrote there: {key_of(read_file(to_path))}")
        return 1
    if stored_number(read_file(from_path)) != 2:
        print(f"FAIL: the state change after a late load did not reach the page "
              f"the load read: {key_of(read_file(from_path))}")
        return 1

    print("PASS: a state change during a page switch reaches neither page's file "
          "wrongly")
    return 0


def check_a_rename_keeps_the_page_the_input_holds(controller) -> int:
    """A page rename re-points the file of the page a deck shows.

    Nothing reloads the inputs for it, so an input must still know the page it
    holds: its next state change belongs in that page, and its next reload is
    a reload of the page it is already on.
    """
    fresh_flush()
    old_path = seed_states_page("StateRenameFrom", 3)
    new_path = os.path.join(gl.page_manager.PAGE_PATH, "StateRenameTo.json")
    page, c_input = show_page(controller, old_path, 3)
    c_input.set_state(1)
    page_flush.get().flush_all()

    gl.page_manager.move_page(old_path, new_path)
    if page.json_path != new_path:
        print(f"FAIL: the rename left the page on {page.json_path} -- this leg "
              "would prove nothing")
        return 1

    c_input.set_state(2)
    if stored_number(page.dict) != 2:
        print(f"FAIL: a state change after a rename never reached the page: "
              f"{key_of(page.dict)}")
        return 1
    page_flush.get().flush_all()
    if stored_number(read_file(new_path)) != 2:
        print(f"FAIL: a state change after a rename never reached the renamed "
              f"file: {key_of(read_file(new_path))}")
        return 1

    # The reload an edit triggers, now under the new name. The deck is on this
    # page already, so it keeps the state it is on.
    controller.load_input(c_input, page)
    if c_input.state != 2:
        print(f"FAIL: a reload after a rename read the page cold and moved the "
              f"input to state {c_input.state}")
        return 1

    print("PASS: a rename keeps the page an input holds, for its writes and its "
          "reloads")
    return 0


def check_the_sidebar_shows_the_state_without_selecting_it(controller) -> int:
    """The sidebar's input editor must never move the input.

    It runs to mirror the input: from the sidebar build, from the task the
    build defers until the window maps, and from every refresh. Each carries
    the state its caller last held, so a selection from there moves the input
    to a state the user did not pick, and the page keeps what it is moved to.
    """
    fresh_flush()
    from src.windows.mainWindow.elements.Sidebar.Sidebar import KeyEditor

    path = seed_states_page("StateSidebar", 3)
    page, c_input = show_page(controller, path, 3)
    c_input.set_state(1)
    page_flush.get().flush_path(path)

    loaded = []

    def recorder(name):
        return SimpleNamespace(
            load_for_identifier=lambda identifier, state: loaded.append((name, state)),
            get_n_states=lambda: len(c_input.states),
        )

    editor = SimpleNamespace(
        sidebar=SimpleNamespace(active_identifier=None),
        state_switcher=recorder("state_switcher"),
        remove_state_button=SimpleNamespace(set_visible=lambda visible: None),
        icon_selector=recorder("icon_selector"),
        image_editor=recorder("image_editor"),
        label_editor=recorder("label_editor"),
        action_editor=recorder("action_editor"),
        background_editor=recorder("background_editor"),
    )

    saved_app, saved_window = gl.app, services.require_main_window
    gl.app = object()
    services.require_main_window = lambda: SimpleNamespace(
        get_active_controller=lambda: controller)
    try:
        # The task the sidebar defers to its map, replayed: it carries the
        # state 0 the build handed it while the window was still hidden.
        KeyEditor.load_for_identifier(editor, IDENT, 0)
    finally:
        gl.app = saved_app
        services.require_main_window = saved_window

    if not any(name == "background_editor" for name, _state in loaded):
        print(f"FAIL: the editor load returned early and reached no row: "
              f"{loaded} -- this leg would prove nothing")
        return 1
    if c_input.state != 1:
        print(f"FAIL: showing the editor moved the input to state "
              f"{c_input.state}")
        return 1
    page_flush.get().flush_all()
    if stored_number(read_file(path)) != 1:
        print(f"FAIL: showing the editor took the state out of the page: "
              f"{key_of(read_file(path))}")
        return 1

    print("PASS: the sidebar's input editor shows the state without selecting it")
    return 0


def check_a_reload_with_no_move_leaves_the_sidebar_alone(controller, port) -> int:
    """Only a load that moves the state refreshes the sidebar.

    The sidebar's input editor puts the main stack back on itself, so a
    refresh with no move takes a user out of an action edit.
    """
    fresh_flush()
    path = seed_states_page("StateSidebarSync", 3)
    page, c_input = show_page(controller, path, 3)
    c_input.set_state(1)

    del port.state_selected[:]
    page.reload_similar_pages(identifier=IDENT, reload_self=True)
    if port.state_selected:
        print(f"FAIL: a reload that moved no state refreshed the sidebar: "
              f"{port.state_selected}")
        return 1
    if c_input.state != 1:
        print(f"FAIL: the reload moved the input to state {c_input.state}")
        return 1

    # A load that does move the state must reach the sidebar, or the editor
    # keeps showing a state the input has left. Take the shown state out of
    # the page, which is what removing a state on another deck does, and
    # reload: the input has nowhere to stay.
    del port.state_selected[:]
    with page.edit() as data:
        data["keys"]["0x0"]["states"].pop("2")
        data["keys"]["0x0"]["states"].pop("1")
    page.reload_similar_pages(identifier=IDENT, reload_self=True)
    if c_input.state != 0:
        print(f"FAIL: the input stayed on state {c_input.state}, which the "
              "page no longer carries -- this leg would prove nothing")
        return 1
    if not port.state_selected:
        print("FAIL: a load that opened another state did not reach the sidebar")
        return 1

    print("PASS: only a load that moves the state refreshes the sidebar")
    return 0


def main() -> int:
    start_watchdog(WATCHDOG_SECONDS, label="scenario_input_state_persistence")
    controller = make_headless_controller(serial="input-state-1")
    port = RecordingPort()
    ui_port.install(port)
    failures = 0
    try:
        for check in (
            check_a_state_change_reaches_the_page_and_its_file,
            check_a_cold_load_opens_the_kept_state,
            check_a_warm_reload_keeps_each_deck_on_its_own_state,
            check_a_page_that_names_no_state_opens_the_first,
            check_a_number_the_input_cannot_show_opens_the_first,
            check_a_write_reaches_only_the_page_the_input_loaded,
            check_a_rename_keeps_the_page_the_input_holds,
            check_the_sidebar_shows_the_state_without_selecting_it,
        ):
            failures += check(controller)
        failures += check_a_reload_with_no_move_leaves_the_sidebar_alone(controller, port)
    finally:
        teardown(controller)

    if failures:
        print(f"FAIL: scenario_input_state_persistence ({failures} failed)")
        return 1
    print("PASS: scenario_input_state_persistence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
