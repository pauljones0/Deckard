"""
An input opens on the state it was last left on.

The page carries which of its states each input shows, so the number survives
a page reload, a page switch and the next launch. A page that never leaves the
first state carries no such number, and a number the input cannot show selects
the first state rather than nothing.
"""

# Timers stay disarmed, so every write here is one a check asks for by name.
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import os

import globals as gl
from fixtures import make_headless_controller, start_watchdog, teardown, wait_until

from src.backend.DeckManagement.InputIdentifier import ACTIVE_STATE_KEY, Input
from src.backend.PageManagement import page_flush

WATCHDOG_SECONDS = 90

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


def fresh_flush() -> None:
    """A flush seam that writes only when told, installed process-wide.

    Every production caller reaches the seam through page_flush.get(), so
    replacing the singleton is the injection point for the whole process.
    """
    page_flush._flush = page_flush.PageFlush(scheduler=NoTimers())


def seed_states_page(name: str, n_states: int, extra: dict | None = None) -> str:
    """Write a page whose key 0x0 carries n_states action-free states.

    extra goes beside the states map, which is where the state number lives,
    so a check can plant one the way a page from another build carries it.
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
    is no barrier: the load builds them first and picks the state last, and
    the count is right already when the page before had as many states.
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


def check_a_reload_and_a_restart_open_the_kept_state(controller) -> int:
    """The number survives a page reload, and the file carries it to the next
    launch."""
    fresh_flush()
    path = seed_states_page("StateRestore", 3)
    page, c_input = show_page(controller, path, 3)

    c_input.set_state(2)
    page_flush.get().flush_path(path)

    # A launch opens every input on state 0, so zero the number in memory.
    # What comes back can then only come from the page.
    c_input.state = 0
    controller.load_input(c_input, page)
    if c_input.state != 2:
        print(f"FAIL: a page reload opened state {c_input.state}, not the "
              "state the page carries")
        return 1

    # The restart. Poison the number the page holds in memory, without
    # marking the page, so the file is the only place the state survives. A
    # second deck mints its own Page for the file, and that read replaces
    # what every Page of this file holds.
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

    print("PASS: the state survives a page reload and a fresh load of the file")
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
    """A number outside the states the input has, and a number that is not a
    state at all, both open the first state."""
    fresh_flush()
    # 3 is what a page written with four states carries after a plugin
    # rebuilt the input with two. The rest are what a hand edit leaves.
    for name, planted in (("StateTooHigh", 3),
                          ("StateNegative", -1),
                          ("StateText", "1"),
                          ("StateBool", True)):
        path = seed_states_page(name, 2, extra={ACTIVE_STATE_KEY: planted})
        page, c_input = show_page(controller, path, 2)
        if c_input.state != 0:
            print(f"FAIL: a page naming {planted!r} opened state "
                  f"{c_input.state} on an input with 2 states")
            return 1

        # The input still works, and the next state change replaces what the
        # page carried.
        c_input.set_state(1)
        page_flush.get().flush_path(path)
        if stored_number(read_file(path)) != 1:
            print(f"FAIL: after a page naming {planted!r}, a state change did "
                  f"not reach the file: {key_of(read_file(path))}")
            return 1

    print("PASS: a number the input cannot show opens the first state, and the "
          "next state change replaces it")
    return 0


def check_a_page_left_on_the_first_state_keeps_its_bytes(controller) -> int:
    """Loading a page and flushing it writes no number into a page that never
    left the first state."""
    fresh_flush()
    path = seed_states_page("StateBytes", 3)
    with open(path, "rb") as f:
        before = f.read()

    show_page(controller, path, 3)
    page_flush.get().flush_all()

    with open(path, "rb") as f:
        after = f.read()
    if after != before:
        print(f"FAIL: loading a page rewrote it:\n  before {before!r}\n  after  {after!r}")
        return 1

    print("PASS: a page nobody takes off the first state keeps the bytes it had")
    return 0


def main() -> int:
    start_watchdog(WATCHDOG_SECONDS, label="scenario_input_state_persistence")
    controller = make_headless_controller(serial="input-state-1")
    failures = 0
    try:
        for check in (
            check_a_state_change_reaches_the_page_and_its_file,
            check_a_reload_and_a_restart_open_the_kept_state,
            check_a_page_that_names_no_state_opens_the_first,
            check_a_number_the_input_cannot_show_opens_the_first,
            check_a_page_left_on_the_first_state_keeps_its_bytes,
        ):
            failures += check(controller)
    finally:
        teardown(controller)

    if failures:
        print(f"FAIL: scenario_input_state_persistence ({failures} failed)")
        return 1
    print("PASS: scenario_input_state_persistence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
