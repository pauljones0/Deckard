"""Validate page, state, and input requests from all control transports without UI or logging.
Invalid requests are results; unexpected exceptions propagate so parked work remains retryable."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

# Keep render-engine imports widget-free; runtime globals are deck and page managers.
# Postponed annotations keep all other imports under TYPE_CHECKING on Python 3.13.
import globals as gl
from src.backend import timer_wheel
from src.backend.DeckManagement.deck_events import KeyEvent
from src.backend.DeckManagement.InputIdentifier import Input

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.deck_controller.inputs import ControllerKey
    from src.backend.PageManagement.Page import Page
    from src.backend.PageManagement.PageManagerBackend import PageManagerBackend


@dataclass(frozen=True)
class ControlResult:
    """A stable machine code and surface-ready message; "" and already-active mean success."""

    # Input: bad-coords, bad-event, bad-state, coords-out-of-bounds, input-blocked

    # Input: input-held, no-such-input, press-not-started, state-out-of-range

    # Page/deck: no-page-manager, no-such-deck, no-such-page, page-build-failed, page-moved

    ok: bool
    code: str = ""
    message: str = ""


def _controllers() -> list[DeckController]:
    """Snapshot live controllers because hotplug and teardown mutate the source across threads."""
    deck_manager = gl.deck_manager
    if deck_manager is None:
        return []
    return list(deck_manager.deck_controller)


def _no_such_deck(serial_number: str) -> ControlResult:
    """Report an unknown serial with connected devices or the possible startup state."""
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
    """Report a press refused after its named page moved.
    Use the refusal-time snapshot because a fresh read can name a later third page."""
    return ControlResult(False, "page-moved",
                         f"Device {controller.serial_number()} stopped showing page "
                         f"'{page_ref}' before the press landed, and shows {showing} "
                         f"now, so nothing was pressed")


def _input_blocked(controller: DeckController) -> ControlResult:
    """Report input blocked by the session lock before the deck can silently drop the press."""
    return ControlResult(False, "input-blocked",
                         f"Device {controller.serial_number()} is not taking input "
                         f"right now, because the session is locked")


def _press_refused(controller: DeckController, page_ref: str, coords: str,
                   code: str, showing: str) -> ControlResult:
    """Render a refused press and expose unknown refusal codes instead of misclassifying them."""
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
    """Resolve a key and parsed coordinates or return a common validation result.
    Bounds come from this device's layout and present inputs, not a global constant."""
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


#: Input event words mirrored in pre-globals cli_forward; long-press exceeds the deck hold time.
PRESS = "press"
LONG_PRESS = "long-press"
EMULATED_EVENTS = (PRESS, LONG_PRESS)

#: Hold a short press for 50 ms or half the deck hold time, whichever is shorter.
_PRESS_DOWN_S = 0.05

#: Keep long-press release 250 ms beyond the hold timer so HOLD_START can dispatch first.
_LONG_PRESS_EXTRA_S = 0.25


def _hold_seconds(controller: DeckController, event: str) -> float | None:
    """Return the event hold time, or None for an unknown word."""
    if event == PRESS:
        return min(_PRESS_DOWN_S, controller.hold_time / 2)
    if event == LONG_PRESS:
        return controller.hold_time + _LONG_PRESS_EXTRA_S
    return None


#: Wait two seconds for immediate wheel dispatch; timeout claims the press under the same lock.
#: This prevents delivery after failure and bounds the control-method context hold.
_PRESS_START_WAIT_S = 2.0


class _Press:
    """One emulated press claimed once by its caller or the timer wheel.
    Deliver through the hardware path off-thread; schedule release without sleeping."""

    def __init__(self, controller: DeckController, identifier: Input.Key,
                 page: "Page", hold_s: float) -> None:
        self._controller = controller
        self._identifier = identifier
        # Track the requested page by path because reloads create new objects for the same file.
        self._page_path = os.path.abspath(page.json_path)
        self._hold_s = hold_s
        self._lock = threading.Lock()
        self._settled = False
        self._verdict = ""
        self._started = threading.Event()
        #: Refusal-time page snapshot; avoid a third active-page read in the error message.
        self.showing = "nothing"
        #: The gesture created by this DOWN and the only gesture its release can end.
        self._gesture: object | None = None

    def deliver(self) -> None:
        """Deliver off-thread only while the named page shows and input stays enabled."""
        with self._lock:
            if self._settled:
                return  # The caller timed out and claimed the press.
            self._settled = True
            self._verdict = self._refuse_now()
            deliver_it = not self._verdict
        # Release the caller after the decision, before paint and action dispatch.
        self._started.set()
        if not deliver_it:
            return

        c_input = self._controller.get_input(self._identifier)
        held_before = None if c_input is None else c_input._gesture
        try:
            self._controller.event_callback(self._identifier, KeyEvent(pressed=True))
        finally:
            self._arm_release(held_before)

    def _refuse_now(self) -> str:
        """Return the locked, delivery-time refusal code, or empty when safe to press."""
        if not self._controller.allow_interaction:
            return "input-blocked"
        active_page = self._controller.active_page
        self.showing = "nothing" if active_page is None else f"'{active_page.get_name()}'"
        if active_page is None or os.path.abspath(active_page.json_path) != self._page_path:
            return "page-moved"
        return ""

    def _arm_release(self, held_before: object | None) -> None:
        """Schedule release only when this DOWN created a new gesture.
        A swallowed DOWN must not later release a finger's or replacement input's gesture."""
        c_input = self._controller.get_input(self._identifier)
        gesture = None if c_input is None else c_input._gesture
        if gesture is None or gesture is held_before:
            return
        self._gesture = gesture
        timer_wheel.schedule(self._hold_s, self._release, name="EmulatedRelease")

    def _release(self) -> None:
        """Release only while the input still holds this press's gesture."""
        c_input = self._controller.get_input(self._identifier)
        if c_input is None or c_input._gesture is not self._gesture:
            return
        self._controller.event_callback(self._identifier, KeyEvent(pressed=False))

    def wait_for_start(self) -> str:
        """Wait for delivery or refusal and return its code.
        Timeout claims the press under the delivery lock so a failed press cannot arrive later."""
        if not self._started.wait(_PRESS_START_WAIT_S):
            with self._lock:
                if not self._settled:
                    self._settled = True
                    self._verdict = "press-not-started"
        return self._verdict


# Wait longer than the media thread's input-load deadline, but bound stalled or superseded loads.
_INPUT_LOAD_WAIT_S = 12.0


class InputLoadBarrier:
    """Publish monotonic completion so stale rebuilds cannot release newer waiters.
    Loads with no rebuild publish completion; an armed higher generation remains blocked."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._armed_gen: int = 0
        self._done_gen: int = 0

    def arm(self, gen: int, rebuilding: bool) -> None:
        """Record a generation as armed when rebuilding, or already done otherwise."""
        with self._cond:
            if rebuilding:
                self._armed_gen = max(self._armed_gen, gen)
            else:
                self._done_gen = max(self._done_gen, gen)
            self._cond.notify_all()

    def publish(self, gen: int | None) -> None:
        """Publish a finished generation; ignore unkeyed loads represented by None."""
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
    """Wait within a bound for the exact input rebuild queued by a page load.
    Do not wait for trailing paint, hold another lock, or block the media thread on itself."""
    media_player = controller.media_player
    if not media_player.is_alive() or threading.current_thread() is media_player:
        return
    controller._input_load_done.wait(_INPUT_LOAD_WAIT_S)


class ControlPlane:
    """Stateless caller-thread rules read deck_manager, page_manager, and window_grabber.
    Page loads own their lock; only the media thread writes devices; methods never call UI."""

    # Core methods take a controller because default-page loading runs before serial registration.
    # Transport wrappers below resolve serials for later calls.

    def change_page_on(self, controller: DeckController, page_ref: str) -> ControlResult:
        """Show a page name or path, but treat an already-active page as a successful no-op."""
        page_manager = gl.page_manager
        if page_manager is None:
            return ControlResult(False, "no-page-manager",
                                 f"Cannot change to page '{page_ref}': no page manager")

        page_path = page_manager.find_matching_page_path(page_ref)
        if page_path is None:
            return _no_such_page(page_ref, page_manager)

        # Snapshot active_page because racing clear, close, or deferred load can replace it.
        active_page = controller.active_page
        if active_page is not None and os.path.abspath(page_path) == os.path.abspath(active_page.json_path):
            return ControlResult(True, "already-active", f"Page '{page_ref}' is already active")

        page = page_manager.get_page(page_path, controller)
        if page is None:
            # Do not pass a failed page build to load_page because None clears the deck.
            return ControlResult(False, "page-build-failed",
                                 f"Page '{page_ref}' could not be loaded")

        # Mark only an actual control-request load as manual so window restore does not undo it.
        window_grabber = gl.window_grabber
        if window_grabber is None:
            controller.load_page(page)
            return ControlResult(True)
        with window_grabber.manual_page_load(controller, page_path):
            controller.load_page(page)
        return ControlResult(True)

    def change_state_on(self, controller: DeckController, page_ref: str,
                        coords: str, state: int | str) -> ControlResult:
        """Load the requested page, then set its addressed input state.
        Validate coordinates and state count from the live device input, not global limits."""
        page_result = self.change_page_on(controller, page_ref)
        if not page_result.ok:
            return page_result

        # Wait for the queued rebuild before reading state count or setting a state it can reset.
        _wait_for_input_load(controller)

        found = _key_at(controller, coords)
        if isinstance(found, ControlResult):
            return found
        c_input, x, y = found

        # Accept strings because direct parked-state writers bypass typed edges.
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
        """Load the page and emulate a hardware-path press at its rotated-layout coordinates.
        Reject bad events first; return at press start or refuse a deferred or moved page."""
        hold_s = _hold_seconds(controller, event)
        if hold_s is None:
            return ControlResult(False, "bad-event",
                                 f"Unknown input event '{event}'. "
                                 f"Expected one of: {', '.join(EMULATED_EVENTS)}")

        page_result = self.change_page_on(controller, page_ref)
        if not page_result.ok:
            return page_result

        # Recheck the page at delivery because another source can change it.
        page = controller.active_page
        if page is None:
            return _page_moved(controller, page_ref, "nothing")

        # Wait for input rebuild so the press cannot reach actions from the outgoing page.
        _wait_for_input_load(controller)

        found = _key_at(controller, coords)
        if isinstance(found, ControlResult):
            return found
        c_input, x, y = found

        if not controller.allow_interaction:
            # Refuse now and at delivery because the session can lock between checks.
            return _input_blocked(controller)

        if c_input.down_start_time is not None:
            # Reject a second DOWN; concurrent requests can still race this gesture check.
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
        """Set live whole-percent brightness; the serial-owning caller persists it.
        Report success during teardown so the returning deck can apply the persisted request."""
        controller.set_brightness(float(value))
        return ControlResult(True, "",
                             f"Set the brightness of {controller.serial_number()} to {value}")

    def sleep_on(self, controller: DeckController) -> ControlResult:
        """Show the idle screensaver without disabling input; do nothing if already asleep."""
        controller.screen_saver.show()
        return ControlResult(True, "",
                             f"Device {controller.serial_number()} is showing its screensaver")

    def wake_on(self, controller: DeckController) -> ControlResult:
        """Hide the screensaver and restore its page; leave an already-awake deck unchanged."""
        controller.screen_saver.hide()
        return ControlResult(True, "",
                             f"Device {controller.serial_number()} is awake")

    def dump_state(self) -> dict[str, Any]:
        """Return stable keyed state for decks and pages.
        Brightness is the last-commanded whole percent or null; active_page is null if unloaded."""
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
        """Read configured action IDs from a page file as (error, data), optionally for one x,y key.
        This does not require or touch a live deck."""
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

    # Serial-resolving transport wrappers

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
        """Return the first controller with this normally unique device serial, else None."""
        for controller in _controllers():
            if controller.serial_number() == serial_number:
                return controller
        return None


# Keep the process-wide stateless control plane out of the shared globals namespace.
_control_plane = ControlPlane()


def get() -> ControlPlane:
    """The process-wide control plane. Never None."""
    return _control_plane
