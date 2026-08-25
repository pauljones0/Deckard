"""Pins the BetterDeck rotation mapping and the async callback setters.

reorder_physical_for_rotation must write out[logical(p)] = orig[p], checked
against get_physical_index. The three async setters call the wrapped deck.

The wrapper is the one place that knows what a rotation means, so the rest
pins the maps it hands out: the strip turn, the touch positions, the dial
order, and the two callbacks that carry those from the reader thread.

Deck shape, stated once so a configurable fake deck can adopt it later: four
dials and an 800 by 100 strip, which is the Stream Deck + shape the fake deck
models. The key legs force their own grid.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)


from fixtures import FaultyFakeDeck, start_watchdog

from src.backend.DeckManagement.BetterDeck import BetterDeck

ROTATIONS = (0, 90, 180, 270)
N_DIALS = 4
STRIP_SIZE = (800, 100)


def check_rotation() -> int:
    deck = FaultyFakeDeck(serial_number="rot-1")
    # Force a 3x5 layout, so the literals below hold.
    deck.key_layout = lambda: (3, 5)
    better = BetterDeck(deck)

    total = 15
    physical = list(range(total))  # value == its physical index

    for rotation in (0, 90, 180, 270):
        better.set_rotation(rotation)
        out = better.reorder_physical_for_rotation(physical)

        # The result must stay a permutation, with nothing lost or duplicated.
        if sorted(out) != physical:
            print(f"FAIL(a): rotation {rotation} output is not a "
                  f"permutation: {out}")
            return 1

        # Check against the inverse formula as an oracle. The value from
        # physical slot p must sit at logical slot l where
        # get_physical_index(l) == p.
        for logical in range(total):
            p = better.get_physical_index(logical)
            if out[logical] != physical[p]:
                print(f"FAIL(a): rotation {rotation}: out[{logical}] = "
                      f"{out[logical]}, expected value from physical slot "
                      f"{p} -- the map is applied in the wrong direction")
                return 1

    # Hand-computed literal for 3 rows by 5 cols at rotation 90.
    # get_logical_index(0) = (0%5)*3 + (3-1-0//5) = 2, so orig[0] lands at out[2].
    better.set_rotation(90)
    out = better.reorder_physical_for_rotation(physical)
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
    better = BetterDeck(deck)

    async def cb(*a):
        pass

    try:
        better.set_key_callback_async(cb)
        better.set_dial_callback_async(cb)
        better.set_touchscreen_callback_async(cb)
    except RecursionError:
        print("FAIL(b): async callback setter recursed into itself")
        return 1

    missing = {"key", "dial", "touch"} - set(received)
    if missing:
        print(f"FAIL(b): setters never reached the wrapped deck: {missing}")
        return 1
    print("PASS: async callback setters delegate to the wrapped deck")
    return 0


def check_strip_turn() -> int:
    """The strip turns end for end at 180 and nowhere else.

    At 90 and 270 the strip stands on its side and the device buffer keeps
    its shape, so there is nothing to turn an upright composite into. Pinning
    the zero there stops a well-meant rotate() that would squash the strip.
    """
    deck = FaultyFakeDeck(serial_number="rot-strip")
    better = BetterDeck(deck)

    expected = {0: 0, 90: 0, 180: 180, 270: 0}
    for rotation in ROTATIONS:
        better.set_rotation(rotation)
        turn = better.touchscreen_image_rotation()
        if turn != expected[rotation]:
            print(f"FAIL(c): rotation {rotation}: strip turn {turn}, "
                  f"expected {expected[rotation]}")
            return 1

    print("PASS: the strip composite turns end for end at 180 only")
    return 0


def check_touch_value() -> int:
    """A touch position is mapped to where the strip was composed."""
    deck = FaultyFakeDeck(serial_number="rot-touch")
    better = BetterDeck(deck)
    width, height = STRIP_SIZE
    if tuple(deck.touchscreen_image_format()["size"]) != STRIP_SIZE:
        print(f"FAIL(d): the fake deck's strip is not {STRIP_SIZE}; the "
              f"literals below assume it")
        return 1

    original = {"x": 100, "y": 20, "x_out": 700, "y_out": 80}

    for rotation in (0, 90, 270):
        better.set_rotation(rotation)
        mapped = better.logical_touch_value(dict(original))
        if mapped != original:
            print(f"FAIL(d): rotation {rotation} moved a touch position to "
                  f"{mapped}; the strip is written in the device's own "
                  f"orientation there, so nothing moves")
            return 1

    better.set_rotation(180)
    mapped = better.logical_touch_value(original)
    expected = {
        "x": width - 1 - original["x"],
        "y": height - 1 - original["y"],
        "x_out": width - 1 - original["x_out"],
        "y_out": height - 1 - original["y_out"],
    }
    if mapped != expected:
        print(f"FAIL(d): rotation 180 mapped {original} to {mapped}, "
              f"expected {expected}")
        return 1
    if original != {"x": 100, "y": 20, "x_out": 700, "y_out": 80}:
        print(f"FAIL(d): the event's own dict was edited in place: {original}")
        return 1

    # A drag reported left to right on the device runs right to left under the
    # user's hand at 180, which is what the consumer compares.
    if not mapped["x"] > mapped["x_out"]:
        print(f"FAIL(d): rotation 180 did not reverse the drag direction: "
              f"{mapped}")
        return 1

    # Keys the mapper does not know are carried through untouched.
    carried = better.logical_touch_value({"x": 0, "y": 0, "pressure": 7})
    if carried.get("pressure") != 7:
        print(f"FAIL(d): rotation 180 dropped an unmapped key: {carried}")
        return 1

    print("PASS: touch positions map to the composed strip at every rotation")
    return 0


def check_dial_order() -> int:
    """Dial order follows the strip: reversed at 180, one for one elsewhere."""
    deck = FaultyFakeDeck(serial_number="rot-dial")
    better = BetterDeck(deck)
    if better.dial_count() != N_DIALS:
        print(f"FAIL(e): the fake deck has {better.dial_count()} dials; the "
              f"literals below assume {N_DIALS}")
        return 1

    # One dial held down, reported in physical order by the device.
    deck.dial_states = lambda: [True] + [False] * (N_DIALS - 1)

    for rotation in ROTATIONS:
        better.set_rotation(rotation)
        logical = [better.get_logical_dial_index(p) for p in range(N_DIALS)]
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
        round_trip = [better.get_physical_dial_index(l) for l in logical]
        if round_trip != list(range(N_DIALS)):
            print(f"FAIL(e): rotation {rotation} dial map is not its own "
                  f"inverse: {round_trip}")
            return 1

        # dial_states() is reported in logical order, as key_states() is.
        states = better.dial_states()
        pressed = states.index(True)
        if pressed != better.get_logical_dial_index(0):
            print(f"FAIL(e): rotation {rotation}: physical dial 0 is pressed, "
                  f"dial_states() reports logical {pressed} pressed")
            return 1

    print("PASS: dial order reverses at 180 and stays one for one elsewhere")
    return 0


def check_event_remap() -> int:
    """The dial and touchscreen callbacks carry logical values."""
    deck = FaultyFakeDeck(serial_number="rot-events")
    better = BetterDeck(deck)

    dials: "list[int]" = []
    touches: "list[dict]" = []
    better.set_dial_callback(lambda _deck, dial, _event, _value: dials.append(dial))
    better.set_touchscreen_callback(lambda _deck, _event, value: touches.append(value))

    for rotation in ROTATIONS:
        better.set_rotation(rotation)

        dials.clear()
        deck.fire_dial_event(0, "TURN", 1)
        expected_dial = better.get_logical_dial_index(0)
        if dials != [expected_dial]:
            print(f"FAIL(f): rotation {rotation}: physical dial 0 dispatched "
                  f"{dials}, expected [{expected_dial}]")
            return 1

        touches.clear()
        deck.fire_touchscreen_event("SHORT", {"x": 10, "y": 10})
        expected_touch = better.logical_touch_value({"x": 10, "y": 10})
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
    rc |= check_strip_turn()
    rc |= check_touch_value()
    rc |= check_dial_order()
    rc |= check_event_remap()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
