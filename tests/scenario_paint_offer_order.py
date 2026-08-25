"""A slower paint of an older state must not take the writer's slot.

Several threads paint one input. Each reads the state, composes a picture from
it, then offers that picture to the writer, whose per-target slot keeps the
offer that arrives last. Compose order and offer order are independent unless
something holds them together, so a slow compose of an older state can offer
after a fast compose of a newer one and overwrite it. The offer stamps its own
hash as in flight while it does, so no later paint corrects the slot: nothing
about the input has changed. The key then shows the older picture until
something else on it moves.

The leg makes that interleaving exact. One thread composes the old picture and
stalls before it offers. The main thread changes the label and paints. The last
offer to reach the writer must show the new label.
"""
import threading
import time

import fixtures

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.deck_controller.inputs import ControllerKey

STALL_S = 0.3


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


def leg_paint_lock_is_released_before_dispatch(controller) -> None:
    """A paint must not hold the lock past its own body.

    The input callback paints and then dispatches action events. A lock still
    held there would serialize plugin callbacks behind every repaint of the
    same key, and a callback that paints that key would depend on re-entrancy
    to survive at all.
    """
    key = controller.get_input(Input.Key("0x0"))
    key.update()
    assert key._paint_lock.acquire(blocking=False), \
        "update() returned with the paint lock still held"
    key._paint_lock.release()

    touchscreen = controller.get_input(Input.Touchscreen("sd-plus"))
    assert touchscreen is not None
    touchscreen.update()
    assert touchscreen._paint_lock.acquire(blocking=False), \
        "the touchscreen update() returned with the paint lock still held"
    touchscreen._paint_lock.release()

    print("PASS: a paint releases its lock before it returns")


def main() -> None:
    fixtures.start_watchdog(120, label="scenario_paint_offer_order")
    controller = fixtures.make_headless_controller(serial="paint-order-1")
    try:
        leg_older_paint_never_wins(controller)
        leg_paint_lock_is_released_before_dispatch(controller)
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_paint_offer_order")


if __name__ == "__main__":
    main()
