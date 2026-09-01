"""Verify coherent slots when producers race drain, Clear, and write-cap
putback; verify both slot wipes and page-generation-before-slot lock ordering."""

# A hooked touchscreen_task property fires a real producer inside each window,
# so every interleave is deterministic rather than left to the scheduler.
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time

from fixtures import start_watchdog


def install_touchscreen_read_hooks(media_player):
    """Install a touchscreen-task property that fires an armed read hook."""
    # Return the pre-producer value to expose a deterministic check-and-set race.
    base = type(media_player)

    class Hooked(base):
        @property
        def touchscreen_task(self):
            value = self.__dict__.get("_ts_slot")
            on_hook_thread = threading.current_thread() is self.__dict__.get("_hook_thread")
            if on_hook_thread:
                nth = self.__dict__.get("_read_hook_on_nth")
                if nth is not None:
                    count = self.__dict__.get("_read_count", 0) + 1
                    self.__dict__["_read_count"] = count
                    target_n, target_hook = nth
                    if count == target_n:
                        self.__dict__["_read_hook_on_nth"] = None
                        # Return the value captured before the producer ran.
                        target_hook()
                        return value
                hook = self.__dict__.get("_read_hook")
                if hook is not None:
                    self.__dict__["_read_hook"] = None
                    hook()
            return value

        @touchscreen_task.setter
        def touchscreen_task(self, value):
            self.__dict__["_ts_slot"] = value

    media_player.__dict__["_ts_slot"] = media_player.__dict__.pop("touchscreen_task", None)
    media_player.__dict__["_read_hook"] = None
    media_player.__dict__["_read_hook_on_nth"] = None
    media_player.__dict__["_read_count"] = 0
    media_player.__dict__["_hook_thread"] = None
    media_player.__class__ = Hooked
    return media_player


def check_touchscreen_drain_race() -> int:
    from src.backend.DeckManagement.InputIdentifier import Input

    controller, media_player, _ = fixtures.make_stub_controller(
        serial="slotrace-1", has_touchscreen=True
    )
    touch = controller.inputs[Input.Touchscreen][0]
    media_player = install_touchscreen_read_hooks(media_player)

    produced = threading.Event()

    def producer():
        media_player.add_touchscreen_task(
            b"\x42" * 64,
            page=controller.active_page,
            config_gen=controller._page_load_generation,
            present=touch.present_state,
            img_hash=4242,
        )
        produced.set()

    def on_drain_read():
        t = threading.Thread(target=producer, daemon=True)
        t.start()
        # Give the producer a real chance to land inside the read-and-null
        # window. With the lock in place it blocks there instead.
        time.sleep(0.25)

    # Seed an old frame so the drain has something to read.
    media_player.add_touchscreen_task(
        b"\x01" * 64,
        page=controller.active_page,
        config_gen=controller._page_load_generation,
        present=touch.present_state,
        img_hash=1,
    )

    media_player.__dict__["_read_hook"] = on_drain_read
    media_player.__dict__["_hook_thread"] = threading.current_thread()
    media_player.perform_media_player_tasks()

    if not produced.wait(timeout=5):
        print("FAIL(1): producer never completed (deadlock?)")
        return 1
    # Let a blocked producer land after the drain released the lock.
    time.sleep(0.1)

    survivor = media_player.__dict__.get("_ts_slot")
    if survivor is None or survivor.ticket.img_hash != 4242:
        print("FAIL(1): the frame produced during the drain window was lost "
              "(slot nulled over it) -- a static strip would stay stale "
              "forever")
        return 1
    print("PASS: producer frame in the drain window survives the read->null")
    return 0


def check_image_clear_race() -> int:
    from src.backend.DeckManagement.InputIdentifier import Input
    from src.backend.DeckManagement.DeckController import ClearMsg

    controller, media_player, _ = fixtures.make_stub_controller(
        serial="slotrace-2", n_keys=3, has_touchscreen=True
    )
    key0 = controller.inputs[Input.Key][0]

    def add_key_frame(payload: bytes):
        media_player.add_image_task(
            0, payload,
            page=controller.active_page,
            config_gen=controller._page_load_generation,
            present=key0.present_state,
            img_hash=hash(payload),
        )

    add_key_frame(b"\x01" * 64)  # this frame predates the Clear
    clear_seq = media_player.next_submit_seq()

    produced = threading.Event()

    class HookedDict(dict):
        armed = [True]

        def get(self, key, default=None):
            value = super().get(key, default)
            if self.armed[0]:
                self.armed[0] = False

                def producer():
                    add_key_frame(b"\x99" * 64)  # newer, so it survives the Clear
                    produced.set()

                t = threading.Thread(target=producer, daemon=True)
                t.start()
                time.sleep(0.25)
            return value

    hooked = HookedDict(media_player.image_tasks)
    media_player.image_tasks = hooked

    media_player._exec_clear(ClearMsg(seq=clear_seq))

    if not produced.wait(timeout=5):
        print("FAIL(2): producer never completed (deadlock?)")
        return 1
    time.sleep(0.1)

    survivor = media_player.image_tasks.get(0)
    if survivor is None or survivor.ticket.img_hash != hash(b"\x99" * 64):
        print("FAIL(2): _exec_clear deleted a newer task whose submit_seq "
              "contractually survives the Clear")
        return 1
    print("PASS: newer image task survives a racing Clear")
    return 0


def check_write_cap_putback_race() -> int:
    """A newer producer frame wins an atomic over-budget putback race."""
    from src.backend.DeckManagement.InputIdentifier import Input

    controller, media_player, _ = fixtures.make_stub_controller(
        serial="slotrace-3", has_touchscreen=True
    )
    touch = controller.inputs[Input.Touchscreen][0]
    media_player = install_touchscreen_read_hooks(media_player)

    # A recent write forces the seeded frame into the over-budget putback.
    media_player._last_touch_write = time.time()

    produced = threading.Event()

    def producer():
        # A newer frame lands between the putback's None check and its set.
        media_player.add_touchscreen_task(
            b"\x99" * 64,
            page=controller.active_page,
            config_gen=controller._page_load_generation,
            present=touch.present_state,
            img_hash=9999,
        )
        produced.set()

    def on_putback_read():
        t = threading.Thread(target=producer, daemon=True)
        t.start()
        # Give the producer a real chance to land inside the check-and-set
        # window. It blocks on the slot lock the putback holds instead.
        time.sleep(0.25)

    # Seed the old frame the drain reads, nulls and then tries to defer.
    media_player.add_touchscreen_task(
        b"\x01" * 64,
        page=controller.active_page,
        config_gen=controller._page_load_generation,
        present=touch.present_state,
        img_hash=1,
    )

    # Fire on the second slot read of the tick. The first is the drain's
    # null, and the second is the putback's None check.
    media_player.__dict__["_read_hook_on_nth"] = (2, on_putback_read)
    media_player.__dict__["_hook_thread"] = threading.current_thread()
    media_player.perform_media_player_tasks()

    if not produced.wait(timeout=5):
        print("FAIL(3): producer never completed (deadlock?)")
        return 1
    # Let a blocked producer land after the putback released the lock.
    time.sleep(0.1)

    survivor = media_player.__dict__.get("_ts_slot")
    if survivor is None or survivor.ticket.img_hash != 9999:
        print("FAIL(3): the newer frame produced in the putback check->set "
              "window was lost (clobbered by the older deferred frame)")
        return 1

    # The over-budget old frame must have been deferred rather than written,
    # so the rate limit holds.
    ts_writes = controller.deck.ops_by_name("set_touchscreen_image")
    if ts_writes:
        print(f"FAIL(3): the deferred over-budget frame was written to the "
              f"device ({len(ts_writes)} touchscreen write(s)) -- the write-cap "
              f"rate-limit was not preserved")
        return 1
    print("PASS: newer frame survives the write-cap putback; deferred frame "
          "not written (rate-limit preserved)")
    return 0


def check_slot_wipes() -> int:
    """Directly clear through both slot-wipe paths and exercise the one nested
    page-generation-before-slot lock order."""
    from src.backend.DeckManagement.InputIdentifier import Input
    from src.backend.DeckManagement.DeckController import DeckController

    controller, media_player, _ = fixtures.make_stub_controller(
        serial="slotrace-4", has_touchscreen=True
    )
    touch = controller.inputs[Input.Touchscreen][0]

    def seed():
        media_player.add_touchscreen_task(
            b"\x01" * 64,
            page=controller.active_page,
            config_gen=controller._page_load_generation,
            present=touch.present_state,
            img_hash=1,
        )

    # Exercise the page-generation then slot-lock ordering through the real method.
    seed()
    DeckController.clear_media_player_tasks(controller, gen=controller._page_load_generation)
    if media_player.touchscreen_task is not None:
        print("FAIL(4): clear_media_player_tasks did not wipe the slot")
        return 1

    # _exec_clear_and_close is the terminal wipe under _slot_lock.
    seed()
    media_player._exec_clear_and_close()
    if media_player.touchscreen_task is not None:
        print("FAIL(4): _exec_clear_and_close did not wipe the slot")
        return 1

    print("PASS: slot wipes (clear_media_player_tasks / _exec_clear_and_close) "
          "leave a coherent slot, no lock inversion")
    return 0


def main() -> int:
    start_watchdog(40, "touchscreen_slot_race")
    rc = check_touchscreen_drain_race()
    rc |= check_image_clear_race()
    rc |= check_write_cap_putback_race()
    rc |= check_slot_wipes()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
