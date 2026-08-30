"""Verify state-scoped multi counts and input-identifier support filtering.
Two one-action states are single; one two-action state is multi; no entry is UNSUPPORTED.
"""
import sys

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PageManagement.Page import Page
from src.backend.PluginManager.ActionCore import ActionCore
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.ActionHolderGroup import ActionHolderGroup
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport

fixtures.start_watchdog(60, label="scenario_plugin_multiaction_support")

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


class FakePage:
    """Carries a real action registry and the real page accessor."""

    get_all_actions_for_input = Page.get_all_actions_for_input

    def __init__(self, action_objects: dict) -> None:
        self.action_objects = action_objects


class FakeAction:
    """Borrows the production get_is_multi_action; stubs its two guards."""

    get_is_multi_action = ActionCore.get_is_multi_action

    def __init__(self, page: FakePage, input_ident: object, state: int) -> None:
        self.page = page
        self.input_ident = input_ident
        self.state = state

    def raise_error_if_not_ready(self) -> None:
        pass

    def get_is_present(self) -> bool:
        return True


ident = Input.Key("0x0")


def multi_action(action_objects: dict, state: int) -> bool:
    page = FakePage(action_objects)
    action = FakeAction(page, ident, state)
    return action.get_is_multi_action()


print("(1) get_is_multi_action counts actions on the own state, not states")

two_states = {"keys": {"0x0": {0: {0: "obj_a"}, 1: {0: "obj_b"}}}}
check("two states, one action each reads single",
      multi_action(two_states, state=0) is False,
      str(multi_action(two_states, state=0)))

two_actions = {"keys": {"0x0": {0: {0: "obj_a", 1: "obj_b"}}}}
check("one state, two actions reads multi",
      multi_action(two_actions, state=0) is True,
      str(multi_action(two_actions, state=0)))

one_action = {"keys": {"0x0": {0: {0: "obj_a"}}}}
check("one state, one action reads single",
      multi_action(one_action, state=0) is False,
      str(multi_action(one_action, state=0)))

mixed = {"keys": {"0x0": {0: {0: "obj_a"}, 1: {0: "obj_b", 1: "obj_c"}}}}
check("multi is read against the action's own state (state 1)",
      multi_action(mixed, state=1) is True,
      str(multi_action(mixed, state=1)))
check("the other state on the same input still reads single (state 0)",
      multi_action(mixed, state=0) is False,
      str(multi_action(mixed, state=0)))


class FakeHolder:
    """Borrows the production get_input_compatibility; carries a support map."""

    get_input_compatibility = ActionHolder.get_input_compatibility

    def __init__(self, action_id: str, action_support: dict) -> None:
        self.action_id = action_id
        self.action_support = action_support


print("(2) get_action_holders_with_min_action_input_support weighs by input")

supported = FakeHolder("plug::supported", {Input.Key: ActionInputSupport.SUPPORTED})
untested = FakeHolder("plug::untested", {Input.Key: ActionInputSupport.UNTESTED})
unsupported = FakeHolder("plug::unsupported", {Input.Key: ActionInputSupport.UNSUPPORTED})

group = ActionHolderGroup("group", [supported, untested, unsupported])

at_least_supported = group.get_action_holders_with_min_action_input_support(
    ident, ActionInputSupport.SUPPORTED)
check("min SUPPORTED keeps only the supported holder",
      at_least_supported == {supported},
      str({h.action_id for h in at_least_supported}))

at_least_untested = group.get_action_holders_with_min_action_input_support(
    ident, ActionInputSupport.UNTESTED)
check("min UNTESTED keeps supported and untested holders",
      at_least_untested == {supported, untested},
      str({h.action_id for h in at_least_untested}))

at_least_unsupported = group.get_action_holders_with_min_action_input_support(
    ident, ActionInputSupport.UNSUPPORTED)
check("min UNSUPPORTED keeps every holder",
      at_least_unsupported == {supported, untested, unsupported},
      str({h.action_id for h in at_least_unsupported}))

# A holder with no entry for this input type defaults to UNSUPPORTED, so it is
# dropped at the SUPPORTED floor and kept only at the UNSUPPORTED floor.
no_entry = FakeHolder("plug::no_entry", {})
group_no_entry = ActionHolderGroup("group2", [supported, no_entry])
check("a holder with no support entry drops at the SUPPORTED floor",
      group_no_entry.get_action_holders_with_min_action_input_support(
          ident, ActionInputSupport.SUPPORTED) == {supported},
      "no_entry leaked into the supported set")


print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
    sys.exit(1)
print("all checks passed")
sys.exit(0)
