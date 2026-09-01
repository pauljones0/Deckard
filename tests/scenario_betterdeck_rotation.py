"""Verify key, strip, touch, dial, and callback maps; all three async setters delegate to its deck.
Use Plus strip/dials and Original 3x5 keys; reorder uses out[logical(p)] = orig[p]."""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)


from fixtures import FaultyFakeDeck, start_watchdog

from src.backend.DeckManagement.BetterDeck import BetterDeck
from src.backend.DeckManagement.Subclasses.FakeDeck import FAKE_DECK_MODELS

ROTATIONS = (0, 90, 180, 270)
PLUS = FAKE_DECK_MODELS["plus"]
ORIGINAL = FAKE_DECK_MODELS["original"]
N_DIALS = PLUS.dial_count
STRIP_SIZE = PLUS.touchscreen_format.size

# Device oracle, row-major 2x4: 90 maps (r,c)->(c,rows-1-r); p0->l1 and p4->l0.
# 270 turns opposite; 180 reverses, catching formulas that agree in the wrong direction.
DIRECTION_ROWS, DIRECTION_COLS = 2, 4
DIRECTION_TABLE = {
    0: [0, 1, 2, 3, 4, 5, 6, 7],
    90: [1, 3, 5, 7, 0, 2, 4, 6],
    180: [7, 6, 5, 4, 3, 2, 1, 0],
    270: [6, 4, 2, 0, 7, 5, 3, 1],
}


def check_rotation() -> int:
    # The Original's 3 by 5 grid, so the literals below hold and a
    # non-square grid is what the permutation is checked over.
    deck = FaultyFakeDeck(serial_number="rot-1", model="original")
    wrapped_deck = BetterDeck(deck)
    rows, cols = ORIGINAL.key_layout
    if (rows, cols) != (3, 5):
        print(f"FAIL(a): the Original preset is now {(rows, cols)}; the "
              f"literals in this check assume 3 by 5")
        return 1

    total = rows * cols
    physical = list(range(total))  # value == its physical index

    for rotation in (0, 90, 180, 270):
        wrapped_deck.set_rotation(rotation)
        out = wrapped_deck.reorder_physical_for_rotation(physical)

        # The result must stay a permutation, with nothing lost or duplicated.
        if sorted(out) != physical:
            print(f"FAIL(a): rotation {rotation} output is not a "
                  f"permutation: {out}")
            return 1

        # Physical value p must occupy logical slot l where get_physical_index(l) == p.
        for logical in range(total):
            p = wrapped_deck.get_physical_index(logical)
            if out[logical] != physical[p]:
                print(f"FAIL(a): rotation {rotation}: out[{logical}] = "
                      f"{out[logical]}, expected value from physical slot "
                      f"{p} -- the map is applied in the wrong direction")
                return 1

    # For a 3-by-5 grid at 90 degrees, get_logical_index(0) = 2, so orig[0] must land at out[2].
    # The literal checks placement; check_rotation_direction uses the device table for direction.
    wrapped_deck.set_rotation(90)
    out = wrapped_deck.reorder_physical_for_rotation(physical)
    if out[2] != 0:
        print(f"FAIL(a): literal check: out[2] = {out[2]}, expected 0")
        return 1

    print("PASS: rotation map applied in the correct direction for 0/90/180/270")
    return 0


def check_async_setters() -> int:
    deck = FaultyFakeDeck(serial_number="rot-2")
    received = {}
    deck.set_key_callback_async = lambda cb, loop=None: received.setdefault("key", (cb, loop))
    deck.set_dial_callback_async = lambda cb, loop=None: received.setdefault("dial", (cb, loop))
    deck.set_touchscreen_callback_async = lambda cb, loop=None: received.setdefault("touch", (cb, loop))
    wrapped_deck = BetterDeck(deck)

    async def cb(*a):
        pass

    try:
        wrapped_deck.set_key_callback_async(cb)
        wrapped_deck.set_dial_callback_async(cb)
        wrapped_deck.set_touchscreen_callback_async(cb)
    except RecursionError:
        print("FAIL(b): async callback setter recursed into itself")
        return 1

    missing = {"key", "dial", "touch"} - set(received)
    if missing:
        print(f"FAIL(b): setters never reached the wrapped deck: {missing}")
        return 1
    print("PASS: async callback setters delegate to the wrapped deck")
    return 0


def check_rotation_direction() -> int:
    """Compare key rotation with a table read from the turned deck.
    Inverse permutation checks alone can pass when both maps use the same wrong direction."""
    deck = FaultyFakeDeck(serial_number="rot-direction", model="plus")
    wrapped_deck = BetterDeck(deck)
    if tuple(PLUS.key_layout) != (DIRECTION_ROWS, DIRECTION_COLS):
        print(f"FAIL(g): the Stream Deck + preset is now {PLUS.key_layout}; "
              f"the table below was read off a "
              f"{DIRECTION_ROWS} by {DIRECTION_COLS} deck")
        return 1
    total = DIRECTION_ROWS * DIRECTION_COLS

    for rotation, table in DIRECTION_TABLE.items():
        wrapped_deck.set_rotation(rotation)
        logical = [wrapped_deck.get_logical_index(p) for p in range(total)]
        if logical != table:
            print(f"FAIL(g): rotation {rotation} maps the physical keys to "
                  f"{logical}; a deck turned that way puts them at {table}")
            return 1
        physical = [wrapped_deck.get_physical_index(l) for l in table]
        if physical != list(range(total)):
            print(f"FAIL(g): rotation {rotation}: the physical map disagrees "
                  f"with the turned deck; it sends {table} back to "
                  f"{physical}, expected {list(range(total))}")
            return 1

    print("PASS: the key map turns the grid in the direction the deck was "
          "turned")
    return 0


def check_strip_turn() -> int:
    """The strip turns counter-clockwise by the rotation, PIL's direction, to cancel the physical
    turn, at the logical size: the device buffer's transpose at the quarter turns."""
    deck = FaultyFakeDeck(serial_number="rot-strip", model="plus")
    wrapped_deck = BetterDeck(deck)
    width, height = STRIP_SIZE

    expected = {0: 0, 90: 90, 180: 180, 270: 270}
    expected_size = {0: (width, height), 90: (height, width),
                     180: (width, height), 270: (height, width)}
    for rotation in ROTATIONS:
        wrapped_deck.set_rotation(rotation)
        turn = wrapped_deck.touchscreen_image_rotation()
        if turn != expected[rotation]:
            print(f"FAIL(c): rotation {rotation}: strip turn {turn}, "
                  f"expected {expected[rotation]}")
            return 1
        size = wrapped_deck.logical_touchscreen_size()
        if size != expected_size[rotation]:
            print(f"FAIL(c): rotation {rotation}: logical strip size {size}, "
                  f"expected {expected_size[rotation]}")
            return 1
        if wrapped_deck.strip_is_transposed() != (rotation in (90, 270)):
            print(f"FAIL(c): rotation {rotation}: strip_is_transposed() is "
                  f"{wrapped_deck.strip_is_transposed()}")
            return 1

    # A deck with no strip has nothing to turn and no logical size.
    plain = BetterDeck(FaultyFakeDeck(serial_number="rot-nostrip",
                                      model="original"))
    plain.set_rotation(90)
    if plain.touchscreen_image_rotation() != 0 or plain.logical_touchscreen_size() is not None:
        print(f"FAIL(c): a deck with no strip answered turn "
              f"{plain.touchscreen_image_rotation()} and size "
              f"{plain.logical_touchscreen_size()}")
        return 1

    print("PASS: the strip composite turns to reach the device upright, at "
          "the logical size")
    return 0


def check_slot_order() -> int:
    """The dial slots run across an upright strip and stack down a turned one.
    Dial 0 keeps the knob at the end the strip starts at: the top at 90, the bottom at 270."""
    deck = FaultyFakeDeck(serial_number="rot-slot", model="plus")
    wrapped_deck = BetterDeck(deck)

    expected = {0: "x", 90: "y-down", 180: "x", 270: "y-up"}
    for rotation in ROTATIONS:
        wrapped_deck.set_rotation(rotation)
        order = wrapped_deck.dial_slot_order()
        if order != expected[rotation]:
            print(f"FAIL(h): rotation {rotation}: slot order {order!r}, "
                  f"expected {expected[rotation]!r}")
            return 1

    print("PASS: the dial slots divide the axis the user sees")
    return 0


def check_touch_value() -> int:
    """A touch position is mapped to where the strip was composed."""
    deck = FaultyFakeDeck(serial_number="rot-touch", model="plus")
    wrapped_deck = BetterDeck(deck)
    width, height = STRIP_SIZE
    if tuple(deck.touchscreen_image_format()["size"]) != STRIP_SIZE:
        print(f"FAIL(d): the fake deck's strip is not {STRIP_SIZE}; the "
              f"literals below assume it")
        return 1

    original = {"x": 100, "y": 20, "x_out": 700, "y_out": 80}

    # A reported position turns back clockwise by the rotation, both axes and both drag ends.
    # 180 mirrors both axes; 90 mirrors the device y into x, 270 mirrors the device x into y.
    expected_by_rotation = {
        0: dict(original),
        90: {"x": height - 1 - original["y"], "y": original["x"],
             "x_out": height - 1 - original["y_out"], "y_out": original["x_out"]},
        180: {"x": width - 1 - original["x"], "y": height - 1 - original["y"],
              "x_out": width - 1 - original["x_out"],
              "y_out": height - 1 - original["y_out"]},
        270: {"x": original["y"], "y": width - 1 - original["x"],
              "x_out": original["y_out"], "y_out": width - 1 - original["x_out"]},
    }
    for rotation in ROTATIONS:
        wrapped_deck.set_rotation(rotation)
        mapped = wrapped_deck.logical_touch_value(original)
        if mapped != expected_by_rotation[rotation]:
            print(f"FAIL(d): rotation {rotation} mapped {original} to "
                  f"{mapped}, expected {expected_by_rotation[rotation]}")
            return 1
        logical = wrapped_deck.logical_touchscreen_size()
        for key, extent in (("x", logical[0]), ("y", logical[1]),
                            ("x_out", logical[0]), ("y_out", logical[1])):
            if not 0 <= mapped[key] < extent:
                print(f"FAIL(d): rotation {rotation}: {key} came out at "
                      f"{mapped[key]}, off a strip of {logical}")
                return 1
        if original != {"x": 100, "y": 20, "x_out": 700, "y_out": 80}:
            print(f"FAIL(d): the event's own dict was edited in place: "
                  f"{original}")
            return 1

    # A drag reported left to right on the device runs right to left under the
    # user's hand at 180, which is what the consumer compares.
    wrapped_deck.set_rotation(180)
    mapped = wrapped_deck.logical_touch_value(original)
    if not mapped["x"] > mapped["x_out"]:
        print(f"FAIL(d): rotation 180 did not reverse the drag direction: "
              f"{mapped}")
        return 1

    # A pair transposes together or not at all; a lone axis is carried through.
    wrapped_deck.set_rotation(90)
    lone = wrapped_deck.logical_touch_value({"x": 100})
    if lone != {"x": 100}:
        print(f"FAIL(d): rotation 90 moved a lone axis to {lone}")
        return 1

    # Keys the mapper does not know are carried through untouched.
    carried = wrapped_deck.logical_touch_value({"x": 0, "y": 0, "pressure": 7})
    if carried.get("pressure") != 7:
        print(f"FAIL(d): rotation 180 dropped an unmapped key: {carried}")
        return 1

    # Keep out-of-range positions outside at every rotation; the library does not clamp them.
    # Mirroring x == width to -1 would make consumer slot arithmetic select the first slot.
    for rotation in ROTATIONS:
        wrapped_deck.set_rotation(rotation)
        edge = wrapped_deck.logical_touch_value({"x": width, "y": height})
        logical_width, logical_height = wrapped_deck.logical_touchscreen_size()
        if 0 <= edge["x"] < logical_width or 0 <= edge["y"] < logical_height:
            print(f"FAIL(d): rotation {rotation} moved a touch at "
                  f"({width}, {height}), which is past the strip, onto it: "
                  f"{edge}")
            return 1

    print("PASS: touch positions map to the composed strip at every rotation")
    return 0


def check_dial_order() -> int:
    """Dial order follows the strip: reversed at 180, one for one elsewhere."""
    deck = FaultyFakeDeck(serial_number="rot-dial", model="plus")
    wrapped_deck = BetterDeck(deck)
    if wrapped_deck.dial_count() != N_DIALS:
        print(f"FAIL(e): the fake deck has {wrapped_deck.dial_count()} dials; the "
              f"literals below assume {N_DIALS}")
        return 1

    # One dial held down, reported in physical order by the device.
    deck.dial_states = lambda: [True] + [False] * (N_DIALS - 1)

    for rotation in ROTATIONS:
        wrapped_deck.set_rotation(rotation)
        logical = [wrapped_deck.get_logical_dial_index(p) for p in range(N_DIALS)]
        expected = (list(reversed(range(N_DIALS))) if rotation == 180
                    else list(range(N_DIALS)))
        if logical != expected:
            print(f"FAIL(e): rotation {rotation} maps physical dials to "
                  f"{logical}, expected {expected}")
            return 1
        if sorted(logical) != list(range(N_DIALS)):
            print(f"FAIL(e): rotation {rotation} dial map is not a bijection: "
                  f"{logical}")
            return 1
        # The map is its own inverse, which is what lets one reversal serve
        # both the event path and the slot the composite drew.
        round_trip = [wrapped_deck.get_physical_dial_index(l) for l in logical]
        if round_trip != list(range(N_DIALS)):
            print(f"FAIL(e): rotation {rotation} dial map is not its own "
                  f"inverse: {round_trip}")
            return 1

        # dial_states() is reported in logical order, as key_states() is.
        states = wrapped_deck.dial_states()
        pressed = states.index(True)
        if pressed != wrapped_deck.get_logical_dial_index(0):
            print(f"FAIL(e): rotation {rotation}: physical dial 0 is pressed, "
                  f"dial_states() reports logical {pressed} pressed")
            return 1

    print("PASS: dial order reverses at 180 and stays one for one elsewhere")
    return 0


def check_event_remap() -> int:
    """The dial and touchscreen callbacks carry logical values."""
    deck = FaultyFakeDeck(serial_number="rot-events", model="plus")
    wrapped_deck = BetterDeck(deck)

    dials: "list[int]" = []
    touches: "list[dict]" = []
    wrapped_deck.set_dial_callback(lambda _deck, dial, _event, _value: dials.append(dial))
    wrapped_deck.set_touchscreen_callback(lambda _deck, _event, value: touches.append(value))

    for rotation in ROTATIONS:
        wrapped_deck.set_rotation(rotation)

        dials.clear()
        deck.fire_dial_event(0, "TURN", 1)
        expected_dial = wrapped_deck.get_logical_dial_index(0)
        if dials != [expected_dial]:
            print(f"FAIL(f): rotation {rotation}: physical dial 0 dispatched "
                  f"{dials}, expected [{expected_dial}]")
            return 1

        touches.clear()
        deck.fire_touchscreen_event("SHORT", {"x": 10, "y": 10})
        expected_touch = wrapped_deck.logical_touch_value({"x": 10, "y": 10})
        if touches != [expected_touch]:
            print(f"FAIL(f): rotation {rotation}: a touch at (10, 10) "
                  f"dispatched {touches}, expected [{expected_touch}]")
            return 1

    print("PASS: the dial and touchscreen callbacks deliver logical values")
    return 0


def main() -> int:
    start_watchdog(30, "betterdeck_rotation")
    fixtures.install_stub_globals()
    rc = check_rotation()
    rc |= check_async_setters()
    rc |= check_rotation_direction()
    rc |= check_strip_turn()
    rc |= check_slot_order()
    rc |= check_touch_value()
    rc |= check_dial_order()
    rc |= check_event_remap()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
