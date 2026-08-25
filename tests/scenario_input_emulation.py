"""Pins the emulated input press in src/backend/control_plane.py.

An emulated press is the deck's own input path with no finger on it. What it
must produce is what a finger produces: the same events, to the same actions,
off the same kind of thread, with the release far enough behind the press to
mean what it says, and on the page the request named rather than whichever one
the deck drifted to. Everything here drives the real DeckController, Page,
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

WATCHDOG_SECONDS = 90

SERIAL = "emu-deck-1"
ROTATED_SERIAL = "emu-deck-rot"
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


# The real wheel, kept so every leg that puts a stub in front of the control
# plane restores the same object.
timer_wheel_real = control_plane.timer_wheel


class StubWheel:
    """The timer wheel with the clock in this scenario's hand.

    A job whose name is in fire_at_once runs on a thread of its own as soon as
    it is scheduled, which is what the real wheel does with a job that is due
    at once. Every other job waits for a leg to fire it, so a delay is read
    rather than waited out. log keeps every job in the order it was scheduled,
    and jobs holds the ones still pending. The control plane keeps no handle,
    so None is a whole answer.
    """

    def __init__(self, fire_at_once: "tuple[str, ...]" = ()) -> None:
        self.log: list[tuple[float, str]] = []
        self.jobs: list[tuple[float, object, str]] = []
        self._fire_at_once = fire_at_once
        self._lock = threading.Lock()

    def schedule(self, delay_s, callback, name="TimerWheelJob"):
        job = (delay_s, callback, name)
        with self._lock:
            self.log.append((delay_s, name))
            self.jobs.append(job)
        if name in self._fire_at_once:
            threading.Thread(target=self._fire, args=(job,),
                             name=f"stub-wheel-{name}", daemon=True).start()
        return None

    def _fire(self, job) -> None:
        with self._lock:
            if job not in self.jobs:
                return
            self.jobs.remove(job)
        job[1]()

    def fire_next(self) -> tuple[float, str]:
        with self._lock:
            delay_s, callback, name = self.jobs.pop(0)
        callback()
        return delay_s, name

    def pending(self) -> list[tuple[float, str]]:
        with self._lock:
            return [(delay_s, name) for (delay_s, _callback, name) in self.jobs]

    def wait_for(self, name: str, timeout: float = 5.0) -> bool:
        return fixtures.wait_until(
            lambda: any(n == name for (_d, n) in self.log), timeout=timeout)


def events_on(ident: str) -> list:
    return [event for (event, _data) in pipeline._delivered_events(ident)]


def load(controller, page_name: str):
    return pipeline._load_page_and_wait(
        controller, gl.page_manager.find_matching_page_path(page_name))


def active_name(controller) -> "str | None":
    page = controller.active_page
    return None if page is None else page.get_name()


def key_input(controller, ident: str = KEY):
    c_input = controller.get_input(Input.Key(ident))
    assert c_input is not None, f"the deck has no input at {ident}"
    return c_input


def stub_wheel(fire_at_once: "tuple[str, ...]" = ()) -> StubWheel:
    """Put a stub wheel in front of the control plane. The caller restores.

    The input's own hold timer keeps the real wheel, because it is reached
    through the module the inputs import and not through this one. A leg that
    swaps here therefore drives the press and leaves the hold semantics alone.
    """
    wheel = StubWheel(fire_at_once=fire_at_once)
    control_plane.timer_wheel = wheel
    return wheel


# 1. What reaches the wheel, and in which order

def leg_wheel_choreography(plane, controller) -> None:
    """The press is a wheel job, and the release is a second one behind it.

    A stub wheel makes each leg an explicit step, so the claims are about the
    order and the delays rather than about how fast a machine happened to be.
    The delays are written out here as numbers. Comparing them against the
    module's own constants would pass whatever those constants became, and one
    of them is load-bearing: the release cancels the input's hold timer, so a
    margin at or near zero cancels HOLD_START before it can fire.
    """
    load(controller, "EmuMain")
    controller.hold_time = 10.0
    pipeline._reset_delivered()

    dispatched_on: list = []
    real_event_callback = controller.event_callback

    def recording_event_callback(ident, *args, **kwargs):
        dispatched_on.append(threading.current_thread())
        return real_event_callback(ident, *args, **kwargs)

    controller.event_callback = recording_event_callback
    wheel = stub_wheel(fire_at_once=("EmulatedPress",))
    try:
        result = plane.emulate_input(SERIAL, "EmuMain", COORDS, "press")
        assert result.ok, result
        assert "press" in result.message and SERIAL in result.message, result.message

        assert wheel.wait_for("EmulatedRelease"), (
            f"the press armed no release: {wheel.log}")
        assert wheel.log == [(0.0, "EmulatedPress"), (0.05, "EmulatedRelease")], (
            f"a short press is a job due at once and a release 0.05s behind it, "
            f"and this was {wheel.log}")
        assert wheel.pending() == [(0.05, "EmulatedRelease")], (
            f"only the release may still be waiting: {wheel.pending()}")

        assert fixtures.wait_until(lambda: DOWN in events_on(KEY)), (
            f"the press job delivered no key down: {events_on(KEY)}")
        assert UP not in events_on(KEY), (
            f"the release ran without being fired: {events_on(KEY)}")

        # The press ran where the wheel put it, and not on the thread that
        # asked for it. Only the press leg is judged here: the release below is
        # fired by this leg, so its thread is this scenario's doing. The real
        # wheel runs both, which the leg after this one covers.
        assert dispatched_on and dispatched_on[0] is not threading.main_thread(), (
            f"the press reached the input path on the caller's own thread: "
            f"{dispatched_on[0].name} -- a hardware press never does")

        wheel.fire_next()
        assert fixtures.wait_until(lambda: UP in events_on(KEY)), (
            f"the release job delivered no key up: {events_on(KEY)}")
        assert wheel.pending() == [], f"the press left work behind: {wheel.pending()}"

        # A long press differs in one number: the deck's own hold time plus the
        # margin the input's hold timer needs to fire before the release
        # cancels it.
        pipeline._reset_delivered()
        wheel.log.clear()
        assert plane.emulate_input(SERIAL, "EmuMain", COORDS, "long-press").ok
        assert wheel.wait_for("EmulatedRelease"), wheel.log
        assert wheel.log == [(0.0, "EmulatedPress"), (10.25, "EmulatedRelease")], (
            f"a long press on a deck whose hold time is 10s must hold for "
            f"10.25s, and this was {wheel.log}")
        # End the gesture through the real path, so nothing stays held for the
        # legs below. Which release event it produces is the stub clock's doing
        # and no claim of this leg.
        wheel.fire_next()
        assert fixtures.wait_until(lambda: UP in events_on(KEY)), events_on(KEY)
    finally:
        control_plane.timer_wheel = timer_wheel_real
        del controller.event_callback

    print("PASS: a press is one job due at once and a release behind it")


def leg_short_press_clamps_to_the_hold_time(plane, controller) -> None:
    """A short press on a deck with a very short hold time stays short.

    The press holds for a twentieth of a second, which is longer than some
    people set the hold time to. Held for that long against a hold time of
    0.05s, an emulated press would come out as a hold, and the word asked for
    was press. The hold is halved against the deck's own hold time for that,
    and the number below is the halved one.
    """
    load(controller, "EmuMain")
    controller.hold_time = 0.05
    pipeline._reset_delivered()

    wheel = stub_wheel(fire_at_once=("EmulatedPress",))
    try:
        assert plane.emulate_input(SERIAL, "EmuMain", COORDS, "press").ok
        assert wheel.wait_for("EmulatedRelease"), wheel.log
        assert wheel.log == [(0.0, "EmulatedPress"), (0.025, "EmulatedRelease")], (
            f"a press must stay under the deck's 0.05s hold time, and this "
            f"one was scheduled as {wheel.log}")
        wheel.fire_next()
        assert fixtures.wait_until(lambda: UP in events_on(KEY)), events_on(KEY)
    finally:
        control_plane.timer_wheel = timer_wheel_real
    controller.hold_time = 2.0

    print("PASS: a short press is halved against a deck's own short hold time")


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
    named.
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


def leg_press_refuses_a_page_that_moved(plane, controller) -> None:
    """A page switch between the request and the deck cancels the press.

    The press is validated against the page the request named and delivered a
    moment later, on a wheel shared with every other delay in the process.
    Anything can switch the page in that gap: a plugin action, a window rule,
    the screensaver, another transport. A press that went ahead would run the
    actions of the page now showing, which nobody asked for, and report success
    for a page it never touched.

    The third page carries an action on the same key as the first, so a press
    that went ahead is visible rather than merely wrong.
    """
    load(controller, "EmuMain")
    controller.hold_time = 2.0
    pipeline._reset_delivered()

    answer: dict = {}
    wheel = stub_wheel()
    # The caller waits while this leg loads a whole page in the gap, which is
    # longer than the wait a press is given in the field. Widen it for the leg,
    # so what is under test is the decision and not the clock.
    saved_wait = control_plane._PRESS_START_WAIT_S
    control_plane._PRESS_START_WAIT_S = 30.0
    try:
        def call() -> None:
            answer["result"] = plane.emulate_input_on(
                controller, "EmuMain", COORDS, "press")

        caller = threading.Thread(target=call, name="emulate-caller")
        caller.start()
        assert wheel.wait_for("EmulatedPress"), (
            f"the press never reached the wheel: {wheel.log}")

        # The deck moves on while the press waits its turn.
        load(controller, "EmuThird")
        pipeline._reset_delivered()
        wheel.fire_next()
        caller.join(timeout=10)
        assert not caller.is_alive(), "the caller never came back from the press"
    finally:
        control_plane._PRESS_START_WAIT_S = saved_wait
        control_plane.timer_wheel = timer_wheel_real

    result = answer["result"]
    assert not result.ok and result.code == "page-moved", (
        f"the press was reported as made on a page the deck had already left: "
        f"{result}")
    assert "EmuMain" in result.message and "EmuThird" in result.message, (
        f"the failure must name the page asked for and the one showing: "
        f"{result.message!r}")
    assert events_on(KEY) == [], (
        f"the press ran on the page that replaced the one it named: "
        f"{events_on(KEY)}")
    assert wheel.pending() == [], (
        f"a press that was never made must arm no release: {wheel.pending()}")

    print("PASS: a press whose page moved is refused instead of landing elsewhere")


# 4. What the deck is left holding

def leg_release_arms_when_the_press_raises(plane, controller) -> None:
    """A press that raises on the way down is still released.

    The wheel swallows what a job raises, so a release armed only after a clean
    return would never be armed at all. The input would keep the gesture it
    took on the way down, with its hold timer running and its press state set:
    a key held for the life of the process, a HOLD_START into a snapshot no
    finger is on, and the next physical release dispatched to a page that has
    moved on.
    """
    load(controller, "EmuMain")
    controller.hold_time = 2.0
    pipeline._reset_delivered()

    c_input = key_input(controller)
    real_event_callback = controller.event_callback
    seen: list = []

    def raising_event_callback(ident, *args, **kwargs):
        seen.append(args)
        real_event_callback(ident, *args, **kwargs)
        if args and args[0]:
            raise RuntimeError("an action raised on the way down")

    controller.event_callback = raising_event_callback
    try:
        assert plane.emulate_input(SERIAL, "EmuMain", COORDS, "press").ok
        assert fixtures.wait_until(lambda: len(seen) == 2, timeout=5.0), (
            f"the press raised and no release followed it: {seen}")
    finally:
        del controller.event_callback

    assert seen == [(True,), (False,)], f"the deck saw {seen}"
    assert fixtures.wait_until(lambda: c_input.down_start_time is None, timeout=5.0), (
        "the input is still holding the gesture it took on the way down")
    assert c_input._gesture is None, (
        "the DOWN-time snapshot outlived the press, which pins the actions of "
        "that page for the life of the process")
    assert c_input.hold_start_timer is None or not c_input.hold_start_timer.is_alive(), (
        "the hold timer is still armed on a key nothing is holding")

    print("PASS: a press that raises still releases the key it took down")


def leg_second_press_on_a_held_key_is_refused(plane, controller) -> None:
    """A press on a key that is already held is refused, not stacked.

    Two presses that overlap deliver DOWN, DOWN and then one release for both,
    which is a shape no hardware makes. An action that latches on the way down
    stays latched, and the second long press comes back as a short one.
    """
    load(controller, "EmuMain")
    controller.hold_time = 1.0
    pipeline._reset_delivered()

    c_input = key_input(controller)
    assert plane.emulate_input(SERIAL, "EmuMain", COORDS, "long-press").ok
    assert fixtures.wait_until(lambda: c_input.down_start_time is not None, timeout=5.0), (
        "the first press never reached the key")

    second = plane.emulate_input(SERIAL, "EmuMain", COORDS, "press")
    assert not second.ok and second.code == "input-held", (
        f"a second press stacked on top of the first: {second}")
    assert "(0,0)" in second.message, (
        f"the failure must name the position: {second.message!r}")

    assert fixtures.wait_until(lambda: UP in events_on(KEY), timeout=5.0), (
        f"the first press never released: {events_on(KEY)}")
    delivered = events_on(KEY)
    assert delivered.count(DOWN) == 1, (
        f"the key went down more than once for one press: {delivered}")

    print("PASS: a press on a key that is already held is refused")


# 5. What a bad request answers

def leg_bad_event_word_moves_nothing(plane, controller) -> None:
    """An event word this build does not know is refused before the switch.

    It is judged without a device, so nothing about it needs the page it names
    to be loaded first, and a deck left on another page is a side effect of a
    request that was never carried out.
    """
    load(controller, "EmuMain")
    pipeline._reset_delivered()

    wheel = stub_wheel()
    try:
        result = plane.emulate_input_on(controller, "EmuOther", OTHER_COORDS, "double-press")
    finally:
        control_plane.timer_wheel = timer_wheel_real

    assert not result.ok and result.code == "bad-event", result
    for word in control_plane.EMULATED_EVENTS:
        assert word in result.message, (
            f"the failure must name the words that do work: {result.message!r}")
    assert active_name(controller) == "EmuMain", (
        f"a request nothing can carry out moved the deck anyway: "
        f"{active_name(controller)}")
    assert wheel.log == [], f"a refused request still scheduled work: {wheel.log}"
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

    wheel = stub_wheel()
    controller.allow_interaction = False
    try:
        result = plane.emulate_input(SERIAL, "EmuMain", COORDS, "press")
    finally:
        controller.allow_interaction = True
        control_plane.timer_wheel = timer_wheel_real

    assert not result.ok and result.code == "input-blocked", result
    assert SERIAL in result.message and "locked" in result.message, result.message
    assert wheel.log == [], f"a refused press still scheduled work: {wheel.log}"
    assert events_on(KEY) == [], events_on(KEY)

    print("PASS: a locked session refuses the press instead of dropping it")


# 6. The device's own geometry

def leg_bounds_follow_the_rotation(plane) -> None:
    """The bounds come from the deck's layout as rotated, not as built.

    A rotated deck reports its key layout the other way round, and both verbs
    read it from the same place. Read from the raw device instead, a request
    for a key that exists is refused and one for a key that does not is
    accepted, on every deck a person has turned.
    """
    controller = fixtures.make_headless_controller(serial=ROTATED_SERIAL)
    try:
        rows_before, cols_before = controller.deck.key_layout()
        controller.deck.set_rotation(90)
        # The identifiers are built from the layout, so they are rebuilt here
        # as they are in the field, where the rotation is set before the inputs
        # are created.
        controller.init_inputs()
        rows, cols = controller.deck.key_layout()
        assert (rows, cols) == (cols_before, rows_before), (
            f"this deck does not report a rotated layout, so the leg proves "
            f"nothing: {(rows_before, cols_before)} became {(rows, cols)}")

        tall = f"0,{rows - 1}"   # in bounds rotated, past the end unrotated
        wide = f"{cols_before - 1},0"  # in bounds unrotated, past the end rotated

        for verb, refused in (
            ("press", plane.emulate_input_on(controller, "Main", wide, "press")),
            ("state", plane.change_state_on(controller, "Main", wide, 0)),
        ):
            assert not refused.ok and refused.code == "coords-out-of-bounds", (
                f"the {verb} accepted {wide!r}, which is past the end of this "
                f"deck as it is turned: {refused}")

        accepted = plane.change_state_on(controller, "Main", tall, 0)
        assert accepted.ok, (
            f"the state change refused {tall!r}, which this deck has as it is "
            f"turned: {accepted}")
        pressed = plane.emulate_input_on(controller, "Main", tall, "press")
        assert pressed.ok, (
            f"the press refused {tall!r}, which this deck has as it is turned: "
            f"{pressed}")
        assert fixtures.wait_until(
            lambda: key_input(controller, tall.replace(",", "x")).down_start_time is None,
            timeout=5.0), "the press on the rotated deck never released"
    finally:
        fixtures.teardown(controller)

    print("PASS: both verbs bound the coordinates by the rotated layout")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_input_emulation")
    fixtures._install_integration_globals()
    gl.plugin_manager = pipeline._PluginManager()

    # The plain seed first: it creates the pages directory the action pages are
    # written into, and gives the controller a default page to boot onto.
    fixtures.seed_page("Main")
    pipeline._seed_page("EmuMain", {"keys": {KEY: True}})
    pipeline._seed_page("EmuOther", {"keys": {OTHER_KEY: True}})
    # A third page with an action on the first page's key, so a press that
    # landed after a switch is visible rather than merely misdirected.
    pipeline._seed_page("EmuThird", {"keys": {KEY: True}})

    plane = control_plane.get()
    controller = fixtures.make_headless_controller(serial=SERIAL)
    try:
        leg_wheel_choreography(plane, controller)
        leg_short_press_clamps_to_the_hold_time(plane, controller)
        leg_short_press_semantics(plane, controller)
        leg_long_press_semantics(plane, controller)
        leg_press_switches_page_first(plane, controller)
        leg_press_waits_for_the_rebuild(plane, controller)
        leg_press_refuses_a_page_that_moved(plane, controller)
        leg_release_arms_when_the_press_raises(plane, controller)
        leg_second_press_on_a_held_key_is_refused(plane, controller)
        leg_bad_event_word_moves_nothing(plane, controller)
        leg_failures_match_the_state_verb(plane, controller)
        leg_locked_session_refuses_the_press(plane, controller)
        leg_bounds_follow_the_rotation(plane)
    finally:
        control_plane.timer_wheel = timer_wheel_real
        fixtures.teardown(controller)

    print("ALL PASS: scenario_input_emulation")


if __name__ == "__main__":
    main()
