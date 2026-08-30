"""Verify clear ordering against frames submitted before and after it."""
import fixtures
from src.backend.DeckManagement.DeckController import ClearMsg
from src.backend.DeckManagement.InputIdentifier import Input


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_clear_ordering")
    controller, media_player, deck_manager = fixtures.make_stub_controller()
    deck = controller.deck
    page = controller.active_page
    gen = controller._page_load_generation

    deck.set_write_latency(0.01)

    # Part 1. Content, then Clear, then more content, all in one generation.
    pre_clear_img = fixtures.make_native_image(fill=1)
    media_player.add_image_task(0, pre_clear_img, page=page, config_gen=gen)
    media_player.perform_media_player_tasks()  # lands on an earlier "tick"

    pre_clear_landed = deck.last_op_for("key:0")
    assert pre_clear_landed is not None and pre_clear_landed[2] == "set_key_image", (
        "fixture sanity: the pre-clear frame must land before the Clear exists"
    )

    # Capture the seq the way DeckController.clear() does, then queue more
    # content for the same page and generation before draining the Clear.
    seq = media_player.next_submit_seq()
    media_player.submit_control(ClearMsg(seq=seq))
    post_clear_img = fixtures.make_native_image(fill=2)
    media_player.add_image_task(0, post_clear_img, page=page, config_gen=gen)

    still_running = media_player.drain_control_queue()
    assert still_running, "ClearMsg must not be terminal"

    blank_entry = deck.last_op_for("key:0")
    assert blank_entry is not None
    assert blank_entry[1] > pre_clear_landed[1], (
        "the blank must land (seq-after) the pre-clear frame -- pre-clear "
        "frames MAY precede the blank, never the reverse"
    )

    # The post-clear frame must not have been wiped. It stays queued and paints
    # on the next media cycle.
    assert 0 in media_player.image_tasks, "post-clear frame was wiped by the Clear"

    media_player.perform_media_player_tasks()

    final_entry = deck.last_op_for("key:0")
    assert final_entry is not None and final_entry[1] > blank_entry[1], (
        "the post-clear frame must land AFTER the blank"
    )
    assert final_entry[4] != blank_entry[4], (
        "the post-clear frame must actually repaint different content, not stay blank"
    )

    # Model screensaver entry: clear first, then require later content to survive.
    deck.clear_journal()

    seq2 = media_player.next_submit_seq()
    media_player.submit_control(ClearMsg(seq=seq2))
    content_img = fixtures.make_native_image(fill=3)
    media_player.add_image_task(0, content_img, page=page, config_gen=gen)

    still_running = media_player.drain_control_queue()
    assert still_running
    media_player.perform_media_player_tasks()

    key0_ops = [e for e in deck.journal() if e[3] == "key:0"]
    assert len(key0_ops) == 2, (
        f"expected exactly [blank, content] for key:0, got {key0_ops}"
    )
    assert key0_ops[0][4] != key0_ops[1][4], (
        "the content submitted after a pre-content Clear must not be wiped -- "
        "the deck must not be left permanently blank"
    )

    leg_executed_seq_is_deck_wide()

    print("PASS: scenario_clear_ordering")


def leg_executed_seq_is_deck_wide() -> None:
    """Require the executed-frame sequence to be deck-wide and write-gated."""
    controller, media_player, _ = fixtures.make_stub_controller(
        serial="clearorder-exec", n_keys=2)
    page = controller.active_page
    gen = controller._page_load_generation

    # The screensaver's shape: the Clear is submitted first, the paints land
    # first, and the blanks overwrite them.
    clear_seq = media_player.next_submit_seq()
    media_player.add_image_task(0, fixtures.make_native_image(fill=9),
                                page=page, config_gen=gen,
                                present=controller.inputs[Input.Key][0].present_state)
    media_player.perform_media_player_tasks()
    assert not controller._full_repaint_pending, (
        "fixture sanity: nothing must have armed the repaint before the Clear"
    )

    media_player.submit_control(ClearMsg(seq=clear_seq, expects_repaint=True))
    media_player.drain_control_queue()
    assert controller._full_repaint_pending, (
        "a Clear that executed after a frame reached the device must arm the "
        "repaint -- its blanks overwrote content the submitter still expects, "
        "and behind a screensaver nothing else would repaint it"
    )

    # A frame whose run() raises must not advance the device-write sequence.
    def boom() -> None:
        raise TypeError("boom-executed-seq")

    before = media_player._max_executed_seq
    media_player.add_image_task(1, fixtures.make_native_image(fill=4),
                                page=page, config_gen=gen)
    media_player.image_tasks[1].run = boom
    try:
        media_player.perform_media_player_tasks()
    except TypeError:
        pass
    else:
        raise AssertionError("fixture sanity: the poisoned run() must escape the drain")
    assert media_player._max_executed_seq == before, (
        "the executed mark must not advance for a frame whose run() raised -- "
        "a later Clear would read it as content that reached the device"
    )

    print("  leg PASS: executed seq is deck-wide and write-gated")


if __name__ == "__main__":
    main()
