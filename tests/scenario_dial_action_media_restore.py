"""Require action-owned dial media to survive state recreation during input load.
A latched action does not repaint, so ownership must support stash and restore.
"""

# Paint through a latched action, then synchronously reload the same config;
# only stash and restore can preserve the exact media object.
import json
import os

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl
from fixtures import make_test_png, start_watchdog, teardown, wait_until

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore

WATCHDOG_SECONDS = 60


def seed_dial_action_page(page_name: str, dial_index: str) -> str:
    """Seed a page whose one dial carries the latch action as its image
    control. The dial twin of fixtures.seed_action_page, under the dials type."""
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({"dials": {dial_index: {"states": {"0": {
            "actions": [{"id": fixtures.STUB_ACTION_ID, "settings": {}}],
            "image-control-action": 0,
        }}}}}, f)
    return path


def ready_action(state) -> ActionCore | None:
    for action in state.get_own_actions():
        if isinstance(action, ActionCore) and action.on_ready_finished:
            return action
    return None


def main() -> None:
    latch_cls = fixtures.make_latch_action_class()
    icon_path = make_test_png(
        os.path.join(gl.DATA_PATH, "media", "dial_icon.png"), color=(0, 200, 0))
    fixtures.install_stub_plugin_manager(latch_cls, icon_path)
    start_watchdog(WATCHDOG_SECONDS, label="scenario_dial_action_media_restore")

    controller = fixtures.make_headless_controller(serial="dial-media-1")
    try:
        dial = controller.inputs[Input.Dial][0]
        dial_ident = dial.identifier.json_identifier

        action_page = gl.page_manager.get_page(
            seed_dial_action_page("DialLatch", dial_ident), controller)
        controller.load_page(action_page, allow_reload=True)

        # Wait for the dial's action to finish its ready sequence, then drive
        # the paint from this thread, off the async race.
        wait_until(lambda: ready_action(dial.get_active_state()) is not None, timeout=5)
        state = dial.get_active_state()
        action = ready_action(state)
        assert action is not None, "the dial action never became ready in this harness"

        # Latch the action so a later on_update dedups and cannot repaint, and
        # paint the dial once through the real set_media path.
        action.current_state = 1
        action.set_media(media_path=icon_path, size=0.8, update=False)

        assert state.image is not None, "set_media did not paint the dial image"

        # Part 1: set_media stamps the owning action on a dial state. Without
        # the stamp the restore below has nothing to key on.
        assert state.media_owner_action is action, (
            "set_media painted the dial but did not stamp the owning action -- "
            "ActionCore._stamp_media_owner does not stamp dial states")

        media_before = state.image

        # Reload synchronously through create_n_states and stash-and-restore;
        # the latched action's on_update does not repaint.
        config = dial.identifier.get_config(controller.active_page)
        dial.load_from_input_dict(config, page=controller.active_page)

        new_state = dial.get_active_state()
        assert new_state is not state, (
            "the reload did not rebuild the dial state; the wipe never ran")

        # Require stash-and-restore to carry the exact object because the
        # latched action leaves a recreated state blank.
        assert new_state.image is media_before, (
            "the dial media did not survive the reload -- create_n_states wiped "
            "the action-owned image and the stash-and-restore did not carry it")
        assert new_state.media_owner_action is action, (
            "the restored dial media lost or changed its owner stamp")

        print("PASS: scenario_dial_action_media_restore")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
