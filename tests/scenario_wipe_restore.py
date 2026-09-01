"""Detach owned media before create_n_states and restore it only to the same action."""

# The blank only appears through the async load pipeline, so the check runs
# bounded trials with a wait_until seam.
import os

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl
from fixtures import start_watchdog, wait_until, teardown

from src.backend.DeckManagement.InputIdentifier import Input

WATCHDOG_SECONDS = 60
# The measured per-trial blank rate is about 0.93, so P(no blank) is about
# 0.07 ** TRIALS. Eight trials put a miss below one in ten million.
TRIALS = 8


def main() -> None:
    latch_cls = fixtures.make_latch_action_class()
    icon_path = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "wipe_icon.png"), color=(0, 200, 0))
    fixtures.install_stub_plugin_manager(latch_cls, icon_path)
    start_watchdog(WATCHDOG_SECONDS, label="scenario_wipe_restore")

    controller = fixtures.make_headless_controller(serial="wipe-restore-1")
    try:
        key = controller.inputs[Input.Key][0]
        key_ident = key.identifier.json_identifier

        def active_image():
            return key.get_active_state().key_image

        blanks = []
        for i in range(TRIALS):
            # Race the action-executor set_media call against create_n_states
            # on the page-load thread with a fresh page each trial.
            action_page = gl.page_manager.get_page(
                fixtures.seed_action_page(f"LatchR{i}", key_ident), controller)
            controller.load_page(action_page, allow_reload=True)
            # Poll the image seam because a fixed sleep cannot order both threads.
            # Deduplicated on_update does not provide a later repaint.
            painted = wait_until(lambda: active_image() is not None, timeout=3)
            if not painted:
                blanks.append(i)

        # The pinned assertion. With the identity-gated stash-and-restore
        # in place, no trial may settle blank.
        assert not blanks, (
            f"the action-control key settled BLANK on {len(blanks)}/{TRIALS} "
            f"loads ({blanks}) -- create_n_states wiped the action-owned image "
            "and the deduping on_update never restored it"
        )
        print("PASS: scenario_wipe_restore")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
