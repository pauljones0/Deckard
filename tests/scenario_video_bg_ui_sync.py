"""Verify that update_all_inputs synchronizes every in-app key preview while a
background video is active."""

# No UI is attached here, so the null port refuses each push and
# set_ui_key_image stores a dirty marker per key instead.
import fixtures
from src.backend.DeckManagement.InputIdentifier import Input


class _FakeBGVideo:
    """Truthy stand-in for a decoded background video (the real object is a
    BackgroundVideo). Only .close() is ever touched here, by teardown."""

    def close(self):
        pass


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_video_bg_ui_sync")
    controller = fixtures.make_headless_controller(serial="uisync-1")
    try:
        tasks = controller.ui_image_changes_while_hidden
        key_ids = {i.identifier for i in controller.inputs[Input.Key]}
        assert key_ids, "fixture sanity: expected key inputs"

        # Stand in a background video so update_all_inputs takes the
        # "don't disturb the video" branch.
        controller.background.video = _FakeBGVideo()

        tasks.clear()
        controller.update_all_inputs()

        marked = {k for k in tasks if k in key_ids}
        assert marked == key_ids, (
            f"update_all_inputs with a background video synced only "
            f"{len(marked)}/{len(key_ids)} keys' in-app previews -- the skipped "
            f"keys would stay stale/black in the app while the deck is correct "
            f"(missing: {sorted(str(k) for k in key_ids - marked)[:8]})"
        )
        print(f"PASS: all {len(key_ids)} keys' in-app previews synced despite the background video")
    finally:
        controller.background.video = None
        fixtures.teardown(controller)

    print("PASS: scenario_video_bg_ui_sync")


if __name__ == "__main__":
    main()
