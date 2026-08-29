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

from PIL import Image, ImageDraw

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


def check_dial_video_to_still(controller) -> None:
    # A dial that switches from a video to a still must close and clear the
    # video. The render path draws state.video before state.image, so a
    # leftover video kept playing over the new still and leaked its capture.
    dial = controller.inputs[Input.Dial][0]
    state = dial.get_active_state()

    video_path = make_test_mp4(os.path.join(gl.DATA_PATH, "media", "dial_switch.mp4"))
    video = InputVideo(controller_input=dial, video_path=video_path, natural_speed=True)
    state.set_video(video)
    check("dial set_video stored the video", state.video is video)

    green = make_test_png(os.path.join(gl.DATA_PATH, "media", "dial_still.png"), color=(0, 200, 0))
    with Image.open(green) as img:
        state.set_image(InputImage(controller_input=dial, image=img.copy(), path=green), update=False)
    check("dial set_image cleared the previous video", state.video is None)
    check("dial set_image closed the previous video", getattr(video, "closed", True))
    check("dial set_image stored the still", state.image is not None)


def check_dial_gif_loads(controller) -> None:
    # #390: a GIF assigned to a dial through the page loader raised
    # NotImplementedError. It must build a KeyGIF instead, like a key.
    from src.backend.DeckManagement.deck_controller.gif_pipeline import KeyGIF

    dial = controller.inputs[Input.Dial][0]
    gif_path = os.path.join(gl.DATA_PATH, "media", "dial.gif")
    frames = [Image.new("RGBA", (48, 48), (0, 0, 0, 0)) for _ in range(3)]
    for i, fr in enumerate(frames):
        ImageDraw.Draw(fr).ellipse([2 + i * 3, 8, 22 + i * 3, 28], fill=(220, 30, 30, 255))
    os.makedirs(os.path.dirname(gif_path), exist_ok=True)
    frames[0].save(gif_path, format="GIF", save_all=True, append_images=frames[1:],
                   duration=[100] * 3, loop=0, disposal=2)

    config = {"states": {"0": {"media": {"path": gif_path, "fps": 10, "loop": True}}}}
    try:
        dial.load_from_input_dict(config, update=False)
    except NotImplementedError:
        check("a GIF on a dial no longer raises NotImplementedError", False)
        return
    state = dial.get_active_state()
    check("a dial GIF loaded as a KeyGIF", isinstance(state.video, KeyGIF))
    check("the dial GIF carried its fps cap", getattr(state.video, "fps", None) == 10)


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


def check_action_media_stash_protocol(controller) -> None:
    """The pair a page load uses to carry action-owned media across the wipe.

    One load path serves every input type that stashes, so the two state
    classes it reaches must answer the same pair with the same semantics. A
    detach hands the media out and clears the slot without closing it, because
    the caller puts that same object back. A closing detach, or one that routed
    through set_image, would hand back released media and repaint a state that
    is about to be destroyed.
    """
    key = controller.inputs[Input.Key][0]
    dial = controller.inputs[Input.Dial][0]

    green = make_test_png(
        os.path.join(gl.DATA_PATH, "media", "stash_icon.png"), color=(0, 200, 0))

    for name, controller_input, slot in (("key", key, "key_image"), ("dial", dial, "image")):
        state = controller_input.get_active_state()
        with Image.open(green) as img:
            media = InputImage(controller_input=controller_input, image=img.copy(), path=green)
        state.set_image(media, update=False)
        check(f"{name} set_image stored the media", getattr(state, slot) is media)

        detached_image, detached_video = state.detach_action_media()
        check(f"{name} detach handed back the media", detached_image is media)
        check(f"{name} detach cleared the still slot", getattr(state, slot) is None)
        check(f"{name} detach reported no video", detached_video is None)
        # The caller puts this object back, so the detach must leave it usable.
        check(f"{name} detach left the media open",
              detached_image is not None and detached_image.get_raw_image() is not None,
              "the detach closed media the restore is about to reattach")

        # The owner stamp is not the pair's to clear. The shared load path
        # clears it, and only for a state whose media it actually stashed.
        state.media_owner_action = None

        state.attach_action_media(detached_image, detached_video)
        check(f"{name} attach put the media back", getattr(state, slot) is media)

        state.attach_action_media(None, None)
        check(f"{name} attach clears with a None pair", getattr(state, slot) is None)
        media.close()

    # The touchscreen runs no stashing load, so it inherits the refusing base
    # rather than a silent no-op that would drop media if a load ever reached
    # it. Both halves of the pair must refuse: an attach that returned quietly
    # would silently swallow the media a future loader handed it.
    touch_state = controller.get_input(Input.Touchscreen("sd-plus")).get_active_state()
    detach_raised = False
    try:
        touch_state.detach_action_media()
    except NotImplementedError:
        detach_raised = True
    check("touchscreen state refuses detach", detach_raised,
          "a state class with no stashing load must refuse detach, not answer nothing")

    attach_raised = False
    try:
        touch_state.attach_action_media(None, None)
    except NotImplementedError:
        attach_raised = True
    check("touchscreen state refuses attach", attach_raised,
          "a state class with no stashing load must refuse attach, not swallow media")


def main() -> None:
    start_watchdog(WATCHDOG_SECONDS, label="scenario_input_state_api_gaps")
    controller = fixtures.make_headless_controller(serial="api-gaps-1")
    try:
        # Let the controller settle its first page load before touching state.
        settle_inputs(controller)
        check_dial_clear(controller)
        check_dial_video_to_still(controller)
        check_dial_gif_loads(controller)
        check_touchscreen_media(controller)
        check_action_media_stash_protocol(controller)
    finally:
        teardown(controller)

    assert not FAILURES, f"ControllerInputState API gaps remain: {FAILURES}"
    print("PASS: scenario_input_state_api_gaps")


if __name__ == "__main__":
    main()
