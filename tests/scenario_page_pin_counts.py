"""Verify page pins protect fetched pages and balance all ownership paths."""

# One reservation per deck bounds an abandoned fetch to one unevictable page,
# retired by that deck's next fetch or load.
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading

from loguru import logger

import globals as gl
from fixtures import FaultyFakeDeck, seed_page, start_watchdog
from src.backend.PageManagement import page_pins


class StubController:
    """The minimal surface clear_old_cached_pages dereferences."""

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
    c = StubController(serial)
    gl.deck_manager.deck_controller.append(c)
    return c


def add_action_sentinel(page) -> None:
    """Inject an action sentinel that makes cache teardown observable."""
    page.action_objects["sentinel"] = {"0x0": {0: {0: object()}}}


def sentinel_actions_present(page) -> bool:
    return bool(page.action_objects.get("sentinel"))


# Leg 1. A page fetched but not yet activated survives eviction pressure,
# stays whole, and is still the cache's page for its key.
def leg_fetched_page_survives_pressure() -> int:
    reset_page_cache()
    controller = fresh_controller("pin-fetch")
    # Roomy budget during setup so get_page's own eviction pass does not
    # reclaim the fillers before the leg has built its state.
    gl.page_manager.max_pages = 100

    active = gl.page_manager.get_page(seed_page("PinActive"), controller)
    controller.active_page = active
    fillers = [gl.page_manager.get_page(seed_page(f"PinFiller{i}"), controller)
               for i in range(2)]

    # A get_page result remains reserved until the deck fetches another page
    # or installs it, which protects this unactivated target.
    target_path = seed_page("PinTarget")
    target = gl.page_manager.get_page(target_path, controller)
    add_action_sentinel(target)
    if target is None or len(fillers) != 2:
        print("FAIL(1-setup): the cache was not seeded")
        return 1

    # Total 4 with budget 1 makes three entries evictable without the target pin.
    gl.page_manager.max_pages = 1
    gl.page_manager.clear_old_cached_pages()

    if not sentinel_actions_present(target):
        print("FAIL(1): the page fetched but not yet activated was gutted -- "
              "its caller is about to hand a corpse to load_page")
        return 1
    cached = gl.page_manager.pages.get(controller, {})
    if target_path not in cached:
        print("FAIL(1): the page fetched but not yet activated was evicted "
              "from the cache")
        return 1

    # A re-fetch must return the same object because twin Pages would register
    # duplicate live event handlers for one controller and path.
    again = gl.page_manager.get_page(target_path, controller)
    if again is not target:
        print("FAIL(1): re-fetching the evicted page minted a twin Page for "
              "one (controller, path)")
        return 1

    # The pressure was real, because the unreserved fillers went.
    survivors = set(gl.page_manager.pages.get(controller, {}))
    if any(f.json_path in survivors for f in fillers):
        print("FAIL(1): no eviction happened at all -- the guard is vacuous")
        return 1
    print("PASS(1): a fetched-but-not-yet-activated page survives eviction "
          "pressure intact, and re-fetching it returns the same Page")
    return 0


# Leg 2. One reservation per deck bounds abandoned fetches when a caller
# raises between fetching and loading a page.
def leg_abandoned_fetches_bounded() -> int:
    reset_page_cache()
    controller = fresh_controller("pin-abandon")
    gl.page_manager.max_pages = 100

    controller.active_page = gl.page_manager.get_page(seed_page("AbandonHome"),
                                                      controller)

    abandoned = []
    for i in range(3):
        try:
            abandoned.append(
                gl.page_manager.get_page(seed_page(f"PinAbandon{i}"), controller))
            raise RuntimeError("the caller blew up before activating the page")
        except RuntimeError:
            pass

    # With total 4 and budget 1 the excess is 3. Only the newest fetch is
    # still reserved, so two of the three abandoned pages must go.
    gl.page_manager.max_pages = 1
    gl.page_manager.clear_old_cached_pages()

    survivors = set(gl.page_manager.pages.get(controller, {}))
    still_here = [p for p in abandoned if p.json_path in survivors]
    if len(still_here) != 1 or still_here[0] is not abandoned[-1]:
        print(f"FAIL(2): abandoned fetches accumulated -- "
              f"{len(still_here)} of 3 survived eviction (expected only the "
              f"newest). A dropped release must cost one page per deck, not "
              f"one per fetch")
        return 1

    # The last one is not special. The deck's next fetch retires its
    # reservation, so it becomes evictable like the rest.
    gl.page_manager.max_pages = 100
    gl.page_manager.get_page(seed_page("AbandonNext"), controller)
    gl.page_manager.max_pages = 1
    gl.page_manager.clear_old_cached_pages()

    if abandoned[-1].json_path in set(gl.page_manager.pages.get(controller, {})):
        print("FAIL(2): the deck's next fetch did not retire the previous "
              "reservation -- the bound leaks one page per deck forever")
        return 1
    print("PASS(2): an abandoned fetch costs at most one unevictable page per "
          "deck, retired by that deck's next fetch")
    return 0


# Leg 3. Installing the page releases the reservation. Nothing else may
# retire it in between, so this leg makes no second fetch.
def leg_install_releases_reservation(controller) -> int:
    pins = gl.page_manager.pins
    page = gl.page_manager.get_page(seed_page("PinInstall"), controller)
    if not pins.is_pinned(page):
        print("FAIL(3): get_page did not reserve the page it returned")
        return 1

    controller.load_page(page)
    if not fixtures.wait_until(lambda: not pins.is_pinned(page)):
        print("FAIL(3): installing the page left its fetch reservation in "
              "place -- the deck now carries a page it can never evict")
        return 1
    print("PASS(3): installing a page on its deck releases the fetch "
          "reservation")
    return 0


# Leg 4. A screensaver-stashed page keeps its reservation until hide()
# installs it.
def leg_screensaver_pending_keeps_reservation(controller) -> int:
    pins = gl.page_manager.pins
    page = gl.page_manager.get_page(seed_page("PinPending"), controller)

    controller.screen_saver.showing = True
    try:
        controller.load_page(page)
        if controller._screensaver_pending_page is not page:
            print("FAIL(4-setup): the page change was not deferred")
            return 1
        if not pins.is_pinned(page):
            print("FAIL(4): a deferred page change dropped its reservation -- "
                  "nothing carries the page across the gap between hide() "
                  "taking it and the load that installs it")
            return 1
    finally:
        controller.screen_saver.showing = False
        controller._screensaver_pending_page = None

    controller.load_page(page)
    if not fixtures.wait_until(lambda: not pins.is_pinned(page)):
        print("FAIL(4): the deferred page kept its reservation after it was "
              "finally installed")
        return 1
    print("PASS(4): a screensaver-deferred page keeps its reservation until "
          "the load that installs it")
    return 0


# Leg 5. Overlapping tick and gesture brackets require counts because the
# first release must not end the second holder's protection.
def leg_brackets_are_counted(controller) -> int:
    pins = gl.page_manager.pins
    page = gl.page_manager.get_page(seed_page("PinBracket"), controller)
    base = pins.count(page)

    controller.mark_page_ready_to_clear(False, page)
    controller.mark_page_ready_to_clear(False, page)
    if pins.count(page) != base + 2:
        print(f"FAIL(5): two brackets on one page counted as "
              f"{pins.count(page) - base}, not 2")
        return 1

    controller.mark_page_ready_to_clear(True, page)
    if not pins.is_pinned(page):
        print("FAIL(5): one bracket ending released the page while the other "
              "was still working on it")
        return 1

    controller.mark_page_ready_to_clear(True, page)
    if pins.count(page) != base:
        print(f"FAIL(5): the brackets did not balance -- {pins.count(page) - base} "
              f"holders left over")
        return 1

    # Unmatched releases clamp at zero. A negative count would make a later
    # holder's pin read as already released.
    controller.mark_page_ready_to_clear(True, page)
    controller.mark_page_ready_to_clear(True, page)
    controller.mark_page_ready_to_clear(False, page)
    if not pins.is_pinned(page):
        print("FAIL(5): unmatched releases drove the count below zero -- a "
              "real holder's pin no longer protects its page")
        return 1
    controller.mark_page_ready_to_clear(True, page)

    # A raising bracket must release because one skipped release permanently
    # pins the page, once per failed call.
    base = pins.count(page)
    for _ in range(3):
        try:
            with page_pins.holding(page):
                raise RuntimeError("the bracketed work blew up")
        except RuntimeError:
            pass
    if pins.count(page) != base:
        print(f"FAIL(5): a bracket whose body raised kept "
              f"{pins.count(page) - base} holder(s) -- permanently, and once "
              f"per raising call")
        return 1
    print("PASS(5): overlapping brackets count, unmatched releases are "
          "clamped at zero, and a raising bracket still releases")
    return 0


# Leg 6. hide() pops and re-reserves the pending page under the load lock,
# then installs it after releasing that lock.
def leg_screensaver_handoff_survives_pressure(controller) -> int:
    saver = controller.screen_saver
    deferred = gl.page_manager.get_page(seed_page("PinHandoff"), controller)
    add_action_sentinel(deferred)

    saver.show()
    try:
        controller.load_page(deferred)
        if controller._screensaver_pending_page is not deferred:
            print("FAIL(6-setup): the page change was not deferred")
            return 1
        # Another fetch on this deck, as window cycling produces. It retires
        # the deferred page's own reservation.
        gl.page_manager.get_page(seed_page("PinHandoffOther"), controller)

        # Squeeze the cache in the hand-off gap, after hide() popped the
        # pending page and before the follow-up installs it.
        real_followup = saver._hide_followup

        def pressure_then_install(*args, **kwargs):
            gl.page_manager.max_pages = 1
            gl.page_manager.clear_old_cached_pages()
            return real_followup(*args, **kwargs)

        saver._hide_followup = pressure_then_install
        try:
            saver.hide()
        finally:
            saver._hide_followup = real_followup
            gl.page_manager.max_pages = 100
    finally:
        saver.showing = False
        controller._screensaver_pending_page = None

    if not sentinel_actions_present(deferred):
        print("FAIL(6): the page the screensaver was holding was gutted in "
              "the hand-off gap -- dismissing the screensaver restores a page "
              "whose every action is dead")
        return 1
    if controller.active_page is not deferred:
        print("FAIL(6): dismissing the screensaver did not install the "
              "deferred page")
        return 1
    print("PASS(6): the page deferred by a screensaver survives cache "
          "pressure in the gap between the hand-off and its load")
    return 0


# The thread the tick loop runs on.
TICK_THREAD = "tick_actions"
# Substrings of the two records the loop's guard writes: one for a failure it
# charges to an input, one for a failure that belongs to the walk itself.
GUARD_MARKER = "action tick failed for"
WALK_MARKER = "the input walk"
# The guard's rate-limit window, shrunk for this leg so both records land
# inside it. The interval is a class attribute for exactly this.
GUARD_INTERVAL_S = 0.2


class RaisingInputs(dict):
    """Raise only for tick-loop iteration, outside the per-input guard."""

    def __iter__(self):
        if threading.current_thread().name == TICK_THREAD:
            raise RuntimeError("the input walk blew up")
        return super().__iter__()


# Leg 7. Both guarded input failures and unguarded walk failures must leave
# the manual tick bracket balanced through its finally block.
def leg_tick_bracket_releases_on_error() -> int:
    pins = gl.page_manager.pins
    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(str(message)), level="TRACE")
    controller = fixtures.make_headless_controller(serial="pin-tick",
                                                   page_name="PinTickHome")
    try:
        page = controller.active_page
        # A clean baseline. The active page carries no reservation once it is
        # installed, and the tick loop brackets it on its own clock.
        if page is None or not fixtures.wait_until(lambda: pins.count(page) == 0):
            print("FAIL(7-setup): the deck's page never settled unpinned")
            return 1

        # Count both bracket calls only on the tick thread to exclude media
        # reads of the same states.
        marks = {"open": 0, "close": 0}
        real_mark = controller.mark_page_ready_to_clear

        def counting_mark(*args, **kwargs):
            if threading.current_thread().name == TICK_THREAD:
                ready = args[0] if args else kwargs.get("ready_to_clear")
                marks["close" if ready else "open"] += 1
            return real_mark(*args, **kwargs)

        def boom():
            raise RuntimeError("a tick body blew up")

        controller.mark_page_ready_to_clear = counting_mark
        # The loop re-reads this delay each walk and floors it at 0.1 seconds,
        # which gives several bracket samples during each injection.
        controller.TICK_DELAY = 0.05
        controller.TICK_ERROR_LOG_INTERVAL_S = GUARD_INTERVAL_S

        # Restore the input failure promptly because the media thread reads the
        # same states and would catch it on every frame.
        patched = [i for input_list in controller.inputs.values()
                   for i in input_list]
        originals = [i.get_active_state for i in patched]
        for controller_input in patched:
            controller_input.get_active_state = boom
        try:
            # Wait on the opening call so a missing release appears as an
            # unbalanced count instead of a timeout.
            charged = fixtures.wait_until(lambda: marks["open"] >= 3, timeout=20)
            charged_record = fixtures.wait_until(
                lambda: any(GUARD_MARKER in record for record in records),
                timeout=20)
            alive_after_input = controller.tick_thread.is_alive()
        finally:
            for controller_input, original in zip(patched, originals):
                controller_input.get_active_state = original

        # A walk-level failure bypasses the per-input guard, so only the outer
        # finally releases the page.
        real_inputs = controller.inputs
        opens_before = marks["open"]
        records.clear()
        controller.inputs = RaisingInputs(real_inputs)
        try:
            unwound = fixtures.wait_until(
                lambda: marks["open"] >= opens_before + 3, timeout=20)
            walk_record = fixtures.wait_until(
                lambda: any(WALK_MARKER in record for record in records),
                timeout=20)
            alive_after_walk = controller.tick_thread.is_alive()
        finally:
            controller.inputs = real_inputs

        # Stop the loop before the pin is counted, so the count cannot land
        # between the two halves of a bracket that is still open.
        controller.keep_actions_ticking = False
        controller._tick_stop_event.set()
        controller.tick_thread.join(timeout=5)
        del controller.mark_page_ready_to_clear

        if not alive_after_input or not alive_after_walk:
            print(f"FAIL(7): a raise killed the tick thread (alive after the "
                  f"input failure: {alive_after_input}, after the walk "
                  f"failure: {alive_after_walk}) -- every animated action on "
                  f"this deck stops for the life of the process")
            return 1
        if not charged or not unwound:
            print(f"FAIL(7-setup): the loop opened {marks['open']} brackets "
                  f"across the two injections -- too few to tell a balanced "
                  f"bracket from a loop that stopped")
            return 1
        if controller.tick_thread.is_alive():
            print("FAIL(7-setup): the tick loop did not stop on request, so "
                  "the count below could read an open bracket")
            return 1
        if not charged_record or not walk_record:
            print(f"FAIL(7): a raise went unreported (charged to an input: "
                  f"{charged_record}, charged to the walk: {walk_record}) -- "
                  f"the loop walks on and the fault stays hidden")
            return 1
        if marks["open"] != marks["close"]:
            print(f"FAIL(7): the tick bracket opened {marks['open']} times and "
                  f"released {marks['close']} -- a walk that raises skips the "
                  f"release")
            return 1
        if pins.count(page) != 0:
            print(f"FAIL(7): a tick that raised left the page it marked "
                  f"pinned ({pins.count(page)} holder(s)) -- unevictable for "
                  f"the life of the process")
            return 1
    finally:
        fixtures.teardown(controller)
        logger.remove(sink_id)
    print(f"PASS(7): {marks['close']} walks under a raising input and a "
          f"raising walk stayed contained, and every bracket released the "
          f"page it marked")
    return 0


# Legs 8 and 9. Deletion branches that install no replacement must retire the
# outstanding fetch that installation normally releases.
def reservation_of(controller):
    """Resolve the deck's outstanding fetch, including stale reservation slots."""
    reference = gl.page_manager.pins._reservations.get(controller)
    return reference() if reference is not None else None


def leg_delete_pending_page_retires_reservation() -> int:
    reset_page_cache()
    controller = fresh_controller("pin-rm-pending")
    pins = gl.page_manager.pins
    gl.page_manager.max_pages = 100

    # Fetch the doomed page last while another page is active, then stash it as
    # the deck's single outstanding screensaver reservation.
    controller.active_page = gl.page_manager.get_page(seed_page("RmPendingHome"),
                                                      controller)
    doomed_path = seed_page("RmPendingDoomed")
    doomed = gl.page_manager.get_page(doomed_path, controller)
    controller._screensaver_pending_page = doomed

    if pins.count(doomed) != 1 or reservation_of(controller) is not doomed:
        print("FAIL(8-setup): the stashed page is not the deck's outstanding "
              "fetch, so there is nothing for the delete to retire")
        return 1

    gl.page_manager.remove_page(doomed_path)

    if controller._screensaver_pending_page is not None:
        print("FAIL(8-setup): the delete did not drop the pending request")
        return 1
    if pins.count(doomed) != 0 or reservation_of(controller) is doomed:
        print(f"FAIL(8): deleting a deck's screensaver-pending page left it "
              f"reserved (holders={pins.count(doomed)}, "
              f"reservation={reservation_of(controller)}) -- nothing will ever "
              f"install it, so nothing else retires it either")
        return 1
    print("PASS(8): deleting a deck's screensaver-pending page retires that "
          "deck's reservation")
    return 0


def leg_delete_last_page_retires_reservation() -> int:
    reset_page_cache()
    controller = fresh_controller("pin-rm-last")
    pins = gl.page_manager.pins
    gl.page_manager.max_pages = 100

    doomed_path = seed_page("RmLastOnly")
    doomed = gl.page_manager.get_page(doomed_path, controller)
    controller.active_page = doomed

    if pins.count(doomed) != 1 or reservation_of(controller) is not doomed:
        print("FAIL(9-setup): the page is not the deck's outstanding fetch")
        return 1

    # Override the shared page list to drive the no-page-left fallback without
    # depending on files seeded by other legs.
    real_get_pages = gl.page_manager.get_pages
    gl.page_manager.get_pages = lambda *args, **kwargs: [doomed_path]
    try:
        gl.page_manager.remove_page(doomed_path)
    finally:
        gl.page_manager.get_pages = real_get_pages

    if pins.count(doomed) != 0 or reservation_of(controller) is doomed:
        print(f"FAIL(9): deleting the last page left it reserved on its deck "
              f"(holders={pins.count(doomed)}, "
              f"reservation={reservation_of(controller)}) -- there was no "
              f"replacement to install, and installing is what releases")
        return 1
    print("PASS(9): deleting the last page retires the deck's reservation on "
          "the branch that installs no replacement")
    return 0


def main() -> int:
    start_watchdog(60, "page_pin_counts")
    fixtures._install_integration_globals()

    rc = 0
    rc |= leg_fetched_page_survives_pressure()
    rc |= leg_abandoned_fetches_bounded()
    rc |= leg_delete_pending_page_retires_reservation()
    rc |= leg_delete_last_page_retires_reservation()

    # The remaining legs drive the real load path, so they need a real
    # controller rather than the stubs above.
    reset_page_cache()
    gl.page_manager.max_pages = 100
    controller = fixtures.make_headless_controller(serial="pin-load",
                                                   page_name="PinLoadHome")
    try:
        rc |= leg_install_releases_reservation(controller)
        rc |= leg_screensaver_pending_keeps_reservation(controller)
        rc |= leg_brackets_are_counted(controller)
        rc |= leg_screensaver_handoff_survives_pressure(controller)
    finally:
        fixtures.teardown(controller)
    rc |= leg_tick_bracket_releases_on_error()

    if rc == 0:
        print("PASS: scenario_page_pin_counts")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
