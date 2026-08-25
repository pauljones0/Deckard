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

--emulate-input is the first of those, and cli_forward.unparkable() is where
that answer lives. Three arms below ask it, one per situation, and each passes
the sentence for the situation it is in rather than for what a probe said: a
listing verb that answers the whole line by itself, a launch that is about to
replace the instance, and a launch that finds none. The boot path reads the
same three the same way, so one command line gets one answer from whichever
half sees it first, and a scenario pins the two halves against each other.

The fourth case, no session bus to open, still hands back, because the boot
path's own attempt reports it and one reporting site is what keeps the two
paths from answering the same command differently.
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
    output: tuple[str, ...] = ()


#: Hand the invocation back to the ordinary startup path.
BOOT = Outcome()


def runs_in_this_process(args: Namespace) -> bool:
    """Does this command need main.py's own imports rather than the bus?

    --list-devices and --list-pages are answered out of this process. main.py
    runs them before it looks at the forwarding requests and returns straight
    after, so an invocation carrying one is that command whatever else is on
    the line, and this module must not answer it instead.

    The rule lives in cli_forward, which the boot path reads too. Two copies of
    it would let one half treat a listing line as a listing and the other half
    forward what was beside it.
    """
    return cli_forward.answered_by_a_listing(args)


def answer_from_running_instance(args: Namespace,
                                 transport: Transport | None = None) -> Outcome:
    """Finish this invocation over the bus, or hand it back to main.py.

    It never prints and never exits, so a scenario drives it. transport is for
    those scenarios; a real invocation builds one only once it knows it has
    something to send.
    """
    if cli_forward.any_instance_verb(args):
        # A read-side or page verb is answered by the running instance, or
        # refused because none runs. It is never handed back: a read cannot be
        # parked, and booting the whole application to answer one would spend
        # the very imports this module exists to skip. The boot path reads the
        # same verbs the same way, and a scenario pins the two halves alike.
        outcome = cli_forward.answer_instance_verbs(args, transport)
        return Outcome(exit_code=1 if outcome.failures else 0,
                       failures=tuple(outcome.failures),
                       output=tuple(outcome.output))

    if runs_in_this_process(args):
        # main.py answers the listing itself and returns straight after, so
        # nothing else on the line runs. A request that can be parked survives
        # that, because the parking outlives this decision; a press does not
        # exist any more once this process has printed a list and left. Refuse
        # the whole line rather than print a listing and drop the press with a
        # successful exit.
        refusals = cli_forward.unparkable(cli_forward.plan_requests(args),
                                          cli_forward.LISTING_MESSAGE)
        if refusals:
            return Outcome(exit_code=1, failures=tuple(refusals))
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
        # requests belong to the decks it opens next, and it parks them. What
        # it cannot park it cannot carry out at all, and says so here rather
        # than boot to find that out.
        refusals = cli_forward.unparkable(plan, cli_forward.CLOSE_RUNNING_MESSAGE)
        if refusals:
            return Outcome(exit_code=1, failures=tuple(refusals))
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
        # Nothing to forward to. A request that cannot be parked cannot be
        # applied by the boot either, and the fall-through below would end in a
        # launch that presses nothing and reports success, so it ends here with
        # its reason.
        refusals = cli_forward.unparkable(plan, cli_forward.NOT_RUNNING_MESSAGE)
        if refusals:
            return Outcome(exit_code=1, failures=tuple(refusals))

        # Boot. The probe that decides is the one the boot path makes once the
        # application is up, which is where it was made before this module
        # existed, so a launch that races another for the application name
        # parks and hands over exactly as it did. This probe decides one thing
        # only: whether the import is worth making.
        return BOOT

    failures = cli_forward.forward(plan, transport)
    return Outcome(exit_code=1 if failures else 0, failures=tuple(failures))
