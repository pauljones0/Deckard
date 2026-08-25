"""The answer to a command line that only needs the instance already running.

`deckard --change-page CL123 Main` asks a running Deckard to do one thing. The
application in this process answers none of it. Before this module existed the
invocation still built that application first: OpenCV, the toolkit, the render
stack, the plugin machinery and the data directory, seconds of work, thrown
away the moment the one bus call came back. main.py now asks here in its own
body, after the rename migration and before its first expensive import, so a
forwarded call ends without any of that work.

Two outcomes, and no third

answer_from_running_instance() either finishes the invocation, and says with
which exit code, or hands it back whole. Handing it back is always safe,
because nothing here parks a request, writes a file, opens a device or logs a
line. The cases it hands back are: a verb this process runs itself, a command
that carries no request, a --close-running that is about to replace the
running instance, no session bus to open, and above all nothing running to
forward to. Each one then takes the path it took before, with the ordinary
probe deciding as it always did.

Where this runs, and what that costs

main.py's module body is outside every exception hook, and it installs them
later. An exception raised here therefore reaches a person as a traceback,
never as a sentence, so cli_forward checks each argument of a request rather
than let the transport raise while it packs the call. The call site in main.py
catches whatever still gets through and prints one sentence, which is the
guarantee: a failed command says what failed and leaves with a non-zero code.

The import contract

This body must not reach globals.py. That module resolves the data path from
argv and creates the directory at import time, work a forwarded call has no
business doing, and it pulls the logger in behind it. So the body holds the
standard library and cli_forward, whose body is the same shape. The bus
transport imports the toolkit inside its constructor, so an invocation that
falls through without forwarding never pays for that either.
tests/scenario_cli_fast_path.py pins both claims from a subprocess.

Adding a verb

A verb that a running instance can carry out belongs in cli_forward.Plan, and
the whole path here follows without an edit. One shape does not: a verb that
cannot be parked for a deck which has not appeared yet. Parking is what makes
the fall-through below harmless, so such a verb needs its own answer for "the
instance is not running" rather than a boot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.backend import cli_forward

if TYPE_CHECKING:
    from argparse import Namespace

    from src.backend.cli_forward import Transport


@dataclass(frozen=True)
class Outcome:
    """What main.py does next.

    exit_code None means this module finished nothing and the process boots.
    An integer means the invocation is over: print every failure on stderr and
    leave with that code.
    """

    exit_code: int | None = None
    failures: tuple[str, ...] = ()


#: Hand the invocation back to the ordinary startup path.
BOOT = Outcome()


def runs_in_this_process(args: Namespace) -> bool:
    """Does this command need main.py's own imports rather than the bus?

    --list-devices and --list-pages are answered out of this process. main.py
    runs them before it looks at the forwarding requests and returns straight
    after, so an invocation carrying one is that command whatever else is on
    the line, and this module must not answer it instead.

    The attributes are named rather than looked up, so a flag renamed in
    cli_args raises here on the next launch instead of quietly turning a
    listing command into a forward.
    """
    return bool(args.list_devices or args.list_pages)


def answer_from_running_instance(args: Namespace,
                                 transport: Transport | None = None) -> Outcome:
    """Finish this invocation over the bus, or hand it back to main.py.

    It never prints and never exits, so a scenario drives it. transport is for
    those scenarios; a real invocation builds one only once it knows it has
    something to send.
    """
    if runs_in_this_process(args):
        return BOOT

    plan = cli_forward.plan_requests(args)
    if plan.failures:
        # A malformed request is malformed whether or not anything runs, and
        # the sentences are the ones the boot path prints. Answering here
        # spares an import that ends in the same exit code.
        return Outcome(exit_code=1, failures=tuple(plan.failures))
    if plan.empty:
        return BOOT
    if args.close_running:
        # This launch is about to stop what runs and take its place, so its
        # requests belong to the decks it opens next, and it parks them.
        return BOOT

    if transport is None:
        try:
            transport = cli_forward.bus_transport()
        except cli_forward.TransportError:
            # No session bus here and now. Hand the invocation back rather than
            # report it: this runs at the very start of a launch, and a bus
            # that is still coming up at login answers the boot path's own
            # attempt a moment later. That attempt is the one that reports the
            # failure, and one reporting site is what keeps the two paths from
            # answering the same command differently. Nothing is lost, because
            # nothing was applied.
            return BOOT

    if not transport.is_running():
        # Boot. The probe that decides is the one the boot path makes once the
        # application is up, which is where it was made before this module
        # existed, so a launch that races another for the application name
        # parks and hands over exactly as it did. This probe decides one thing
        # only: whether the import is worth making.
        return BOOT

    failures = cli_forward.forward(plan, transport)
    return Outcome(exit_code=1 if failures else 0, failures=tuple(failures))
