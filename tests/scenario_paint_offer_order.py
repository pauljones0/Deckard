"""A slower paint of an older state must not take the writer's slot.

Several threads paint one input. Each reads the state, composes a picture from
it, then offers that picture to the writer, whose per-target slot keeps the
offer that arrives last. Compose order and offer order are independent unless
something holds them together, so a slow compose of an older state can offer
after a fast compose of a newer one and overwrite it. The offer stamps its own
hash as in flight while it does, so no later paint corrects the slot: nothing
about the input has changed. The key then shows the older picture until
something else on it moves.

The legs make that interleaving exact. One thread composes the old picture and
stalls before it offers. The main thread changes the label and paints. The last
offer to reach the writer must show the new label. A key and the touch strip
each get a leg, because each owns its own paint lock: the strip is the harder
target, since every dial and an extended background video converge on the one
strip slot.
"""
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
        # Compose first, so the picture this paint carries is the one the
        # state held before the label moved. The stall then models a paint
        # whose compose was fast and whose offer is late.
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
    """The strip twin of leg_older_paint_never_wins.

    Every dial composites into the one strip slot, so a dial's label is part of
    the strip composite. One thread composes the strip with the old dial label
    and stalls before it offers; the main thread moves the label and paints the
    strip. The last touchscreen task the writer receives must carry the new
    label. Without the strip's own paint lock the stalled paint offers last and
    the strip holds the pre-edit picture.
    """
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
        # Compose first, so the picture this paint carries reads the dial label
        # from before it moved. The stall then models a paint whose compose was
        # fast and whose offer is late.
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
    """A paint must not hold the lock past its own body.

    The input callback paints and then dispatches action events. A lock still
    held there would serialize plugin callbacks behind every repaint of the
    same input, and a callback that paints that input would depend on
    re-entrancy to survive at all.

    The probe runs on a second thread. _paint_lock is re-entrant, so a
    non-blocking acquire on the thread that owns it, or that just ran the paint,
    returns True even while the lock is held. A separate thread sees the real
    state, so a paint that leaked its lock, or never released it, blocks the
    probe and fails the leg.
    """
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
