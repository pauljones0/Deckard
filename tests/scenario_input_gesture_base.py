"""One gesture body serves every input type that dispatches a gesture.

cancel_gesture and on_hold_timer_end live on ControllerInput. Those two methods
are byte-identical between a key and a dial apart from the hold-start event each
sends, which each names through HOLD_START_EVENT. The rest of the two gesture
paths does differ, for reasons that are not this dedup's: a dial stops its hold
timer before it dispatches the release, and a key after. So these legs pin the
two shared methods only. The shared body must serve a key and a dial alike, and
each input type must still dispatch its own event class. The touchscreen
dispatches no gesture and inherits a body that clears state which is already
clear.
"""
import fixtures

from StreamDeck.Devices.StreamDeck import DialEventType

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.deck_controller.inputs import (
    ControllerDial,
    ControllerInput,
    ControllerKey,
    ControllerTouchScreen,
)
from src.backend.PluginManager.ActionCore import ActionCore

KEY_DOWN = Input.Key.Events.DOWN
KEY_HOLD_START = Input.Key.Events.HOLD_START
DIAL_DOWN = Input.Dial.Events.DOWN
DIAL_HOLD_START = Input.Dial.Events.HOLD_START


class RecordingAction(ActionCore):
    """Minimal ActionCore that records every raw event it is dispatched."""

    def __init__(self, tag: str, deck_controller, page, input_ident):
        super().__init__(
            action_id=f"test::{tag}", action_name=tag,
            deck_controller=deck_controller, page=page, plugin_base=None,
            state=0, input_ident=input_ident,
        )
        self.tag = tag
        self.received: list = []

    def _raw_event_callback(self, event, data=None):
        self.received.append(event)


def inject(page, ident, actions: list) -> None:
    """Place stub action objects where get_all_actions_for_input reads them."""
    per_state = page.action_objects.setdefault(ident.input_type, {}).setdefault(ident.json_identifier, {})
    per_state[0] = {i: a for i, a in enumerate(actions)}


def leg_one_body_serves_every_input() -> None:
    """The two methods resolve to the base function for every input type.

    A subclass that re-forks either body reintroduces the drift this pins
    against: two copies that differ only in an event name, which then diverge
    one fix at a time.
    """
    for cls in (ControllerKey, ControllerDial, ControllerTouchScreen):
        assert cls.cancel_gesture is ControllerInput.cancel_gesture, (
            f"{cls.__name__} carries its own cancel_gesture again")
        assert cls.on_hold_timer_end is ControllerInput.on_hold_timer_end, (
            f"{cls.__name__} carries its own on_hold_timer_end again")

    # The event name is the whole of the per-type difference, and only the two
    # input types that arm a hold timer name one.
    assert ControllerKey.HOLD_START_EVENT is Input.Key.Events.HOLD_START
    assert ControllerDial.HOLD_START_EVENT is Input.Dial.Events.HOLD_START
    assert "HOLD_START_EVENT" not in vars(ControllerTouchScreen), (
        "the touchscreen arms no hold timer and must name no hold-start event")

    print("PASS: one cancel_gesture and one on_hold_timer_end serve every input type")


def leg_hold_start_keeps_its_own_event_class(controller, deck, page) -> None:
    """A held key dispatches the key event and a held dial the dial event.

    The shared body reads HOLD_START_EVENT off the input. A key that names the
    dial's event, or the reverse, sends an event no action on that input type
    subscribes to, and every hold binding on the deck goes silent.
    """
    controller.hold_time = 0.3

    key_ident = Input.Key("0x0")
    dial_ident = Input.Dial("0")
    key_recorder = RecordingAction(
        tag="key_hold", deck_controller=controller, page=page, input_ident=key_ident)
    dial_recorder = RecordingAction(
        tag="dial_hold", deck_controller=controller, page=page, input_ident=dial_ident)
    inject(page, key_ident, [key_recorder])
    inject(page, dial_ident, [dial_recorder])

    deck.fire_key_event(0, True)
    assert fixtures.wait_until(lambda: KEY_DOWN in key_recorder.received), \
        f"the key press never reached its action: {key_recorder.received}"
    assert fixtures.wait_until(
        lambda: KEY_HOLD_START in key_recorder.received,
        timeout=controller.hold_time + 2.0), (
        "a held key dispatched no key hold-start event: "
        f"{key_recorder.received}")
    assert DIAL_HOLD_START not in key_recorder.received, (
        "a held key dispatched the dial's hold-start event: "
        f"{key_recorder.received}")
    deck.fire_key_event(0, False)

    deck.fire_dial_event(0, DialEventType.PUSH, True)
    assert fixtures.wait_until(lambda: DIAL_DOWN in dial_recorder.received), \
        f"the dial press never reached its action: {dial_recorder.received}"
    assert fixtures.wait_until(
        lambda: DIAL_HOLD_START in dial_recorder.received,
        timeout=controller.hold_time + 2.0), (
        "a held dial dispatched no dial hold-start event: "
        f"{dial_recorder.received}")
    assert KEY_HOLD_START not in dial_recorder.received, (
        "a held dial dispatched the key's hold-start event: "
        f"{dial_recorder.received}")
    deck.fire_dial_event(0, DialEventType.PUSH, False)

    print("PASS: each input type dispatches its own hold-start event class")


def leg_cancel_parity(controller, deck, page) -> None:
    """The base cancel drops the whole gesture on a key and on a dial alike.

    Each input is driven through ControllerInput.cancel_gesture directly, so
    the leg fails if either subclass stops being served by that body. A hold
    threshold well past the wait keeps the timer armed until the cancel.
    """
    controller.hold_time = 10.0

    key_ident = Input.Key("0x1")
    dial_ident = Input.Dial("1")
    key_recorder = RecordingAction(
        tag="key_cancel", deck_controller=controller, page=page, input_ident=key_ident)
    dial_recorder = RecordingAction(
        tag="dial_cancel", deck_controller=controller, page=page, input_ident=dial_ident)
    inject(page, key_ident, [key_recorder])
    inject(page, dial_ident, [dial_recorder])

    key_input = controller.get_input(key_ident)
    dial_input = controller.get_input(dial_ident)
    key_index = key_ident.get_index(controller)

    deck.fire_key_event(key_index, True)
    assert fixtures.wait_until(lambda: KEY_DOWN in key_recorder.received), \
        f"the key press never reached its action: {key_recorder.received}"
    deck.fire_dial_event(1, DialEventType.PUSH, True)
    assert fixtures.wait_until(lambda: DIAL_DOWN in dial_recorder.received), \
        f"the dial press never reached its action: {dial_recorder.received}"

    for tag, controller_input in (("key", key_input), ("dial", dial_input)):
        assert controller_input.down_start_time is not None, \
            f"the {tag} took no gesture clock on the way down"
        assert controller_input._gesture is not None, \
            f"the {tag} pinned no down-time snapshot"
        assert controller_input.hold_start_timer is not None, \
            f"the {tag} armed no hold timer"

        # Call through the base explicitly. A subclass that re-forks the body
        # is then no longer what runs here.
        ControllerInput.cancel_gesture(controller_input)

        assert controller_input.down_start_time is None, \
            f"the {tag} kept its gesture clock past the cancel"
        assert controller_input._gesture is None, \
            f"the {tag} kept its down-time snapshot past the cancel"
        assert controller_input.hold_start_timer is None, \
            f"the {tag} kept its hold timer armed past the cancel"

    # The cancelled gestures must stay silent for longer than a hold would
    # have taken had the timer survived.
    controller.hold_time = 0.3
    assert not fixtures.wait_until(
        lambda: KEY_HOLD_START in key_recorder.received, timeout=1.0), (
        "a cancelled key gesture still fired its hold-start event: "
        f"{key_recorder.received}")
    assert DIAL_HOLD_START not in dial_recorder.received, (
        "a cancelled dial gesture still fired its hold-start event: "
        f"{dial_recorder.received}")

    # Release both. Each takes the no-gesture-clock branch, which cancels
    # again and must stay a no-op.
    deck.fire_key_event(key_index, False)
    deck.fire_dial_event(1, DialEventType.PUSH, False)

    print("PASS: the base cancel drops the whole gesture on a key and on a dial")


def leg_touchscreen_inherits_a_quiet_body(controller) -> None:
    """The touchscreen inherits both bodies and dispatches nothing.

    It arms no hold timer, so its snapshot is always None and the hold body
    returns before it reads an event name it does not have. Neither call may
    raise.
    """
    touchscreen = controller.get_input(Input.Touchscreen("sd-plus"))
    assert touchscreen is not None, "the fake deck has no touchscreen"

    assert touchscreen._gesture is None
    assert touchscreen.down_start_time is None

    touchscreen.cancel_gesture()
    touchscreen.on_hold_timer_end()

    assert touchscreen._gesture is None
    assert touchscreen.down_start_time is None
    assert touchscreen.hold_start_timer is None

    print("PASS: the touchscreen inherits a gesture body that stays quiet")


def main() -> None:
    fixtures.start_watchdog(90, label="scenario_input_gesture_base")
    leg_one_body_serves_every_input()

    controller = fixtures.make_headless_controller(serial="gesture-base-1")
    try:
        deck = fixtures.raw_deck(controller)
        page = controller.active_page
        assert page is not None

        leg_hold_start_keeps_its_own_event_class(controller, deck, page)
        leg_cancel_parity(controller, deck, page)
        leg_touchscreen_inherits_a_quiet_body(controller)
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_input_gesture_base")


if __name__ == "__main__":
    main()
