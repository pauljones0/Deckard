"""Check the background-load and screensaver race.

A generation-checked worker must not write after the screensaver.
"""

# A gate inside Background.set_from_path parks the worker with the lock held,
# so show() blocks on that same lock and its background lands last.
import os
import threading
import time

import fixtures
import globals as gl
from src.backend.DeckManagement.DeckController import Background

WATCHDOG_SECONDS = 30


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_screensaver_bg_race")

    controller = fixtures.make_headless_controller(serial="bgrace-1")
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()

    page_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "race_page.png"), color=(10, 200, 10))
    ss_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "race_ss.png"), color=(10, 10, 200))
    # Persist the settings because each load_page reloads them from the page.
    # Without them, media_path resets to None and show() paints blank.
    page_path = fixtures.seed_page_with_background_and_screensaver(
        "RacePage", page_png, ss_png, screensaver_time_delay=60
    )
    page = gl.page_manager.get_page(page_path, controller)

    def settled():
        return all(deck.last_op_for(f"key:{k}") is not None for k in range(key_count))

    # Learn each state's paint signature in isolation, before the race.
    controller.load_page(page, allow_reload=True)
    ok = fixtures.wait_until(settled, timeout=5)
    assert ok, "fixture setup: page content never painted"
    time.sleep(0.1)
    page_sig = {k: deck.last_op_for(f"key:{k}")[4] for k in range(key_count)}

    controller.screen_saver.show()
    ok = fixtures.wait_until(lambda: controller.screen_saver.showing and settled(), timeout=5)
    assert ok, "fixture setup: screensaver never showed"
    time.sleep(0.1)
    ss_sig = {k: deck.last_op_for(f"key:{k}")[4] for k in range(key_count)}

    for k in range(key_count):
        assert page_sig[k] != ss_sig[k], (
            f"key {k}: page and screensaver produced the same hash -- the "
            f"fixture isn't actually distinguishing the two states"
        )

    controller.screen_saver.hide()
    ok = fixtures.wait_until(lambda: not controller.screen_saver.showing and settled(), timeout=5)
    assert ok, "fixture setup: screensaver never hid"
    time.sleep(0.1)

    real_set_from_path = Background.set_from_path
    gate = threading.Event()
    worker_parked = threading.Event()

    def gated_set_from_path(self, *args, **kwargs):
        worker_parked.set()
        gate.wait(timeout=10)
        return real_set_from_path(self, *args, **kwargs)

    Background.set_from_path = gated_set_from_path
    try:
        deck.clear_journal()
        # Park load_background after its generation check with the lock held.
        controller.load_page(page, allow_reload=True)
        ok = fixtures.wait_until(worker_parked.is_set, timeout=5)
        assert ok, "the load_background worker never reached the gate"

        # show() needs the same lock, so it waits until the worker is released.
        show_done = threading.Event()

        def do_show():
            controller.screen_saver.show()
            show_done.set()

        t_show = threading.Thread(target=do_show, name="RaceShow")
        t_show.start()

        # Let an unlocked show() finish before the worker's stale write can land.
        time.sleep(0.3)

        gate.set()  # release the parked worker, which writes the page content
        t_show.join(timeout=10)
        assert show_done.is_set(), "screen_saver.show() did not complete -- possible deadlock"

        ok = fixtures.wait_until(settled, timeout=5)
        assert ok, "deck did not settle after the race"
        time.sleep(0.3)  # quiescence window
    finally:
        Background.set_from_path = real_set_from_path

    assert controller.screen_saver.showing is True, "screensaver must still be showing after the race"

    for k in range(key_count):
        last = deck.last_op_for(f"key:{k}")
        assert last is not None, f"key {k} was never painted during the race"
        assert last[4] == ss_sig[k], (
            f"key {k}: final content does not match the screensaver's signature "
            f"(got {last[4]}, ss={ss_sig[k]}, page={page_sig[k]}) -- the stale "
            f"load_background worker overwrote the screensaver's background (C-F6)"
        )
        assert last[4] != page_sig[k], (
            f"key {k}: final content matches the stale page's signature -- "
            f"the delayed load_background worker won the race (C-F6 regression)"
        )

    fixtures.teardown(controller)
    print("PASS: scenario_screensaver_bg_race")


if __name__ == "__main__":
    main()
