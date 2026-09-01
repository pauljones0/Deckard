"""An idle deck with a static screensaver must cost nothing per tick.

Its fresh inputs have no action, media, or label; imagery lives in background.
"""

# The tick loop therefore calls update() on no input at all, and hide() still
# repaints every key.
import os
import threading
import time

import fixtures
import globals as gl

# Complete tick iterations to observe. TICK_DELAY is 1s, so this is also
# roughly the observation window's length in seconds.
OBSERVED_WINDOWS = 2


def wait_until_quiet(deck, quiet_for: float = 0.5, timeout: float = 15.0) -> bool:
    """Wait until no device write lands for quiet_for seconds.

    This excludes the variable drain time of show() from the observation window.
    """
    deadline = time.monotonic() + timeout
    seen = len(deck.journal())
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.05)
        now = len(deck.journal())
        if now != seen:
            seen = now
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= quiet_for:
            return True
    return False


def all_keys_painted_after(deck, key_count: int, seq: int) -> bool:
    return all(
        (e := deck.last_op_for(f"key:{k}")) is not None and e[1] > seq
        for k in range(key_count)
    )


def idle_tick_costs_nothing(controller, deck, key_count) -> None:
    """Require zero per-input updates from the tick thread.

    Attribute calls by thread because the media thread also calls update().
    """
    lock = threading.Lock()
    updates: dict[str, int] = {}
    marks: dict[str, int] = {}

    def count_updates(controller_input) -> None:
        original = controller_input.update

        def counting(*args, **kwargs):
            with lock:
                name = threading.current_thread().name
                updates[name] = updates.get(name, 0) + 1
            return original(*args, **kwargs)

        controller_input.update = counting

    for input_list in controller.inputs.values():
        for controller_input in input_list:
            count_updates(controller_input)

    # tick_actions makes two marks per iteration; thread attribution isolates it.
    original_mark = controller.mark_page_ready_to_clear

    def counting_mark(*args, **kwargs):
        with lock:
            name = threading.current_thread().name
            marks[name] = marks.get(name, 0) + 1
        return original_mark(*args, **kwargs)

    controller.mark_page_ready_to_clear = counting_mark

    def observed_windows() -> int:
        # Discount a possible lone first mark to keep a full-iteration lower bound.
        with lock:
            return max(0, marks.get("tick_actions", 0) // 2 - 1)

    def tick_updates() -> int:
        with lock:
            return updates.get("tick_actions", 0)

    journal_at_start = len(deck.journal())
    repaint_ts_at_start = controller._last_full_repaint_ts

    assert fixtures.wait_until(
        lambda: observed_windows() >= OBSERVED_WINDOWS,
        timeout=controller.TICK_DELAY * (OBSERVED_WINDOWS + 2) + 20,
    ), (
        f"the action tick loop completed only {observed_windows()} fully observed "
        f"iterations -- the observation window never happened, so nothing below "
        f"is pinned"
    )

    observed = observed_windows()
    assert tick_updates() == 0, (
        f"the action tick repainted inputs {tick_updates()} times across "
        f"{observed} iterations while a static screensaver was showing -- an "
        f"idle deck is re-compositing and re-hashing every input every second"
    )

    if controller._last_full_repaint_ts != repaint_ts_at_start:
        # A 5s scheduling gap arms a resume repaint, so skip only device silence.
        # The per-thread update count remains valid.
        print("NOTE: a full repaint fired during the window (loaded machine); "
              "skipping the device-silence check")
    else:
        assert len(deck.journal()) == journal_at_start, (
            f"the deck was written to {len(deck.journal()) - journal_at_start} "
            f"times while a static screensaver sat idle"
        )
    print(f"PASS: {observed} fully observed tick iterations under a static "
          f"screensaver, 0 input repaints from the tick thread")


def late_clear_recovers_content(controller, deck, key_count, blank_hash,
                                     page_sig, ss_sig) -> None:
    """Check recovery from a late Clear that blanks the screensaver.

    show() submits Clear before it enqueues the screensaver paints.
    """
    # A tick can paint first, then pop Clear after the now-empty slots.
    media_player = controller.media_player
    arm = threading.Event()
    parked = threading.Event()
    release = threading.Event()
    original_check = media_player.check_resume_gap

    def parking_check(now=None):
        # Park once between drain_control_queue and perform_media_player_tasks.
        if arm.is_set() and not parked.is_set():
            parked.set()
            release.wait(timeout=20)
        return original_check(now)

    media_player.check_resume_gap = parking_check
    seq_before_show = deck.current_seq()
    try:
        arm.set()
        assert parked.wait(timeout=15), "the media writer never reached the park point"
        # Put Clear on the drained queue and paints in the parked tick's slots.
        controller.screen_saver.show()
        assert controller.screen_saver.showing is True
    finally:
        release.set()
        media_player.check_resume_gap = original_check

    # The interleave must have happened, or the recovery check proves
    # nothing. A blank has to land after the screensaver content it wiped.
    def blanked_after_content() -> bool:
        painted = False
        for entry in deck.journal():
            if entry[1] <= seq_before_show or entry[3] != "key:0":
                continue
            if entry[4] != blank_hash:
                painted = True
            elif painted:
                return True
        return False

    assert fixtures.wait_until(blanked_after_content, timeout=15), (
        "the late-Clear interleave did not reproduce -- the parking probe no "
        "longer lands between the control drain and the task drain, so the "
        "recovery assertion below would be vacuous"
    )

    # Require screensaver content because a repaint race can leave page imagery.
    def recovered() -> bool:
        return all(
            (e := deck.last_op_for(f"key:{k}")) is not None and e[4] == ss_sig[k]
            for k in range(key_count)
        )

    ok = fixtures.wait_until(recovered, timeout=15)
    if not ok:
        stuck = {
            k: deck.last_op_for(f"key:{k}")[4]
            for k in range(key_count)
            if deck.last_op_for(f"key:{k}")[4] != ss_sig[k]
        }
        blank_keys = [k for k, h in stuck.items() if h == blank_hash]
        page_keys = [k for k, h in stuck.items() if h == page_sig[k]]
        raise AssertionError(
            "the deck did not come back to the screensaver's content behind a "
            f"showing screensaver. Blank keys {blank_keys} (a Clear that executed "
            "after the screensaver's paints wiped them with nothing left to "
            f"restore the picture); previous-page keys {page_keys} (a recovery "
            "repaint raced show()'s own update_all_inputs and the pre-swap "
            f"composite landed last); other {sorted(set(stuck) - set(blank_keys) - set(page_keys))}"
        )
    print("PASS: a late-executing Clear recovers to the screensaver's own content")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_screensaver_idle_tick")
    controller = fixtures.make_headless_controller(serial="ss-idle-1")
    try:
        deck = fixtures.raw_deck(controller)
        key_count = controller.deck.key_count()

        # Capture the bootstrap _write_blank_frames hash before later paints.
        blank_hash = next(e[4] for e in deck.journal() if e[3] == "key:0")

        screensaver_png = fixtures.make_test_png(
            os.path.join(gl.DATA_PATH, "media", "idle_saver.png"), color=(0, 160, 220)
        )
        controller.screen_saver.set_media_path(screensaver_png)

        # Let the default page land before the transition under test.
        assert fixtures.wait_until(
            lambda: deck.last_op_for("key:0") is not None, timeout=10
        ), "fixture sanity: the default page never painted"

        assert wait_until_quiet(deck), "the default page never settled"
        page_sig = {k: deck.last_op_for(f"key:{k}")[4] for k in range(key_count)}

        seq_before_show = deck.current_seq()
        controller.screen_saver.show()
        assert controller.screen_saver.showing is True

        # Wait for every key so a mid-flight bulk batch cannot enter the window.
        assert fixtures.wait_until(
            lambda: all_keys_painted_after(deck, key_count, seq_before_show), timeout=15
        ), "the screensaver never painted every key"
        assert wait_until_quiet(deck), (
            "a STATIC screensaver never stopped writing to the deck -- the window "
            "below cannot attribute anything"
        )

        # Capture a clean screensaver signature distinct from page and blank.
        ss_sig = {k: deck.last_op_for(f"key:{k}")[4] for k in range(key_count)}
        for k in range(key_count):
            assert ss_sig[k] not in (blank_hash, page_sig[k]), (
                f"fixture sanity: key {k}'s screensaver content is "
                f"indistinguishable from {'blank' if ss_sig[k] == blank_hash else 'the page'}"
            )

        idle_tick_costs_nothing(controller, deck, key_count)

        # The wake path must be untouched.
        seq_before_hide = deck.current_seq()
        controller.screen_saver.hide()
        assert controller.screen_saver.showing is False
        assert fixtures.wait_until(
            lambda: all_keys_painted_after(deck, key_count, seq_before_hide), timeout=15
        ), "hiding the screensaver no longer repaints every key"
        print("PASS: hide() still repaints every key")

        # hide() reloads the unset page media path, so restore it before re-entry.
        assert wait_until_quiet(deck), "the restored page never settled"
        controller.screen_saver.set_media_path(screensaver_png)
        late_clear_recovers_content(
            controller, deck, key_count, blank_hash, page_sig, ss_sig
        )
    finally:
        fixtures.teardown(controller)

    print("\nALL PASS: scenario_screensaver_idle_tick")


if __name__ == "__main__":
    main()
