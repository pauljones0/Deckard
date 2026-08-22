"""update_all_inputs must paint an opaque key on the device under a video.

With a background video set, update_all_inputs leaves the keys to the
per-frame video loop and syncs the in-app previews only. That loop never
repaints a key whose composed color is fully opaque, because an opaque tile
hides the video and nothing about that key changes between frames. So the
opaque key alone gets its device paint here, on the spot, or the deck keeps
the previous page's content on it until someone presses it.

A key that is not fully opaque must stay with the video loop and take the
preview-only path, even when its content just changed.

This overlaps scenario_deck_lifecycle_trio.check_opaque_initial_paint on
purpose, and neither covers the other. That one drives a stopped writer and
a hand-drained queue, and proves the content assertion is not vacuous by
varying the color. This one runs against the live writer, pins the alpha
boundary one step below opaque, and proves the silence on the non-opaque
keys is the branch and not a hash skip, by repainting one of them directly.
"""
import time

import fixtures
# The trio's expected-native helper, shared rather than copied. Importing it
# only defines helpers, because its own legs run under its __main__ guard.
import scenario_deck_lifecycle_trio as trio
from src.backend.DeckManagement.InputIdentifier import Input


class _FakeBGVideo:
    """Truthy stand-in for a decoded background video (the real object is a
    BackgroundVideo).

    page is deliberately not the deck's active page, so the media loop's own
    frame branch treats it as belonging elsewhere and renders nothing from
    it. Every device write in this scenario is then one the code under test
    asked for. Only .close() is touched, by teardown.
    """

    page = None

    def close(self):
        pass


def key_writes_after(deck, seq: int, index: int) -> list:
    return [e for e in deck.ops_after(seq)
            if e[2] == "set_key_image" and e[3] == f"key:{index}"]


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_opaque_first_paint")
    controller = fixtures.make_headless_controller(serial="opaque-1")
    deck = fixtures.raw_deck(controller)
    try:
        keys = controller.inputs[Input.Key]
        assert len(keys) >= 3, "fixture sanity: expected at least three keys"
        opaque, boundary, translucent = keys[0], keys[1], keys[2]

        # Let the default page's own paints land, so the writes below are the
        # ones this scenario caused.
        fixtures.wait_until(
            lambda: deck.last_op_for(f"key:{opaque.index}") is not None, timeout=3)
        time.sleep(0.1)

        # All three keys get new content. Only the first is fully opaque, and
        # the second sits one step below it, where the video loop still owns
        # the key.
        opaque.get_active_state().background_manager.set_page_color(
            [10, 20, 30, 255], update=False)
        boundary.get_active_state().background_manager.set_page_color(
            [200, 50, 50, 254], update=False)
        translucent.get_active_state().background_manager.set_page_color(
            [200, 50, 50, 128], update=False)

        controller.background.video = _FakeBGVideo()

        # What the device must receive for the opaque key: the new color, not
        # whatever it held before.
        expected_hash = trio._expected_native_hash(controller, opaque)

        seq_before = deck.current_seq()
        controller.update_all_inputs()

        ok = fixtures.wait_until(
            lambda: key_writes_after(deck, seq_before, opaque.index), timeout=3)
        assert ok, (
            "an opaque key must be painted on the device by update_all_inputs "
            "under a background video -- the per-frame video loop skips it, so "
            "nothing else would ever write it"
        )
        written_hash = key_writes_after(deck, seq_before, opaque.index)[-1][4]
        assert written_hash == expected_hash, (
            f"the opaque key was painted with the wrong content (journal "
            f"{written_hash} != expected {expected_hash} for its new color) -- "
            f"a stale frame reached the device"
        )

        time.sleep(0.3)  # let a would-be regression's write land
        for key, alpha in ((boundary, 254), (translucent, 128)):
            extra = key_writes_after(deck, seq_before, key.index)
            assert not extra, (
                f"a key at alpha {alpha} is not fully opaque, so it must keep "
                f"the preview-only path under a background video and not be "
                f"written here, got {len(extra)} device write(s)"
            )

        # Control leg. The silence above must come from the opacity branch and
        # not from dedup: the boundary key's content is new, so its own
        # update() writes it.
        seq_before_direct = deck.current_seq()
        boundary.update()
        ok = fixtures.wait_until(
            lambda: key_writes_after(deck, seq_before_direct, boundary.index),
            timeout=3)
        assert ok, (
            "fixture sanity: the boundary key's content changed, so a direct "
            "update() must reach the device (otherwise the assertion above "
            "proves nothing)"
        )
    finally:
        controller.background.video = None
        fixtures.teardown(controller)

    print("PASS: scenario_opaque_first_paint")


if __name__ == "__main__":
    main()
