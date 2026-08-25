"""
The action tick clears its re-entrancy flag on every path that leaves its window.

own_actions_tick_threaded arms a per-state flag, then submits the tick onto the
action pool, and the submitted work clears the flag when it completes. Between
the arming and the submit there is a window. A raise inside that window used to
leave the flag armed with nothing left to clear it, and that one input never
ticked again for the life of the page: its animated actions froze while the
tick loop, whose guard contains the raise, kept walking every other input.

The window has exactly one owner per outcome. A submit that reaches the
completion callback hands the flag to it. Every other way out of the window,
a raise or a submit that took no worker, clears the flag on the spot.
"""

import os
import threading
import time

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl
from fixtures import start_watchdog, teardown, wait_until

from src.backend.DeckManagement.DeckController import ControllerInputState
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore

# The whole scenario stays well inside run_all.py's per-scenario timeout, so a
# failing leg reports its own assertion instead of being killed from outside.
WATCHDOG_SECONDS = 75
# Budget for one wait. Only one wait per run can time out, because the assert
# that follows it raises.
WAIT_SECONDS = 15
# Tick iterations to observe before a submit count is read.
OBSERVED_WINDOWS = 2
# The thread DeckController gives tick_actions. Submits are attributed by
# calling thread, because the pool also serves updates and input events.
TICK_THREAD = "tick_actions"
# How long a leg holds the tick worker to read the flag while work is in
# flight. Long enough to survive a loaded machine, short enough to not matter.
HOLD_SECONDS = 5


class FlagProbe:
    """Counts tick-thread submits and flag clears, per input and state.

    Both patches sit on the class, not on a state object, because a page load
    builds fresh state objects. Counts are keyed by identifier and state index
    for the same reason.
    """

    def __init__(self, controller):
        self.lock = threading.Lock()
        self.submits: dict[tuple[str, str, int], int] = {}
        self.clears: dict[tuple[str, str, int], int] = {}
        self.marks = 0
        self.controller = controller
        self.real_submit = ControllerInputState._submit_action_callback
        self.real_clear = ControllerInputState._on_tick_done
        self.real_mark = controller.mark_page_ready_to_clear
        # Set to an identifier to make every submit for it raise.
        self.raise_for: str | None = None

    @staticmethod
    def key(state):
        identifier = state.controller_input.identifier
        return (identifier.input_type, identifier.json_identifier, state.state)

    def install(self) -> None:
        probe = self

        def counting_submit(state, fn, *args):
            key = probe.key(state)
            if probe.raise_for == key[1]:
                raise RuntimeError("injected failure inside the tick window")
            if threading.current_thread().name == TICK_THREAD:
                with probe.lock:
                    probe.submits[key] = probe.submits.get(key, 0) + 1
            return probe.real_submit(state, fn, *args)

        def counting_clear(state, future):
            with probe.lock:
                key = probe.key(state)
                probe.clears[key] = probe.clears.get(key, 0) + 1
            return probe.real_clear(state, future)

        def counting_mark(*args, **kwargs):
            if threading.current_thread().name == TICK_THREAD:
                with probe.lock:
                    probe.marks += 1
            return probe.real_mark(*args, **kwargs)

        ControllerInputState._submit_action_callback = counting_submit
        ControllerInputState._on_tick_done = counting_clear
        self.controller.mark_page_ready_to_clear = counting_mark

    def remove(self) -> None:
        ControllerInputState._submit_action_callback = self.real_submit
        ControllerInputState._on_tick_done = self.real_clear
        self.controller.mark_page_ready_to_clear = self.real_mark

    def reset(self) -> None:
        with self.lock:
            self.submits.clear()
            self.clears.clear()
            self.marks = 0

    def windows(self) -> int:
        # tick_actions brackets each iteration between a False call and a True
        # call of mark_page_ready_to_clear, so two marks make one iteration.
        # Minus one, because the probe can install midway through an iteration
        # and see a lone True call first.
        with self.lock:
            return max(0, self.marks // 2 - 1)

    def submit_count(self, identifier, state: int = 0) -> int:
        with self.lock:
            return self.submits.get(
                (identifier.input_type, identifier.json_identifier, state), 0)

    def clear_count(self, identifier, state: int = 0) -> int:
        with self.lock:
            return self.clears.get(
                (identifier.input_type, identifier.json_identifier, state), 0)


def observe_windows(probe) -> int:
    """Wait out OBSERVED_WINDOWS full tick iterations and return the count."""
    assert wait_until(lambda: probe.windows() >= OBSERVED_WINDOWS,
                      timeout=WAIT_SECONDS), (
        f"the action tick completed only {probe.windows()} fully observed "
        f"iterations -- the observation window never happened, so any count "
        f"read from it pins nothing")
    return probe.windows()


def actions_for(controller, identifier) -> list:
    return controller.active_page.get_all_actions_for_input(identifier, 0)


def set_page_actions(controller, identifier, action_ids) -> None:
    """Rewrite one input's action list, the shape the action sidebar writes."""
    page = controller.active_page
    state_dict = identifier.ensure_state_dict(page, 0)
    state_dict["actions"] = [{"id": action_id, "settings": {}}
                             for action_id in action_ids]
    state_dict.setdefault("image-control-action", 0)
    state_dict.setdefault("label-control-actions", [0, 0, 0])
    state_dict.setdefault("background-control-action", 0)
    page.save()
    page.load()
    page.reload_similar_pages(identifier=identifier, reload_self=True)


def give_action(controller, identifier, action_id):
    """Put one real action on an input and hand back its live state."""
    set_page_actions(controller, identifier, [action_id])
    assert wait_until(
        lambda: any(isinstance(a, ActionCore)
                    for a in actions_for(controller, identifier)),
        timeout=WAIT_SECONDS), (
        f"fixture sanity: no real action reached {identifier}'s action table, "
        f"so the tick would return before it ever arms the flag")
    controller_input = controller.get_input(identifier)
    assert controller_input is not None, f"fixture sanity: no input {identifier}"
    return controller_input.get_active_state()


def raise_in_the_window_leaves_no_stranded_flag(controller, probe, identifier) -> None:
    """A raise between the flag write and the submit must not silence the input.

    The submit is made to raise, which is the window the flag has no other
    owner in. The tick loop guards each input, so the loop survives either
    way; what this leg reads is whether the input it raised on ticks again.
    """
    state = give_action(controller, identifier, fixtures.STUB_ACTION_ID)

    probe.raise_for = identifier.json_identifier
    try:
        state._tick_running = False
        try:
            state.own_actions_tick_threaded()
        except RuntimeError:
            pass
        else:
            raise AssertionError(
                "fixture sanity: the injected failure never reached the tick, "
                "so this leg exercises no window at all")
        assert state._tick_running is False, (
            f"the tick raised inside its window and left the re-entrancy flag "
            f"set on {identifier} -- no submit exists to clear it, so this "
            f"input never ticks again and its animated actions stay frozen")
    finally:
        probe.raise_for = None

    # The frozen input is the observable symptom. Read it: the tick loop must
    # submit for this input again once the failure is gone.
    probe.reset()
    observed = observe_windows(probe)
    assert probe.submit_count(identifier) > 0, (
        f"{identifier} drew no tick submit across {observed} iterations after "
        f"a raise inside the tick window -- the input is frozen")
    print(f"PASS: a raise inside the tick window clears the flag, and the "
          f"input ticks again ({probe.submit_count(identifier)} submits in "
          f"{observed} iterations)")


def the_normal_path_clears_once_and_not_early(controller, probe, identifier) -> None:
    """A submitted tick keeps the flag until its own completion clears it.

    The worker is held, and the flag is read while it is in flight. A clear
    added ahead of the completion would read False there, and a second owner
    would show as more than one clear for one submit.
    """
    state = give_action(controller, identifier, fixtures.STUB_ACTION_ID)

    release = threading.Event()
    entered = threading.Event()
    real_tick = state.own_actions_tick

    def holding_tick(*args, **kwargs):
        entered.set()
        release.wait(HOLD_SECONDS)
        return real_tick(*args, **kwargs)

    state.own_actions_tick = holding_tick
    try:
        probe.reset()
        state._tick_running = False
        state.own_actions_tick_threaded()
        assert wait_until(entered.is_set, timeout=WAIT_SECONDS), (
            f"fixture sanity: the tick worker for {identifier} never ran, so "
            f"the flag reads below prove nothing")
        assert state._tick_running is True, (
            f"the re-entrancy flag on {identifier} is already clear while its "
            f"tick worker still runs -- a second tick can enter on top of the "
            f"first, which is what the flag exists to stop")
        assert probe.clear_count(identifier) == 0, (
            f"the tick completion for {identifier} ran while its worker is "
            f"still in flight")
    finally:
        release.set()
        state.own_actions_tick = real_tick

    assert wait_until(lambda: state._tick_running is False, timeout=WAIT_SECONDS), (
        f"the tick worker for {identifier} finished and left the re-entrancy "
        f"flag set -- that input never ticks again")
    # Let the loop keep walking, then read the clears against the submits. The
    # loop's own submits count too, so the two numbers have to move together.
    # Read at a settle point rather than an instant: a submit counted just
    # before an instantaneous read whose worker completes just after would
    # show clears one behind and fail a correct tree.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        time.sleep(0.1)
    # Equilibrium read: both counters grow together and clears trails only
    # while a tick is in flight, so the two match whenever the input is idle.
    assert fixtures.wait_until(
        lambda: probe.clear_count(identifier)
        == probe.submit_count(identifier) + 1, timeout=3.0), (
        f"{identifier} took {probe.submit_count(identifier) + 1} tick submits "
        f"and {probe.clear_count(identifier)} flag clears -- one owner per "
        f"submit is the invariant, and a mismatch means either a stranded "
        f"flag or a second owner clearing early")
    submits = probe.submit_count(identifier) + 1  # the direct call above
    print(f"PASS: {submits} submitted ticks cleared the flag exactly once each, "
          f"and never before the worker returned")


def main() -> None:
    latch_cls = fixtures.make_latch_action_class()
    icon_path = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "tick_flag_icon.png"), color=(0, 90, 180))
    fixtures.install_stub_plugin_manager(latch_cls, icon_path)
    start_watchdog(WATCHDOG_SECONDS, label="scenario_tick_flag_clear")

    controller = fixtures.make_headless_controller(serial="tick-flag-1",
                                                   page_name="TickFlagHome")
    probe = FlagProbe(controller)
    probe.install()
    try:
        keys = controller.inputs[Input.Key]
        assert len(keys) >= 2, (
            f"fixture sanity: the fake deck exposes {len(keys)} keys, and the "
            f"legs below need one key each")
        raising = keys[0].identifier
        normal = keys[1].identifier

        raise_in_the_window_leaves_no_stranded_flag(controller, probe, raising)
        the_normal_path_clears_once_and_not_early(controller, probe, normal)
    finally:
        probe.remove()
        teardown(controller)

    print("\nALL PASS: scenario_tick_flag_clear")


if __name__ == "__main__":
    main()
