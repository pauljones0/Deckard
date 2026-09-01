"""Verify page-cache eviction counts, ordering, and budget changes."""

# A controller with active_page None inflates total but never gives up its own
# pages.
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl
from fixtures import FaultyFakeDeck, seed_page, start_watchdog


class StubController:
    """The minimal surface clear_old_cached_pages dereferences, namely a
    serial, an active_page and a _screensaver_pending_page slot."""

    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None
        self._screensaver_pending_page = None

    def serial_number(self) -> str:
        return self.deck.get_serial_number()


def reset_page_cache() -> None:
    """Clear shared controllers and cached pages before each leg."""
    gl.deck_manager.deck_controller.clear()
    gl.page_manager.pages.clear()
    gl.page_manager._loads_in_flight.clear()


def fresh_controller(serial: str) -> StubController:
    controller = StubController(serial)
    gl.deck_manager.deck_controller.append(controller)
    return controller


def cache_page(controller, name: str):
    """Cache and return an evictable page for controller."""
    return gl.page_manager.get_page(seed_page(name), controller)


def cached_paths(controller):
    return set(gl.page_manager.pages.get(controller, {}).keys())


def cached_count(controller):
    return len(gl.page_manager.pages.get(controller, {}))


# Leg 1. clear_old_cached_pages removes exactly (total - max_pages) pages.
def leg_excess_count() -> int:
    reset_page_cache()
    controller = fresh_controller("budget-excess")
    # A large budget during setup, so the clear_old_cached_pages that get_page
    # runs after each load evicts no candidate before the set is complete.
    gl.page_manager.max_pages = 100

    pages = [cache_page(controller, f"Excess{i}") for i in range(8)]
    controller.active_page = pages[-1]  # one page is active, so never evictable

    if cached_count(controller) != 8:
        print(f"FAIL(1-setup): expected 8 cached, got {cached_count(controller)}")
        return 1

    # total=8, excess = 8 - 3 = 5 must be evicted, leaving 3.
    gl.page_manager.max_pages = 3
    gl.page_manager.clear_old_cached_pages()

    remaining = cached_count(controller)
    if remaining != 3:
        print(f"FAIL(1): excess arithmetic wrong -- expected 3 pages left "
              f"(total 8 - max_pages 3 = 5 evicted), got {remaining} "
              f"({8 - remaining} evicted)")
        return 1

    # The active page is one of the survivors (never evictable).
    if pages[-1].json_path not in cached_paths(controller):
        print("FAIL(1): the active page was evicted")
        return 1
    print("PASS(1): clear_old_cached_pages evicts exactly (total - max_pages)")
    return 0


# Leg 2. Eviction removes the lowest lru_stamp entries first. get_page bumps
# lru_stamp on every access, so a re-touched page outlives an older sibling.
def leg_oldest_first() -> int:
    reset_page_cache()
    controller = fresh_controller("budget-oldest")
    gl.page_manager.max_pages = 100

    # Load in order A, B, C, D. lru_stamp ascends A<B<C<D.
    paths = {name: seed_page(f"Order{name}") for name in ("A", "B", "C", "D")}
    for name in ("A", "B", "C", "D"):
        gl.page_manager.get_page(paths[name], controller)

    # Re-touch A. get_page bumps its lru_stamp to the newest. Now the
    # oldest-by-lru_stamp order is B < C < D < A.
    gl.page_manager.get_page(paths["A"], controller)

    # Make D active, so it is exempt whatever its number. The decision among
    # the rest must be oldest-first.
    controller.active_page = gl.page_manager.pages[controller][paths["D"]]["page"]

    # Budget 2 evicts oldest pages B and C; re-touched A and active D survive.
    gl.page_manager.max_pages = 2
    gl.page_manager.clear_old_cached_pages()

    survivors = cached_paths(controller)
    if paths["B"] in survivors or paths["C"] in survivors:
        print(f"FAIL(2): eviction was not oldest-first -- B/C should be gone. "
              f"survivors={sorted(p.split('/')[-1] for p in survivors)}")
        return 1
    if paths["A"] not in survivors:
        print("FAIL(2): the re-touched (newest) page A was wrongly evicted -- "
              "lru_stamp bump on access is not respected by the ordering")
        return 1
    if paths["D"] not in survivors:
        print("FAIL(2): the active page D was evicted")
        return 1
    print("PASS(2): eviction removes the lowest-lru_stamp (oldest-access) "
          "pages first")
    return 0


# Leg 3. A shrink through set_pages_to_cache runs an eviction pass. A grow
# evicts nothing.
def leg_set_pages_to_cache_shrink() -> int:
    reset_page_cache()
    controller = fresh_controller("budget-shrink")
    gl.page_manager.max_pages = 100

    pages = [cache_page(controller, f"Shrink{i}") for i in range(6)]
    controller.active_page = pages[-1]
    if cached_count(controller) != 6:
        print(f"FAIL(3-setup): expected 6 cached, got {cached_count(controller)}")
        return 1

    # Growing the budget must evict nothing.
    gl.page_manager.set_pages_to_cache(200)
    if cached_count(controller) != 6:
        print(f"FAIL(3): growing the cache budget evicted pages "
              f"({cached_count(controller)} left, expected 6)")
        return 1

    # A shrink must run clear_old_cached_pages. set_pages_to_cache(n) sets
    # max_pages to n + 1, so n=1 gives max_pages 2, total 6 and excess 4.
    gl.page_manager.set_pages_to_cache(1)
    remaining = cached_count(controller)
    if remaining != 2:
        print(f"FAIL(3): set_pages_to_cache(1) -> max_pages 2 should leave 2 "
              f"pages (6 total - 4 excess), got {remaining}")
        return 1
    if pages[-1].json_path not in cached_paths(controller):
        print("FAIL(3): shrink evicted the active page")
        return 1
    print("PASS(3): set_pages_to_cache shrinks the budget and runs an "
          "eviction pass; growing it does not evict")
    return 0


# Leg 4. Pages on a controller without an active page count toward the budget
# but cannot be evicted, which displaces evictions onto active controllers.
def leg_active_none_distorts_budget() -> int:
    reset_page_cache()
    # A controller mid-init or torn down but not discarded has active_page
    # None and still holds cached pages.
    inactive_controller = fresh_controller("budget-ghost")
    active_controller = fresh_controller("budget-live")
    gl.page_manager.max_pages = 100

    # The inactive controller holds 4 cached pages but has active_page None.
    cache_page(inactive_controller, "Ghost0")
    cache_page(inactive_controller, "Ghost1")
    cache_page(inactive_controller, "Ghost2")
    cache_page(inactive_controller, "Ghost3")
    # The active controller holds 4 pages, the last one active.
    live_pages = [cache_page(active_controller, f"Live{i}") for i in range(4)]
    active_controller.active_page = live_pages[-1]
    # inactive_controller.active_page stays None (the distortion condition).

    if cached_count(inactive_controller) != 4 or cached_count(active_controller) != 4:
        print(
            f"FAIL(4-setup): inactive={cached_count(inactive_controller)} "
            f"active={cached_count(active_controller)}"
        )
        return 1

    # Total 8 with budget 5 requires three evictions, all from the active
    # controller because the inactive controller provides no eviction candidates.
    gl.page_manager.max_pages = 5
    gl.page_manager.clear_old_cached_pages()

    inactive_left = cached_count(inactive_controller)
    active_left = cached_count(active_controller)

    # The inactive controller's pages are never reclaimed, because active_page
    # None skips them.
    if inactive_left != 4:
        print(f"FAIL(4): an active_page=None controller's pages were evicted "
              f"({inactive_left}/4 left) -- if this changed, the :236 guard was "
              f"altered (a pin-count redesign landing?); rewrite this "
              f"leg to the new budget contract")
        return 1
    # The active controller takes all 3 evictions, from 4 pages down to 1.
    if active_left != 1:
        print(f"FAIL(4): expected the active controller over-evicted to 1 page "
              f"(all 3 excess evictions displaced onto it by the inactive controller's "
              f"budget distortion), got {active_left} left -- if the distortion "
              f"was fixed (a pin-count redesign), rewrite this leg to "
              f"the new budget contract")
        return 1
    if active_controller.active_page.json_path not in cached_paths(active_controller):
        print("FAIL(4): the active controller's active page was evicted")
        return 1
    print("PASS(4): an active_page=None controller inflates `total` and "
          "displaces all evictions onto active controllers; its own pages are "
          "never reclaimed (audit row-5 budget distortion, documented)")
    return 0


def main() -> int:
    start_watchdog(30, "page_cache_eviction")
    fixtures._install_integration_globals()

    rc = 0
    rc |= leg_excess_count()
    rc |= leg_oldest_first()
    rc |= leg_set_pages_to_cache_shrink()
    rc |= leg_active_none_distorts_budget()
    if rc == 0:
        print("PASS: scenario_page_cache_eviction")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
