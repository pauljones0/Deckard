"""Plan, park, or forward CLI control requests without importing globals or the toolkit.
Page and state changes can wait; inputs cannot. Lost launch races forward parked work."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

import appinfo

if TYPE_CHECKING:
    from argparse import Namespace
    from typing import Protocol

    # Keep GLib outside the pre-globals runtime closure; _connect() imports it on demand.
    from gi.repository import GLib

    class Transport(Protocol):
        """What forwarding needs from the running instance."""

        def is_running(self) -> bool:
            """Is there an instance to forward to?"""

        def change_page(self, serial: str, page: str) -> str:
            """Empty on success, else the reason the instance gave."""

        def change_state(self, serial: str, page: str, coords: str,
                         state: int) -> str:
            """Empty on success, else the reason the instance gave."""

        def emulate_input(self, serial: str, page: str, coords: str,
                          event: str) -> str:
            """Empty on success, else the reason the instance gave."""

        def query_state(self) -> str:
            """One JSON object: the state dump, or {"error": "..."}."""

        def list_actions(self, page: str, coords: str) -> str:
            """One JSON object: the page's actions, or {"error": "..."}."""

        def set_brightness(self, serial: str, value: int) -> str:
            """Empty on success, else the reason the instance gave."""

        def sleep(self, serial: str) -> str:
            """Empty on success, else the reason the instance gave."""

        def wake(self, serial: str) -> str:
            """Empty on success, else the reason the instance gave."""

        def rename_page(self, old: str, new: str) -> str:
            """Empty on success, else the reason the instance gave."""

        def duplicate_page(self, source: str, new: str) -> str:
            """Empty on success, else the reason the instance gave."""


# Bound shared CLI D-Bus probes at five seconds instead of the 25-second default.
DBUS_CALL_TIMEOUT_MS = 5000

# Cover input-load and press-admission waits without inviting retries that duplicate presses.
# Page-lock and page-build work can exceed this; handover probes retain the shorter timeout.
CONTROL_CALL_TIMEOUT_MS = 20000

# The top-level object's interface is the application ID.
TOP_IFACE = appinfo.APP_ID

# GDBus versions use either worker-thread error for missing methods or objects.
# This indicates an old, unpublished, or exiting instance; a wedged current instance times out.
_METHOD_MISSING = (
    "org.freedesktop.DBus.Error.UnknownMethod",
    "org.freedesktop.DBus.Error.UnknownInterface",
)

# A current CLI reports missing methods; an old Gio-action client cannot detect unknown actions.
SKEW_MESSAGE = (
    "The running Deckard is not answering to the control methods this command "
    "needs. An older build does not have them at all, and restarting "
    "it is what picks up a build that does. An instance that is shutting down "
    "answers the same way until it is gone, so if one was just quitting, try "
    "again in a moment."
)

# Reject values wider than ChangeState's signed int32 before GLib packing can raise.
# This is a wire limit, unlike device-dependent coordinates and state counts.
MAX_STATE_NUMBER = 2**31 - 1

# Mirror control_plane event words here to keep pre-globals parsing independent.
# Reject unknown words before contacting the instance.
EMULATE_EVENTS = ("press", "long-press")

USAGE = """
Usage examples:
  --change-state CL123456789 Main 0,0 1
  --change-state CL123456789 Soundboard 2,1 0
  --emulate-input CL123456789 Main 0,0 press
  --emulate-input CL123456789 Soundboard 2,1 long-press
  --json
  --get-brightness CL123456789
  --set-brightness CL123456789 60
  --sleep CL123456789
  --wake CL123456789
  --list-actions Main
  --list-actions Main 0,0
  --rename-page Main Home
  --duplicate-page Main Home

Parameters:
  SERIAL_NUMBER: Device serial (e.g., CL123456789)
  PAGE_NAME: Page name (e.g., Main, Soundboard)
  COORDINATES: Position as x,y (e.g., 0,0 for top-left)
  STATE_NUMBER: State to change to (e.g., 0, 1, 2)
  EVENT: press or long-press
  VALUE: brightness, a whole number from 0 to 100"""

# Both CLI paths use these reasons because emulated input is never parked.
_UNPARKABLE_WHY = (
    "An emulated input means nothing to a deck that is not open yet. A page or "
    "a state change waits for its deck and applies when it appears; a press "
    "cannot wait, because it would land at a moment nobody asked for.")

NOT_RUNNING_MESSAGE = (
    f"Error: Deckard is not running, so nothing was pressed. {_UNPARKABLE_WHY} "
    f"Start Deckard, then send the command again.")

CLOSE_RUNNING_MESSAGE = (
    f"Error: --close-running makes this launch the Deckard that runs, so "
    f"nothing was pressed. {_UNPARKABLE_WHY} Run the two commands one after "
    f"the other instead.")

# Listing exits before other arguments run, so refuse an adjacent press instead of dropping it.
LISTING_MESSAGE = (
    "Error: a listing is answered by this command alone, and nothing else on "
    "the line runs, so nothing was pressed. Ask for the listing and the press "
    "in two commands.")


class OlderInstance(Exception):
    """The running instance has none of the required bus control methods."""


class TransportError(Exception):
    """A bus conversation failed or could not start; its text is safe for the CLI."""


@dataclass(frozen=True)
class Verdict:
    """The invocation outcome: whether to stop, stderr failures, and stdout lines.
    Any failure marks the invocation failed, regardless of handled."""

    handled: bool = False
    failures: list[str] = field(default_factory=list)
    output: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Plan:
    """One invocation's complete argv-only control plan.
    Any failure voids the plan; page and state requests can park, but inputs cannot."""

    page_requests: list[tuple[str, str]] = field(default_factory=list)
    state_requests: list[tuple[str, str, str, int]] = field(default_factory=list)
    emulate_requests: list[tuple[str, str, str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.page_requests or self.state_requests
                    or self.emulate_requests)


# Syntax

def _unsendable(where: str, what: str, value: str) -> str | None:
    """Return an error for argv text that the UTF-8 bus cannot carry, else None."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return (f"Error: {what} in {where} is not text this command can send: "
                f"{value!r}. The bus carries UTF-8 and these bytes are not.")
    return None


def _bad_coords(where: str, coords: str) -> str | None:
    """Validate non-negative x,y syntax without imposing device-specific bounds."""
    if not coords or "," not in coords:
        return (f"Error: Invalid coordinate format in {where}: '{coords}'. "
                f"Expected format: 'x,y' (e.g., '0,0')")
    try:
        x, y = (int(part) for part in coords.split(","))
    except ValueError:
        return (f"Error: Invalid coordinate format in {where}: '{coords}'. "
                f"Expected integers like '0,0'")
    if x < 0 or y < 0:
        return f"Error: Coordinates must be non-negative in {where}: '{coords}'"
    return None


def _parse_page_requests(raw: list[Any]) -> tuple[list[tuple[str, str]],
                                                   list[str]]:
    """Parse --change-page groups and leave serial and page existence to the instance."""
    parsed: list[tuple[str, str]] = []
    failures: list[str] = []
    for i, (serial_number, page_name) in enumerate(raw):
        where = f"--change-page argument {i + 1}"
        if not serial_number:
            failures.append(f"Error: Invalid serial number in {where}: '{serial_number}'")
            continue
        if not page_name:
            failures.append(f"Error: Invalid page name in {where}: '{page_name}'")
            continue
        unsendable = (_unsendable(where, "The serial number", serial_number)
                      or _unsendable(where, "The page name", page_name))
        if unsendable:
            failures.append(unsendable)
            continue
        parsed.append((serial_number, page_name))
    return parsed, failures


def _parse_state_requests(raw: list[Any]) -> tuple[list[tuple[str, str, str, int]],
                                               list[str]]:
    """Parse --change-state groups atomically and validate only device-independent shape.
    The running instance validates serials, pages, coordinates, and available states."""
    parsed: list[tuple[str, str, str, int]] = []
    failures: list[str] = []
    for i, (serial_number, page_name, coords, state_number) in enumerate(raw):
        # Include the flag because different request groups can have the same argument number.
        where = f"--change-state argument {i + 1}"
        if not serial_number:
            failures.append(f"Error: Invalid serial number in {where}: '{serial_number}'")
            continue
        if not page_name:
            failures.append(f"Error: Invalid page name in {where}: '{page_name}'")
            continue
        unsendable = (_unsendable(where, "The serial number", serial_number)
                      or _unsendable(where, "The page name", page_name))
        if unsendable:
            failures.append(unsendable)
            continue
        bad_coords = _bad_coords(where, coords)
        if bad_coords is not None:
            failures.append(bad_coords)
            continue
        try:
            state = int(state_number)
        except ValueError:
            failures.append(
                f"Error: Invalid state number in {where}: '{state_number}'. "
                f"Must be an integer")
            continue
        if state < 0:
            failures.append(
                f"Error: State number must be non-negative in {where}: '{state_number}'")
            continue
        if state > MAX_STATE_NUMBER:
            failures.append(
                f"Error: State number is too large in {where}: '{state_number}'. "
                f"The largest one the running instance can be asked for is "
                f"{MAX_STATE_NUMBER}")
            continue
        parsed.append((serial_number, page_name, coords, state))
    return parsed, failures


def _parse_emulate_requests(raw: list[Any]) -> tuple[list[tuple[str, str, str, str]],
                                                      list[str]]:
    """Parse --emulate-input groups atomically and reject unknown event words locally."""
    parsed: list[tuple[str, str, str, str]] = []
    failures: list[str] = []
    for i, (serial_number, page_name, coords, event) in enumerate(raw):
        where = f"--emulate-input argument {i + 1}"
        if not serial_number:
            failures.append(f"Error: Invalid serial number in {where}: '{serial_number}'")
            continue
        if not page_name:
            failures.append(f"Error: Invalid page name in {where}: '{page_name}'")
            continue
        unsendable = (_unsendable(where, "The serial number", serial_number)
                      or _unsendable(where, "The page name", page_name))
        if unsendable:
            failures.append(unsendable)
            continue
        bad_coords = _bad_coords(where, coords)
        if bad_coords is not None:
            failures.append(bad_coords)
            continue
        if event not in EMULATE_EVENTS:
            failures.append(
                f"Error: Invalid event in {where}: '{event}'. "
                f"Expected one of: {', '.join(EMULATE_EVENTS)}")
            continue
        parsed.append((serial_number, page_name, coords, event))
    return parsed, failures


def answered_by_a_listing(args: Namespace) -> bool:
    """Return whether this command exits after a local device or page listing.
    Use named attributes so a renamed flag fails instead of silently forwarding."""
    return bool(args.list_devices or args.list_pages)


def unparkable(plan: Plan, message: str) -> list[str]:
    """Reject the whole plan when it contains an input that this process cannot apply.
    The caller supplies the reason for no instance, replacement, or listing exit."""
    if not plan.emulate_requests:
        return []
    return [message]


def plan_requests(args: Namespace) -> Plan:
    """Build an argv-only plan and reject values that would fail during wire packing."""
    raw_page_requests = args.change_page or []
    raw_state_requests = args.change_state or []
    raw_emulate_requests = args.emulate_input or []
    if not raw_page_requests and not raw_state_requests and not raw_emulate_requests:
        return Plan()

    # Use tuples to match requests reclaimed from the startup queue after a lost launch race.
    page_requests, page_failures = _parse_page_requests(raw_page_requests)
    state_requests, state_failures = _parse_state_requests(raw_state_requests)
    emulate_requests, emulate_failures = _parse_emulate_requests(raw_emulate_requests)
    failures = page_failures + state_failures + emulate_failures
    if failures:
        # A parse failure prevents all parking and forwarding.
        return Plan(failures=failures + [USAGE])

    return Plan(page_requests=page_requests, state_requests=state_requests,
                emulate_requests=emulate_requests)


# Request destinations

def park(plan: Plan) -> None:
    """Park page and state requests by serial for decks that this process opens.
    Last write wins; callers must reject emulated inputs before this late import."""
    from src.backend import startup_queue

    queue = startup_queue.get()
    for serial_number, page_name in plan.page_requests:
        queue.park_page_request(serial_number, page_name)
    for serial_number, page_name, coords, state in plan.state_requests:
        queue.park_state_request(serial_number, {
            "page_name": page_name,
            "coords": coords,
            "state": state,
        })


def forward(plan: Plan, transport: Transport) -> list[str]:
    """Forward every request and collect failures without stopping later independent work.
    Preserve per-kind argv order: pages, states, then inputs; presses use the new state."""
    failures: list[str] = []
    try:
        for serial_number, page_name in plan.page_requests:
            message = transport.change_page(serial_number, page_name)
            if message:
                failures.append(message)
        for serial_number, page_name, coords, state in plan.state_requests:
            message = transport.change_state(serial_number, page_name, coords, state)
            if message:
                failures.append(message)
        for serial_number, page_name, coords, event in plan.emulate_requests:
            message = transport.emulate_input(serial_number, page_name, coords, event)
            if message:
                failures.append(message)
    except OlderInstance:
        # One skew result covers this request and every request behind it.
        failures.append(SKEW_MESSAGE)
    except TransportError as e:
        # Report a broken conversation once instead of repeating its timeout for each request.
        failures.append(str(e))
    return failures


def forward_cli_requests(args: Namespace,
                         transport: Transport | None = None) -> Verdict:
    """Park requests for this boot or forward them to the running instance without printing.
    Inputs fail when no running instance can apply them at the requested moment."""
    if any_instance_verb(args):
        # Instance-only verbs take precedence and never park or boot.
        outcome = answer_instance_verbs(args, transport)
        return Verdict(handled=True, failures=outcome.failures, output=outcome.output)

    if answered_by_a_listing(args):
        # Refuse an adjacent press because a listing is the whole command.
        return Verdict(handled=False,
                       failures=unparkable(plan_requests(args), LISTING_MESSAGE))

    plan = plan_requests(args)
    if plan.failures:
        return Verdict(handled=False, failures=plan.failures)
    if plan.empty:
        return Verdict()

    if transport is None:
        try:
            transport = bus_transport()
        except TransportError as e:
            # Do not boot on an unknown bus state because another instance can hold the deck.
            return Verdict(handled=False, failures=[str(e)])

    # Replacement applies only parkable work and makes that decision before any request is parked.
    # Check --close-running before probing because takeover does not depend on current ownership.
    if args.close_running:
        refusals = unparkable(plan, CLOSE_RUNNING_MESSAGE)
        if refusals:
            return Verdict(handled=False, failures=refusals)
        park(plan)
        return Verdict(handled=False)

    if not transport.is_running():
        refusals = unparkable(plan, NOT_RUNNING_MESSAGE)
        if refusals:
            return Verdict(handled=False, failures=refusals)
        park(plan)
        return Verdict(handled=False)

    return Verdict(handled=True, failures=forward(plan, transport))


def forward_parked_requests(transport: Transport | None = None) -> list[str]:
    """Forward parked requests after this launch loses the application-name race.
    Do not re-probe or re-parse; a concurrent owner exit is reported as a send failure."""
    from src.backend import startup_queue

    page_requests, parked_states = startup_queue.get().claim_parked_requests()
    if not page_requests and not parked_states:
        return []
    if transport is None:
        transport = bus_transport()
    return forward(Plan(
        page_requests=page_requests,
        state_requests=[
            (serial, parked["page_name"], parked["coords"], parked["state"])
            for serial, parked in parked_states
        ],
    ), transport)


# Running-instance reads and commands never park; without an instance, refuse the whole command.

_NOT_RUNNING_INSTANCE_MESSAGE = (
    "Error: Deckard is not running, so there was nothing to read or change. "
    "These commands act on the running Deckard, and none of them can wait for "
    "a deck that is not open yet. Start Deckard, then run the command again.")


@dataclass(frozen=True)
class InstanceOutcome:
    """Ordered stdout lines and stderr failures from instance-only verbs.
    Any failure gives the command exit code 1, even when output exists."""

    failures: list[str] = field(default_factory=list)
    output: list[str] = field(default_factory=list)


def any_instance_verb(args: Namespace) -> bool:
    """Return whether the command has a verb that only a running instance can answer.
    Test single-serial values against None so an explicit empty serial remains a verb."""
    return bool(args.json
                or args.get_brightness is not None
                or args.sleep is not None
                or args.wake is not None
                or args.set_brightness
                or args.list_actions
                or args.rename_page
                or args.duplicate_page)


def _plan_instance_verbs(args: Namespace) -> tuple[list[tuple[str, tuple[str, ...]]],
                                                    list[str]]:
    """Plan instance verbs in fixed order and validate all bus text before forwarding.
    One malformed verb voids the whole command."""
    jobs: list[tuple[str, tuple[str, ...]]] = []
    failures: list[str] = []

    if args.json:
        jobs.append(("json", ()))

    for flag, attr in (("--get-brightness", args.get_brightness),
                       ("--sleep", args.sleep), ("--wake", args.wake)):
        if attr is None:
            # None means absent; an empty serial remains a verb and is refused as an unknown deck.
            continue
        bad = _unsendable(flag, "The serial number", attr)
        if bad:
            failures.append(bad)
        else:
            jobs.append((flag[2:], (attr,)))

    if args.set_brightness:
        serial, value = args.set_brightness
        where = "--set-brightness"
        bad = _unsendable(where, "The serial number", serial)
        if bad:
            failures.append(bad)
        else:
            try:
                number = int(value)
            except ValueError:
                failures.append(f"Error: Invalid brightness in {where}: '{value}'. "
                                f"Expected a whole number from 0 to 100")
            else:
                if number < 0 or number > 100:
                    failures.append(f"Error: Brightness out of range in {where}: "
                                    f"'{value}'. Expected a whole number from 0 to 100")
                else:
                    jobs.append(("set-brightness", (serial, str(number))))

    if args.list_actions:
        where = "--list-actions"
        parts = args.list_actions
        if len(parts) > 2:
            failures.append(f"Error: Too many values in {where}: {parts!r}. "
                            f"Expected a page name, and coordinates at most")
        else:
            page = parts[0]
            coords = parts[1] if len(parts) == 2 else ""
            bad = _unsendable(where, "The page name", page)
            if not bad and coords:
                bad = (_unsendable(where, "The coordinates", coords)
                       or _bad_coords(where, coords))
            if bad:
                failures.append(bad)
            else:
                jobs.append(("list-actions", (page, coords)))

    for flag, pair in (("--rename-page", args.rename_page),
                       ("--duplicate-page", args.duplicate_page)):
        if not pair:
            continue
        first, second = pair
        bad = (_unsendable(flag, "The page name", first)
               or _unsendable(flag, "The new page name", second))
        if not bad and not first:
            bad = f"Error: Invalid page name in {flag}: '{first}'"
        if not bad and not second:
            bad = f"Error: Invalid page name in {flag}: '{second}'"
        if bad:
            failures.append(bad)
        else:
            jobs.append((flag[2:], (first, second)))

    return jobs, failures


def _int_if_whole(value: Any) -> Any:
    """Render a whole-number brightness as an int, so 75.0 reads as 75."""
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return value
    return int(as_float) if as_float.is_integer() else value


def _query_error(payload: str) -> str | None:
    """Return a top-level JSON error or an unreadable-payload error, else None."""
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError):
        return "The running Deckard sent an answer this command could not read"
    if isinstance(obj, dict) and "error" in obj:
        return str(obj["error"])
    return None


def _brightness_from_dump(serial: str, payload: str) -> tuple[str, str | None]:
    """Return one deck's brightness and an optional error from a QueryState dump.
    Unknown serials list connected devices, and an unset brightness is an explicit error."""
    try:
        state = json.loads(payload)
    except (ValueError, TypeError):
        return ("", "The running Deckard sent a state this command could not read")
    decks = state.get("decks", []) if isinstance(state, dict) else []
    available: list[str] = []
    for deck in decks:
        if not isinstance(deck, dict):
            continue
        available.append(str(deck.get("serial")))
        if deck.get("serial") == serial:
            brightness = deck.get("brightness")
            if brightness is None:
                return ("", f"The brightness of {serial} is not set yet")
            return (str(_int_if_whole(brightness)), None)
    if available:
        return ("", f"StreamDeck with serial '{serial}' not found. "
                    f"Available devices: {', '.join(available)}")
    return ("", f"StreamDeck with serial '{serial}' not found. No StreamDeck "
                f"devices connected yet (the app may still be starting)")


def _run_one_verb(kind: str, params: tuple[str, ...], transport: Transport,
                  output: list[str], failures: list[str]) -> None:
    """Append one verb's failure or output; get-brightness extracts a QueryState value."""
    if kind == "json":
        payload = transport.query_state()
        error = _query_error(payload)
        if error:
            failures.append(error)
        else:
            output.append(payload)
        return
    if kind == "get-brightness":
        (serial,) = params
        payload = transport.query_state()
        error = _query_error(payload)
        if error:
            failures.append(error)
            return
        value, why = _brightness_from_dump(serial, payload)
        if why is not None:
            failures.append(why)
        else:
            output.append(value)
        return
    if kind == "list-actions":
        page, coords = params
        payload = transport.list_actions(page, coords)
        error = _query_error(payload)
        if error:
            failures.append(error)
        else:
            output.append(payload)
        return
    if kind == "set-brightness":
        serial, value = params
        message = transport.set_brightness(serial, int(value))
    elif kind == "sleep":
        message = transport.sleep(params[0])
    elif kind == "wake":
        message = transport.wake(params[0])
    elif kind == "rename-page":
        message = transport.rename_page(params[0], params[1])
    else:  # duplicate-page
        message = transport.duplicate_page(params[0], params[1])
    if message:
        failures.append(message)


def run_instance_verbs(jobs: list[tuple[str, tuple[str, ...]]],
                       transport: Transport) -> tuple[list[str], list[str]]:
    """Run every independent job, but stop once for version skew or a broken transport."""
    output: list[str] = []
    failures: list[str] = []
    try:
        for kind, params in jobs:
            _run_one_verb(kind, params, transport, output, failures)
    except OlderInstance:
        failures.append(SKEW_MESSAGE)
    except TransportError as e:
        failures.append(str(e))
    return output, failures


def answer_instance_verbs(args: Namespace,
                          transport: Transport | None = None) -> InstanceOutcome:
    """Answer all instance-only verbs without parking or booting.
    Syntax errors void the command; instance verbs outrank parkable requests."""
    jobs, failures = _plan_instance_verbs(args)
    if failures:
        return InstanceOutcome(failures=failures + [USAGE])
    if transport is None:
        try:
            transport = bus_transport()
        except TransportError as e:
            return InstanceOutcome(failures=[str(e)])
    if not transport.is_running():
        return InstanceOutcome(failures=[_NOT_RUNNING_INSTANCE_MESSAGE])
    output, run_failures = run_instance_verbs(jobs, transport)
    return InstanceOutcome(failures=run_failures, output=output)


# D-Bus transport

def bus_transport() -> Transport:
    """Build the private late-import transport and convert connection failures to CLI errors.
    Catch broadly because naming the toolkit error here would import the toolkit at module scope."""
    try:
        return _BusTransport()
    except Exception as e:
        raise TransportError(
            f"Could not open the session bus, so nothing was applied: {e}") from e


class _BusTransport:
    """Access the running instance while keeping toolkit imports out of module scope."""

    def __init__(self) -> None:
        from gi.repository import Gio, GLib

        self._gio = Gio
        self._glib = GLib
        self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def is_running(self) -> bool:
        """Check application-name ownership without D-Bus activation.
        Probe errors mean no owner: parkable requests boot, but instance-only verbs fail."""
        try:
            reply = self._connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                self._glib.Variant("(s)", (appinfo.APP_ID,)),
                self._glib.VariantType("(b)"),
                self._gio.DBusCallFlags.NO_AUTO_START,
                DBUS_CALL_TIMEOUT_MS,
                None,
            )
        except self._glib.Error:
            return False
        return bool(reply.unpack()[0])

    def change_page(self, serial: str, page: str) -> str:
        return self._call("ChangePage", self._glib.Variant("(ss)", (serial, page)))

    def change_state(self, serial: str, page: str, coords: str, state: int) -> str:
        return self._call(
            "ChangeState",
            self._glib.Variant("(sssi)", (serial, page, coords, state)),
        )

    def emulate_input(self, serial: str, page: str, coords: str, event: str) -> str:
        return self._call(
            "EmulateInput",
            self._glib.Variant("(ssss)", (serial, page, coords, event)),
        )

    def query_state(self) -> str:
        return self._call("QueryState", None)

    def list_actions(self, page: str, coords: str) -> str:
        return self._call(
            "ListActions", self._glib.Variant("(ss)", (page, coords)))

    def set_brightness(self, serial: str, value: int) -> str:
        return self._call(
            "SetDeckBrightness", self._glib.Variant("(si)", (serial, value)))

    def sleep(self, serial: str) -> str:
        return self._call("Sleep", self._glib.Variant("(s)", (serial,)))

    def wake(self, serial: str) -> str:
        return self._call("Wake", self._glib.Variant("(s)", (serial,)))

    def rename_page(self, old: str, new: str) -> str:
        return self._call("RenamePage", self._glib.Variant("(ss)", (old, new)))

    def duplicate_page(self, source: str, new: str) -> str:
        return self._call(
            "DuplicatePage", self._glib.Variant("(ss)", (source, new)))

    def _call(self, method: str, params: "GLib.Variant | None") -> str:
        try:
            reply = self._connection.call_sync(
                appinfo.APP_ID,
                appinfo.DBUS_OBJECT_PATH,
                TOP_IFACE,
                method,
                params,
                self._glib.VariantType("(s)"),
                self._gio.DBusCallFlags.NO_AUTO_START,
                CONTROL_CALL_TIMEOUT_MS,
                None,
            )
        except self._glib.Error as e:
            if self._gio.DBusError.get_remote_error(e) in _METHOD_MISSING:
                raise OlderInstance from e
            # Convert all other bus failures to CLI text instead of an uncaught false-success crash.
            raise TransportError(str(e)) from e
        return str(reply.unpack()[0])
