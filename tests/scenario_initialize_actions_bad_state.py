"""Skip non-integer page state keys during action reads and writes.
Initialization must still finish for a valid state after a corrupt key."""

# fixtures must import first: it points argv at an isolated data dir.
import fixtures

from fixtures import make_headless_controller, start_watchdog, wait_until

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore

BAD_STATE_KEY = "garbage"
ACTION_ID = "dev_test::BadStateStrand"


class StrandProbeAction(ActionCore):
    """Use real event overrides and record completion of the ready handshake."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ready_ran = False

    def on_ready(self):
        self.ready_ran = True


def main() -> int:
    start_watchdog(60, "initialize_actions_bad_state")
    fixtures._install_integration_globals()

    controller = make_headless_controller(serial="bad-state-strand")
    try:
        page = controller.active_page
        assert page is not None, "controller loaded no page"
        ident = Input.Key("0x0")

        # Put a populated invalid key before state 0 so each scan reaches it.
        page.dict.setdefault(ident.input_type, {})[ident.json_identifier] = {
            "states": {
                BAD_STATE_KEY: {"actions": [{"id": ACTION_ID}]},
                "0": {"actions": [{"id": ACTION_ID}]},
            }
        }

        action = StrandProbeAction(
            action_id=ACTION_ID,
            action_name="BadStateStrand",
            deck_controller=controller,
            page=page,
            plugin_base=None,
            state=0,
            input_ident=ident,
        )
        page.action_objects.setdefault(ident.input_type, {})[ident.json_identifier] = {0: {0: action}}

        # Confirm that the invalid key cannot be converted to an integer.
        raised = False
        try:
            int(BAD_STATE_KEY)
        except ValueError:
            raised = True
        assert raised, "the bad state key parses as an int -- this test proves nothing"

        # Skip the invalid key and resolve the valid state's action.
        found = page.get_action_dict(action_object=action)
        assert found == {"id": ACTION_ID}, (
            f"get_action_dict returned {found!r}, not the state-0 action dict -- "
            "the bad-key skip must still resolve the real state"
        )

        # Event assignment reads use the same state scan.
        assert page.get_action_event_assignments(action_object=action) == {}, (
            "get_action_event_assignments must resolve past the bad key"
        )

        # Initialization must complete the ready handshake after the invalid key.
        assert not action.on_ready_called, "fresh action already claimed ready"
        page.initialize_actions()

        assert action.on_ready_called, (
            "initialize_actions never claimed the action -- setup is wrong"
        )
        finished = wait_until(lambda: action.on_ready_finished, timeout=5)
        assert finished, (
            "the action never reached on_ready_finished -- a bad state key "
            "stranded it in the ready handshake"
        )
        assert action.ready_ran, "on_ready never ran despite the finished flag"

        # Settings and event assignment writes must target the valid state.
        page.set_action_settings(action_object=action, settings={"probe": 1})
        assert page.get_action_settings(action_object=action) == {"probe": 1}, (
            "set_action_settings did not persist onto the real state's dict"
        )

        page.set_action_event_assigment(None, "Key Down", action_object=action)
        assert page.get_action_event_assignments(action_object=action) == {"Key Down": None}, (
            "set_action_event_assigment did not persist onto the real state's dict"
        )

        # The bad state's action dict must stay untouched by the writes.
        bad_actions = page.dict[ident.input_type][ident.json_identifier]["states"][BAD_STATE_KEY]["actions"]
        assert bad_actions == [{"id": ACTION_ID}], (
            f"the corrupt state was rewritten: {bad_actions!r}"
        )

        print("PASS: scenario_initialize_actions_bad_state")
    finally:
        fixtures.teardown(controller)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
