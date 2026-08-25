"""A turned deck dispatches every input to the position the user sees.

The deck wrapper maps a physical event onto the logical layout, and the
controller resolves that logical value against the same layout the input
registry was built from. This drives a real DeckController over a fake deck
and checks the whole chain at all four rotations:

  (a) every key of the grid reaches its own registered key, never another
      one, and the eight keys together cover the registry exactly once;
  (b) turning the deck rebuilds the input set for the new layout and leaves
      no present hash behind, so the corrective repaint is not hash-skipped;
  (c) a dial event reaches the dial that sits under the slot the composite
      drew, which runs the other way at 180;
  (d) a touch reaches the slot the user touched, and a drag keeps its
      direction under the user's hand.

Deck shape, stated once so a configurable fake deck can adopt it later: a 2
by 4 key grid, four dials and an 800 by 100 strip, which is the Stream Deck +
shape the fake deck models. The scenario asserts the shape before it starts,
so a changed fake fails here and does not quietly weaken the checks.
"""
import fixtures  # must be first; isolates DATA_PATH before import globals

from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType

from src.backend.DeckManagement.InputIdentifier import Input

ROTATIONS = (0, 90, 180, 270)
KEY_ROWS, KEY_COLS = 2, 4
KEY_TOTAL = KEY_ROWS * KEY_COLS
N_DIALS = 4
STRIP_SIZE = (800, 100)


def check_deck_shape(controller) -> int:
    deck = controller.deck
    deck.set_rotation(0)
    if tuple(deck.key_layout()) != (KEY_ROWS, KEY_COLS):
        print(f"FAIL(shape): key grid is {tuple(deck.key_layout())}, this "
              f"scenario reasons about {(KEY_ROWS, KEY_COLS)}")
        return 1
    if deck.dial_count() != N_DIALS:
        print(f"FAIL(shape): {deck.dial_count()} dials, expected {N_DIALS}")
        return 1
    if tuple(controller.get_touchscreen_image_size()) != STRIP_SIZE:
        print(f"FAIL(shape): strip is {controller.get_touchscreen_image_size()}, "
              f"expected {STRIP_SIZE}")
        return 1
    return 0


def registered_key_identifiers(controller) -> "set[str]":
    return {key.identifier.json_identifier
            for key in controller.inputs[Input.Key]}


def check_key_dispatch(controller) -> int:
    """(a) Each physical key reaches its own registered key at every
    rotation. The logical index the wrapper produces is decoded against the
    wrapper's own layout, which is what named the registry. Decoding it
    against the raw handle's unrotated layout instead misnames six of these
    eight keys at 90 and at 270."""
    deck = controller.deck
    raw = fixtures.raw_deck(controller)
    seen: "list[str]" = []
    controller.event_callback = lambda ident, *a, **k: seen.append(
        ident.json_identifier)
    try:
        for rotation in ROTATIONS:
            controller.set_rotation(rotation)
            registry = registered_key_identifiers(controller)
            logical_rows, logical_cols = deck.key_layout()
            if logical_rows * logical_cols != KEY_TOTAL:
                print(f"FAIL(a): rotation {rotation} layout "
                      f"{(logical_rows, logical_cols)} holds "
                      f"{logical_rows * logical_cols} keys, expected "
                      f"{KEY_TOTAL}")
                return 1

            for physical in range(KEY_TOTAL):
                seen.clear()
                raw.fire_key_event(physical, True)
                logical = deck.get_logical_index(physical)
                expected = f"{logical % logical_cols}x{logical // logical_cols}"
                if seen != [expected]:
                    print(f"FAIL(a): rotation {rotation}: physical key "
                          f"{physical} (logical {logical}) reached {seen}, "
                          f"expected [{expected}]")
                    return 1
                if expected not in registry:
                    print(f"FAIL(a): rotation {rotation}: physical key "
                          f"{physical} reached {expected}, which is not a "
                          f"registered key: {sorted(registry)}")
                    return 1

            seen.clear()
            for physical in range(KEY_TOTAL):
                raw.fire_key_event(physical, True)
            if sorted(seen) != sorted(registry):
                print(f"FAIL(a): rotation {rotation}: the grid reached "
                      f"{sorted(seen)}, which does not cover the registry "
                      f"{sorted(registry)} exactly once")
                return 1
    finally:
        del controller.event_callback

    print("PASS: every key reaches its own registered position at 0/90/180/270")
    return 0


def check_rotation_rebuild(controller) -> int:
    """(b) Turning the deck rebuilds the input set and carries no present
    hash over, so the repaint that follows is written and not hash-skipped.

    The published set is read inside the transition and not after it. The
    page load the turn ends in paints, which stamps fresh hashes on its own
    schedule, and reading the state after that races the media thread.
    """
    controller.set_rotation(0)
    before_ids = {id(key) for key in controller.inputs[Input.Key]}
    before_identifiers = registered_key_identifiers(controller)

    # Stand in for a deck that has been painted: every slot believes it shows
    # this image, and believes the same image is on its way to it.
    stale_hash = 4242
    for key in controller.inputs[Input.Key]:
        key.present_state.last_presented_hash = stale_hash
        key.present_state.last_enqueued_hash = stale_hash
    for touchscreen in controller.inputs[Input.Touchscreen]:
        touchscreen.present_state.last_presented_hash = stale_hash
        touchscreen.present_state.last_enqueued_hash = stale_hash

    published: "dict[str, object]" = {}
    real_init_inputs = controller.init_inputs

    def spy_init_inputs() -> None:
        real_init_inputs()
        keys = controller.inputs[Input.Key]
        published["identifiers"] = registered_key_identifiers(controller)
        published["ids"] = {id(key) for key in keys}
        published["hashes"] = [
            (published_input.identifier.json_identifier,
             published_input.present_state.last_presented_hash,
             published_input.present_state.last_enqueued_hash)
            for published_input in keys + controller.inputs[Input.Touchscreen]
        ]
        # The behaviour those hashes decide: the picture the deck already
        # showed must still be written, because it belongs on another key now.
        published["offered"] = keys[0].present_state.offer(
            controller.media_player, page=controller.active_page,
            config_gen=keys[0].config_gen, img_hash=stale_hash,
            encode=lambda: b"native")

    controller.init_inputs = spy_init_inputs
    try:
        controller.set_rotation(90)
    finally:
        del controller.init_inputs

    if not published:
        print("FAIL(b): turning the deck published no new input set; the old "
              f"one still names {sorted(before_identifiers)}")
        return 1

    expected = {f"{i % KEY_ROWS}x{i // KEY_ROWS}" for i in range(KEY_TOTAL)}
    if published["identifiers"] != expected:
        print(f"FAIL(b): after turning to 90 the registry is "
              f"{sorted(published['identifiers'])}, expected "
              f"{sorted(expected)} for the transposed grid (it was "
              f"{sorted(before_identifiers)})")
        return 1
    if published["ids"] & before_ids:
        print("FAIL(b): the input set was not rebuilt; inputs from the old "
              "layout survived the turn")
        return 1

    for identifier, presented, enqueued in published["hashes"]:
        if presented is not None or enqueued is not None:
            print(f"FAIL(b): {identifier} kept a present hash across the turn "
                  f"({presented}, {enqueued}); its repaint is hash-skipped")
            return 1
    if not published["offered"]:
        print("FAIL(b): the first key hash-skipped the repaint of the image "
              "it showed before the turn")
        return 1

    if registered_key_identifiers(controller) != expected:
        print(f"FAIL(b): the published set was replaced again before the turn "
              f"finished: {sorted(registered_key_identifiers(controller))}")
        return 1

    print("PASS: turning the deck rebuilds the inputs and repaints them")
    return 0


def check_dial_dispatch(controller) -> int:
    """(c) A dial event reaches the dial whose slot sits on that knob."""
    raw = fixtures.raw_deck(controller)
    seen: "list[str]" = []
    controller.event_callback = lambda ident, *a, **k: seen.append(
        ident.json_identifier)
    try:
        for rotation in ROTATIONS:
            controller.set_rotation(rotation)
            for physical in range(N_DIALS):
                seen.clear()
                raw.fire_dial_event(physical, DialEventType.TURN, 1)
                expected = str(controller.deck.get_logical_dial_index(physical))
                if seen != [expected]:
                    print(f"FAIL(c): rotation {rotation}: physical dial "
                          f"{physical} reached dial {seen}, expected "
                          f"[{expected}]")
                    return 1
            reached = []
            for physical in range(N_DIALS):
                seen.clear()
                raw.fire_dial_event(physical, DialEventType.TURN, 1)
                reached.extend(seen)
            if sorted(reached) != sorted(str(i) for i in range(N_DIALS)):
                print(f"FAIL(c): rotation {rotation}: the dials reached "
                      f"{reached}, which is not each dial once")
                return 1
    finally:
        del controller.event_callback

    print("PASS: every dial reaches its own slot at 0/90/180/270")
    return 0


def patch_dial_recorders(controller, events: "list[tuple[str, str]]") -> None:
    """Record which dial's actions each touch event is dispatched to."""
    for dial in controller.inputs[Input.Dial]:
        state = dial.get_active_state()
        index = dial.identifier.json_identifier

        def record(event, *a, _index=index, **k):
            events.append((_index, str(event)))

        state.own_actions_event_callback_threaded = record


def check_touch_dispatch(controller) -> int:
    """(d) A touch reaches the slot under the finger, and a drag keeps the
    direction the user drew it in."""
    raw = fixtures.raw_deck(controller)
    width, _height = STRIP_SIZE
    # A touch in the middle of the first slot of the device's own strip.
    slot_width = width // N_DIALS
    device_x = slot_width // 2

    for rotation in ROTATIONS:
        controller.set_rotation(rotation)
        # The turn ends in a page load, which rebuilds every input's states on
        # the media thread. Wait for it, or the recorders below are installed
        # on state objects the load then replaces.
        if not fixtures.wait_until(controller._input_load_done.is_set, timeout=10.0):
            print(f"FAIL(d): rotation {rotation}: the input load did not "
                  f"finish")
            return 1
        touchscreen = controller.get_input(Input.Touchscreen("sd-plus"))
        if touchscreen is None:
            print("FAIL(d): the deck reports no touchscreen input")
            return 1

        events: "list[tuple[str, str]]" = []
        patch_dial_recorders(controller, events)

        raw.fire_touchscreen_event(TouchscreenEventType.SHORT,
                                   {"x": device_x, "y": 50})
        expected_dial = str(controller.deck.get_logical_dial_index(0))
        if len(events) != 1 or events[0][0] != expected_dial:
            print(f"FAIL(d): rotation {rotation}: a touch on the first slot "
                  f"of the device's strip reached {events}, expected dial "
                  f"{expected_dial}")
            return 1

        # A drag drawn from the far end towards the near end of the device's
        # strip. At 180 the user drew it the other way round.
        drag_events: "list[str]" = []
        state = touchscreen.get_active_state()
        state.own_actions_event_callback_threaded = (
            lambda event, *a, **k: drag_events.append(str(event)))
        raw.fire_touchscreen_event(
            TouchscreenEventType.DRAG,
            {"x": width - 1 - device_x, "y": 50, "x_out": device_x, "y_out": 50})
        expected_drag = (str(Input.Touchscreen.Events.DRAG_RIGHT) if rotation == 180
                         else str(Input.Touchscreen.Events.DRAG_LEFT))
        if drag_events != [expected_drag]:
            print(f"FAIL(d): rotation {rotation}: a drag towards the start of "
                  f"the device's strip dispatched {drag_events}, expected "
                  f"[{expected_drag}]")
            return 1

    print("PASS: touches and drags reach the position the user touched")
    return 0


def main() -> int:
    fixtures.start_watchdog(80, label="deck_rotation_dispatch")
    controller = fixtures.make_headless_controller(
        "rot-dispatch", key_layout=[KEY_ROWS, KEY_COLS])
    try:
        rc = check_deck_shape(controller)
        if rc:
            return rc
        rc |= check_key_dispatch(controller)
        rc |= check_rotation_rebuild(controller)
        rc |= check_dial_dispatch(controller)
        rc |= check_touch_dispatch(controller)
    finally:
        fixtures.teardown(controller)
    if rc == 0:
        print("PASS: scenario_deck_rotation_dispatch")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
