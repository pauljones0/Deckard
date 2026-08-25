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


def press_on_a_worker(plane, controller, wheel, page_ref="EmuMain", coords=COORDS,
                      event="press"):
    """Start a press on another thread and wait until it reaches the wheel.

    The caller blocks until the press is taken or refused, so a leg that wants
    to change the deck in that gap cannot be the caller. Returns the thread and
    a dict that carries the answer once it comes back.
    """
    answer: dict = {}

    def call() -> None:
        answer["result"] = plane.emulate_input_on(controller, page_ref, coords, event)

    caller = threading.Thread(target=call, name="emulate-caller")
    caller.start()
    assert wheel.wait_for("EmulatedPress"), (
        f"the press never reached the wheel: {wheel.log}")
    return caller, answer


def armed_releases(wheel) -> list:
    return [name for (_delay, name) in wheel.log if name == "EmulatedRelease"]


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


def keys_are_idle(controller) -> bool:
    """Whether no key on this deck is holding a gesture.

    A key holds one from its DOWN until its release. The snapshot is dropped
    last, after the release dispatched every event it owes, so a key that
    holds none has handed all of them to the pool. The hold timer is read as
    well: each path that ends a gesture cancels it, so a timer still alive
    means none of them has run yet.
    """
    for c_input in controller.inputs[Input.Key]:
        if c_input.down_start_time is not None or c_input._gesture is not None:
            return False
        timer = c_input.hold_start_timer
        if timer is not None and timer.is_alive():
            return False
    return True


def key_is_holding(c_input) -> bool:
    """Whether this key has taken a press, and finished taking it.

    The DOWN branch sets the gesture clock, resolves the snapshot, arms the
    hold timer and dispatches, in that order, and resolving the snapshot is a
    real call. A leg that waits on the clock alone acts on a press the key is
    still in the middle of taking: it reads a snapshot that is not there yet,
    or cancels a hold timer that is armed a moment after it.
    """
    return (c_input.down_start_time is not None
            and c_input._gesture is not None
            and c_input.hold_start_timer is not None)


def drain_action_pool(controller, timeout: float = 10.0) -> None:
    """Return once every action callback already queued has run.

    The deck hands each input event to a pool of worker threads, so the order
    the events are recorded in is not the order they were dispatched in: a
    DOWN handed to a busy worker is recorded after an UP handed to a free one.
    One job per worker, all waiting at the same barrier, holds the whole pool
    at once. The pool has no worker left over for a job queued before them, so
    every one of those has finished by the time the barrier trips.
    """
    pool = controller.action_executor
    assert pool is not None, "the deck has no action pool to drain"
    workers = pool._max_workers
    barrier = threading.Barrier(workers + 1)
    for _ in range(workers):
        assert pool.submit(barrier.wait, timeout) is not None, (
            "the action pool refused a drain job")
    try:
        barrier.wait(timeout)
    except threading.BrokenBarrierError:
        raise AssertionError(
            f"the action pool held work for longer than {timeout:g}s, so an "
            f"event dispatched here can still be recorded later") from None


def settle(controller) -> None:
    """Leave the deck owing nothing to whatever runs next.

    A leg proves the events it waited for, and nothing else. Two things
    outlive it otherwise. A release armed on the real wheel runs once the leg
    has returned, and the pool records an event dispatched earlier later,
    because it hands each one to whichever worker is free. Both land in the
    next leg, which cleared the recorder on its way in, and read there as
    events that leg produced: a key that went down twice, or a release with no
    press in front of it.

    So a leg ends here. First no key is holding a gesture, which is what a
    release drops last and therefore proof that it dispatched. Then the pool
    is drained, which is proof that what it dispatched was recorded.
    """
    assert fixtures.wait_until(lambda: keys_are_idle(controller), timeout=10.0), (
        "a key is still holding the gesture this leg started, so its release "
        "would land in the leg after it")
    drain_action_pool(controller)


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

    # The UP this leg waited for says nothing about the events in front of it,
    # which the pool may still be holding. Read the whole press only once they
    # are all recorded.
    settle(controller)
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

    settle(controller)
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


def leg_press_survives_a_reload_of_the_same_page(plane, controller) -> None:
    """A page rebuilt in the gap is still the page the request named.

    A cache eviction and a save in the page editor both build a new Page object
    for the same file. The deck is showing what it was asked for either way, so
    a press judged on the object rather than on the file is refused with a
    failure that names one page on both sides of itself.
    """
    load(controller, "EmuMain")
    controller.hold_time = 2.0
    pipeline._reset_delivered()

    path = gl.page_manager.find_matching_page_path("EmuMain")
    wheel = stub_wheel()
    saved_wait = control_plane._PRESS_START_WAIT_S
    control_plane._PRESS_START_WAIT_S = 30.0
    try:
        caller, answer = press_on_a_worker(plane, controller, wheel)
        # The same file, built again. This is what an eviction leaves behind.
        rebuilt = gl.page_manager.load_page(path, controller)
        assert rebuilt is not None and rebuilt is not controller.active_page, (
            "this leg needs a second object for the same page file")
        controller.load_page(rebuilt)
        assert fixtures.wait_until(lambda: controller.active_page is rebuilt), (
            "the rebuilt page never became the active one")

        wheel.fire_next()
        caller.join(timeout=10)
        assert not caller.is_alive(), "the caller never came back from the press"
    finally:
        control_plane._PRESS_START_WAIT_S = saved_wait
        control_plane.timer_wheel = timer_wheel_real

    result = answer["result"]
    assert result.ok, (
        f"a page rebuilt from the same file was read as a different page: "
        f"{result}")
    assert fixtures.wait_until(lambda: DOWN in events_on(KEY), timeout=5.0), (
        f"the press never reached the rebuilt page: {events_on(KEY)}")
    assert armed_releases(wheel) == ["EmulatedRelease"], wheel.log
    wheel.fire_next()
    assert fixtures.wait_until(lambda: UP in events_on(KEY), timeout=5.0), events_on(KEY)

    print("PASS: a press survives its page being rebuilt from the same file")


def leg_locked_in_the_gap_refuses_the_press(plane, controller) -> None:
    """A session that locks in the gap refuses the press at the deck.

    The caller checks before it arms and the deck checks again before the key
    goes down, because the lock arrives from logind whenever it arrives. Only
    the second check can be true when it matters: the deck's own entry point
    drops every event while interaction is off, so a press that went ahead
    would be reported as made and dropped without a word.
    """
    load(controller, "EmuMain")
    pipeline._reset_delivered()

    wheel = stub_wheel()
    saved_wait = control_plane._PRESS_START_WAIT_S
    control_plane._PRESS_START_WAIT_S = 30.0
    try:
        caller, answer = press_on_a_worker(plane, controller, wheel)
        controller.allow_interaction = False
        wheel.fire_next()
        caller.join(timeout=10)
        assert not caller.is_alive(), "the caller never came back from the press"
    finally:
        controller.allow_interaction = True
        control_plane._PRESS_START_WAIT_S = saved_wait
        control_plane.timer_wheel = timer_wheel_real

    result = answer["result"]
    assert not result.ok and result.code == "input-blocked", (
        f"a press into a locked session was reported as made: {result}")
    assert events_on(KEY) == [], events_on(KEY)
    assert armed_releases(wheel) == [], (
        f"a press that never happened armed a release: {wheel.log}")

    print("PASS: a session locked in the gap refuses the press at the deck")


def leg_press_gives_up_when_the_wheel_stalls(plane, controller) -> None:
    """A caller whose wait runs out takes the press with it.

    The two threads settle who owns the press under one lock. Without that, a
    caller that reports a failure leaves a job that presses the key anyway,
    which is the same false answer in the other direction: a person told the
    press was not made, and a deck that made it.
    """
    load(controller, "EmuMain")
    pipeline._reset_delivered()

    wheel = stub_wheel()
    saved_wait = control_plane._PRESS_START_WAIT_S
    control_plane._PRESS_START_WAIT_S = 0.2
    try:
        result = plane.emulate_input(SERIAL, "EmuMain", COORDS, "press")
        assert not result.ok and result.code == "press-not-started", (
            f"a press that never reached the deck was reported as made: {result}")
        assert f"{0.2:g}s" in result.message, (
            f"the failure must say how long it waited: {result.message!r}")

        # The job arrives late, finds the press claimed, and presses nothing.
        wheel.fire_next()
    finally:
        control_plane._PRESS_START_WAIT_S = saved_wait
        control_plane.timer_wheel = timer_wheel_real

    assert not fixtures.wait_until(lambda: bool(events_on(KEY)), timeout=1.0), (
        f"the press was reported as not made and the key went down anyway: "
        f"{events_on(KEY)}")
    assert armed_releases(wheel) == [], (
        f"a press nobody owns armed a release: {wheel.log}")

    print("PASS: a press the caller gave up on is not made by the job behind it")


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
    # The release clears the gesture clock first and drops the snapshot last,
    # with a dispatch between the two. A wait on the clock alone reads the
    # snapshot while the release is still running, and reports a press that
    # held on to it when what it caught was this leg's own timing.
    assert fixtures.wait_until(lambda: keys_are_idle(controller), timeout=5.0), (
        f"the release never finished: gesture clock "
        f"{c_input.down_start_time!r}, snapshot still held "
        f"{c_input._gesture is not None}")
    assert c_input.down_start_time is None, (
        "the input is still holding the gesture it took on the way down")
    assert c_input._gesture is None, (
        "the DOWN-time snapshot outlived the press, which pins the actions of "
        "that page for the life of the process")
    assert c_input.hold_start_timer is None or not c_input.hold_start_timer.is_alive(), (
        "the hold timer is still armed on a key nothing is holding")

    print("PASS: a press that raises still releases the key it took down")


def leg_swallowed_press_arms_no_release(plane, controller) -> None:
    """A DOWN the deck swallows leaves no release behind it.

    The deck drops a DOWN without a word in more than one state, and this leg
    uses the one it can produce on demand: the addressed input is gone by the
    time the job runs, which a rebuild of the input set does. Nothing is held
    down afterwards, so a release armed anyway would land on whatever the key
    is doing later. A finger on that key would have its own press ended under
    it, and then its own release swallowed as the stray it looks like.
    """
    load(controller, "EmuMain")
    pipeline._reset_delivered()

    wheel = stub_wheel()
    saved_wait = control_plane._PRESS_START_WAIT_S
    saved_keys = controller.inputs[Input.Key]
    control_plane._PRESS_START_WAIT_S = 30.0
    try:
        caller, answer = press_on_a_worker(plane, controller, wheel)
        # The input set goes out from under the press.
        controller.inputs[Input.Key] = []
        wheel.fire_next()
        caller.join(timeout=10)
        assert not caller.is_alive(), "the caller never came back from the press"
    finally:
        controller.inputs[Input.Key] = saved_keys
        control_plane._PRESS_START_WAIT_S = saved_wait
        control_plane.timer_wheel = timer_wheel_real

    # The deck took the press and dropped it, as it drops a physical press on
    # an input that is being rebuilt. What must not happen is the release.
    assert answer["result"].ok, answer["result"]
    assert events_on(KEY) == [], events_on(KEY)
    assert armed_releases(wheel) == [], (
        f"a DOWN that the deck swallowed armed a release: {wheel.log}")

    print("PASS: a press the deck swallows arms no release")


def leg_release_only_ends_its_own_gesture(plane, controller) -> None:
    """A release ends the press it made, and never the one it finds.

    A screensaver sweep, a rebuild of the inputs, or a finger that takes the
    key all end the gesture an emulated press took. The release that was armed
    for it must not then end whatever is on the key instead: that cuts a
    finger's press short while the finger is still down, and the finger's own
    release afterwards delivers nothing, because the key is already up.
    """
    load(controller, "EmuMain")
    controller.hold_time = 1.0
    pipeline._reset_delivered()

    c_input = key_input(controller)
    deck = fixtures.raw_deck(controller)
    wheel = stub_wheel(fire_at_once=("EmulatedPress",))
    try:
        assert plane.emulate_input(SERIAL, "EmuMain", COORDS, "long-press").ok
        assert wheel.wait_for("EmulatedRelease"), wheel.log
        assert fixtures.wait_until(lambda: key_is_holding(c_input)), (
            "the emulated press never reached the key")

        # The emulated gesture ends without its release, which is what the
        # screensaver's own sweep does to every input it confiscates. It is
        # cancelled once the key has finished taking the press, because a
        # cancel inside that would leave the hold timer armed behind it.
        c_input.cancel_gesture()
        pipeline._reset_delivered()

        # A finger takes the same key.
        deck.fire_key_event(0, True)
        assert fixtures.wait_until(lambda: key_is_holding(c_input)), (
            "the finger's press never reached the key")
        finger_gesture = c_input._gesture
        assert finger_gesture is not None

        # The release armed for the emulated press arrives now. It is not the
        # finger's to end. fire_next runs the callback on this thread, so the
        # state below is settled by the time it returns.
        wheel.fire_next()
        assert c_input._gesture is finger_gesture, (
            "the stale release ended the finger's press")
        assert c_input.down_start_time is not None, (
            "the stale release let the key up while the finger was still down")
        assert SHORT_UP not in events_on(KEY) and UP not in events_on(KEY), (
            f"the stale release dispatched a release against the finger's "
            f"gesture: {events_on(KEY)}")

        # Let the finger go, so the deck is left as this leg found it.
        deck.fire_key_event(0, False)
        assert fixtures.wait_until(lambda: UP in events_on(KEY), timeout=5.0), (
            f"the finger's own release delivered nothing: {events_on(KEY)}")
    finally:
        control_plane.timer_wheel = timer_wheel_real

    print("PASS: a release ends the gesture its own press took, and no other")


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
    assert fixtures.wait_until(lambda: key_is_holding(c_input), timeout=5.0), (
        "the first press never reached the key")

    second = plane.emulate_input(SERIAL, "EmuMain", COORDS, "press")
    assert not second.ok and second.code == "input-held", (
        f"a second press stacked on top of the first: {second}")
    assert "(0,0)" in second.message, (
        f"the failure must name the position: {second.message!r}")

    assert fixtures.wait_until(lambda: UP in events_on(KEY), timeout=5.0), (
        f"the first press never released: {events_on(KEY)}")
    settle(controller)
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
        # Every leg is settled before the next one starts, whether or not it
        # left anything behind: see settle. Driving that from here rather than
        # from each leg keeps a leg added later from having to know.
        for leg in (
            leg_wheel_choreography,
            leg_short_press_clamps_to_the_hold_time,
            leg_short_press_semantics,
            leg_long_press_semantics,
            leg_press_switches_page_first,
            leg_press_waits_for_the_rebuild,
            leg_press_refuses_a_page_that_moved,
            leg_press_survives_a_reload_of_the_same_page,
            leg_locked_in_the_gap_refuses_the_press,
            leg_press_gives_up_when_the_wheel_stalls,
            leg_release_arms_when_the_press_raises,
            leg_swallowed_press_arms_no_release,
            leg_release_only_ends_its_own_gesture,
            leg_second_press_on_a_held_key_is_refused,
            leg_bad_event_word_moves_nothing,
            leg_failures_match_the_state_verb,
            leg_locked_session_refuses_the_press,
        ):
            leg(plane, controller)
            settle(controller)
        # This one builds a deck of its own and tears it down again, and no
        # leg follows it, so it owes nothing to the deck above.
        leg_bounds_follow_the_rotation(plane)
    finally:
        control_plane.timer_wheel = timer_wheel_real
        fixtures.teardown(controller)

    print("ALL PASS: scenario_input_emulation")


if __name__ == "__main__":
    main()
