"""The CLI half of the control plane. This decides what an invocation that
carries --change-page or --change-state does with the requests it was given.

An invocation that names a deck has two jobs, by whether Deckard already runs.
With nothing running, and with --close-running, which is about to stop what
runs, the invocation becomes the instance. It parks its requests for the deck
that has not enumerated yet and boots; see legs B and C in
src/backend/startup_queue.py. With an instance running, the requests go over
the bus to that instance and this process ends.

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


# Every CLI-side D-Bus call carries this rather than the 25s bus default.
# Without it a wedged instance that accepts and never replies blocks startup
# for that long. The probes in main.py import it from here, so one number
# serves both and no two numbers drift apart.
DBUS_CALL_TIMEOUT_MS = 5000

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
    "The running Deckard is not answering to the page and state methods this "
    "command needs. An older build does not have them at all, and restarting "
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

USAGE = """
Usage examples:
  --change-state CL123456789 Main 0,0 1
  --change-state CL123456789 Soundboard 2,1 0

Parameters:
  SERIAL_NUMBER: Device serial (e.g., CL123456789)
  PAGE_NAME: Page name (e.g., Main, Soundboard)
  COORDINATES: Position as x,y (e.g., 0,0 for top-left)
  STATE_NUMBER: State to change to (e.g., 0, 1, 2)"""


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
    invocation, whatever handled says.
    """

    handled: bool = False
    failures: list[str] = field(default_factory=list)


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
    """

    page_requests: list[tuple[str, str]] = field(default_factory=list)
    state_requests: list[tuple[str, str, str, int]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.page_requests and not self.state_requests


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
        # The coordinates take no such check of their own. A lone surrogate is
        # no digit, so the int conversion below refuses one as a bad coordinate
        # before it can reach the wire.
        if not coords or "," not in coords:
            failures.append(
                f"Error: Invalid coordinate format in {where}: '{coords}'. "
                f"Expected format: 'x,y' (e.g., '0,0')")
            continue
        try:
            x, y = (int(part) for part in coords.split(","))
        except ValueError:
            failures.append(
                f"Error: Invalid coordinate format in {where}: '{coords}'. "
                f"Expected integers like '0,0'")
            continue
        if x < 0 or y < 0:
            failures.append(f"Error: Coordinates must be non-negative in {where}: '{coords}'")
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
    if not raw_page_requests and not raw_state_requests:
        return Plan()

    # argparse hands each group over as a list. A tuple per request keeps one
    # shape for both kinds and matches what the startup queue hands back when a
    # lost launch claims its parking again.
    page_requests, page_failures = _parse_page_requests(raw_page_requests)
    state_requests, state_failures = _parse_state_requests(raw_state_requests)
    failures = page_failures + state_failures
    if failures:
        # Nothing is parked or sent yet, and nothing gets parked or sent.
        return Plan(failures=failures + [USAGE])

    return Plan(page_requests=page_requests, state_requests=state_requests)


# The two things an invocation can do with its requests

def park(plan: Plan) -> None:
    """Hand every request to the startup queue, for the decks this process
    enumerates next. The serial keys each request and the last write wins,
    which is the parking contract rather than an effect of this loop.

    The startup queue imports globals, and this module's body must not, so the
    import sits here. Only a process that goes on to boot reaches this call.
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
    request as argv gave them. argparse collects the two flags into two lists,
    so nothing here recovers how they interleaved, and this is the order the
    CLI applies.

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
    """Apply the invocation's --change-page and --change-state requests.

    It parks them for this process to pick up as it boots, or forwards them to
    the instance already running; see the module docstring. It never exits and
    never prints. The verdict says what happened, and main.py owns both.

    This is the boot path's call. A process that reaches it has imported the
    application, so the probe below is the one that decides, and it is the
    probe that always decided. The fast path asks the same question earlier
    and only to learn whether it can skip that import; a no there changes
    nothing here.
    """
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

    if not transport.is_running() or args.close_running:
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
                DBUS_CALL_TIMEOUT_MS,
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
