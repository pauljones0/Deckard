"""Verify that ActionCore.set_media paints media through the public touchscreen
action interface."""
import json
import os

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl
from fixtures import make_test_png, start_watchdog, teardown, wait_until

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore

WATCHDOG_SECONDS = 60


def seed_touchscreen_action_page(page_name: str, ts_index: str) -> str:
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({"touchscreens": {ts_index: {"states": {"0": {
            "actions": [{"id": fixtures.STUB_ACTION_ID, "settings": {}}],
            "image-control-action": 0,
        }}}}}, f)
    return path


def ready_action(state) -> "ActionCore | None":
    for action in state.get_own_actions():
        if isinstance(action, ActionCore) and action.on_ready_finished:
            return action
    return None


def main() -> int:
    latch_cls = fixtures.make_latch_action_class()
    icon_path = make_test_png(
        os.path.join(gl.DATA_PATH, "media", "ts_icon.png"), color=(0, 180, 220))
    fixtures.install_stub_plugin_manager(latch_cls, icon_path)
    start_watchdog(WATCHDOG_SECONDS, label="scenario_touchscreen_set_media")

    controller = fixtures.make_headless_controller(serial="ts-set-media-1")
    failures: list[str] = []
    try:
        touch = controller.get_input(Input.Touchscreen("sd-plus"))
        ts_ident = touch.identifier.json_identifier

        action_page = gl.page_manager.get_page(
            seed_touchscreen_action_page("TSLatch", ts_ident), controller)
        controller.load_page(action_page, allow_reload=True)

        wait_until(lambda: ready_action(touch.get_active_state()) is not None, timeout=5)
        state = touch.get_active_state()
        action = ready_action(state)
        if action is None:
            print("FAIL: the touchscreen action never became ready in this harness")
            return 1

        # Drive media through the real public seam, not the state object.
        state.image = None
        action.set_media(media_path=icon_path, update=False)

        if state.image is None:
            failures.append("ActionCore.set_media stored nothing on the touchscreen "
                            "state -- the seam still returns early for a touchscreen")
        else:
            painted = state.get_current_image()
            state.image = None
            if painted.tobytes() == state.get_current_image().tobytes():
                failures.append("the media set through the seam did not change the "
                                "touchscreen composite")
    finally:
        teardown(controller)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: ActionCore.set_media paints a touchscreen action's media")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
