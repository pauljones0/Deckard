"""Map all Neo grid keys bijectively at each rotation and drop touch buttons.
Physical indexes 8 and 9 are outside the 2x4 grid and must never dispatch keys."""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

from fixtures import FaultyFakeDeck, start_watchdog

from src.backend.DeckManagement.BetterDeck import BetterDeck

ROTATIONS = (0, 90, 180, 270)
GRID_ROWS, GRID_COLS = 2, 4  # Stream Deck Neo key grid.
GRID_TOTAL = GRID_ROWS * GRID_COLS  # 8 grid keys.
TOUCH_INDEXES = (8, 9)  # Reported past the grid, through the key callback.


def check_neo_touch_dispatch() -> int:
    deck = FaultyFakeDeck(serial_number="neo-1")
    # Pin the Neo grid, so the physical/logical literals below hold.
    deck.key_layout = lambda: (GRID_ROWS, GRID_COLS)
    better = BetterDeck(deck)

    dispatched: "list[int]" = []
    better.set_key_callback(lambda _deck, key, _state: dispatched.append(key))

    for rotation in ROTATIONS:
        better.set_rotation(rotation)

        # (a) Every grid index maps to its correct logical key, and the grid
        # keys together cover 0..GRID_TOTAL-1 exactly once (a bijection).
        for physical in range(GRID_TOTAL):
            dispatched.clear()
            deck.fire_key_event(physical, True)
            expected = better.get_logical_index(physical)
            if dispatched != [expected]:
                print(f"FAIL(a): rotation {rotation} physical {physical} "
                      f"dispatched {dispatched}, expected [{expected}]")
                return 1
            if expected is None or not 0 <= expected < GRID_TOTAL:
                print(f"FAIL(a): rotation {rotation} physical {physical} "
                      f"maps to out-of-grid logical {expected}")
                return 1

        dispatched.clear()
        for physical in range(GRID_TOTAL):
            deck.fire_key_event(physical, True)
        if sorted(dispatched) != list(range(GRID_TOTAL)):
            print(f"FAIL(a): rotation {rotation} grid keys are not a "
                  f"bijection over 0..{GRID_TOTAL - 1}: {sorted(dispatched)}")
            return 1

        # (b) A touch button past the grid never dispatches. It must not reach
        # the consumer as a bogus grid key (1, 6) or a negative index (-1, -2).
        for physical in TOUCH_INDEXES:
            dispatched.clear()
            deck.fire_key_event(physical, True)
            if dispatched:
                print(f"FAIL(b): rotation {rotation} touch button physical "
                      f"{physical} dispatched grid key(s) {dispatched}; it "
                      f"must be dropped")
                return 1

    print("PASS: grid keys map correctly and Neo touch buttons never "
          "dispatch a grid key at any rotation")
    return 0


def main() -> int:
    start_watchdog(30, "neo_touch_key_dispatch")
    fixtures.install_stub_globals()
    return check_neo_touch_dispatch()


if __name__ == "__main__":
    raise SystemExit(main())
