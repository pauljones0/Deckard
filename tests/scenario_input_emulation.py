"""Pins the emulated input press in src/backend/control_plane.py.

An emulated press is the deck's own input path with no finger on it. What it
must produce is what a finger produces: the same events, to the same actions,
off the same kind of thread, with the release far enough behind the press to
mean what it says. Everything here drives the real DeckController, Page,
ControllerKey and ActionCore machinery, so a press that stopped reaching them
fails here rather than on a desk.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import threading  # noqa: E402
import time  # noqa: E402

import globals as gl  # noqa: E402

from src.backend import control_plane  # noqa: E402
from src.backend.DeckManagement.InputIdentifier import Input  # noqa: E402

# The recording action and the stub plugin manager behind it, shared rather
# than copied. Importing that scenario only defines them, because its own legs
# run under its __main__ guard.
import scenario_input_pipeline as pipeline  # noqa: E402

WATCHDOG_SECONDS = 60

SERIAL = "emu-deck-1"
KEY = "0x0"
COORDS = "0,0"
# The second page addresses a key the first page leaves empty, so an event
# recorded there can only have come from a press that switched pages first.
OTHER_KEY = "1x0"
OTHER_COORDS = "1,0"

DOWN = Input.Key.Events.DOWN
UP = Input.Key.Events.UP
SHORT_UP = Input.Key.Events.SHORT_UP
HOLD_START = Input.Key.Events.HOLD_START
HOLD_STOP = Input.Key.Events.HOLD_STOP


class StubWheel:
    """The timer wheel with the clock in this scenario's hand.

    It records what the control plane schedules and runs nothing until a leg
    says so. The control plane keeps no handle, so None is a whole answer.
    """

    def __init__(self) -> None:
        self.jobs: list[tuple[float, object, str]] = []

    def schedule(self, delay_s, callback, name="TimerWheelJob"):
        self.jobs.append((delay_s, callback, name))
        return None

    def fire_next(self) -> tuple[float, str]:
        delay_s, callback, name = self.jobs.pop(0)
        callback()
        return delay_s, name


def events_on(ident: str) -> list:
    return [event for (event, _data) in pipeline._delivered_events(ident)]


def load(controller, page_name: str):
    return pipeline._load_page_and_wait(
        controller, gl.page_manager.find_matching_page_path(page_name))


def active_name(controller) -> "str | None":
    page = controller.active_page
    return None if page is None else page.get_name()


# 1. What reaches the wheel, and in which order

def leg_wheel_choreography(plane, controller) -> None:
    """The press is scheduled, never run by the caller, and the release is a
    timer behind it.

    A stub wheel makes each leg of the press an explicit step, so the claims
    are about the order and the delays rather than about how fast a machine
    happened to be. The two claims a real wheel cannot make either way: that
    the calling thread dispatches nothing itself, and that the release is armed
    only once the press has been delivered.
    """
    load(controller, "EmuMain")
    controller.hold_time = 10.0
    pipeline._reset_delivered()

    wheel = StubWheel()
    saved = control_plane.timer_wheel
    control_plane.timer_wheel = wheel
    try:
        result = plane.emulate_input(SERIAL, "EmuMain", COORDS, "press")
        assert result.ok, result
        assert "press" in result.message and SERIAL in result.message, result.message

        assert len(wheel.jobs) == 1, (
            f"an emulated press must reach the wheel as exactly one job: "
            f"{[(d, n) for (d, _c, n) in wheel.jobs]}")
        delay_s, _callback, name = wheel.jobs[0]
        assert delay_s == 0.0, (
            f"the press leg waits for nothing, so its delay must be zero: {delay_s}")
        assert name == "EmulatedPress", name
        assert events_on(KEY) == [], (
            f"the caller's own thread dispatched the press: {events_on(KEY)} -- "
            f"a hardware press arrives on the deck's reader thread, and a "
            f"caller that is the main loop would be dispatching into itself")

        press_delay, press_name = wheel.fire_next()
        assert fixtures.wait_until(lambda: DOWN in events_on(KEY)), (
            f"the press job delivered no key down: {events_on(KEY)}")
        assert UP not in events_on(KEY), (
            f"the release ran with the press: {events_on(KEY)}")
        assert (press_delay, press_name) == (0.0, "EmulatedPress")

        assert len(wheel.jobs) == 1, (
            f"the release must be armed by the press leg, once it has been "
            f"delivered: {[(d, n) for (d, _c, n) in wheel.jobs]}")
        hold_s, _callback, release_name = wheel.jobs[0]
        assert release_name == "EmulatedRelease", release_name
        assert hold_s == control_plane._PRESS_DOWN_S, (
            f"a short press holds for {control_plane._PRESS_DOWN_S}s here, not {hold_s}s")

        wheel.fire_next()
        assert fixtures.wait_until(lambda: UP in events_on(KEY)), (
            f"the release job delivered no key up: {events_on(KEY)}")
        assert not wheel.jobs, f"the press left work behind: {wheel.jobs}"

        # A long press differs in one number, and that number is the deck's own
        # hold time plus the margin the hold timer needs to fire first.
        pipeline._reset_delivered()
        assert plane.emulate_input(SERIAL, "EmuMain", COORDS, "long-press").ok
        wheel.fire_next()
        assert fixtures.wait_until(lambda: DOWN in events_on(KEY)), events_on(KEY)
        long_hold_s, _callback, _name = wheel.jobs[0]
        assert long_hold_s == controller.hold_time + control_plane._LONG_PRESS_EXTRA_S, (
            f"a long press must outlast the deck's own hold time of "
            f"{controller.hold_time}s, and did not: {long_hold_s}s")
        # End the gesture through the real path, so nothing stays held for the
        # legs below. Which release event this produces is the stub clock's
        # doing and no claim of this leg: the deck's hold time is 10s here and
        # this release lands at once.
        wheel.fire_next()
        assert fixtures.wait_until(lambda: UP in events_on(KEY)), events_on(KEY)
    finally:
        control_plane.timer_wheel = saved

    print("PASS: a press reaches the wheel as a press leg and then a release leg")


# 2. What an action sees

def leg_short_press_semantics(plane, controller) -> None:
    """A short press delivers DOWN, then SHORT_UP and UP, off the main thread.

    The deck's hold time is set well above the press, so a release that lands
    inside it is the case under test rather than a race with the hold timer.
    """
    load(controller, "EmuMain")
    controller.hold_time = 2.0
    pipeline._reset_delivered()

    dispatched_on: list = []
    real_event_callback = controller.event_callback

    def recording_event_callback(ident, *args, **kwargs):
        dispatched_on.append(threading.current_thread())
        return real_event_callback(ident, *args, **kwargs)

    controller.event_callback = recording_event_callback
    try:
        assert plane.emulate_input(SERIAL, "EmuMain", COORDS, "press").ok
        assert fixtures.wait_until(lambda: UP in events_on(KEY), timeout=5.0), (
            f"the release never arrived: {events_on(KEY)}")
    finally:
        del controller.event_callback

    delivered = events_on(KEY)
    assert SHORT_UP in delivered, (
        f"a press inside the deck's hold time must read as a short press: {delivered}")
    assert HOLD_START not in delivered and HOLD_STOP not in delivered, (
        f"a short press must not produce the hold events: {delivered}")
    assert delivered.index(DOWN) < delivered.index(UP), (
        f"the events arrived out of order: {delivered}")

    assert len(dispatched_on) == 2, (
        f"a press is one down and one up at the deck's own entry point, not "
        f"{len(dispatched_on)} calls")
    assert all(thread is not threading.main_thread() for thread in dispatched_on), (
        f"an emulated press reached the input path on the main thread: "
        f"{[t.name for t in dispatched_on]} -- a hardware press never does, so "
        f"an action that marshals its own work onto the main thread behaves "
        f"differently under one than under the other")

    print("PASS: a short press delivers down, short up and up, off the main thread")


def leg_long_press_semantics(plane, controller) -> None:
    """A long press outlasts the hold time: HOLD_START, then HOLD_STOP and UP.

    The hold time is shrunk so the wheel's own fire is quick. It stays real
    time, and far under the watchdog.
    """
    load(controller, "EmuMain")
    controller.hold_time = 0.2
    pipeline._reset_delivered()

    assert plane.emulate_input(SERIAL, "EmuMain", COORDS, "long-press").ok
    assert fixtures.wait_until(lambda: HOLD_START in events_on(KEY), timeout=5.0), (
        f"the hold never started: {events_on(KEY)} -- the release cancelled "
        f"the input's own hold timer before it could fire")
    assert fixtures.wait_until(lambda: UP in events_on(KEY), timeout=5.0), (
        f"the release never arrived: {events_on(KEY)}")

    delivered = events_on(KEY)
    assert HOLD_STOP in delivered, (
        f"a release past the hold time must read as the end of a hold: {delivered}")
    assert SHORT_UP not in delivered, (
        f"a long press must not also read as a short one: {delivered}")
    assert delivered.index(DOWN) < delivered.index(HOLD_START) < delivered.index(UP), (
        f"the events arrived out of order: {delivered}")

    print("PASS: a long press delivers hold start, hold stop and up")


# 3. The page the press lands on

def leg_press_switches_page_first(plane, controller) -> None:
    """A press on a page that is not showing switches to it and lands there.

    The second page carries its action on a key the first page leaves empty, so
    an event recorded on that key can only have come from the page this request
    named. The switch queues the input rebuild on the media thread and returns
    before it runs, which is why the press waits for that rebuild: without the
    wait it reaches the outgoing page's inputs.
    """
    load(controller, "EmuMain")
    controller.hold_time = 2.0
    pipeline._reset_delivered()

    result = plane.emulate_input_on(controller, "EmuOther", OTHER_COORDS, "press")
    assert result.ok, result
    assert active_name(controller) == "EmuOther", (
        f"the press did not switch the page it named: {active_name(controller)}")
    assert fixtures.wait_until(lambda: DOWN in events_on(OTHER_KEY), timeout=5.0), (
        f"the press never reached the new page's action: {events_on(OTHER_KEY)}")
    assert events_on(KEY) == [], (
        f"the press landed on the page that was showing before it: {events_on(KEY)}")
    assert fixtures.wait_until(lambda: UP in events_on(OTHER_KEY), timeout=5.0), (
        f"the release never arrived: {events_on(OTHER_KEY)}")

    print("PASS: a press on another page switches to it and lands on its actions")


def leg_press_waits_for_the_rebuild(plane, controller) -> None:
    """The press waits for the input rebuild the page switch queued.

    A switch hands load_all_inputs to the media thread and returns before it
    runs, so a press dispatched straight after reaches inputs that still carry
    the outgoing page's actions. On a fake deck that rebuild is over in under a
    millisecond, which is why this leg holds it: without the hold, a press that
    skipped the barrier would land correctly by luck and prove nothing.
    """
    load(controller, "EmuMain")
    controller.hold_time = 2.0
    pipeline._reset_delivered()

    hold_s = 2.0
    real_load_input = controller.load_input

    def held_load_input(controller_input, page, *args, **kwargs):
        if page.get_name() == "EmuOther":
            time.sleep(hold_s)
        return real_load_input(controller_input, page, *args, **kwargs)

    controller.load_input = held_load_input
    try:
        start = time.monotonic()
        result = plane.emulate_input_on(controller, "EmuOther", OTHER_COORDS, "press")
        elapsed = time.monotonic() - start
    finally:
        del controller.load_input

    assert result.ok, result
    assert elapsed >= hold_s, (
        f"the press returned in {elapsed:.2f}s, before the {hold_s}s rebuild it "
        f"asked for could have finished -- it dispatched into the inputs of the "
        f"page that was showing before it")
    # And it released on that rebuild rather than on the wait's own bound: a
    # press that rode the bound out would satisfy the line above as well.
    assert elapsed < control_plane._INPUT_LOAD_WAIT_S - 2.0, (
        f"the press returned in {elapsed:.2f}s, near the "
        f"{control_plane._INPUT_LOAD_WAIT_S}s bound -- the barrier timed out "
        f"instead of releasing on the rebuild the switch armed")
    assert fixtures.wait_until(lambda: DOWN in events_on(OTHER_KEY), timeout=5.0), (
        f"the press never reached the rebuilt page's action: "
        f"{events_on(OTHER_KEY)}")
    assert events_on(KEY) == [], (
        f"the press landed on the page that was showing before it: {events_on(KEY)}")
    assert fixtures.wait_until(lambda: UP in events_on(OTHER_KEY), timeout=5.0), (
        f"the release never arrived: {events_on(OTHER_KEY)}")

    print("PASS: a press waits for the rebuild its own page switch queued")


# 4. What a bad request answers

def leg_bad_event_word_moves_nothing(plane, controller) -> None:
    """An event word this build does not know is refused before the switch.

    It is judged without a device, so nothing about it needs the page it names
    to be loaded first, and a deck left on another page is a side effect of a
    request that was never carried out.
    """
    load(controller, "EmuMain")
    pipeline._reset_delivered()

    wheel = StubWheel()
    saved = control_plane.timer_wheel
    control_plane.timer_wheel = wheel
    try:
        result = plane.emulate_input_on(controller, "EmuOther", OTHER_COORDS, "double-press")
    finally:
        control_plane.timer_wheel = saved

    assert not result.ok and result.code == "bad-event", result
    for word in control_plane.EMULATED_EVENTS:
        assert word in result.message, (
            f"the failure must name the words that do work: {result.message!r}")
    assert active_name(controller) == "EmuMain", (
        f"a request nothing can carry out moved the deck anyway: "
        f"{active_name(controller)}")
    assert wheel.jobs == [], f"a refused request still scheduled work: {wheel.jobs}"
    assert events_on(OTHER_KEY) == [], events_on(OTHER_KEY)

    print("PASS: an unknown event word is refused, and the deck stays where it was")


def leg_failures_match_the_state_verb(plane, controller) -> None:
    """The coordinates are read by one rule, so both verbs refuse alike.

    A copy per verb drifts, and the person who typed the command then reads two
    different sentences about one mistake.
    """
    load(controller, "EmuMain")

    for coords in ("nope", "0,0,0", "", "x,y", "-1,0", "99,0"):
        pressed = plane.emulate_input_on(controller, "EmuMain", coords, "press")
        stated = plane.change_state_on(controller, "EmuMain", coords, 0)
        assert not pressed.ok, f"{coords!r} was accepted as coordinates: {pressed}"
        assert pressed.code == stated.code, (
            f"{coords!r} answers {pressed.code!r} to a press and {stated.code!r} "
            f"to a state change")
        assert pressed.message == stated.message, (
            f"{coords!r} answers two different sentences:\n  {pressed.message}\n"
            f"  {stated.message}")

    unknown_deck = plane.emulate_input("not-a-deck", "EmuMain", COORDS, "press")
    assert not unknown_deck.ok and unknown_deck.code == "no-such-deck", unknown_deck
    assert unknown_deck.message == plane.change_page("not-a-deck", "EmuMain").message, (
        "an unknown serial must answer the same whatever was asked of it")

    unknown_page = plane.emulate_input_on(controller, "no-such-page", COORDS, "press")
    assert not unknown_page.ok and unknown_page.code == "no-such-page", unknown_page
    assert "EmuMain" in unknown_page.message, (
        f"the failure must list the pages that exist: {unknown_page.message!r}")

    print("PASS: a press and a state change refuse the same request identically")


def leg_locked_session_refuses_the_press(plane, controller) -> None:
    """A deck that is not taking input says so rather than swallow the press.

    The lock screen turns interaction off for as long as the session is locked,
    and the deck's own entry point drops every event while it is off. A press
    reported as done and dropped there is indistinguishable from one that ran.
    """
    load(controller, "EmuMain")
    pipeline._reset_delivered()

    wheel = StubWheel()
    saved = control_plane.timer_wheel
    control_plane.timer_wheel = wheel
    controller.allow_interaction = False
    try:
        result = plane.emulate_input(SERIAL, "EmuMain", COORDS, "press")
    finally:
        controller.allow_interaction = True
        control_plane.timer_wheel = saved

    assert not result.ok and result.code == "input-blocked", result
    assert SERIAL in result.message and "locked" in result.message, result.message
    assert wheel.jobs == [], f"a refused press still scheduled work: {wheel.jobs}"
    assert events_on(KEY) == [], events_on(KEY)

    print("PASS: a locked session refuses the press instead of dropping it")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_input_emulation")
    fixtures._install_integration_globals()
    gl.plugin_manager = pipeline._PluginManager()

    # The plain seed first: it creates the pages directory the action pages are
    # written into, and gives the controller a default page to boot onto.
    fixtures.seed_page("Main")
    pipeline._seed_page("EmuMain", {"keys": {KEY: True}})
    pipeline._seed_page("EmuOther", {"keys": {OTHER_KEY: True}})

    plane = control_plane.get()
    controller = fixtures.make_headless_controller(serial=SERIAL)
    try:
        leg_wheel_choreography(plane, controller)
        leg_short_press_semantics(plane, controller)
        leg_long_press_semantics(plane, controller)
        leg_press_switches_page_first(plane, controller)
        leg_press_waits_for_the_rebuild(plane, controller)
        leg_bad_event_word_moves_nothing(plane, controller)
        leg_failures_match_the_state_verb(plane, controller)
        leg_locked_session_refuses_the_press(plane, controller)
    finally:
        fixtures.teardown(controller)

    print("ALL PASS: scenario_input_emulation")


if __name__ == "__main__":
    main()
