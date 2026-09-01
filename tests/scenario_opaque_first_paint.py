"""Paint fully opaque keys immediately under video while other keys stay preview-only.
Alpha 254 must remain on the video path, and a direct update must still paint it."""
import time

import fixtures
# The trio's expected-native helper, shared rather than copied. Importing it
# only defines helpers, because its own legs run under its __main__ guard.
import scenario_deck_lifecycle_trio as trio
from src.backend.DeckManagement.InputIdentifier import Input


class _FakeBGVideo:
    """Use a non-active page so the live media loop performs no device writes.
    This leaves only writes requested by the code under test."""

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

        # Give all keys new content; alpha 254 remains owned by the video loop.
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

        # A direct boundary-key update proves prior silence came from opacity routing, not dedup.
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
