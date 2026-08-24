"""The fetched-not-yet-activated window in the page cache.

get_page hands back a Page that nothing references yet, so an ownership pin
covers that window. Eviction spares a reserved page, and installing the page
retires the reservation.
"""

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


def reset_world() -> None:
    """Isolate a leg. The legs share the singleton page manager, so a prior
    leg's controllers and cached pages would inflate total and displace this
    leg's evictions."""
    gl.deck_manager.deck_controller.clear()
    gl.page_manager.pages.clear()
    gl.page_manager._loads_in_flight.clear()


def fresh_controller(serial: str) -> StubController:
    c = StubController(serial)
    gl.deck_manager.deck_controller.append(c)
    return c


def arm(page) -> None:
    """Inject a sentinel action object. The seeded pages carry no actions, so
    this is what makes a teardown observable, because clear_action_objects
    empties every state dict."""
    page.action_objects["sentinel"] = {"0x0": {0: {0: object()}}}


def actions_alive(page) -> bool:
    return bool(page.action_objects.get("sentinel"))


# Leg 1. A page fetched but not yet activated survives eviction pressure,
# stays whole, and is still the cache's page for its key.
def leg_fetched_page_survives_pressure() -> int:
    reset_world()
    controller = fresh_controller("pin-fetch")
    # Roomy budget during setup so get_page's own eviction pass does not
    # reclaim the fillers before the leg has built its state.
    gl.page_manager.max_pages = 100

    active = gl.page_manager.get_page(seed_page("PinActive"), controller)
    controller.active_page = active
    fillers = [gl.page_manager.get_page(seed_page(f"PinFiller{i}"), controller)
               for i in range(2)]

    # The page under test is fetched last, so this is the state a caller sits
    # in between get_page and load_page.
    target_path = seed_page("PinTarget")
    target = gl.page_manager.get_page(target_path, controller)
    arm(target)
    if target is None or len(fillers) != 2:
        print("FAIL(1-setup): the cache was not seeded")
        return 1

    # With total 4 and budget 1 the excess is 3, and 3 entries are evictable.
    # Without a pin the fetched page is one of them, because the cache sees no
    # reference to it.
    gl.page_manager.max_pages = 1
    gl.page_manager.clear_old_cached_pages()

    if not actions_alive(target):
        print("FAIL(1): the page fetched but not yet activated was gutted -- "
              "its caller is about to hand a corpse to load_page")
        return 1
    cached = gl.page_manager.pages.get(controller, {})
    if target_path not in cached:
        print("FAIL(1): the page fetched but not yet activated was evicted "
              "from the cache")
        return 1

    # The caller then sees the consequence. A re-fetch must return the same
    # object, because a twin Page for one (controller, path) registers two
    # sets of live event handlers for one key.
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


# Leg 2. Abandoned fetches are bounded, not accumulated. A caller that raises
# between the fetch and the load runs no release, so the bound of one
# reservation per deck is what stops the cache filling with pinned pages.
def leg_abandoned_fetches_bounded() -> int:
    reset_world()
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


# Leg 4. The screensaver hand-off. A page change while the screensaver shows
# is stashed, not installed, so its reservation must survive. The stash and
# the reservation together carry the page until hide() installs it.
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


# Leg 5. Bracketed work is counted, not flagged. The tick loop and a key
# gesture bracket the same page routinely, and with a flag the first release
# ends the protection the second still relies on.
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

    # A bracket whose body raises must still release. A count does not heal
    # on the next bracket, so one skipped release pins the page for the life
    # of the process, once per raising call.
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


# Leg 6. The screensaver hand-off, end to end. hide() pops the pending page
# under the load lock and installs it after releasing that lock, so in between
# the page is neither pending nor active. A second fetch during the
# screensaver retires the stash's own reservation, which leaves only the
# re-reservation hide() takes at the pop.
def leg_screensaver_handoff_survives_pressure(controller) -> int:
    saver = controller.screen_saver
    deferred = gl.page_manager.get_page(seed_page("PinHandoff"), controller)
    arm(deferred)

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

    if not actions_alive(deferred):
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
    """The deck's input table, raising when the tick loop iterates it.

    A raise here belongs to no input, so the loop's per-input guard cannot
    catch it and its finally is the only thing that gives the page back. Every
    other reader, on every other thread, gets the real table's answer.
    """

    def __iter__(self):
        if threading.current_thread().name == TICK_THREAD:
            raise RuntimeError("the input walk blew up")
        return super().__iter__()


# Leg 7. The tick loop brackets by hand rather than with a context manager,
# because the liveness probe other scenarios hang off it counts both calls.
# Its release lives in a finally, so a walk that raises still releases. The
# loop's guard holds a failure it can charge to one input, and the bracket
# then runs its ordinary path; a failure in the walk itself unwinds to the
# finally. Both shapes run here, and every bracket either opens has to
# balance. A throwaway deck runs this, because the injections stand over
# several walks.
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

        # Count both halves of the bracket, on the tick thread alone. That
        # counts walks, where counting the raise would also count the media
        # thread's reads of the same states.
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
        # Ten walks a second instead of one, so several bracketed walks pass
        # under each injection and the leg stays short. The loop re-reads this
        # every walk and floors its wait at 0.1s.
        controller.TICK_DELAY = 0.05
        controller.TICK_ERROR_LOG_INTERVAL_S = GUARD_INTERVAL_S

        # Failure one, charged to an input. Restore as soon as the proof
        # lands: the media thread reads the same states, and its log.catch
        # would swallow this on every frame for as long as it stands.
        patched = [i for input_list in controller.inputs.values()
                   for i in input_list]
        originals = [i.get_active_state for i in patched]
        for controller_input in patched:
            controller_input.get_active_state = boom
        try:
            # Wait on the open half. It runs ahead of the guarded body on
            # every walk, so a release that goes missing shows up as an
            # unbalanced count below and never as a wait that timed out.
            charged = fixtures.wait_until(lambda: marks["open"] >= 3, timeout=20)
            charged_record = fixtures.wait_until(
                lambda: any(GUARD_MARKER in record for record in records),
                timeout=20)
            alive_after_input = controller.tick_thread.is_alive()
        finally:
            for controller_input, original in zip(patched, originals):
                controller_input.get_active_state = original

        # Failure two, in the walk itself. No per-input guard covers this one,
        # so the release in the loop's finally is the only thing that hands
        # the page back.
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


# Legs 8 and 9. Deleting a page retires the deck's outstanding fetch on the
# two branches of remove_page that install nothing in its place. Installing a
# page is what normally retires a reservation, so such a branch would leave
# the deleted page reserved and unevictable. The two branches are mutually
# exclusive per controller, so each leg drives exactly one release.
def reservation_of(controller):
    """The deck's outstanding fetch, resolved. None when it has none.

    This reads the reservation table directly. count reports whether the page
    is held, and this reports whether the deck reserves it. A release that
    unpins without retiring the entry leaves a stale slot."""
    reference = gl.page_manager.pins._reservations.get(controller)
    return reference() if reference is not None else None


def leg_delete_pending_page_retires_reservation() -> int:
    reset_world()
    controller = fresh_controller("pin-rm-pending")
    pins = gl.page_manager.pins
    gl.page_manager.max_pages = 100

    # The deck shows something else and the doomed page is fetched last, so
    # it is the deck's one outstanding reservation. Leg 4 pins that the
    # screensaver deferral keeps it there.
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
    reset_world()
    controller = fresh_controller("pin-rm-last")
    pins = gl.page_manager.pins
    gl.page_manager.max_pages = 100

    doomed_path = seed_page("RmLastOnly")
    doomed = gl.page_manager.get_page(doomed_path, controller)
    controller.active_page = doomed

    if pins.count(doomed) != 1 or reservation_of(controller) is not doomed:
        print("FAIL(9-setup): the page is not the deck's outstanding fetch")
        return 1

    # The branch under test is "no page left to switch to". Other legs seed
    # pages into the shared data dir, so this states the emptiness instead of
    # arranging it on disk. The deck has no default page, so remove_page takes
    # the fallback and finds the list empty.
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
    reset_world()
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
