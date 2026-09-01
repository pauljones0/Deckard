"""Verify that action ticks submit only for live ActionCore entries to prevent
permanent pool growth, and fail open when the action table changes concurrently."""

# Missing plugins leave truthy placeholders, so an emptiness check is not enough.
# Concurrent edits can make the unlocked gate read raise, so it must fail open.
import os
import threading

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl
from fixtures import start_watchdog, teardown, wait_until

from src.backend.DeckManagement.DeckController import ControllerInputState
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore

# The whole scenario stays well inside run_all.py's per-scenario timeout, so a
# failing leg reports its own assertion instead of being killed from outside.
WATCHDOG_SECONDS = 75
# Budget for one wait; the first timeout ends the run.
WAIT_SECONDS = 15
# Complete tick iterations to observe before a no-submit count is read.
OBSERVED_WINDOWS = 2
# The thread DeckController gives tick_actions. Submits are attributed by
# calling thread, because the pool also serves updates and input events.
TICK_THREAD = "tick_actions"
# An action id no holder resolves, which loads as a placeholder.
MISSING_ACTION_ID = "dev_test_MissingPlugin"


class TickProbe:
    """Count tick-thread submissions and iterations across rebuilt states."""

    def __init__(self, controller):
        self.lock = threading.Lock()
        self.submits: dict[tuple[str, str, int], int] = {}
        self.marks = 0
        self.controller = controller
        self.real_submit = ControllerInputState._submit_action_callback
        self.real_mark = controller.mark_page_ready_to_clear

    def install(self) -> None:
        probe = self

        def counting_submit(state, fn, *args):
            if threading.current_thread().name == TICK_THREAD:
                identifier = state.controller_input.identifier
                key = (identifier.input_type, identifier.json_identifier, state.state)
                with probe.lock:
                    probe.submits[key] = probe.submits.get(key, 0) + 1
            return probe.real_submit(state, fn, *args)

        def counting_mark(*args, **kwargs):
            if threading.current_thread().name == TICK_THREAD:
                with probe.lock:
                    probe.marks += 1
            return probe.real_mark(*args, **kwargs)

        ControllerInputState._submit_action_callback = counting_submit
        self.controller.mark_page_ready_to_clear = counting_mark

    def remove(self) -> None:
        ControllerInputState._submit_action_callback = self.real_submit
        self.controller.mark_page_ready_to_clear = self.real_mark

    def reset(self) -> None:
        with self.lock:
            self.submits.clear()
            self.marks = 0

    def windows(self) -> int:
        # Two marks bracket an iteration; exclude a possible partial first one.
        with self.lock:
            return max(0, self.marks // 2 - 1)

    def total(self) -> int:
        with self.lock:
            return sum(self.submits.values())

    def count(self, identifier, state: int = 0) -> int:
        with self.lock:
            return self.submits.get(
                (identifier.input_type, identifier.json_identifier, state), 0)

    def busiest(self) -> str:
        with self.lock:
            return ", ".join(
                f"{input_type} {json_identifier} state {state}: {n}"
                for (input_type, json_identifier, state), n in sorted(
                    self.submits.items(), key=lambda item: -item[1])[:5])


def observe_windows(controller, probe) -> int:
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
    """Rewrite one input's actions, getting its state dict on each call because
    the preceding call's page reload replaced the dict."""
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


def running_flags(controller) -> list:
    """Every state that holds the tick's re-entrancy flag set."""
    return [(str(controller_input.identifier), index)
            for input_list in controller.inputs.values()
            for controller_input in input_list
            for index, state in controller_input.states.items()
            if state._tick_running]


def empty_page_submits_nothing(controller, probe) -> None:
    """No input on the seeded page carries an action, so nothing is submitted."""
    # A visible screensaver would skip all inputs and make this count invalid.
    assert not controller.screen_saver.showing, (
        "a screensaver is showing, and the tick loop skips every input while "
        "it does -- the count below would pass with no gate at all")

    probe.reset()
    observed = observe_windows(controller, probe)
    assert probe.total() == 0, (
        f"the action tick made {probe.total()} pool submits across {observed} "
        f"iterations on a page with no action anywhere ({probe.busiest()}) -- "
        f"every one of them occupies a pool worker that is never retired")

    # The gate must return before setting the re-entrancy flag.
    stranded = running_flags(controller)
    assert not stranded, (
        f"the tick's re-entrancy flag is still set on {stranded} after "
        f"{observed} iterations that submitted nothing -- those inputs are "
        f"silenced for the life of the process")
    print(f"PASS: {observed} fully observed tick iterations on an action-free "
          f"page, 0 pool submits, no stranded re-entrancy flag")


def placeholder_only_submits_nothing(controller, probe, identifier) -> None:
    """A truthy unresolved-action placeholder draws no submit until it resolves."""
    set_page_actions(controller, identifier, [MISSING_ACTION_ID])
    assert wait_until(lambda: len(actions_for(controller, identifier)) == 1,
                      timeout=WAIT_SECONDS), (
        "fixture sanity: the unresolvable action never reached the page's "
        "action table")
    held = actions_for(controller, identifier)[0]
    assert not isinstance(held, ActionCore), (
        f"fixture sanity: an unresolvable action id loaded as "
        f"{type(held).__name__} and not as a placeholder, so the leg below "
        f"measures a real action")

    probe.reset()
    observed = observe_windows(controller, probe)
    assert probe.count(identifier) == 0, (
        f"the action tick made {probe.count(identifier)} pool submits for "
        f"{identifier} across {observed} iterations while that input held only "
        f"a placeholder -- the worker discards it and runs nothing, so each "
        f"submit spends a pool worker on nothing")
    print(f"PASS: a placeholder-only {identifier} draws no submit across "
          f"{observed} iterations")

    # Installing the missing plugin rebuilds the page's action objects, and the
    # per-tick read picks the resolved action up on the next iteration.
    set_page_actions(controller, identifier, [fixtures.STUB_ACTION_ID])
    assert wait_until(
        lambda: any(isinstance(a, ActionCore) for a in actions_for(controller, identifier)),
        timeout=WAIT_SECONDS), (
        "fixture sanity: the placeholder never resolved to a real action, so "
        "the resumed submit below would prove nothing")

    probe.reset()
    assert wait_until(lambda: probe.count(identifier) > 0, timeout=WAIT_SECONDS), (
        f"the action tick still submits nothing for {identifier} after the "
        f"rebuild resolved its placeholder into a real action -- the gate "
        f"holds a stale verdict and the action never ticks")
    print(f"PASS: submits resume for {identifier} once its placeholder resolves")


def added_action_resumes_submits(controller, probe, identifier) -> None:
    """An input that gains an action starts submitting again."""
    set_page_actions(controller, identifier, [fixtures.STUB_ACTION_ID])
    assert wait_until(
        lambda: any(isinstance(a, ActionCore) for a in actions_for(controller, identifier)),
        timeout=WAIT_SECONDS), (
        "fixture sanity: the added action never reached the page's action "
        "table, so a resumed submit below would prove nothing")

    probe.reset()
    assert wait_until(lambda: probe.count(identifier) > 0, timeout=WAIT_SECONDS), (
        f"the action tick still submits nothing for {identifier} after that "
        f"input gained an action -- the gate holds an empty verdict past the "
        f"page reload and the action never ticks")
    print(f"PASS: submits resume for {identifier} once the page gives it an action")


def raising_gate_still_submits(controller, probe, identifier) -> None:
    """A gate read that raises costs one gate miss, never the tick thread."""
    controller_input = controller.get_input(identifier)
    assert controller_input is not None, f"fixture sanity: no input {identifier}"
    state = controller_input.get_active_state()
    real_get_own_actions = state.get_own_actions

    def raising_gate(*args, **kwargs):
        # Raise only for the gate's tick-thread call to get_own_actions.
        if threading.current_thread().name == TICK_THREAD:
            raise RuntimeError("a gate read blew up")
        return real_get_own_actions(*args, **kwargs)

    # Restore as soon as the proof lands, so the injection covers the observed
    # window and nothing after it.
    state.get_own_actions = raising_gate
    try:
        probe.reset()
        submitted = wait_until(lambda: probe.count(identifier) > 0,
                               timeout=WAIT_SECONDS)
    finally:
        del state.get_own_actions

    assert controller.tick_thread.is_alive(), (
        f"a gate read that raised killed the tick thread -- {identifier} and "
        f"every other input on this deck stop ticking for the life of the "
        f"process")
    assert submitted, (
        f"a gate read that raised suppressed the submit for {identifier} -- "
        f"the gate fails closed, so an input whose page is being edited "
        f"silently loses its ticks")
    print(f"PASS: a raising gate read leaves the tick thread alive and still "
          f"submits for {identifier}")


def check_shutdown_pool_tick(controller, probe, identifier) -> None:
    """A shut-down but attached pool drops ticks without raising or stranding
    re-entrancy flags; this final check leaves the pool unusable."""
    controller_input = controller.get_input(identifier)
    assert controller_input is not None, f"fixture sanity: no input {identifier}"
    state = controller_input.get_active_state()
    assert any(isinstance(a, ActionCore) for a in state.get_own_actions()), (
        f"fixture sanity: {identifier} carries no real action, so the gate would "
        f"return before it ever submits and this leg would prove nothing")

    pool = controller.action_executor
    assert pool is not None, "fixture sanity: the deck built no action pool"
    pool.shutdown()  # shut down, and deliberately not nulled
    assert pool.is_shutdown, "fixture sanity: the pool did not take the shutdown"

    state._tick_running = False
    try:
        state.own_actions_tick_threaded()
    except Exception as error:
        raise AssertionError(
            f"a tick submit onto a shut-down pool raised {error!r} -- the loop's "
            f"guard costs {identifier} its tick and strands its re-entrancy "
            f"flag set, which silences that input for good") from error
    assert state._tick_running is False, (
        f"the tick took no worker, because the pool is shut down, but left the "
        f"re-entrancy flag set on {identifier} -- that input never ticks again")

    # The tick thread walks every input through the same path once a second.
    probe.reset()
    observed = observe_windows(controller, probe)
    assert controller.tick_thread.is_alive(), (
        f"the tick thread died on a shut-down pool after {observed} iterations")
    stranded = running_flags(controller)
    assert not stranded, (
        f"the re-entrancy flag is set on {stranded} after {observed} iterations "
        f"against a shut-down pool")
    print(f"PASS: a shut-down pool drops the tick quietly across {observed} iterations")


def main() -> None:
    latch_cls = fixtures.make_latch_action_class()
    icon_path = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "tick_gate_icon.png"), color=(0, 180, 90))
    fixtures.install_stub_plugin_manager(latch_cls, icon_path)
    start_watchdog(WATCHDOG_SECONDS, label="scenario_tick_submit_gate")

    controller = fixtures.make_headless_controller(serial="tick-gate-1",
                                                   page_name="TickGateHome")
    probe = TickProbe(controller)
    probe.install()
    try:
        keys = controller.inputs[Input.Key]
        assert len(keys) >= 3, (
            f"fixture sanity: the fake deck exposes {len(keys)} keys, and the "
            f"legs below need one key per action shape")
        with_action = keys[0].identifier
        raising = keys[1].identifier
        placeholder = keys[2].identifier

        # The action-free leg runs first: it counts every input on the deck,
        # so it has to see the page before any other leg edits it.
        empty_page_submits_nothing(controller, probe)
        placeholder_only_submits_nothing(controller, probe, placeholder)
        added_action_resumes_submits(controller, probe, with_action)
        raising_gate_still_submits(controller, probe, raising)
        # Last: it leaves the deck without a usable action pool.
        check_shutdown_pool_tick(controller, probe, with_action)
    finally:
        probe.remove()
        teardown(controller)

    print("\nALL PASS: scenario_tick_submit_gate")


if __name__ == "__main__":
    main()
