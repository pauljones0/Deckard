"""
A non-integer state key in a page json must not strand an action.

initialize_actions runs load_event_overrides on the caller thread before it
schedules the ready callback. That path reaches Page.get_action_dict, which
scans every state key and coerces it with int(). A corrupt page json can hold a
state key that is not an integer; a bare int() then raises, the raise unwinds
through the @log.catch on initialize_actions, and the action is left with
on_ready_called True and on_ready_finished False for the life of the page --
its tick and on_update redraw gates never open. The scan must skip the bad key
and reach the real state instead.

The write path scans the same keys: set_action_dict backs set_action_settings
and set_action_event_assigment, so a bare int() there breaks every plugin that
persists settings or an event assignment on a corrupt page.
"""

# fixtures must import first: it points argv at an isolated data dir.
import fixtures

from fixtures import make_headless_controller, start_watchdog, wait_until

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore

BAD_STATE_KEY = "garbage"
ACTION_ID = "dev_test::BadStateStrand"


class StrandProbeAction(ActionCore):
    """Uses the real load_event_overrides, the strand path under test. on_ready
    records that it ran, so the ready handshake is observable."""

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

        # A non-integer state key, ordered before the real "0" so the scan hits
        # it first. Both states carry a non-empty actions list, or the inner
        # loop never reaches the int() coercion.
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

        # Mutation witness: the bad key is genuinely int-hostile, so a bare
        # int() over it raises. This is what strands the action pre-fix.
        raised = False
        try:
            int(BAD_STATE_KEY)
        except ValueError:
            raised = True
        assert raised, "the bad state key parses as an int -- this test proves nothing"

        # The scan must skip the bad key and return the real state's dict rather
        # than raise. Pre-fix this call raises ValueError.
        found = page.get_action_dict(action_object=action)
        assert found == {"id": ACTION_ID}, (
            f"get_action_dict returned {found!r}, not the state-0 action dict -- "
            "the bad-key skip must still resolve the real state"
        )

        # The strand entry point: load_event_overrides walks the same path.
        # Pre-fix it raises out of the ready handshake.
        assert page.get_action_event_assignments(action_object=action) == {}, (
            "get_action_event_assignments must resolve past the bad key"
        )

        # End to end: initialize_actions must carry the action to a finished
        # ready. Pre-fix the int() raise unwinds through @log.catch and leaves
        # on_ready_finished False forever.
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

        # The write path scans the same state keys. A plugin persisting its
        # settings or an event assignment must survive the bad key too, and the
        # value must land on the real state's action dict.
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
