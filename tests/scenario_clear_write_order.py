"""Check DeckController.clear write ordering.

Queued blanking writes every key, then the touchscreen, without interleaving.
"""
import time

import fixtures


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_clear_write_order")
    controller = fixtures.make_headless_controller(serial="clear-write-order-1")
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()
    is_touch = controller.deck.is_touch()

    # Let the initial page settle, so the clear sequence does not compete
    # with the boot paint.
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)
    time.sleep(0.1)

    deck.clear_journal()

    controller.clear()  # submits a seq-stamped ClearMsg and returns

    expected_clear_ops = key_count + (1 if is_touch else 0)
    ok = fixtures.wait_until(lambda: len(deck.journal()) >= expected_clear_ops, timeout=3)
    assert ok, f"clear() writes never landed: {deck.journal()}"
    time.sleep(0.1)  # give a straggler a chance to land, since none should follow

    journal = deck.journal()
    assert len(journal) == expected_clear_ops, (
        f"expected exactly {expected_clear_ops} blank writes, got {len(journal)}: {journal}"
    )

    for k in range(key_count):
        assert journal[k][2] == "set_key_image" and journal[k][3] == f"key:{k}", (
            f"expected key {k}'s clear write at position {k}, got {journal[k]}"
        )
    if is_touch:
        assert journal[key_count][2] == "set_touchscreen_image", (
            f"expected the touchscreen clear write last among the clear ops, got {journal[key_count]}"
        )

    fixtures.teardown(controller)
    print("PASS: scenario_clear_write_order")


if __name__ == "__main__":
    main()
