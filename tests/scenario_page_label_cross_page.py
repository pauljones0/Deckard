"""
A Page label setter reaches only decks that currently show this page.

get_controller_inputs iterates every controller. Without a page filter a setter
called on page A writes its label into a deck that shows page B (cross-page
bleed), because that deck owns an input with the same identifier and state. The
filter keeps the setter on the active-page scope update_input already uses:
same-json_path decks are reached, a deck on another page is not.
"""

# fixtures must import first: it points argv at an isolated data dir before
# globals resolves DATA_PATH.
import fixtures

import globals as gl

from src.backend.DeckManagement.InputIdentifier import Input

LABEL_POSITION = "center"
SENTINEL = "cross-page-sentinel"


def _label(controller, identifier, state):
    c_input = controller.get_input(identifier)
    assert c_input is not None, f"{controller.serial_number()} has no {identifier}"
    input_state = c_input.states.get(state)
    assert input_state is not None, (
        f"{controller.serial_number()} has no state {state} for {identifier}"
    )
    return input_state.label_manager.page_labels[LABEL_POSITION]


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_page_label_cross_page")

    # deck_a and deck_c load the default "Main" page; deck_b is switched to a
    # second page. All three are Fake Decks, so all three own Key 0x0 state 0.
    deck_a = fixtures.make_headless_controller(serial="cross-page-a", page_name="Main")
    deck_c = fixtures.make_headless_controller(serial="cross-page-c", page_name="Main")
    deck_b = fixtures.make_headless_controller(serial="cross-page-b", page_name="Main")
    try:
        seed_other = fixtures.seed_page("OtherPage")
        other = gl.page_manager.get_page(seed_other, deck_b)
        deck_b.load_page(other)

        page_a = deck_a.active_page
        assert page_a is not None, "deck_a loaded no page"
        identifier = Input.Key("0x0")
        state = 0

        # The scopes must actually differ, or the filter is untestable here.
        assert deck_b.active_page is not None, "deck_b loaded no page"
        assert deck_a.active_page.json_path == deck_c.active_page.json_path, (
            "deck_a and deck_c must share a page json_path for the same-page leg"
        )
        assert deck_b.active_page.json_path != page_a.json_path, (
            "deck_b must show a different page for the cross-page leg"
        )

        # A LabelManager must exist on every deck, or the setter skips its
        # guarded block and the assertions below prove nothing.
        label_a = _label(deck_a, identifier, state)
        label_b = _label(deck_b, identifier, state)
        label_c = _label(deck_c, identifier, state)

        # Mutation witness: the pre-fix, unfiltered walk (iterate every
        # controller, no page filter) reaches deck_b. If it did not, deck_b
        # could never bleed and the no-bleed assertion would be vacuous.
        unfiltered = []
        for controller in gl.deck_manager.deck_controller:
            for c_input in controller.get_inputs(identifier):
                if c_input.identifier == identifier:
                    unfiltered.append(controller.serial_number())
        assert "cross-page-b" in unfiltered, (
            "the unfiltered walk did not reach deck_b -- this scenario could "
            f"not observe a bleed (reached: {unfiltered})"
        )

        # The filtered accessor the setters use must exclude deck_b and include
        # both same-page decks.
        covered = page_a.get_controller_input_states(identifier, state)
        serials = {s.controller_input.deck_controller.serial_number() for s in covered}
        assert "cross-page-a" in serials, f"deck_a not covered: {serials}"
        assert "cross-page-c" in serials, (
            f"a same-page deck was excluded by the filter: {serials}"
        )
        assert "cross-page-b" not in serials, (
            f"deck_b (on another page) is still in the setter's scope: {serials}"
        )

        before_b = label_b.text

        page_a.set_label_text(identifier, state, LABEL_POSITION, SENTINEL)

        assert label_a.text == SENTINEL, (
            f"the setter did not reach its own deck (deck_a text {label_a.text!r})"
        )
        assert label_c.text == SENTINEL, (
            f"the setter did not reach a same-page deck (deck_c text {label_c.text!r})"
        )
        assert label_b.text != SENTINEL, (
            "cross-page bleed: a setter on page A overwrote the label of a deck "
            f"showing page B (deck_b text {label_b.text!r})"
        )
        assert label_b.text == before_b, (
            f"deck_b's label changed from {before_b!r} to {label_b.text!r} -- "
            "the setter must leave a deck on another page untouched"
        )

        print("PASS: scenario_page_label_cross_page")
    finally:
        fixtures.teardown(deck_a)
        fixtures.teardown(deck_b)
        fixtures.teardown(deck_c)


if __name__ == "__main__":
    main()
