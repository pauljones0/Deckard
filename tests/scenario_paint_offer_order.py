"""Verify key and touch-strip paint locks preserve compose-to-offer order.
A stalled old composite must not replace a newer offer, and dispatch must receive released locks."""
import threading
import time

import fixtures

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.deck_controller.inputs import ControllerKey
from src.backend.DeckManagement.deck_controller.input_state_classes import (
    ControllerTouchScreenState,
)

STALL_S = 0.3


def set_dial_label(dial, text: str) -> None:
    """Write a dial's page label without a repaint. A dial composites into the
    shared strip, so its label is part of the strip composite the leg races."""
    state = dial.get_active_state()
    state.label_manager.set_page_label(
        "center",
        KeyLabel(controller_input=dial, text=text, font_size=14,
                 color=[255, 255, 255, 255]),
        update=False)


def set_label(key, text: str) -> None:
    """Write a page label without a repaint, so the leg decides when a paint
    happens."""
    state = key.get_active_state()
    state.label_manager.set_page_label(
        "center",
        KeyLabel(controller_input=key, text=text, font_size=14,
                 color=[255, 255, 255, 255]),
        update=False)


def leg_older_paint_never_wins(controller) -> None:
    key = controller.get_input(Input.Key("0x0"))
    assert key is not None
    assert controller.is_visual(), "the fake deck must have screens for an offer to run"

    set_label(key, "OLD")

    offers: list = []
    real_add_image_task = controller.media_player.add_image_task

    def recording_add_image_task(key_index, native_image, **kwargs):
        offers.append((key_index, kwargs.get("img_hash")))
        return real_add_image_task(key_index, native_image, **kwargs)

    in_stall = threading.Event()
    go = threading.Event()
    stalled_once = threading.Event()
    real_get_current_image = ControllerKey.get_current_image

    def stalling_get_current_image(self):
        # Compose before the label changes, then stall to make the old
        # composite reach the offer stage late.
        image = real_get_current_image(self)
        if self is key and not stalled_once.is_set():
            stalled_once.set()
            in_stall.set()
            go.wait(20)
            time.sleep(STALL_S)
        return image

    controller.media_player.add_image_task = recording_add_image_task
    ControllerKey.get_current_image = stalling_get_current_image
    try:
        worker = threading.Thread(target=key.update, name="older-paint", daemon=True)
        worker.start()
        assert in_stall.wait(20), "the stalling paint never composed"

        # The state moves on while that paint is still in flight.
        set_label(key, "NEW")
        go.set()

        # The newer paint. Without the compose-to-offer lock it runs to
        # completion here and the stalled paint offers after it.
        key.update()
        worker.join(20)
        assert not worker.is_alive(), "the stalled paint never finished"
    finally:
        ControllerKey.get_current_image = real_get_current_image
        controller.media_player.add_image_task = real_add_image_task

    key_offers = [img_hash for index, img_hash in offers if index == key.index]
    assert len(key_offers) >= 2, (
        "the leg needs both paints to reach the writer, so the order between "
        f"them can be judged: {key_offers}")

    with real_get_current_image(key) as current:
        expected = hash(current.tobytes())
    assert key_offers[-1] == expected, (
        "an older paint took the writer's slot from a newer one: the last "
        "offer does not show the label the key carries now, so the deck holds "
        "the pre-edit picture and no repaint corrects it")

    print("PASS: the newest composite is the last offer the writer receives")


def leg_older_strip_paint_never_wins(controller) -> None:
    """Verify a stalled strip composite cannot replace a newer strip offer.
    Every dial converges on the shared strip slot, which has its own paint lock."""
    touchscreen = controller.get_input(Input.Touchscreen("sd-plus"))
    dial = controller.inputs[Input.Dial][0]
    assert touchscreen is not None and dial is not None
    assert controller.is_visual(), "the fake deck must have screens for an offer to run"

    set_dial_label(dial, "OLD")

    offers: list = []
    real_add_touchscreen_task = controller.media_player.add_touchscreen_task

    def recording_add_touchscreen_task(native_image, **kwargs):
        offers.append(kwargs.get("img_hash"))
        return real_add_touchscreen_task(native_image, **kwargs)

    in_stall = threading.Event()
    go = threading.Event()
    stalled_once = threading.Event()
    real_get_current_image = ControllerTouchScreenState.get_current_image

    def stalling_get_current_image(self):
        # Compose before the dial label changes, then stall to make the old
        # strip composite reach the offer stage late.
        image = real_get_current_image(self)
        if self is touchscreen.get_active_state() and not stalled_once.is_set():
            stalled_once.set()
            in_stall.set()
            go.wait(20)
            time.sleep(STALL_S)
        return image

    controller.media_player.add_touchscreen_task = recording_add_touchscreen_task
    ControllerTouchScreenState.get_current_image = stalling_get_current_image
    try:
        worker = threading.Thread(target=touchscreen.update, name="older-strip-paint", daemon=True)
        worker.start()
        assert in_stall.wait(20), "the stalling strip paint never composed"

        # The dial label moves while that paint is still in flight.
        set_dial_label(dial, "NEW")
        go.set()

        # The newer strip paint. Without the compose-to-offer lock it runs to
        # completion here and the stalled paint offers after it.
        touchscreen.update()
        worker.join(20)
        assert not worker.is_alive(), "the stalled strip paint never finished"
    finally:
        ControllerTouchScreenState.get_current_image = real_get_current_image
        controller.media_player.add_touchscreen_task = real_add_touchscreen_task

    assert len(offers) >= 2, (
        "the leg needs both strip paints to reach the writer, so the order "
        f"between them can be judged: {offers}")

    with real_get_current_image(touchscreen.get_active_state()) as current:
        expected = hash(current.tobytes())
    assert offers[-1] == expected, (
        "an older strip paint took the writer's slot from a newer one: the "
        "last touchscreen task does not show the dial label the strip carries "
        "now, so the deck holds the pre-edit strip and no repaint corrects it")

    print("PASS: the newest strip composite is the last offer the writer receives")


def leg_paint_lock_is_released_before_dispatch(controller) -> None:
    """Verify a paint releases its RLock before callback dispatch.
    The non-blocking probe uses a second thread because the lock owner can reacquire an RLock."""
    def probe(lock) -> bool:
        got: list = []

        def run() -> None:
            acquired = lock.acquire(timeout=5)
            got.append(acquired)
            if acquired:
                # Release on this same thread. An RLock is owned by whoever
                # took it, and a release from another thread raises.
                lock.release()

        t = threading.Thread(target=run, name="paint-lock-probe")
        t.start()
        t.join(10)
        assert not t.is_alive(), "the lock probe thread never returned"
        return bool(got and got[0])

    key = controller.get_input(Input.Key("0x0"))
    key.update()
    assert probe(key._paint_lock), \
        "update() returned with the key paint lock still held"

    touchscreen = controller.get_input(Input.Touchscreen("sd-plus"))
    assert touchscreen is not None
    touchscreen.update()
    assert probe(touchscreen._paint_lock), \
        "the touchscreen update() returned with the paint lock still held"

    print("PASS: a paint releases its lock before it returns")


def main() -> None:
    fixtures.start_watchdog(120, label="scenario_paint_offer_order")
    controller = fixtures.make_headless_controller(serial="paint-order-1")
    try:
        leg_older_paint_never_wins(controller)
        leg_older_strip_paint_never_wins(controller)
        leg_paint_lock_is_released_before_dispatch(controller)
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_paint_offer_order")


if __name__ == "__main__":
    main()
