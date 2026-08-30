"""Require detached actions to return off-page values instead of raising."""

import threading
import types

import fixtures  # noqa: F401  (isolated --data tempdir; import first)

import globals as gl  # noqa: F401,E402  (import order)

from src.backend.DeckManagement.InputIdentifier import Input  # noqa: E402
from src.backend.PageManagement.Page import Page  # noqa: E402
from src.backend.PluginManager.ActionCore import ActionCore  # noqa: E402


def _action(page: "Page | None") -> ActionCore:
    """Build only the ActionCore state used by teardown and page readers."""
    action = ActionCore.__new__(ActionCore)
    action.page = page
    action.state = 0
    action.action_id = "com_test_detached::Action"
    action.action_name = "Detached"
    action.input_ident = Input.Key("0x0")
    action.on_ready_called = True
    action.on_ready_finished = True
    action._cleaned_up = False
    action._cleanup_lock = threading.Lock()
    action._connected_signals = []
    action.generative_ui_objects = []
    action.backend_connection = None
    action.backend = None
    action.server = None
    action.backend_process = None
    return action


def check_clear_action_objects_detaches() -> None:
    """The teardown of a page's actions detaches each one from that page."""
    page = Page.__new__(Page)
    action = _action(page)
    page.action_objects = {
        Input.Key: {action.input_ident.json_identifier: {0: {0: action}}},
    }

    page.clear_action_objects()

    assert action.page is None, (
        "clear_action_objects left the action attached to the page it tore down"
    )
    assert page.action_objects[Input.Key] == {}, "the action registry was not emptied"
    print("PASS: the page teardown detaches every action it drops")


def check_detached_readers_answer() -> None:
    """Every reader of action.page answers for a detached action."""
    action = _action(None)

    assert action.get_is_present() is False, "a detached action reported itself present"
    assert action.get_settings() == {}, "a detached action's settings read did not answer"
    action.set_settings({"a": 1})  # must not raise
    assert action.get_is_multi_action() is False, (
        "get_is_multi_action raised or answered wrong for a detached action"
    )
    assert action.has_custom_user_asset() is False, (
        "has_custom_user_asset raised or answered wrong for a detached action"
    )
    assert action.get_own_action_index() == -1, (
        "get_own_action_index must report the off-page answer, not raise"
    )
    assert action.get_event_assignments() == {}, (
        "get_event_assignments raised or answered wrong for a detached action"
    )

    assignments = action.get_page_event_assignments()
    all_events = Input.AllEvents()
    assert set(assignments) == set(all_events), (
        "get_page_event_assignments dropped events for a detached action"
    )
    assert all(assignments[event] is event for event in all_events), (
        "a detached action must map every event to itself, as a page with no "
        f"stored assignment does: {assignments}"
    )

    action.set_event_assignment(Input.Key.Events.DOWN, None)  # must not raise
    print("PASS: every reader of a detached action's page answers instead of raising")


def check_presence_still_decides_the_index() -> None:
    """Require an attached but inactive-page action to report index -1."""

    class _Screensaver:
        showing = False

    class _Controller:
        def __init__(self):
            self.active_page = None
            self.screen_saver = _Screensaver()

    class _Page:
        def __init__(self):
            self.deck_controller = _Controller()
            self.actions: list = []

        def get_all_actions(self):
            return list(self.actions)

        def get_all_actions_for_input(self, ident, state):
            return list(self.actions)

    page = _Page()
    first = _action(page)
    second = _action(page)
    page.actions = [first, second]

    # The deck shows another page, so this one is not live.
    assert first.get_is_present() is False, "an off-page action reported itself present"
    assert first.get_own_action_index() == -1, (
        f"an off-page action reported a live index: {first.get_own_action_index()}"
    )
    assert first.get_is_multi_action() is False, "an off-page action reported multi-action"
    assert first.has_custom_user_asset() is False, (
        "an off-page action reported a custom asset"
    )

    # The deck loads this page, so both actions are live.
    page.deck_controller.active_page = page
    assert first.get_is_present() is True, "a live action reported itself absent"
    assert first.get_own_action_index() == 0, (
        f"a live action lost its position: {first.get_own_action_index()}"
    )
    assert second.get_own_action_index() == 1, (
        f"the second live action lost its position: {second.get_own_action_index()}"
    )
    assert first.get_is_multi_action() is True, (
        "a key holding two actions did not report multi-action"
    )
    print("PASS: presence, and not the page reference alone, decides the action index")


def check_attached_readers_still_reach_the_page() -> None:
    """Require attached actions to continue reading and writing through their page."""
    reads: list = []

    class _Page:
        def get_action_event_assignments(self, action_object):
            reads.append("get_action_event_assignments")
            return {Input.Key.Events.DOWN.string_name: str(Input.Key.Events.UP)}

        def set_action_event_assigment(self, event_assigner, input_event, action_object):
            reads.append("set_action_event_assigment")

        def get_action_settings(self, action_object):
            reads.append("get_action_settings")
            return {"stored": True}

    action = _action(_Page())
    action.event_manager = types.SimpleNamespace(
        set_overrides=lambda overrides: reads.append("set_overrides"),
        get_event_map=lambda: {},
    )

    assert action.get_settings() == {"stored": True}, "an attached action lost its settings read"
    assert action.get_event_assignments() == {
        Input.Key.Events.DOWN.string_name: str(Input.Key.Events.UP)
    }, "an attached action lost its event-assignment read"

    assignments = action.get_page_event_assignments()
    assert assignments[Input.Key.Events.DOWN] == Input.Key.Events.UP, (
        f"a stored assignment did not reach an attached action: {assignments}"
    )

    action.set_event_assignment(Input.Key.Events.DOWN, None)
    assert "set_action_event_assigment" in reads, (
        "an attached action's event assignment was not written to its page"
    )
    assert "set_overrides" in reads, "the write did not reload the event overrides"
    print("PASS: an attached action still reads and writes through its page")


def main() -> None:
    # Below the per-scenario timeout of run_all.py, so a stall reports here
    # with a message instead of an opaque runner timeout.
    fixtures.start_watchdog(60, label="scenario_detached_action_guards")

    check_clear_action_objects_detaches()
    check_detached_readers_answer()
    check_presence_still_decides_the_index()
    check_attached_readers_still_reach_the_page()

    print("PASS: scenario_detached_action_guards")


if __name__ == "__main__":
    main()
