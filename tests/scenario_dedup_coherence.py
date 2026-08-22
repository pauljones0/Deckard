"""DeckController.clear() must reset dedup state before it writes the blanks.

A repaint of visually identical content after a clear must reach the device.
Two identical touchscreen composites without a clear must write once.
"""
import time

import fixtures


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_dedup_coherence")
    controller = fixtures.make_headless_controller(serial="dedup-1")
    deck = fixtures.raw_deck(controller)
    try:
        run_legs(controller, deck)
    finally:
        # Without this a failed assertion leaves the controller and its writer
        # alive, the process hangs, and the watchdog reports a deadlock that
        # never happened.
        fixtures.teardown(controller)
    print("PASS: scenario_dedup_coherence")


def run_legs(controller, deck) -> None:
    # The bootstrap clear() of DeckController.__init__ paints a deterministic
    # blank image. Capture its hash as the blank reference.
    blank_hash = next(e[4] for e in deck.journal() if e[3] == "key:0")

    # Let the real static content of the default page land.
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)
    time.sleep(0.1)
    content_hash = deck.last_op_for("key:0")[4]
    assert content_hash != blank_hash, "fixture sanity: default page content should not be blank"

    key0 = controller.get_key_by_index(0)

    # Sanity check. Without a clear, repainting identical content is
    # hash-skipped, which is the existing dual-hash behavior.
    seq_before_noop = deck.current_seq()
    controller.ui_image_changes_while_hidden.pop(key0.identifier, None)
    key0.update()
    time.sleep(0.2)
    assert deck.current_seq() == seq_before_noop, (
        "fixture sanity: identical repaint without a clear should hash-skip"
    )
    # The skip returns before the UI mirror too. With no UI attached the port
    # refuses each push and the input dirty-marks itself, so a marker here
    # would mean the skip stopped short of the whole paint.
    assert key0.identifier not in controller.ui_image_changes_while_hidden, (
        "a hash-skipped repaint must not push the in-app preview either"
    )

    # The dedup-coherence fix. A clear() then a repaint of identical content
    # must reach the device instead of being hash-skipped.
    controller.clear()

    def blank_landed():
        e = deck.last_op_for("key:0")
        return e is not None and e[4] == blank_hash and e[1] > seq_before_noop

    ok = fixtures.wait_until(blank_landed, timeout=3)
    assert ok, "clear() must blank key 0"

    seq_after_blank = deck.current_seq()
    key0.update()  # identical content to content_hash

    def repainted_with_same_content():
        e = deck.last_op_for("key:0")
        return e is not None and e[1] > seq_after_blank and e[4] == content_hash

    ok = fixtures.wait_until(repainted_with_same_content, timeout=3)
    assert ok, (
        "identical content must repaint after a clear -- dedup state must be "
        "reset by Clear (plan §3), otherwise the device is stuck on blank"
    )

    # Touchscreen dedup. Two identical composites without a clear between them
    # must produce exactly one device write.
    if controller.deck.is_touch():
        from src.backend.DeckManagement.InputIdentifier import Input

        touchscreen = controller.inputs[Input.Touchscreen][0]
        # Force a clean slate, so the first update() below lands. That decouples
        # this from whatever the boot and dial-tick machinery already painted.
        touchscreen.present_state.reset()

        seq_before_ts = deck.current_seq()
        touchscreen.update()

        def first_ts_landed():
            return len([e for e in deck.ops_after(seq_before_ts) if e[2] == "set_touchscreen_image"]) == 1

        ok = fixtures.wait_until(first_ts_landed, timeout=3)
        assert ok, "first touchscreen paint should land"

        seq_after_first_ts = deck.current_seq()
        touchscreen.update()  # identical composite, so a no-op
        time.sleep(0.3)  # let a would-be regression write land

        extra_ts_writes = [e for e in deck.ops_after(seq_after_first_ts) if e[2] == "set_touchscreen_image"]
        assert not extra_ts_writes, (
            f"identical touchscreen composite must be hash-skipped, got "
            f"{len(extra_ts_writes)} additional write(s)"
        )

    # A write that raised must leave the present state where it was. The
    # deterministic tier from here on: stop the live writer and drain by hand,
    # or the loop races these assertions.
    controller.media_player.stop(timeout=3.0)
    controller.media_player.perform_media_player_tasks()  # drain the leftovers

    key0.present_state.reset()
    deck.fail_next("set_key_image", count=1)
    key0.update()
    controller.media_player.perform_media_player_tasks()
    assert key0.present_state.last_presented_hash is None, (
        "a device write that raised must not record its image as presented -- "
        "the device never took it, and the correcting repaint would be skipped"
    )

    seq_before_retry = deck.current_seq()
    key0.update()  # the same content the failed write carried
    controller.media_player.perform_media_player_tasks()
    retry_writes = [e for e in deck.ops_after(seq_before_retry) if e[3] == "key:0"]
    assert retry_writes, (
        "after a failed write, an identical repaint must reach the device -- "
        "otherwise the key keeps whatever survived the failure"
    )


if __name__ == "__main__":
    main()
