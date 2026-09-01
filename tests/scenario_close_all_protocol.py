"""Verify the two-phase close protocol across all controllers."""
import time

import fixtures

from src.backend.DeckManagement.DeckManager import close_all_controllers


def _settle_and_clear(controller, deck) -> None:
    """Wait for boot paint, permit a trailing brightness write, then clear."""
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)
    time.sleep(0.1)
    deck.clear_journal()


def _assert_clean_close(deck, key_count: int, is_touch: bool) -> None:
    """Require blank writes followed by one terminal close per controller."""
    journal = deck.journal()
    expected_clear_ops = key_count + (1 if is_touch else 0)

    closes = [e for e in journal if e[2] == "close"]
    assert len(closes) == 1, f"exactly one close() per deck, got {len(closes)}: {journal}"
    assert journal[-1][2] == "close", "nothing may land after close()"

    # The blank writes are the expected_clear_ops entries immediately before the
    # close.
    blanks = journal[-(expected_clear_ops + 1):-1]
    assert len(blanks) == expected_clear_ops, (
        f"expected {expected_clear_ops} blank writes before close, got {blanks} in {journal}"
    )
    for k in range(key_count):
        assert blanks[k][2] == "set_key_image" and blanks[k][3] == f"key:{k}", (
            f"expected key {k}'s blank write at position {k} of the blanks, got {blanks[k]}"
        )
    if is_touch:
        assert blanks[key_count][2] == "set_touchscreen_image", (
            f"expected the touchscreen blank write last among the blanks, got {blanks[key_count]}"
        )


def test_close_all_two_controllers() -> None:
    first_controller = fixtures.make_headless_controller(serial="close-all-1")
    second_controller = fixtures.make_headless_controller(serial="close-all-2")
    first_deck = fixtures.raw_deck(first_controller)
    second_deck = fixtures.raw_deck(second_controller)

    _settle_and_clear(first_controller, first_deck)
    _settle_and_clear(second_controller, second_deck)

    first_key_count = first_controller.deck.key_count()
    second_key_count = second_controller.deck.key_count()
    first_is_touch = first_controller.deck.is_touch()
    second_is_touch = second_controller.deck.is_touch()

    # Drive the real free function, not gl.deck_manager.close_all.
    close_all_controllers([first_controller, second_controller])

    assert fixtures.wait_until(
        lambda: (
            not first_controller.media_player.is_alive()
            and not second_controller.media_player.is_alive()
        ),
        timeout=3.0,
    ), "both media threads must exit within the bounded join"

    _assert_clean_close(first_deck, first_key_count, first_is_touch)
    _assert_clean_close(second_deck, second_key_count, second_is_touch)
    print("PASS: close_all_controllers() clears+closes every controller and joins every writer")

    # Stop the non-daemon tick threads, so the process can exit.
    fixtures.teardown(first_controller)
    fixtures.teardown(second_controller)


def test_controller_without_media_closes() -> None:
    """Close a partially constructed controller without a media player directly."""
    c = fixtures.make_headless_controller(serial="close-all-3")
    d = fixtures.raw_deck(c)
    _settle_and_clear(c, d)

    # Model a controller whose writer thread never came up. Stop and detach the
    # real media_player, so the None branch runs, then close directly.
    c.media_player.stop(timeout=2.0)
    fixtures.wait_until(lambda: not c.media_player.is_alive(), timeout=3.0)
    d.clear_journal()
    c.media_player = None

    close_all_controllers([c])
    closes = [e for e in d.journal() if e[2] == "close"]
    assert len(closes) == 1, (
        f"a controller with no media_player must be closed directly (one close()), got {d.journal()}"
    )
    print("PASS: close_all_controllers() closes a media_player-less controller directly")

    fixtures.teardown(c)


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_close_all_protocol")
    test_close_all_two_controllers()
    test_controller_without_media_closes()
    print("ALL PASS: scenario_close_all_protocol")


if __name__ == "__main__":
    main()
