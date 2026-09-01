"""Drop stale-generation paints at the present boundary.

Current-generation paints still land; each direct task pass is one cycle.
"""
import fixtures


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_stale_generation_paint")
    controller, media_player, deck_manager = fixtures.make_stub_controller()
    deck = controller.deck
    page = controller.active_page
    original_generation = controller._page_load_generation

    # Supersede generation G without a page switch, as load_page does under its lock.
    stale_image = fixtures.make_native_image(fill=1)
    media_player.add_image_task(0, stale_image, page=page, config_gen=original_generation)

    new_gen = controller.bump_generation()
    assert new_gen == original_generation + 1, f"expected gen to bump to {original_generation + 1}, got {new_gen}"

    media_player.perform_media_player_tasks()  # one media cycle

    assert deck.last_op_for("key:0") is None, (
        f"stale-gen frame must be dropped, but journal has: {deck.journal()}"
    )

    # A frame enqueued against the now-current generation must land.
    fresh_image = fixtures.make_native_image(fill=2)
    media_player.add_image_task(0, fresh_image, page=page, config_gen=controller._page_load_generation)
    media_player.perform_media_player_tasks()

    landed = deck.last_op_for("key:0")
    assert landed is not None, "current-gen frame must land"
    assert landed[2] == "set_key_image"

    print("PASS: scenario_stale_generation_paint")


if __name__ == "__main__":
    main()
