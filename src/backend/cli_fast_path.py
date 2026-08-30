"""Forward instance-only CLI requests before expensive application imports.
This module must not import globals or perform side effects when it returns BOOT."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.backend import cli_forward

if TYPE_CHECKING:
    from argparse import Namespace

    from src.backend.cli_forward import Transport


@dataclass(frozen=True)
class Outcome:
    """Tell main.py to boot for None or complete with an integer exit code."""

    exit_code: int | None = None
    failures: tuple[str, ...] = ()
    output: tuple[str, ...] = ()


#: Hand the invocation back to the ordinary startup path.
BOOT = Outcome()


def runs_in_this_process(args: Namespace) -> bool:
    """Return whether main.py must answer the command after its own imports.
    Listing commands take precedence over other arguments."""
    return cli_forward.answered_by_a_listing(args)


def answer_from_running_instance(args: Namespace,
                                 transport: Transport | None = None) -> Outcome:
    """Forward requests or return BOOT without printing or exiting.
    transport can inject a bus implementation."""
    if cli_forward.any_instance_verb(args):
        # Answer instance verbs here because reads cannot be parked.
        # Never boot the full application only to answer one.
        outcome = cli_forward.answer_instance_verbs(args, transport)
        return Outcome(exit_code=1 if outcome.failures else 0,
                       failures=tuple(outcome.failures),
                       output=tuple(outcome.output))

    if runs_in_this_process(args):
        # Refuse unparkable input beside a listing instead of dropping it.
        # Parkable requests survive because their store outlives this process.
        refusals = cli_forward.unparkable(cli_forward.plan_requests(args),
                                          cli_forward.LISTING_MESSAGE)
        if refusals:
            return Outcome(exit_code=1, failures=tuple(refusals))
        return BOOT

    plan = cli_forward.plan_requests(args)
    if plan.failures:
        # Reject malformed requests before expensive application imports.
        return Outcome(exit_code=1, failures=tuple(plan.failures))
    if plan.empty:
        return BOOT
    if args.close_running:
        # Park requests for the replacement instance after --close-running.
        # Refuse inputs that cannot be parked.
        refusals = cli_forward.unparkable(plan, cli_forward.CLOSE_RUNNING_MESSAGE)
        if refusals:
            return Outcome(exit_code=1, failures=tuple(refusals))
        return BOOT

    if transport is None:
        try:
            transport = cli_forward.bus_transport()
        except cli_forward.TransportError:
            # Let startup retry a missing session bus and report the failure once.
            # No request was applied, so handing back is safe.
            return BOOT

    if not transport.is_running():
        # Refuse unparkable requests when no instance can apply them.
        # Booting would otherwise report success without applying the input.
        refusals = cli_forward.unparkable(plan, cli_forward.NOT_RUNNING_MESSAGE)
        if refusals:
            return Outcome(exit_code=1, failures=tuple(refusals))

        # Boot only when no running instance justifies the application import.
        # Startup owns the final application-name race and request parking.
        return BOOT

    failures = cli_forward.forward(plan, transport)
    return Outcome(exit_code=1 if failures else 0, failures=tuple(failures))
