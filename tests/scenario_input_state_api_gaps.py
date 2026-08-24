"""
Closes the ControllerInputState plugin-API gaps.

Three state classes must present the same media protocol. A dial state must
implement clear(), so ControllerInput.clear() does not crash on a dial. A
touchscreen state must implement set_image() and set_video(), so a plugin that
drives touchscreen media stores and paints it instead of raising.
"""

# The dial clear() mirrors ControllerKeyState.clear(): it releases the media
# and resets the page-owned layers. The touchscreen set_image/set_video mirror
# the key and dial slots and compose over the strip background.
import os

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl
from fixtures import make_test_mp4, make_test_png, start_watchdog, teardown, wait_until

from PIL import Image

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo

WATCHDOG_SECONDS = 60

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def settle_inputs(controller) -> None:
    """Wait until the async input load stops replacing state objects.

    make_headless_controller returns with a page loaded, but background load
    threads can rebuild an input's states once more. Capturing a state before
    that settles would paint a stale object. This waits until two reads of a
    dial's active state a moment apart return the same object.
    """
    wait_until(lambda: controller.active_page is not None, timeout=5)
    dial = controller.inputs[Input.Dial][0]
    prev: list = [object()]

    def stable() -> bool:
        cur = dial.get_active_state()
        same = cur is prev[0]
        prev[0] = cur
        return same

    wait_until(stable, timeout=5, interval=0.1)


def check_dial_clear(controller) -> None:
    dial = controller.inputs[Input.Dial][0]
    state = dial.get_active_state()

    green = make_test_png(os.path.join(gl.DATA_PATH, "media", "dial_icon.png"), color=(0, 200, 0))
    with Image.open(green) as img:
        state.set_image(InputImage(controller_input=dial, image=img.copy(), path=green), update=False)
    check("dial set_image stored the image", state.image is not None)

    # ControllerInput.clear() drives active_state.clear(). Before the fix a
    # dial state had no clear() and this raised AttributeError.
    dial.clear(update=False)
    check("dial clear() released the media", state.image is None and state.video is None)
    check("dial clear() reset the media owner", state.media_owner_action is None)


def check_touchscreen_media(controller) -> None:
    touch = controller.get_input(Input.Touchscreen("sd-plus"))
    state = touch.get_active_state()

    baseline = state.get_current_image()
    baseline_bytes = baseline.tobytes()

    # set_image on a touchscreen state raised NotImplementedError before the
    # fix (it inherited the base declaration). It must store and paint now.
    green = make_test_png(
        os.path.join(gl.DATA_PATH, "media", "strip_icon.png"),
        size=touch.get_screen_dimensions(), color=(0, 220, 0),
    )
    with Image.open(green) as img:
        state.set_image(InputImage(controller_input=touch, image=img.copy(), path=green), update=False)
    check("touchscreen set_image stored the image", state.image is not None)

    painted = state.get_current_image()
    check("touchscreen media changes the composite", painted.tobytes() != baseline_bytes,
          "get_current_image is identical with and without the media -- set_image is a no-op")

    # set_video must store an animated provider and switch off the still image.
    video_path = make_test_mp4(os.path.join(gl.DATA_PATH, "media", "strip.mp4"))
    state.set_video(InputVideo(controller_input=touch, video_path=video_path, natural_speed=True))
    check("touchscreen set_video stored the video", state.video is not None)
    check("touchscreen set_video cleared the still image", state.image is None)

    with_video = state.get_current_image()
    check("touchscreen video composites without raising",
          isinstance(with_video, Image.Image) and with_video.size == touch.get_screen_dimensions())

    # close_resources releases both media slots and is safe to repeat.
    state.close_resources()
    check("touchscreen close_resources released the media", state.image is None and state.video is None)
    state.close_resources()


def main() -> None:
    start_watchdog(WATCHDOG_SECONDS, label="scenario_input_state_api_gaps")
    controller = fixtures.make_headless_controller(serial="api-gaps-1")
    try:
        # Let the controller settle its first page load before touching state.
        settle_inputs(controller)
        check_dial_clear(controller)
        check_touchscreen_media(controller)
    finally:
        teardown(controller)

    assert not FAILURES, f"ControllerInputState API gaps remain: {FAILURES}"
    print("PASS: scenario_input_state_api_gaps")


if __name__ == "__main__":
    main()
