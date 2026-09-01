"""Drive emulated presses through the physical input path.
Preserve event, thread, release timing, and requested-page semantics."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import threading  # noqa: E402
import time  # noqa: E402

import globals as gl  # noqa: E402

from src.backend import control_plane  # noqa: E402
from src.backend.DeckManagement.deck_events import KeyEvent
from src.backend.DeckManagement.InputIdentifier import Input  # noqa: E402

# Reuse the recording action and plugin manager without running that scenario.
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
    """Record scheduled jobs and run selected due jobs on worker threads.
    Other jobs wait for explicit firing so tests can inspect their delays."""

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


def start_worker_press(plane, controller, wheel, page_ref="EmuMain", coords=COORDS,
                      event="press"):
    """Start a press on a worker and wait until it reaches the wheel.
    Return the blocked thread and the dict that later receives its result."""
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
    """Return whether all keys released their gesture snapshots and timers.
    Snapshot removal follows release dispatch, so idle keys owe no events."""
    for c_input in controller.inputs[Input.Key]:
        if c_input.down_start_time is not None or c_input._gesture is not None:
            return False
        timer = c_input.hold_start_timer
        if timer is not None and timer.is_alive():
            return False
    return True


def key_is_holding(c_input) -> bool:
    """Return whether the key completed press setup.
    The clock alone can precede snapshot resolution and timer creation."""
    return (c_input.down_start_time is not None
            and c_input._gesture is not None
            and c_input.hold_start_timer is not None)


def drain_action_pool(controller, timeout: float = 10.0) -> None:
    """Wait until every queued action callback runs.
    A barrier job on each worker drains earlier work from the whole pool."""
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
    """Wait for releases, then drain callbacks before the next leg.
    This order prevents delayed releases or recordings from crossing legs."""
    assert fixtures.wait_until(lambda: keys_are_idle(controller), timeout=10.0), (
        "a key is still holding the gesture this leg started, so its release "
        "would land in the leg after it")
    drain_action_pool(controller)


def stub_wheel(fire_at_once: "tuple[str, ...]" = ()) -> StubWheel:
    """Replace only the control-plane wheel; the input hold timer stays real."""
    wheel = StubWheel(fire_at_once=fire_at_once)
    control_plane.timer_wheel = wheel
    return wheel


def leg_wheel_choreography(plane, controller) -> None:
    """Require fixed press and release wheel jobs in order.
    The long-press margin must let HOLD_START fire before release cancels it."""
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

        # Judge only the press thread because this leg fires the release itself.
        assert dispatched_on and dispatched_on[0] is not threading.main_thread(), (
            f"the press reached the input path on the caller's own thread: "
            f"{dispatched_on[0].name} -- a hardware press never does")

        wheel.fire_next()
        assert fixtures.wait_until(lambda: UP in events_on(KEY)), (
            f"the release job delivered no key up: {events_on(KEY)}")
        assert wheel.pending() == [], f"the press left work behind: {wheel.pending()}"

        # Add a margin to the deck hold time before a long-press release.
        pipeline._reset_delivered()
        wheel.log.clear()
        assert plane.emulate_input(SERIAL, "EmuMain", COORDS, "long-press").ok
        assert wheel.wait_for("EmulatedRelease"), wheel.log
        assert wheel.log == [(0.0, "EmulatedPress"), (10.25, "EmulatedRelease")], (
            f"a long press on a deck whose hold time is 10s must hold for "
            f"10.25s, and this was {wheel.log}")
        # End through the real path so later legs inherit no held gesture.
        wheel.fire_next()
        assert fixtures.wait_until(lambda: UP in events_on(KEY)), events_on(KEY)
    finally:
        control_plane.timer_wheel = timer_wheel_real
        del controller.event_callback

    print("PASS: a press is one job due at once and a release behind it")


def leg_short_press_hold_clamp(plane, controller) -> None:
    """Clamp a short press below a shorter deck hold threshold.
    A 0.05-second threshold requires the 0.025-second release below."""
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


def leg_short_press_semantics(plane, controller) -> None:
    """Deliver DOWN, SHORT_UP, and UP off-main before the hold threshold."""
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

    # Drain the pool because UP can be recorded before earlier events.
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
    """Deliver HOLD_START, HOLD_STOP, and UP after a shortened real hold time."""
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


def leg_press_switches_page_first(plane, controller) -> None:
    """Switch to the requested page before delivering its press.
    Its action uses a key that is empty on the initial page."""
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


def leg_rebuild_wait(plane, controller) -> None:
    """Wait for the asynchronous input rebuild queued by the page switch.
    Holding the fast fake-deck rebuild makes a skipped barrier observable."""
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


def leg_page_moved_press(plane, controller) -> None:
    """Cancel a press when the active page changes before delivery.
    The replacement page shares the key, so an incorrect delivery is visible."""
    load(controller, "EmuMain")
    controller.hold_time = 2.0
    pipeline._reset_delivered()

    answer: dict = {}
    wheel = stub_wheel()
    # Widen the wait so the page-moved decision, not the test clock, wins.
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


def leg_same_page_reload(plane, controller) -> None:
    """Accept a new Page object loaded from the same requested file."""
    load(controller, "EmuMain")
    controller.hold_time = 2.0
    pipeline._reset_delivered()

    path = gl.page_manager.find_matching_page_path("EmuMain")
    wheel = stub_wheel()
    saved_wait = control_plane._PRESS_START_WAIT_S
    control_plane._PRESS_START_WAIT_S = 30.0
    try:
        caller, answer = start_worker_press(plane, controller, wheel)
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


def leg_lock_before_delivery(plane, controller) -> None:
    """Refuse a press when the session locks between request and delivery.
    The deck must recheck because disabled interaction drops input silently."""
    load(controller, "EmuMain")
    pipeline._reset_delivered()

    wheel = stub_wheel()
    saved_wait = control_plane._PRESS_START_WAIT_S
    control_plane._PRESS_START_WAIT_S = 30.0
    try:
        caller, answer = start_worker_press(plane, controller, wheel)
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


def leg_stalled_press_timeout(plane, controller) -> None:
    """Cancel the wheel job when its caller times out.
    Ownership must prevent a reported failure from pressing the key later."""
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


def leg_release_after_press_error(plane, controller) -> None:
    """Release a key even when press dispatch raises.
    The wheel swallows job errors, so release must not depend on clean return."""
    load(controller, "EmuMain")
    controller.hold_time = 2.0
    pipeline._reset_delivered()

    c_input = key_input(controller)
    real_event_callback = controller.event_callback
    seen: list = []

    def raising_event_callback(ident, *args, **kwargs):
        seen.append(args)
        real_event_callback(ident, *args, **kwargs)
        if args and args[0].pressed:
            raise RuntimeError("an action raised on the way down")

    controller.event_callback = raising_event_callback
    try:
        assert plane.emulate_input(SERIAL, "EmuMain", COORDS, "press").ok
        assert fixtures.wait_until(lambda: len(seen) == 2, timeout=5.0), (
            f"the press raised and no release followed it: {seen}")
    finally:
        del controller.event_callback

    assert seen == [(KeyEvent(pressed=True),), (KeyEvent(pressed=False),)], f"the deck saw {seen}"
    # Wait for snapshot removal because the gesture clock clears before dispatch.
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
    """Arm no release when a rebuilt input set swallows DOWN.
    A later release could otherwise end an unrelated physical press."""
    load(controller, "EmuMain")
    pipeline._reset_delivered()

    wheel = stub_wheel()
    saved_wait = control_plane._PRESS_START_WAIT_S
    saved_keys = controller.inputs[Input.Key]
    control_plane._PRESS_START_WAIT_S = 30.0
    try:
        caller, answer = start_worker_press(plane, controller, wheel)
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


def leg_stale_release_gesture_isolation(plane, controller) -> None:
    """End only the gesture created by the matching emulated press.
    A stale release must not end a later physical press on the same key."""
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

        # Cancel after press setup, as the screensaver sweep does.
        c_input.cancel_gesture()
        pipeline._reset_delivered()

        # A finger takes the same key.
        deck.fire_key_event(0, True)
        assert fixtures.wait_until(lambda: key_is_holding(c_input)), (
            "the finger's press never reached the key")
        finger_gesture = c_input._gesture
        assert finger_gesture is not None

        # fire_next settles the stale release on this thread before inspection.
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


def leg_held_key_second_press(plane, controller) -> None:
    """Refuse a second press while the key is held.
    Hardware cannot produce overlapping DOWN events with one shared release."""
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


def leg_bad_event_word_moves_nothing(plane, controller) -> None:
    """Refuse an unknown event word before changing the page."""
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


def leg_state_verb_failures(plane, controller) -> None:
    """Return the same coordinate failures for press and state-change verbs."""
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


def leg_locked_session_press(plane, controller) -> None:
    """Report blocked input instead of sending a press into a locked session."""
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


def leg_rotated_bounds(plane) -> None:
    """Bound both verbs against the rotated logical layout, not raw geometry."""
    controller = fixtures.make_headless_controller(serial=ROTATED_SERIAL)
    try:
        rows_before, cols_before = controller.deck.key_layout()
        controller.deck.set_rotation(90)
        # Rebuild identifiers after rotation, matching controller initialization.
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
    gl.plugin_manager = pipeline._StubPluginManager()

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
        # Settle centrally so each leg starts without pending input work.
        for leg in (
            leg_wheel_choreography,
            leg_short_press_hold_clamp,
            leg_short_press_semantics,
            leg_long_press_semantics,
            leg_press_switches_page_first,
            leg_rebuild_wait,
            leg_page_moved_press,
            leg_same_page_reload,
            leg_lock_before_delivery,
            leg_stalled_press_timeout,
            leg_release_after_press_error,
            leg_swallowed_press_arms_no_release,
            leg_stale_release_gesture_isolation,
            leg_held_key_second_press,
            leg_bad_event_word_moves_nothing,
            leg_state_verb_failures,
            leg_locked_session_press,
        ):
            leg(plane, controller)
            settle(controller)
        # This one builds a deck of its own and tears it down again, and no
        # leg follows it, so it owes nothing to the deck above.
        leg_rotated_bounds(plane)
    finally:
        control_plane.timer_wheel = timer_wheel_real
        fixtures.teardown(controller)

    print("ALL PASS: scenario_input_emulation")


if __name__ == "__main__":
    main()
