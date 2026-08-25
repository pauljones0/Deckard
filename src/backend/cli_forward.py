"""The CLI half of the control plane. This decides what an invocation that
carries --change-page, --change-state or --emulate-input does with the
requests it was given.

An invocation that names a deck has two jobs, by whether Deckard already runs.
With nothing running, and with --close-running, which is about to stop what
runs, the invocation becomes the instance. It parks its requests for the deck
that has not enumerated yet and boots; see legs B and C in
src/backend/startup_queue.py. With an instance running, the requests go over
the bus to that instance and this process ends.

An emulated input has only the second job. It is a press, which happens at a
moment, so there is nothing to hold for a deck that appears later. Such an
invocation is refused where the others park; see unparkable().

Parking happens before the process knows which of the two it is. A launch
that parks and then loses the race for the application name holds requests
that belong to another process. forward_parked_requests takes them there.

Nothing can import main.py, because its module body re-execs the process, so
this work lives here where a scenario drives it.

This body imports the standard library and appinfo, and nothing else. The CLI
fast path (src/backend/cli_fast_path.py) executes it before the application
imports anything, which is before globals.py exists, so the startup queue and
the toolkit are both imported where they are used rather than here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

import appinfo

if TYPE_CHECKING:
    from argparse import Namespace
    from typing import Protocol

    # _connect() imports GLib on demand, so this stays out of the runtime
    # closure and the annotation that uses it stays a string.
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


# Every CLI-side D-Bus probe carries this rather than the 25s bus default.
# Without it a wedged instance that accepts and never replies blocks startup
# for that long. The probes in main.py import it from here, so one number
# serves both and no two numbers drift apart.
DBUS_CALL_TIMEOUT_MS = 5000

# What a control method gets instead. A caller that gives up on work the
# instance goes on to do is the failure to avoid: a state change survives being
# asked twice, and a press does not, so a command that reports a timeout and
# presses anyway invites the retry that presses twice.
#
# This clears the two waits a control method makes by design and states a bound
# for: the control plane's input load wait, and its wait for a press to reach
# the deck. It is not a bound on everything such a method can do. A page switch
# serializes on the deck's page lock, and building a page is as long as the
# page is, so a call can still outlast this number. It clears what can be
# named. scenario_cli_forward_all pins the ordering, because the two sides of
# it live in modules that cannot import each other.
#
# The cost is the other direction: an instance wedged mid-call holds a typed
# command for this long rather than for the probe timeout above. A probe
# decides whether to boot, so it stays short; a control call is work a person
# asked for, so it waits. One tail is left standing either way: the handover
# call in src/backend/instance_gate.py keeps the probe timeout, so a second
# launch can still time out against a main context that a long control call is
# holding, and it reports that rather than lose anything.
CONTROL_CALL_TIMEOUT_MS = 20000

# The interface that the top-level object carries, which is the app id.
TOP_IFACE = appinfo.APP_ID

# What comes back when the methods are absent. GDBus uses these two spellings
# for a missing method, by its version, and for a missing object as well. It
# answers both from its own worker thread, so they arrive promptly whether or
# not the instance dispatches anything, which measurement confirms.
#
# A starting instance does not answer this way. The app publishes its objects
# before it takes the bus name (src/backend/instance_gate.py), so a name owned
# by such a build always has the methods behind it. Two cases still reach here. A build too old to carry
# them, and an instance on its way out, because teardown takes the interface
# off the bus first and keeps the name until the process ends. An instance
# whose publish failed looks the same, and says so in its own log. A wedged
# instance answers differently. Its objects are up, the call queues behind
# whatever blocks the loop, and the caller gets the timeout below.
_METHOD_MISSING = (
    "org.freedesktop.DBus.Error.UnknownMethod",
    "org.freedesktop.DBus.Error.UnknownInterface",
)

# What a current CLI says to an instance without the methods. The other
# direction cannot speak. A CLI from an older install forwards a change_page
# or change_state Gio action, which a current instance does not register, and
# org.gtk.Actions answers an unknown action by doing nothing. It needs both
# installs on one machine, it ends as soon as the old install goes, and the
# parked boot path stays correct, so this documents it rather than defends
# against it.
SKEW_MESSAGE = (
    "The running Deckard is not answering to the control methods this command "
    "needs. An older build does not have them at all, and restarting "
    "it is what picks up a build that does. An instance that is shutting down "
    "answers the same way until it is gone, so if one was just quitting, try "
    "again in a moment."
)

# The widest state number that can be asked for. src/api.py declares
# ChangeState with an int32 state, so a wider number cannot go on the wire at
# all: GLib.Variant raises OverflowError while it packs the call, past every
# place that turns a bad request into a sentence and out of a module body that
# no exception hook covers. This is the wire's own limit rather than a guess at
# what hardware has, which is why it is checked and the coordinates are not.
MAX_STATE_NUMBER = 2**31 - 1

# The event words --emulate-input takes. control_plane.EMULATED_EVENTS is the
# same list, and owns it: that module decides what each word does to an input.
# This copy exists because this body stays importable before globals and that
# module is not, and scenario_cli_forward_all pins the two to each other. A
# word missing from this copy is refused here, before the instance that would
# have carried it out is asked.
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

# What an invocation carrying an emulated input is told when there is no
# running instance to press against. Both halves of the CLI answer with these,
# and neither parks such a request; see park() and Plan.
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

# --list-devices and --list-pages are answered by the launched process itself,
# which prints and leaves. Nothing else on the line runs, so a press on it
# would be dropped with a successful exit, which is the outcome this whole rule
# exists to prevent.
LISTING_MESSAGE = (
    "Error: a listing is answered by this command alone, and nothing else on "
    "the line runs, so nothing was pressed. Ask for the listing and the press "
    "in two commands.")


class OlderInstance(Exception):
    """The running instance carries no control methods on the bus. See
    _METHOD_MISSING for the cases that reach this."""


class TransportError(Exception):
    """The conversation with the running instance failed, or never started.

    It never replied, the connection went away, it refused the call, or there
    was no session bus to open in the first place. This carries the bus's own
    text, so nothing outside this module needs to know a GLib.Error, and its
    text is what the person who typed the command reads."""


@dataclass(frozen=True)
class Verdict:
    """What the invocation should do next.

    handled means that a running instance took the requests and this process
    has nothing left to do. False means that the requests are parked, or that
    none arrived, and this process boots on. failures holds the sentences for
    the person who typed the command. A non-empty list marks a failed
    invocation, whatever handled says. output holds the lines a read verb
    prints to stdout, which is empty for every request that only forwards or
    parks.
    """

    handled: bool = False
    failures: list[str] = field(default_factory=list)
    output: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Plan:
    """What one invocation asks a deck to do, read from argv alone.

    This is the whole set of work a running instance can take off a command
    line. Both callers read it from here: the fast path before the application
    imports anything, and the boot path that parks. One reader would drift
    from the other, and an invocation that forwards a request the parking half
    does not know about loses it.

    empty means the command asks for nothing and is an ordinary launch.
    failures holds the sentences for the person who typed the command, and a
    non-empty list voids the whole command; see _parse_state_requests.

    A kind of request added here needs four more edits, and each one is a
    silent loss on its own: a reader in plan_requests, a send in forward(), a
    park in park(), and a term in empty below. Nothing derives them, because a
    send and a park are per-kind work either way. The fast path
    (src/backend/cli_fast_path.py) needs no edit, with one exception named in
    its own docstring: a kind that cannot be parked.

    The emulated inputs are that exception, and their fourth edit is
    unparkable() rather than a branch in park(). A press is an instant and not
    a setting, so there is nothing to apply when the deck finally appears.
    Both halves of the CLI ask unparkable() before they park, and the
    invocation ends there with a sentence and a non-zero code.
    """

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
    """The sentence for an argument that cannot go on the bus, or None.

    The bus carries UTF-8. Python decodes argv with surrogateescape, so a
    command line holding a byte that is not valid UTF-8 arrives here as a
    string with a lone surrogate in it. Nothing refuses that until GLib.Variant
    packs the call and raises UnicodeEncodeError, which is past every place
    that turns a bad request into a sentence.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return (f"Error: {what} in {where} is not text this command can send: "
                f"{value!r}. The bus carries UTF-8 and these bytes are not.")
    return None


def _bad_coords(where: str, coords: str) -> str | None:
    """The sentence for an x,y group that does not read as one, or None.

    Shape alone, which needs no device. Whether (9,9) sits on the deck is the
    running instance's answer, and the text travels there as it was typed. A
    cap here, such as coordinates at most 10, matches no hardware and rejects a
    valid request for a large deck before a device sees it.

    The coordinates take no UTF-8 check of their own. A lone surrogate is no
    digit, so the int conversion refuses one as a bad coordinate before it can
    reach the wire. Both kinds of request that carry coordinates read them
    here, so both refuse the same text with the same sentence.
    """
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
    """Read the --change-page groups into (serial, page).

    These groups went unchecked until a page name that argv carried as
    non-UTF-8 bytes reached the transport and raised out of it. Like the state
    groups below, this judges shape alone: whether the serial and the page name
    are real is the running instance's answer, not this module's.
    """
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
    """Read the --change-state groups into (serial, page, coords, state).

    All or nothing. A command with one malformed group applies none of its
    requests, because a part-applied command costs more than either other
    outcome.

    This judges shape alone, which needs no device. Do the coordinates read as
    two non-negative integers, and does the state read as a non-negative
    integer. The running instance answers from the device itself. It says
    whether (9,9) sits on the deck, whether state 19 exists on that input,
    and whether the page or the serial is real. A cap here, such as
    coordinates at most 10 and state at most 20, matches no hardware. Such a
    cap rejects a valid request for a large deck before a device sees it.
    """
    parsed: list[tuple[str, str, str, int]] = []
    failures: list[str] = []
    for i, (serial_number, page_name, coords, state_number) in enumerate(raw):
        # Named by flag, because a command carrying both kinds otherwise
        # numbers two groups "argument 1" and leaves the reader to guess.
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
    """Read the --emulate-input groups into (serial, page, coords, event).

    All or nothing, as the state groups are. The event word is judged here
    against this module's own copy of the vocabulary, because a word no
    instance knows costs a round trip to learn and reads better named beside
    the ones that work.
    """
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
    """Does this command line carry a verb the launched process answers itself?

    --list-devices and --list-pages are answered inside main.py, which prints
    and returns before it looks at the requests on the line. Such an invocation
    is that command whatever else was typed, and nothing else on the line is
    applied. Both halves of the CLI read that from here, so a listing line gets
    one answer whichever half sees it.

    The attributes are named rather than looked up, so a flag renamed in
    cli_args raises on the next launch instead of quietly turning a listing
    command into a forward.
    """
    return bool(args.list_devices or args.list_pages)


def unparkable(plan: Plan, message: str) -> list[str]:
    """The sentences for the requests in plan that this process cannot apply.

    Parking is what lets an invocation that finds nothing running boot and
    apply its requests to the decks it opens next. An emulated input has no
    such meaning, so it is refused instead, and the whole command with it:
    every other outcome either presses at a moment nobody asked for or applies
    half of what was typed.

    One predicate, three situations. The caller passes the message for the one
    it is in, because it is the half that knows: nothing is running, this
    launch is about to replace what is, or a listing verb is going to answer
    the whole command by itself. Both halves of the CLI ask this before they
    park, which is what keeps the fast path's fall-through harmless and park()
    free of a kind it cannot take, and each situation reads the same in both
    halves because the message travels with the situation and not with the
    probe.
    """
    if not plan.emulate_requests:
        return []
    return [message]


def plan_requests(args: Namespace) -> Plan:
    """Read this invocation's requests out of argv. It touches nothing else.

    No bus, no data directory, no globals. That is what lets the fast path
    call it before the application imports anything.

    Every argument a request carries is checked here rather than at the
    transport. The fast path runs in main.py's module body, which no exception
    hook covers, so an argument that raises where it is packed for the wire
    leaves a traceback on screen instead of a sentence. The two shapes that did
    are a state number wider than the wire's integer and a string argv carried
    as non-UTF-8 bytes.
    """
    raw_page_requests = args.change_page or []
    raw_state_requests = args.change_state or []
    raw_emulate_requests = args.emulate_input or []
    if not raw_page_requests and not raw_state_requests and not raw_emulate_requests:
        return Plan()

    # argparse hands each group over as a list. A tuple per request keeps one
    # shape for every kind and matches what the startup queue hands back when a
    # lost launch claims its parking again.
    page_requests, page_failures = _parse_page_requests(raw_page_requests)
    state_requests, state_failures = _parse_state_requests(raw_state_requests)
    emulate_requests, emulate_failures = _parse_emulate_requests(raw_emulate_requests)
    failures = page_failures + state_failures + emulate_failures
    if failures:
        # Nothing is parked or sent yet, and nothing gets parked or sent.
        return Plan(failures=failures + [USAGE])

    return Plan(page_requests=page_requests, state_requests=state_requests,
                emulate_requests=emulate_requests)


# The two things an invocation can do with its requests

def park(plan: Plan) -> None:
    """Hand every parkable request to the startup queue, for the decks this
    process enumerates next. The serial keys each request and the last write
    wins, which is the parking contract rather than an effect of this loop.

    The startup queue imports globals, and this module's body must not, so the
    import sits here. Only a process that goes on to boot reaches this call.

    A plan carrying an emulated input never reaches here, because both callers
    ask unparkable() first and end the invocation on its answer. That is why
    this takes no branch for a kind it has no queue for, and why nothing on
    that queue has to be swept for a press that arrived too early.
    """
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
    """Send every request to the running instance and collect what it said.

    This sends every request. A return after the first send moves deck A and
    leaves deck B alone for --change-page A X --change-page B Y. It keeps a
    --change-state inside the process, on a command that also carries a
    --change-page.

    The order is every page request as argv gave them, then every state
    request, then every emulated input, each group in the order argv gave it.
    argparse collects each flag into a list of its own, so nothing here
    recovers how they interleaved, and this is the order the CLI applies. The
    presses come last on purpose: a command that sets a state and then presses
    the input presses the state it just set.

    The caller must see a failure. The bus methods answer with a sentence, and
    those sentences reach the verdict for main.py to print. A failed request
    does not stop the ones behind it, because each one is independent. A
    person who asked for four changes is better served by three applied and
    one explained than by a prefix.
    """
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
        # Every request behind this one fails the same way, and one answer
        # covers all of them.
        failures.append(SKEW_MESSAGE)
    except TransportError as e:
        # The conversation broke. It does not heal inside one command, and a
        # retry of each remaining request spends the call timeout again per
        # request against an instance that answers nothing. Report the failure
        # once and drop the rest.
        failures.append(str(e))
    return failures


def forward_cli_requests(args: Namespace,
                         transport: Transport | None = None) -> Verdict:
    """Apply the invocation's --change-page, --change-state and
    --emulate-input requests.

    It parks them for this process to pick up as it boots, or forwards them to
    the instance already running; see the module docstring. A press is never
    parked, and an invocation carrying one with nothing to press against ends
    in the verdict's failures instead. It never exits and never prints. The
    verdict says what happened, and main.py owns both.

    This is the boot path's call. A process that reaches it has imported the
    application, so the probe below is the one that decides, and it is the
    probe that always decided. The fast path asks the same question earlier
    and only to learn whether it can skip that import; a no there changes
    nothing here.
    """
    if any_instance_verb(args):
        # A read-side or page verb is answered by the running instance and
        # never parked or booted. This is read before the listing and the
        # request flow, so an instance verb is the command whatever else the
        # line carries, and the fast path reads it the same way.
        outcome = answer_instance_verbs(args, transport)
        return Verdict(handled=True, failures=outcome.failures, output=outcome.output)

    if answered_by_a_listing(args):
        # First, as in the fast path, so that one command line reaches one
        # answer from either half. The listing is the whole command: a request
        # that could be parked is left alone, because nothing on the line runs
        # after the listing prints, and a press is refused, because a press
        # that nothing applies must not exit as though it was made.
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
            # No bus is no answer. This process cannot tell whether an instance
            # runs, so a park and a boot would open a deck on a guess, next to
            # an instance that may hold it. Report the failure, which is what
            # ends the invocation with a non-zero code.
            return Verdict(handled=False, failures=[str(e)])

    # This process is the instance that applies these requests, once it has
    # booted and opened the decks they name. Anything it cannot apply that way
    # ends the invocation here, before a single request is parked: a command
    # applies all of itself or none of it.
    #
    # --close-running is read before the probe, and not after it, because the
    # answer does not depend on it. That launch takes the decks over whether or
    # not an instance is running now, so the situation is the same either way,
    # and the fast path, which cannot probe at that point, gives the same
    # answer for the same command line.
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
    """Hand this process's parked requests to the instance that owns the name.

    This serves the launch that parked and then lost. Parking happens before
    the race for the application name settles, and it must, because the
    requests exist for the boot that follows. So an invocation parks, gets as
    far as registering, and learns there that another launch took the name
    first. The requests then sit in a process that exits without a deck open.
    Without this call they leave with it, apply nothing, and report
    success.

    This probes nothing first. The caller just learned that another instance
    owns the name, which beats a second question. When that instance went away
    in between, the send fails and says so, like any other forward. The shapes
    come from this module's own parking a moment earlier, so nothing re-reads
    them, and the state is already the integer the bus method takes.

    Returns the failures to print. An empty list means that the instance took
    everything.
    """
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


# The read-side and page verbs
#
# These ask the running instance a question (--json, --get-brightness,
# --list-actions) or give it one thing to do (--set-brightness, --sleep,
# --wake, --rename-page, --duplicate-page). None of them park. A read has
# nothing to answer without an instance, and a deck or page command has nothing
# to act on, so with none running the whole command is refused with one
# sentence, the way an emulated input is. Both halves of the CLI answer them
# through answer_instance_verbs, and a scenario pins the two halves alike.

_NOT_RUNNING_INSTANCE_MESSAGE = (
    "Error: Deckard is not running, so there was nothing to read or change. "
    "These commands act on the running Deckard, and none of them can wait for "
    "a deck that is not open yet. Start Deckard, then run the command again.")


@dataclass(frozen=True)
class InstanceOutcome:
    """What answering the instance verbs produced.

    output holds the lines a read verb prints to stdout, in the order the verbs
    were read. failures holds the sentences for stderr. A non-empty failures
    marks a failed command, exit code 1, whatever output holds.
    """

    failures: list[str] = field(default_factory=list)
    output: list[str] = field(default_factory=list)


def any_instance_verb(args: Namespace) -> bool:
    """Does this command carry a verb only a running instance can answer?

    The named attributes raise if a flag is renamed in cli_args, rather than
    quietly treating the command as an ordinary launch. Both halves read this
    before the parking flow, so one command line reaches one answer.
    """
    return bool(args.json or args.get_brightness or args.set_brightness
                or args.sleep or args.wake or args.list_actions
                or args.rename_page or args.duplicate_page)


def _plan_instance_verbs(args: Namespace) -> tuple[list[tuple[str, tuple[str, ...]]],
                                                   list[str]]:
    """Read and syntax-check every instance verb on the line.

    Returns the jobs to forward, in a fixed order, and the syntax failures. A
    failure voids the whole command, as the request parser does, so nothing is
    asked of the instance when one verb is malformed. Every string an argument
    carries is checked for bytes the bus cannot send, because the fast path
    packs the call in a module body no exception hook covers.
    """
    jobs: list[tuple[str, tuple[str, ...]]] = []
    failures: list[str] = []

    if args.json:
        jobs.append(("json", ()))

    for flag, attr in (("--get-brightness", args.get_brightness),
                       ("--sleep", args.sleep), ("--wake", args.wake)):
        if not attr:
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
    """The reason a query verb failed, or None for a payload to print.

    A read method answers with one JSON object. A failure is {"error": "..."},
    and no state or actions dump carries a top-level "error" key, so the two
    never read alike. A payload that will not parse is itself the failure.
    """
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError):
        return "The running Deckard sent an answer this command could not read"
    if isinstance(obj, dict) and "error" in obj:
        return str(obj["error"])
    return None


def _brightness_from_dump(serial: str, payload: str) -> tuple[str, str | None]:
    """Read one deck's brightness out of a QueryState dump, as (value, error).

    error is None on success. It names an unknown serial, listing the serials
    that are connected, because the answer to "did I mistype it?" belongs in
    the failure. A brightness the instance has not sent to the device yet reads
    as null, and this says so rather than print nothing.
    """
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
    """Forward one instance-verb job and collect its answer.

    A query returns (error, payload): the error goes to failures and the
    payload to output. A command returns a sentence on failure and nothing on
    success, exactly as change_page does. An instance-returned reason is
    printed as it arrived, without an "Error:" prefix, the way every other
    forwarded failure is.
    """
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
    """Forward every job and collect (output, failures).

    A failed job does not stop the ones behind it, because each is independent.
    A version-skewed instance or a broken conversation ends the run once, the
    way forward() does: every job behind it fails the same way, and one answer
    covers all of them.
    """
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
    """Answer every read-side or page verb on the line, over the instance.

    Both halves of the CLI call this, so one command line gets one answer from
    whichever half sees it first. A syntax error voids the command before the
    bus. With no session bus to open, or no instance to reach, the command is
    refused with one sentence and never parked or booted: a read has nothing to
    answer and a deck or page command nothing to act on, and neither waits for
    a deck that is not open. This is why the fast path answers these here rather
    than hand them back, which would boot the whole application to read a line.
    """
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


# The real transport

def bus_transport() -> Transport:
    """Build the transport below, for a caller outside this module.

    A function rather than the class itself, so nothing outside reaches for a
    private name, and so the toolkit import stays where it is, inside the
    constructor. A scenario that drives the transport's own private call
    surface still names the class, which is the one blessed exception.

    A failed connection becomes a TransportError. The constructor's failure is
    a GLib.Error out of bus_get_sync, which means there is no session bus to
    reach an instance on, and both callers must answer that rather than let it
    escape: it reached main.py's @log.catch before, which printed a traceback
    and exited zero, and a dropped request that reports success is the worst of
    the outcomes. The catch is broad because naming the toolkit's error type
    here would put the toolkit in this module's body.
    """
    try:
        return _BusTransport()
    except Exception as e:
        raise TransportError(
            f"Could not open the session bus, so nothing was applied: {e}") from e


class _BusTransport:
    """The running instance, over the session bus.

    forward_cli_requests takes this object, so a test drives the forwarding
    rules without a bus, a daemon or a second process.

    The import of the toolkit sits here rather than at module scope, so this
    module's body stays standard-library-only. The deployment floor executes
    that body with every import stubbed out, and a CLI invocation that carries
    no request never builds one of these.
    """

    def __init__(self) -> None:
        from gi.repository import Gio, GLib

        self._gio = Gio
        self._glib = GLib
        self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def is_running(self) -> bool:
        """Does anything own the app's name on the bus right now?

        This asks the bus daemon and passes NO_AUTO_START. A call addressed to
        the well-known name activates it over D-Bus, which turns a probe into
        a launch. A probe that cannot complete reads as "nobody home", so a
        bus error parks the requests and boots rather than lose them.
        """
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
            # Every other error, such as a timeout against a wedged
            # instance, a dropped connection or a refused call, becomes text
            # the caller prints. A GLib.Error left in place escapes the CLI
            # and logs as a crash, which puts a traceback on screen and still
            # exits zero, which reads as success.
            raise TransportError(str(e)) from e
        return str(reply.unpack()[0])
