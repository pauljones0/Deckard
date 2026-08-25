"""One place decides whether a page or state switch is valid.

"Switch deck S to page P", "set input (x,y) on page P of deck S to state N"
and "press input (x,y) on page P of deck S" arrive from several transports.
D-Bus methods carry them to the running instance, from an external tool or
from a second CLI invocation. Argv requests carry the first two into a booting
process, which parks each one for a deck that has not enumerated yet. A press
is not one of those: see emulate_input_on.
A copy of the rules per transport drifts apart, so this module is the one rule
set they all ask. The decision lives here, and the rendering stays at the
surface. Nothing here logs, and nothing here touches the toolkit.

No blanket except

An invalid request is a result. An unexpected exception, such as a load_page
that raises or a device gone mid-call, propagates to the caller untouched. The
boot path peeks a parked state request, applies it through here, and resolves
it once the apply returns. An exception on the way through therefore leaves
the request parked for the next load to retry
(see src/backend/startup_queue.py). A catch here turns that retry into a
silent drop.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

# globals and the input identifiers at runtime, everything else under
# TYPE_CHECKING, and no gi. The deck controller imports this module, so it
# sits inside the render engine's import closure and takes both guards of that
# closure, the widget-free rule (scenario_headless_engine_no_gtk) and the
# named gl surface (scenario_engine_gl_surface, and this module reads
# deck_manager and page_manager and nothing else). The deployment floor runs
# Python 3.13, which evaluates an annotation at definition time, and the
# future import above prevents that.
import globals as gl
from src.backend import timer_wheel
from src.backend.DeckManagement.InputIdentifier import Input

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.deck_controller.inputs import ControllerKey
    from src.backend.PageManagement.Page import Page
    from src.backend.PageManagement.PageManagerBackend import PageManagerBackend


@dataclass(frozen=True)
class ControlResult:
    """The outcome of one control request.

    code is the vocabulary a caller branches on. message says the same thing
    to a person, and every current surface renders that part alone. A message
    names the deck only where the failure is about the deck, which is an
    unknown serial or a state changed. Each surface adds its own context.

      ""                      the request applied
      "already-active"        the deck already shows that page, so nothing
                              happens. This is an ok result, because a no-op
                              fulfils the request.
      "no-such-deck"          no controller reports that serial
      "no-such-page"          no page file matches that name or path
      "page-build-failed"     the page exists and could not be built
      "no-page-manager"       asked before the page store exists, on boot only
      "bad-coords"            the coordinates do not read as x,y
      "coords-out-of-bounds"  outside this device's key layout
      "no-such-input"         in bounds, and the deck has no input there
      "bad-state"             the state number is no integer
      "state-out-of-range"    that input has no such state
      "bad-event"             no emulated input event goes by that word
      "input-blocked"         the deck is not taking input, because the
                              session is locked
      "input-held"            that input is already held down
      "page-moved"            the page the press named stopped showing before
                              the press reached the deck
      "press-not-started"     the press did not reach the deck in time, and
                              will not be made
    """

    ok: bool
    code: str = ""
    message: str = ""


def _controllers() -> list[DeckController]:
    """Snapshot of the live controllers. A snapshot because USB hotplug and
    teardown mutate this list from their own threads while a request walks
    it."""
    deck_manager = gl.deck_manager
    if deck_manager is None:
        return []
    return list(deck_manager.deck_controller)


def _no_such_deck(serial_number: str) -> ControlResult:
    """The unknown-serial result. It lists what is connected, because the
    answer to "did I mistype the serial?" belongs in the failure itself.

    With nothing connected there is no list to give, and an empty answer reads
    two ways. The app answers a request from the moment it takes the bus name,
    which comes before it enumerates a deck. So the empty case names that
    possibility, rather than let a person conclude that a deck is unplugged
    while it is only not open yet.
    """
    available = [controller.serial_number() for controller in _controllers()]
    if available:
        message = (f"StreamDeck with serial '{serial_number}' not found. "
                   f"Available devices: {', '.join(available)}")
    else:
        message = ("No StreamDeck devices connected yet "
                   "(the app may still be starting)")
    return ControlResult(False, "no-such-deck", message)


def _no_such_page(page_ref: str, page_manager: PageManagerBackend) -> ControlResult:
    """The unknown-page result, listing the page names that do exist."""
    available = [os.path.splitext(os.path.basename(p))[0]
                 for p in page_manager.get_pages()]
    return ControlResult(False, "no-such-page",
                         f"Page '{page_ref}' not found. "
                         f"Available pages: {', '.join(available)}")


def _page_moved(controller: DeckController, page_ref: str, showing: str) -> ControlResult:
    """The result for a press whose page stopped showing before it landed.

    A page switch can arrive from a plugin action, another transport, a window
    rule or the screensaver at any point, and a press that went ahead anyway
    would run the actions of a page nobody asked for. showing is what the deck
    was on when the press was refused, read there rather than here: a fresh
    read would name a third page, later than either half saw.
    """
    return ControlResult(False, "page-moved",
                         f"Device {controller.serial_number()} stopped showing page "
                         f"'{page_ref}' before the press landed, and shows {showing} "
                         f"now, so nothing was pressed")


def _input_blocked(controller: DeckController) -> ControlResult:
    """The result for a deck that is not taking input.

    The lock screen turns interaction off for as long as the session is locked,
    and the deck's own entry point drops every event while it is off. A press
    reported as done and then dropped there is the one outcome a caller cannot
    tell from a press that ran.
    """
    return ControlResult(False, "input-blocked",
                         f"Device {controller.serial_number()} is not taking input "
                         f"right now, because the session is locked")


def _press_refused(controller: DeckController, page_ref: str, coords: str,
                   code: str, showing: str) -> ControlResult:
    """Render what the deck said about a press it would not make.

    One arm per code, and a last one that says the code it does not know
    rather than dress it as one of the others. A code added to _Press without
    a sentence here then reads as itself, which is wrong and obvious, instead
    of reading as a wait that ran out, which is wrong and quiet.
    """
    if code == "page-moved":
        return _page_moved(controller, page_ref, showing)
    if code == "input-blocked":
        return _input_blocked(controller)
    if code == "press-not-started":
        return ControlResult(False, code,
                             f"The press on ({coords}) did not reach device "
                             f"{controller.serial_number()} within "
                             f"{_PRESS_START_WAIT_S:g}s, so nothing was pressed")
    return ControlResult(False, code,
                         f"The press on ({coords}) was not made on device "
                         f"{controller.serial_number()}: {code}")


def _key_at(controller: DeckController, coords: str) -> "tuple[ControllerKey, int, int] | ControlResult":
    """The key input at coords on controller, with its coordinates, or the
    result that says why there is none.

    Both entry points that address one input read it from here, so they refuse
    the same coordinates with the same sentence. The coordinates come back
    parsed, because each caller names them in its own message. The bounds are
    this device's own key layout and this input's own presence. No constant
    supplies them, because no constant holds for every deck.
    """
    try:
        x, y = map(int, coords.split(","))
    except (ValueError, AttributeError):
        return ControlResult(False, "bad-coords",
                             f"Invalid coordinate format '{coords}'. "
                             f"Expected format: 'x,y' (e.g., '0,0')")

    rows, cols = controller.deck.key_layout()
    if x < 0 or x >= cols or y < 0 or y >= rows:
        return ControlResult(False, "coords-out-of-bounds",
                             f"Coordinates ({x},{y}) are out of bounds for this device. "
                             f"Valid range: x=0-{cols - 1}, y=0-{rows - 1}")

    c_input = controller.get_input(Input.Key(f"{x}x{y}"))
    if c_input is None:
        return ControlResult(False, "no-such-input",
                             f"Could not find input at coordinates ({x},{y})")
    return c_input, x, y


#: The event words emulate_input_on knows. A press is one that ends before the
#: deck's hold time; a long press is one that outlasts it, which is what makes
#: an input's HOLD_START and HOLD_STOP fire. src/backend/cli_forward.py holds a
#: copy, because it stays importable before globals and this module does not,
#: and a scenario pins the two lists to each other.
PRESS = "press"
LONG_PRESS = "long-press"
EMULATED_EVENTS = (PRESS, LONG_PRESS)

#: How long an emulated short press holds the input down. A press with no
#: duration is a shape no finger makes, and an action that reads the gap
#: between its DOWN and its UP would see one. It is halved against a deck whose
#: hold time is under twice this, so a short press stays short whatever that
#: setting says.
_PRESS_DOWN_S = 0.05

#: How far past the deck's hold time an emulated long press holds the input.
#: The input arms its own hold timer for hold_time on the same wheel, and that
#: timer is what fires HOLD_START. The release cancels that timer, so it must
#: land far enough behind it for it to have fired: a margin under the wheel's
#: dispatch delay turns a long press into a release with no HOLD_START before
#: it.
_LONG_PRESS_EXTRA_S = 0.25


def _hold_seconds(controller: DeckController, event: str) -> float | None:
    """How long event holds the input down on controller, or None for a word
    this module does not know."""
    if event == PRESS:
        return min(_PRESS_DOWN_S, controller.hold_time / 2)
    if event == LONG_PRESS:
        return controller.hold_time + _LONG_PRESS_EXTRA_S
    return None


#: How long emulate_input_on waits for its press to reach the deck. The press
#: leg is due at once, so this covers a wheel that a loaded machine has not
#: scheduled yet and no work of its own. A caller whose wait runs out takes the
#: press with it: the two threads settle who owns it under one lock, so a press
#: is never both reported as failed and delivered. It bounds how long a control
#: method holds the context it was dispatched on, which is what
#: cli_forward.CONTROL_CALL_TIMEOUT_MS is sized against.
_PRESS_START_WAIT_S = 2.0


class _Press:
    """One emulated press, owned by whichever thread claims it first.

    The caller validates the request and arms the press; the wheel delivers it.
    Either side can find that it should not happen: the wheel when the deck
    stopped showing the page or stopped taking input, the caller when its wait
    runs out. The lock settles that once, so the press is delivered exactly
    once or not at all, and the caller's answer says which.

    Both legs go through DeckController.event_callback, which is where a
    hardware press arrives once the reader thread has turned a key index into
    an identifier. Everything past that point is the same code, so the event
    assigners, the DOWN-time gesture snapshot and the hold timer treat this
    press exactly as they treat a finger.

    Neither leg runs on the caller's thread. A hardware press arrives on the
    device's reader thread and never on the main loop, and an action that
    marshals its own work onto the main thread would wedge against a caller
    that is the main thread and is inside this call. A zero delay on the wheel
    buys that context, because the wheel runs every callback on a thread of its
    own.

    The release is a timer and not a sleep. A parked thread per emulated press
    is a thread held for the length of a long press, and the wheel that already
    carries the hold timers costs none.
    """

    def __init__(self, controller: DeckController, identifier: Input.Key,
                 page: "Page", hold_s: float) -> None:
        self._controller = controller
        self._identifier = identifier
        # The page the request named, by path. Identity would do for a switch
        # and for nothing else: a reload of the same page, which a cache
        # eviction or a save in the page editor performs, builds a new object
        # for the same file, and a press refused there names one page on both
        # sides of its own failure.
        self._page_path = os.path.abspath(page.json_path)
        self._hold_s = hold_s
        self._lock = threading.Lock()
        self._settled = False
        self._verdict = ""
        self._started = threading.Event()
        #: What the deck was showing when deliver looked, rendered for the
        #: message. Recorded there, so nothing reads active_page a third time
        #: and reports a page that neither half saw.
        self.showing = "nothing"
        #: The gesture this press's own DOWN created, and the only one its
        #: release may end.
        self._gesture: object | None = None

    def deliver(self) -> None:
        """Press the input, unless the request is no longer worth carrying
        out. The wheel runs this, off the caller's thread.

        The deck is read again here, and not only at validation. Between the
        two the caller answers nothing and this job sits on a wheel it shares
        with every other delay in the process, so a page switch or a screen
        lock from any source can land in between. A press that went ahead
        anyway would run the actions of a page nobody named, or report as done
        a press the deck drops.
        """
        with self._lock:
            if self._settled:
                return  # the caller stopped waiting, and took the press with it
            self._settled = True
            self._verdict = self._refuse_now()
            deliver_it = not self._verdict
        # Release the caller before the press runs. It waits for the decision,
        # which is this, and not for the paint and the dispatch behind it.
        self._started.set()
        if not deliver_it:
            return

        c_input = self._controller.get_input(self._identifier)
        held_before = None if c_input is None else c_input._gesture
        try:
            self._controller.event_callback(self._identifier, True)
        finally:
            self._arm_release(held_before)

    def _refuse_now(self) -> str:
        """The code for a press that must not happen, or empty. Under the lock.

        Every reason here is the deck's own state a moment before the key goes
        down, and each has a caller-side check of its own. Both are needed: the
        caller's is what lets a request be refused before anything is armed,
        and this one is what keeps the answer true.
        """
        if not self._controller.allow_interaction:
            return "input-blocked"
        active_page = self._controller.active_page
        self.showing = "nothing" if active_page is None else f"'{active_page.get_name()}'"
        if active_page is None or os.path.abspath(active_page.json_path) != self._page_path:
            return "page-moved"
        return ""

    def _arm_release(self, held_before: object | None) -> None:
        """Arm the release for the gesture this press took, and for no other.

        The deck swallows a DOWN without a word in more than one state: input
        turned off, the addressed input gone, a screensaver that takes the
        press as the wake-up it is. Nothing is held down in any of them, and a
        release armed anyway lands later on whatever the key is doing then. A
        finger holding that key would get its own press ended under it, and
        then its own release swallowed as a stray.

        The gesture the input takes on the way down is the thing this press
        owns, so the release is armed only when the DOWN created one, and it
        carries that object. This also covers a DOWN that raised: it raised
        after the gesture, so there is something to release, and it raised
        before, so there is not.
        """
        c_input = self._controller.get_input(self._identifier)
        gesture = None if c_input is None else c_input._gesture
        if gesture is None or gesture is held_before:
            return
        self._gesture = gesture
        timer_wheel.schedule(self._hold_s, self._release, name="EmulatedRelease")

    def _release(self) -> None:
        """End this press's own gesture, if the input is still holding it.

        A screensaver sweep, a rebuild of the inputs, or a finger that took the
        key in the meantime all end that gesture. The key is not this press's
        to release then, and a release sent anyway ends someone else's press.
        """
        c_input = self._controller.get_input(self._identifier)
        if c_input is None or c_input._gesture is not self._gesture:
            return
        self._controller.event_callback(self._identifier, False)

    def wait_for_start(self) -> str:
        """Block until the press is delivered or refused, and say which.

        Empty means the deck has the press. Anything else is a result code for
        the caller to render. A wait that runs out claims the press under the
        same lock the wheel uses, so a job that arrives late finds it settled
        and presses nothing: the alternative is an answer that reports a
        failure and a deck that presses anyway.
        """
        if not self._started.wait(_PRESS_START_WAIT_S):
            with self._lock:
                if not self._settled:
                    self._settled = True
                    self._verdict = "press-not-started"
        return self._verdict


# The longest change_state_on waits for a page's input rebuild before it reads
# a live state count. It sits above the media thread's own input-load deadline
# (DeckController.LOAD_INPUTS_TIMEOUT) so a genuinely slow load is not cut
# short, while a stalled or superseded load still returns the caller in bounded
# time rather than never.
_INPUT_LOAD_WAIT_S = 12.0


class InputLoadBarrier:
    """Publishes which page generation's input rebuild has finished.

    A bare flag cannot say which load it signals. Page loads overlap: a switch
    can arm a rebuild while an earlier page's rebuild is still queued or
    running, and that earlier rebuild finishing must not release a waiter that
    asked about the newer page. So the generation is the key. arm records the
    generation whose rebuild is outstanding, publish records the newest
    generation whose rebuild has ended, and a wait clears only once the second
    has caught up with the first.

    Both numbers only ever rise, so a publish that lands out of order is a
    no-op. Every load that ends without rebuilding anything, superseded, its
    executor gone, or a page load that reloads no inputs, publishes its own
    generation: nothing more is coming for it, and a waiter must not sit out
    its bound for a rebuild that will never run. A superseder that armed a
    higher generation keeps its own waiter blocked past those publishes.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._armed_gen: int = 0
        self._done_gen: int = 0

    def arm(self, gen: int, rebuilding: bool) -> None:
        """Record generation gen. rebuilding says whether a rebuild for it is
        on its way; when it is not, gen counts as already done."""
        with self._cond:
            if rebuilding:
                self._armed_gen = max(self._armed_gen, gen)
            else:
                self._done_gen = max(self._done_gen, gen)
            self._cond.notify_all()

    def publish(self, gen: int | None) -> None:
        """Record that the rebuild for generation gen has ended. gen is None
        for a load outside the page-load path, which no waiter keys on."""
        with self._cond:
            if gen is not None:
                self._done_gen = max(self._done_gen, gen)
            self._cond.notify_all()

    def wait(self, timeout: float) -> bool:
        """Block until the armed generation's rebuild has ended, or timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._done_gen < self._armed_gen:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True


def _wait_for_input_load(controller: DeckController) -> None:
    """Block until the input rebuild a page load queued has finished, or the
    bound elapses.

    change_page_on hands load_all_inputs to the media thread and returns before
    it runs, so a state check straight after a page switch races that rebuild
    and reads an input's pre-rebuild single state. load_page arms the barrier
    with the generation it queues the rebuild for, and load_all_inputs
    publishes that generation when the rebuild finishes, so this waits on
    exactly that rebuild and not on an older one that lands late.

    It does not wait on the trailing paint task the load also queues. That task
    blocks the media thread for the whole background-video decode, and a barrier
    behind it would freeze this caller (the GTK main thread, for a D-Bus change)
    for the length of the decode. The rebuild is all a state check needs: it is
    where the page's own states become real.

    The barrier starts level, so a wait with no rebuild pending, a same-page
    change that queues no load, returns at once. The wait holds no other lock
    and never runs on the media thread, so it cannot invert the single-writer
    order or wait on itself. The bound covers a rebuild stalled before it could
    finish.
    """
    media_player = controller.media_player
    if not media_player.is_alive() or threading.current_thread() is media_player:
        return
    controller._input_load_done.wait(_INPUT_LOAD_WAIT_S)


class ControlPlane:
    """The rules. This holds no state, and every call reads the gl slots it
    needs. A controller list or a page store rebound underneath it, which the
    test harness does, still applies.

    The caller's thread runs every method, whichever it is, which covers the
    boot thread, a USB hotplug thread, the GTK main thread and the D-Bus
    dispatch. load_page serializes itself under the controller's page lock,
    the media thread stays the one device writer, and nothing here calls the
    UI. _controllers() snapshots the manager's list before a walk, because a
    hotplug thread appends to it and removes from it.
    """

    # The cores, which take a controller. load_default_page runs inside
    # DeckController.__init__, before anything appends the controller to
    # gl.deck_manager.deck_controller, so it holds a controller and cannot
    # look itself up by serial. The wrappers below add that lookup for the
    # transports that speak serials.

    def change_page_on(self, controller: DeckController, page_ref: str) -> ControlResult:
        """Show page_ref, which is a page name or a page path, on controller.

        The load is skipped while that page is already the active one. That
        no-op is why this check has one home. A repeated switch request
        otherwise reloads the deck on one transport and does nothing on the
        others. On a real deck a reload flickers and re-renders every key.
        """
        page_manager = gl.page_manager
        if page_manager is None:
            return ControlResult(False, "no-page-manager",
                                 f"Cannot change to page '{page_ref}': no page manager")

        page_path = page_manager.find_matching_page_path(page_ref)
        if page_path is None:
            return _no_such_page(page_ref, page_manager)

        # Snapshot the page and check it for None. active_page can be None,
        # after a racing close or clear, or a load deferred by a showing
        # screensaver, and another thread can swap it between the read and the
        # compare. No current page means the requested one differs, so the
        # load proceeds.
        active_page = controller.active_page
        if active_page is not None and os.path.abspath(page_path) == os.path.abspath(active_page.json_path):
            return ControlResult(True, "already-active", f"Page '{page_ref}' is already active")

        page = page_manager.get_page(page_path, controller)
        if page is None:
            # The file matched a name and did not build into a page. A None
            # handed to load_page clears the deck, so a page that failed to
            # build blanks the device and reports success. A deck that keeps
            # what it shows is the better failure, and this tells the caller
            # the reason.
            return ControlResult(False, "page-build-failed",
                                 f"Page '{page_ref}' could not be loaded")

        # A request that names a page is the user choosing the deck's page,
        # whether it arrives from the CLI or over D-Bus. The window grabber
        # owns the mark that says the page arrived by an automatic switch, and
        # nothing else clears it, so without this the deck keeps that mark and
        # a later restore takes it back to a page the user already left. The
        # wrap sits here, around the one load that actually happens: a refused
        # request and a page already active leave the mark alone.
        window_grabber = gl.window_grabber
        if window_grabber is None:
            controller.load_page(page)
            return ControlResult(True)
        with window_grabber.manual_page_load(controller, page_path):
            controller.load_page(page)
        return ControlResult(True)

    def change_state_on(self, controller: DeckController, page_ref: str,
                        coords: str, state: int | str) -> ControlResult:
        """Set the input at coords on page_ref to state, and load the page
        first when it is not the active one.

        The page comes first, because the requested page defines the input
        this addresses, and that input's state count bounds the state number.
        The bounds come from this device's own key layout and this input's own
        state list. No constant supplies them, because no constant holds for
        every deck.
        """
        page_result = self.change_page_on(controller, page_ref)
        if not page_result.ok:
            return page_result

        # change_page_on queued the input rebuild on the media thread and
        # returned before it ran, so the live inputs can still carry their
        # pre-rebuild single state. Let that rebuild finish before this reads a
        # state count or sets a state below, or a valid request is rejected as
        # "only has 1 state" and a set_state that beat the rebuild is reset by
        # it.
        _wait_for_input_load(controller)

        found = _key_at(controller, coords)
        if isinstance(found, ControlResult):
            return found
        c_input, x, y = found

        # The state number is an int on the D-Bus method's signature and on
        # the CLI's parked requests, which convert at their own edge. The boot
        # path's own tests write into the parked dict directly, and so does
        # anything else that holds gl.api_state_requests, so a string still
        # reaches here. A conversion costs less than a rule that every writer
        # of that dict must get the type right.
        try:
            state_number = int(state)
        except (TypeError, ValueError):
            return ControlResult(False, "bad-state",
                                 f"Invalid state number '{state}'. Must be an integer")

        state_count = len(c_input.states)
        if state_number < 0 or state_number >= state_count:
            if state_count == 1:
                has = "only has 1 state (state 0)"
            else:
                has = f"has {state_count} states (0-{state_count - 1})"
            return ControlResult(False, "state-out-of-range",
                                 f"Position ({x},{y}) {has}. "
                                 f"Requested state {state_number} does not exist")

        c_input.set_state(state_number)
        return ControlResult(True, "",
                             f"Successfully changed state of ({x},{y}) to state "
                             f"{state_number} on device {controller.serial_number()}")

    def emulate_input_on(self, controller: DeckController, page_ref: str,
                         coords: str, event: str) -> ControlResult:
        """Press the input at coords on page_ref of controller as a finger
        would, and load the page first when it is not the active one.

        event is one of EMULATED_EVENTS. A press comes back to an action as
        DOWN and then SHORT_UP and UP. A long press outlasts the deck's hold
        time, which is what turns those into HOLD_START, HOLD_STOP and UP.

        The coordinates address the key the way --change-state addresses it,
        which is by the deck's own layout after its rotation. They are not the
        physical key positions a finger meets on a rotated deck.

        This returns once the deck has the press, and not once the actions have
        run or the release has landed. It waits for the press to reach the deck
        so that the answer is about this request: a return before that would
        report a press that a page switch could still take away.

        An event word this module does not know is refused before the page
        switch. It is judged without a device, and a request that nothing can
        carry out must not move the deck first. The coordinates are the
        device's own answer, so they are judged after the switch, exactly as
        change_state_on judges them.

        A screensaver that is showing swallows the press and wakes the deck,
        which is what it does to the first press of a finger. It also holds the
        page switch: DeckController.load_page defers a switch made while the
        screensaver shows, and answers before it happens. A press behind such a
        switch therefore names a page the deck is not on yet, and is refused as
        moved rather than run against the page still showing.
        """
        hold_s = _hold_seconds(controller, event)
        if hold_s is None:
            return ControlResult(False, "bad-event",
                                 f"Unknown input event '{event}'. "
                                 f"Expected one of: {', '.join(EMULATED_EVENTS)}")

        page_result = self.change_page_on(controller, page_ref)
        if not page_result.ok:
            return page_result

        # The page this request is for, read where the switch leaves it. The
        # press is checked against it again at the deck, because every step
        # below can be overtaken by a switch from another source.
        page = controller.active_page
        if page is None:
            return _page_moved(controller, page_ref, "nothing")

        # The switch queued the input rebuild on the media thread and returned
        # before it ran, so a press dispatched now reaches the actions the
        # outgoing page had on that input. Wait for the rebuild, as a state
        # change does, and the press lands on the page it names.
        _wait_for_input_load(controller)

        found = _key_at(controller, coords)
        if isinstance(found, ControlResult):
            return found
        c_input, x, y = found

        if not controller.allow_interaction:
            # Asked here so a locked deck is refused before anything is armed,
            # and asked again at the deck, because the session can lock in
            # between. See _Press._refuse_now.
            return _input_blocked(controller)

        if c_input.down_start_time is not None:
            # Something is already holding this key: a finger, or a press
            # emulated a moment ago whose release has not landed. A second DOWN
            # on top of it is a shape no hardware makes. The input would deliver
            # DOWN, DOWN, then one release for both, which leaves an action that
            # latches on DOWN jammed, and turns the second long press into a
            # short one. This reads the moment it is asked, so two presses that
            # race each other can still both pass; what it rules out is the
            # ordinary case, one press arriving on top of another.
            return ControlResult(False, "input-held",
                                 f"Position ({x},{y}) on device {controller.serial_number()} "
                                 f"is already held down. Let that press finish first")

        press = _Press(controller, Input.Key(f"{x}x{y}"), page, hold_s)
        timer_wheel.schedule(0.0, press.deliver, name="EmulatedPress")
        verdict = press.wait_for_start()
        if verdict:
            return _press_refused(controller, page_ref, f"{x},{y}", verdict, press.showing)

        return ControlResult(True, "",
                             f"Emulated a {event} on ({x},{y}) on device "
                             f"{controller.serial_number()}")

    def set_brightness_on(self, controller: DeckController, value: int) -> ControlResult:
        """Set the brightness of controller to value, a whole percent.

        The device layer clamps the value to 0..100. This applies the change
        live; the caller that holds the serial writes the persisted deck
        setting (see src/api.py), so the brightness survives the next page
        reload rather than reverting to the stored value.

        A deck mid-teardown answers get_alive() False, and set_brightness()
        no-ops on it. This still reports success and the caller still persists
        the value, which is deliberate: the value is what the user asked for,
        and the deck picks it up from the setting when it comes back.
        """
        controller.set_brightness(float(value))
        return ControlResult(True, "",
                             f"Set the brightness of {controller.serial_number()} to {value}")

    def sleep_on(self, controller: DeckController) -> ControlResult:
        """Show controller's screensaver, the way its idle timer would.

        allow_interaction is left alone, so the first press wakes the deck, as
        it does for a screensaver that came up on its own. Turning interaction
        off is what a locked session does, and a sleep request is not that. A
        deck already showing its screensaver is left as it is.
        """
        controller.screen_saver.show()
        return ControlResult(True, "",
                             f"Device {controller.serial_number()} is showing its screensaver")

    def wake_on(self, controller: DeckController) -> ControlResult:
        """Hide controller's screensaver and restore the page under it. A deck
        that is already awake is left as it is."""
        controller.screen_saver.hide()
        return ControlResult(True, "",
                             f"Device {controller.serial_number()} is awake")

    def dump_state(self) -> dict[str, Any]:
        """The running state a read command reports: every deck with its
        serial, active page and last-commanded brightness, and the page names
        that exist. A stable shape, because a script reads it by key.

        brightness is the value last sent to the device, which is null before
        the first send. It reports as a whole number, because the CLI and the
        docs describe it as one from 0 to 100. active_page is null on a deck
        with nothing loaded.
        """
        decks: list[dict[str, Any]] = []
        for controller in _controllers():
            page = controller.active_page
            brightness = controller.brightness
            if isinstance(brightness, float) and brightness.is_integer():
                brightness = int(brightness)
            decks.append({
                "serial": controller.serial_number(),
                "active_page": None if page is None else page.get_name(),
                "brightness": brightness,
            })
        page_manager = gl.page_manager
        pages = page_manager.get_page_names() if page_manager is not None else []
        return {"decks": decks, "pages": pages}

    def list_page_actions(self, page_ref: str, coords: str) -> "tuple[str | None, dict[str, Any]]":
        """The actions a page holds, read from its file, as (error, data).

        error is a sentence when the page does not exist, and None otherwise.
        data maps each input type this page configures to its inputs, each
        input to its states, and each state to the ids of the actions on it.
        coords, when it is not empty, narrows the read to the one key at x,y
        and refuses a coordinate that does not read as x,y.

        This reads the page file, not a live deck, so it answers for every
        page and not only the one a deck shows. No device is touched, so no
        serial is needed.
        """
        page_manager = gl.page_manager
        if page_manager is None:
            return ("Cannot read the actions on a page: no page manager", {})
        page_path = page_manager.find_matching_page_path(page_ref)
        if page_path is None:
            return (_no_such_page(page_ref, page_manager).message, {})

        key_filter: str | None = None
        if coords:
            try:
                x, y = (int(part) for part in coords.split(","))
            except (ValueError, AttributeError):
                return (f"Invalid coordinate format '{coords}'. "
                        f"Expected format: 'x,y' (e.g., '0,0')", {})
            key_filter = f"{x}x{y}"

        page_dict = page_manager.get_page_data(page_path)
        actions: dict[str, Any] = {}
        for input_type in ("keys", "dials", "touchscreens"):
            group = page_dict.get(input_type, {})
            if not isinstance(group, dict):
                continue
            for input_id, input_dict in group.items():
                if key_filter is not None and (input_type != "keys" or input_id != key_filter):
                    continue
                states = input_dict.get("states", {}) if isinstance(input_dict, dict) else {}
                by_state: dict[str, list[str]] = {}
                for state_key, state_dict in states.items():
                    entries = state_dict.get("actions", []) if isinstance(state_dict, dict) else []
                    by_state[str(state_key)] = [
                        str(entry.get("id")) for entry in entries
                        if isinstance(entry, dict) and entry.get("id")
                    ]
                actions.setdefault(input_type, {})[input_id] = by_state
        return (None, {"page": page_ref, "actions": actions})

    # The wrappers, which resolve a serial.

    def change_page(self, serial_number: str, page_ref: str) -> ControlResult:
        """change_page_on for the deck that reports serial_number."""
        controller = self._find(serial_number)
        if controller is None:
            return _no_such_deck(serial_number)
        return self.change_page_on(controller, page_ref)

    def change_state(self, serial_number: str, page_ref: str,
                     coords: str, state: int | str) -> ControlResult:
        """change_state_on for the deck that reports serial_number."""
        controller = self._find(serial_number)
        if controller is None:
            return _no_such_deck(serial_number)
        return self.change_state_on(controller, page_ref, coords, state)

    def emulate_input(self, serial_number: str, page_ref: str, coords: str,
                      event: str) -> ControlResult:
        """emulate_input_on for the deck that reports serial_number."""
        controller = self._find(serial_number)
        if controller is None:
            return _no_such_deck(serial_number)
        return self.emulate_input_on(controller, page_ref, coords, event)

    def set_brightness(self, serial_number: str, value: int) -> ControlResult:
        """set_brightness_on for the deck that reports serial_number."""
        controller = self._find(serial_number)
        if controller is None:
            return _no_such_deck(serial_number)
        return self.set_brightness_on(controller, value)

    def sleep(self, serial_number: str) -> ControlResult:
        """sleep_on for the deck that reports serial_number."""
        controller = self._find(serial_number)
        if controller is None:
            return _no_such_deck(serial_number)
        return self.sleep_on(controller)

    def wake(self, serial_number: str) -> ControlResult:
        """wake_on for the deck that reports serial_number."""
        controller = self._find(serial_number)
        if controller is None:
            return _no_such_deck(serial_number)
        return self.wake_on(controller)

    def _find(self, serial_number: str) -> DeckController | None:
        """The first controller that reports serial_number, or None. A serial
        is unique per device. A duplicate means a deck that reports another
        deck's identity, and the first controller is as good an answer as
        exists."""
        for controller in _controllers():
            if controller.serial_number() == serial_number:
                return controller
        return None


# The process-wide control plane. A module singleton rather than a gl slot,
# for the reason the startup queue is one. A named protocol should shrink the
# shared namespace.
_control_plane = ControlPlane()


def get() -> ControlPlane:
    """The process-wide control plane. Never None."""
    return _control_plane
