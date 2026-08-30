"""Check idempotent close, controller collection, and screensaver-stash cleanup."""
import gc
import threading
import time
import weakref

import fixtures
import globals as gl
from gi.repository import GLib


def test_double_close_is_safe() -> None:
    controller = fixtures.make_headless_controller(serial="close-double-1")
    fixtures.wait_until(lambda: controller.active_page is not None, timeout=3)

    controller.close(remove_media=True)
    assert controller._closing is True, "close() must set _closing"

    # The second call must be an immediate no-op. It must not raise and must
    # not redo teardown work over already-None executors.
    t0 = time.monotonic()
    controller.close(remove_media=True)
    elapsed = time.monotonic() - t0
    # Keep the liveness ceiling below the 2 s stop timeout that repeated teardown
    # would incur, with headroom for a loaded CI runner.
    assert elapsed < 1.5, f"second close() call should be an immediate no-op, took {elapsed:.2f}s"

    if controller in gl.deck_manager.deck_controller:
        gl.deck_manager.deck_controller.remove(controller)
    print("PASS: close() called twice is safe")


def test_remove_controller_frees_everything() -> None:
    controller = fixtures.make_headless_controller(serial="close-remove-1")
    deck = fixtures.raw_deck(controller)
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)

    assert controller in gl.page_manager.pages, "fixture sanity: controller should have a cached page before teardown"

    # Mirrors DeckManager.remove_controller, without the UI-stack removal, which
    # the null UIPort no-ops away here.
    fixtures.teardown(controller)

    assert controller not in gl.page_manager.pages, "close() must discard the controller's cached pages (step 8)"
    assert controller not in gl.deck_manager.deck_controller

    tick_dead = fixtures.wait_until(lambda: not controller.tick_thread.is_alive(), timeout=2)
    assert tick_dead, "tick thread should have been joined by close() (step 4)"
    media_dead = fixtures.wait_until(lambda: not controller.media_player.is_alive(), timeout=2)
    assert media_dead, "media thread should have been stopped by close() (step 5)"

    assert controller.action_executor is None, "action_executor should be shut down and cleared (step 9)"
    assert controller.load_executor is None, "load_executor should be shut down and cleared (step 9)"

    # Drop every scenario-owned strong reference before the same plain
    # gc.collect() that ends close().
    ref = weakref.ref(controller)
    del controller
    del deck
    # Drain the idle callback that holds the controller through its bound method;
    # this headless harness does not run the main loop.
    ctx = GLib.MainContext.default()
    while ctx.iteration(False):
        pass
    gc.collect()
    assert ref() is None, "controller should become collectible after close() + gc.collect()"

    print("PASS: remove_controller-style teardown frees the whole controller graph")


class _SpyCloseable:
    """Record close() so a real resource sweep differs from a dropped stash."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_close_sweeps_screensaver_stash() -> None:
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = fixtures.make_headless_controller(serial="close-stash-1")
    deck = fixtures.raw_deck(controller)
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)

    # Use a closeable spy as the loaded image of a real pre-screensaver key.
    # ControllerKeyState.close_resources() needs only close().
    real_key = controller.inputs[Input.Key][0]
    spy = _SpyCloseable()
    real_key.get_active_state().key_image = spy

    controller.screen_saver.show()
    assert controller.screen_saver.showing is True, "fixture sanity: show() should flip showing"
    assert controller.inputs[Input.Key][0] is not real_key, "fixture sanity: show() should install fresh transient inputs"

    # Confirm the real key reached the stash if the queued media release has not
    # already cleared it.
    stashed_keys = controller.screen_saver.original_inputs.get(Input.Key, [])
    if stashed_keys:
        assert stashed_keys[0] is real_key, "fixture sanity: original_inputs should hold the real (pre-show) key objects"

    # Wait for both resource closure and the final stash clear; observing only
    # the spy can catch the release loop before it clears the container.
    released = fixtures.wait_until(
        lambda: spy.closed and controller.screen_saver.original_inputs == {},
        timeout=5,
    )
    assert released, "show() must release the stashed input's resources (mem-plan P2.6)"
    assert real_key.get_active_state().key_image is None, "show()'s release must clear the closed reference"
    assert controller.screen_saver.original_inputs == {}, "show()'s release must clear the stashed input set"

    controller.close(remove_media=True)

    assert spy.closed is True, "close() must call close_resources() on stashed inputs, not just drop the container"
    assert real_key.get_active_state().key_image is None, "close_resources() must clear the closed reference"
    assert controller.screen_saver.original_inputs == {}, "close() must clear the stashed input set"
    assert controller.screen_saver.original_background is None, "close() must release the stashed background"

    if controller in gl.deck_manager.deck_controller:
        gl.deck_manager.deck_controller.remove(controller)
    print("PASS: close() sweeps the screensaver stash while showing")


def test_close_sweeps_stash_unplug_race() -> None:
    """Require close to sweep a stash when the queued media release does not."""
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = fixtures.make_headless_controller(serial="close-stash-race-1")
    try:
        deck = fixtures.raw_deck(controller)
        fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)

        real_key = controller.inputs[Input.Key][0]

        # Let the release message drain without clearing the stash, leaving it
        # populated for close(). The rebind affects only this instance.
        release_seen = threading.Event()

        def _record_only_release(msg):
            # This does not close_resources() and does not clear the stash.
            # That is the job of close() in this race, asserted below.
            release_seen.set()

        controller.media_player._exec_release_stashed_inputs = _record_only_release

        controller.screen_saver.show()
        assert controller.screen_saver.showing is True, "fixture sanity: show() should flip showing"

        # Require the neutered release to run so a missing enqueue cannot make
        # this case pass for the wrong reason.
        assert release_seen.wait(timeout=5), "show() must enqueue the P2.6 release control message"

        # Require the record-only release to leave a non-empty stash for close().
        stashed = controller.screen_saver.original_inputs
        assert stashed.get(Input.Key), (
            "the stash must still be populated at close() time -- the whole "
            "point of this leg is close() sweeping a non-empty stash"
        )
        assert stashed[Input.Key][0] is real_key, "the stash must hold the real pre-show key object"

        # Plant the spy immediately before close so an earlier transient paint
        # cannot change its state.
        spy = _SpyCloseable()
        real_key.get_active_state().key_image = spy
        assert spy.closed is False, "fixture sanity: the freshly-planted spy starts unclosed"

        controller.close(remove_media=True)

        assert spy.closed is True, (
            "close() must close_resources() the stashed inputs when the P2.6 "
            "release never emptied the stash (unplug-races-screensaver)"
        )
        assert real_key.get_active_state().key_image is None, "close()'s sweep must clear the closed reference"
        assert controller.screen_saver.original_inputs == {}, "close() must clear the populated stash"
    finally:
        # Always stop a possibly live media thread after an early assertion.
        fixtures.teardown(controller)
        if controller in gl.deck_manager.deck_controller:
            gl.deck_manager.deck_controller.remove(controller)
    print("PASS: close() sweeps a still-populated screensaver stash (unplug race)")


def main() -> None:
    # Fail before the scenario timeout if close hangs or leaves a live media
    # thread after a leg fails.
    fixtures.start_watchdog(60, label="scenario_deck_close")
    test_double_close_is_safe()
    test_remove_controller_frees_everything()
    test_close_sweeps_screensaver_stash()
    test_close_sweeps_stash_unplug_race()
    # Keep submit-control rejection in its unit-tier scenario because the
    # tier-mixing guard refuses it in this integration process.
    print("PASS: scenario_deck_close")


if __name__ == "__main__":
    main()
