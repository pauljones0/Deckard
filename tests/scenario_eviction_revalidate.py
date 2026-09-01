"""Require page eviction to revalidate under lock and pop before teardown."""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl
from fixtures import FaultyFakeDeck, seed_page, start_watchdog


class StubController:
    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None
        self._screensaver_pending_page = None

    def serial_number(self) -> str:
        return self.deck.get_serial_number()


def fill_cache(controller, n: int, prefix: str):
    pages = []
    for i in range(n):
        path = seed_page(f"{prefix}{i}")
        page = gl.page_manager.get_page(path, controller)
        pages.append(page)
    return pages


def actions_alive(page) -> bool:
    # clear_action_objects() empties every state dict. The seeded pages carry no
    # actions, so use a sentinel injected into action_objects instead.
    return bool(page.action_objects.get("sentinel"))


def seed_action_sentinel(page):
    # Real schema depth is type, json_identifier, state, then index to action.
    page.action_objects["sentinel"] = {"0x0": {0: {0: object()}}}


def main() -> int:
    start_watchdog(30, "eviction_revalidate")
    fixtures._install_integration_globals()

    controller = StubController("evict-1")
    gl.deck_manager.deck_controller.append(controller)
    gl.page_manager.max_pages = 3

    # 1. A screensaver-pending page survives eviction pressure.
    pages = fill_cache(controller, 6, "Evict")
    pending = pages[0]  # oldest -> first eviction candidate
    seed_action_sentinel(pending)
    controller._screensaver_pending_page = pending
    controller.active_page = pages[-1]

    gl.page_manager.clear_old_cached_pages()

    if not actions_alive(pending):
        print("FAIL(1): the screensaver-pending page was gutted -- hide() "
              "would load a page whose every action is dead")
        return 1
    print("PASS: screensaver-pending page survives eviction")

    # Non-vacuous. The pressure was real, so some page did get evicted.
    cached = gl.page_manager.pages[controller]
    if len(cached) > gl.page_manager.max_pages + 1:  # plus one for the pending page
        print(f"FAIL: eviction did nothing ({len(cached)} cached, "
              f"max {gl.page_manager.max_pages}) -- guard is vacuous")
        return 1
    print("PASS: stale pages still get evicted under pressure")

    # 2. Activation between the snapshot and the teardown, made deterministic.
    activation_controller = StubController("evict-2")
    gl.deck_manager.deck_controller.append(activation_controller)
    activation_pages = fill_cache(activation_controller, 6, "Toctou")
    cleared_page, activated_page = activation_pages[0], activation_pages[1]
    seed_action_sentinel(cleared_page)
    seed_action_sentinel(activated_page)
    activation_controller.active_page = activation_pages[-1]

    real_clear = cleared_page.clear_action_objects

    def clear_and_activate():
        # Runs during the eviction loop, outside the lock. This is the page
        # switch the snapshot could not see.
        activation_controller.active_page = activated_page
        real_clear()

    cleared_page.clear_action_objects = clear_and_activate

    gl.page_manager.clear_old_cached_pages()

    if not actions_alive(activated_page):
        print("FAIL(2): a page activated mid-eviction was gutted while "
              "ACTIVE (snapshot TOCTOU)")
        return 1
    print("PASS: page activated mid-eviction is skipped by re-validation")

    # 3. Pop under lock before teardown so a concurrent get_page uses the
    # single-flight builder instead of receiving the object being cleared.
    refetch_controller = StubController("evict-3")
    gl.deck_manager.deck_controller.append(refetch_controller)
    refetch_pages = fill_cache(refetch_controller, 6, "Refetch")
    victim = refetch_pages[0]  # oldest -> first eviction candidate
    victim_path = victim.json_path
    refetch_controller.active_page = refetch_pages[-1]

    captured = {}
    original_refetch_clear = victim.clear_action_objects

    def clear_and_refetch():
        # Runs during the eviction loop, outside the lock and after the pop. A
        # concurrent get_page() for the same controller and path lands here.
        captured["page"] = gl.page_manager.get_page(victim_path, refetch_controller)
        original_refetch_clear()

    victim.clear_action_objects = clear_and_refetch

    gl.page_manager.clear_old_cached_pages()

    refetched = captured.get("page")
    if refetched is None:
        print("FAIL(3): the teardown-gap get_page() never ran")
        return 1
    if refetched is victim:
        print("FAIL(3): a get_page() during the teardown gap was handed the "
              "gutted corpse -- pop must precede clear_action_objects()")
        return 1
    # The fresh object is usable and not itself gutted. A newly minted Page has
    # its own action_objects dict, untouched by the victim teardown.
    seed_action_sentinel(refetched)
    if not actions_alive(refetched):
        print("FAIL(3): the freshly minted Page is not usable")
        return 1
    print("PASS: get_page() during the teardown gap mints a fresh Page, "
          "not the gutted corpse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
