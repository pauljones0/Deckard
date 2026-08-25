"""The CLI answers a running instance without building the application.

Three legs. The decision table, driven with a transport that records, covers
what the fast path finishes and what it hands back. The import fence, from a
subprocess, holds the module and its decision clear of globals and the
toolkit. The last leg runs the real entry point against a stand-in instance on
a private bus, with a stub cv2 that ends the process, so the exit code says
whether the heavy imports ran.

The fast path runs in main.py's module body, where no exception hook is
installed yet, so several checks here are about what a person sees rather than
about what the code returns: an argument that cannot go on the wire, and an
unforeseen failure behind it, must both arrive as one sentence and a non-zero
exit, never as a traceback and never as a silent success.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import contextlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

import globals as gl  # noqa: E402

import appinfo  # noqa: E402
from cli_args import argparser  # noqa: E402
from src.backend import cli_fast_path, cli_forward  # noqa: E402

from scenario_api_lifecycle_publish import (  # noqa: E402
    _die_with_parent, start_private_bus, stop_private_bus,
)
from scenario_cli_forward_all import Recorder  # noqa: E402

# Under the harness's own 90s kill, so this message and its dump arrive first.
WATCHDOG_SECONDS = 60

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(_REPO_ROOT, "main.py")
STUB_INSTANCE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "cli_fast_path_instance.py")

# What the stub cv2 leaves as an exit code. main.py imports cv2 first of the
# expensive imports and before globals, so this code means "the invocation
# fell through to the ordinary startup path", and its absence means the fast
# path finished the invocation. A number no other exit here uses.
HEAVY_IMPORT_EXIT = 77

SERIAL = "fastpath-deck-1"

# The bus address before this scenario starts its own daemon. Every child runs
# on the private one; a leg that reached this one could be answered by a
# Deckard the developer is running, because the fast path addresses the app's
# real name and no test-scoped id can be substituted for it.
ORIGINAL_BUS = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")


# What a command-line argument holding a byte that is not valid UTF-8 looks
# like once Python has decoded argv. The interpreter uses surrogateescape, so
# the byte arrives as a lone surrogate, and no encode accepts one. Built the
# same way the interpreter builds it rather than written as a literal.
NOT_UTF8 = "Alpha" + b"\xff".decode("utf-8", "surrogateescape")


def parse(argv: list[str]):
    """The app's own parser, so what is decided below is what argv produces."""
    return argparser.parse_args(argv)


@contextlib.contextmanager
def bus_transport_raising():
    """Make the real transport unbuildable, the way a missing bus does.

    bus_transport() turns the constructor's GLib.Error into a TransportError,
    and this stands in for that without taking the session bus away from a
    process that is about to need it for the legs after this one.
    """
    original = cli_forward.bus_transport

    def refuse():
        raise cli_forward.TransportError("no session bus in this test")

    cli_forward.bus_transport = refuse
    try:
        yield
    finally:
        cli_forward.bus_transport = original


def assert_nothing_parked(what: str) -> None:
    """The fast path runs before globals exists, so it must never park.

    Parking here would write requests into a process that is about to leave,
    and they would leave with it.
    """
    assert not gl.api_page_requests and not gl.api_state_requests, (
        f"{what}: the fast path parked {gl.api_page_requests} / "
        f"{gl.api_state_requests}, and a process that exits applies neither")


# 1. The decision table

FORWARD_ARGV = ["--change-page", "deck-a", "Alpha",
                "--change-state", "deck-b", "Beta", "0,0", "1"]
FORWARDS = [("page", "deck-a", "Alpha"), ("state", "deck-b", "Beta", "0,0", 1)]


def leg_decision_table() -> None:
    print("leg 1: what the fast path finishes and what it hands back")

    # A running instance takes every request, and the invocation is over.
    recorder = Recorder(running=True)
    outcome = cli_fast_path.answer_from_running_instance(parse(FORWARD_ARGV), recorder)
    assert outcome.exit_code == 0, (
        f"a forward the instance accepted must end the invocation with a "
        f"success code, not {outcome.exit_code!r}")
    assert outcome.failures == (), outcome.failures
    assert recorder.forwards() == FORWARDS, (
        f"the instance was sent {recorder.forwards()} instead of {FORWARDS}")
    assert_nothing_parked("a forwarded command")

    # Nothing running. The invocation is handed back whole, and the fast path
    # sends nothing, so the boot path decides with the probe that always
    # decided.
    recorder = Recorder(running=False)
    outcome = cli_fast_path.answer_from_running_instance(parse(FORWARD_ARGV), recorder)
    assert outcome.exit_code is None, (
        f"with nothing running the invocation must boot, not exit "
        f"{outcome.exit_code!r} -- its requests would be lost")
    assert recorder.forwards() == [], (
        f"nothing owns the name, so nothing may be sent: {recorder.forwards()}")
    assert_nothing_parked("a cold command")

    # --close-running replaces the instance, so its requests belong to the
    # decks this launch opens next and it must boot to park them.
    recorder = Recorder(running=True)
    outcome = cli_fast_path.answer_from_running_instance(
        parse([*FORWARD_ARGV, "--close-running"]), recorder)
    assert outcome.exit_code is None, (
        f"--close-running must boot, not exit {outcome.exit_code!r}")
    assert recorder.forwards() == [], (
        f"--close-running is about to stop the instance, so nothing may be "
        f"sent to it: {recorder.forwards()}")

    # A listing verb runs in this process and main.py returns straight after
    # it, so it is that command whatever else the line carries.
    for verb in ("--list-devices", "--list-pages"):
        recorder = Recorder(running=True)
        outcome = cli_fast_path.answer_from_running_instance(
            parse([*FORWARD_ARGV, verb]), recorder)
        assert outcome.exit_code is None, (
            f"{verb} must boot so this process can answer it, not exit "
            f"{outcome.exit_code!r}")
        assert recorder.forwards() == [], (
            f"{verb} forwarded {recorder.forwards()} -- that changes what the "
            f"command does, not only how fast it does it")

    # An ordinary launch carries no request and is not this module's business.
    recorder = Recorder(running=True)
    outcome = cli_fast_path.answer_from_running_instance(parse(["--devel"]), recorder)
    assert outcome.exit_code is None, (
        f"a launch with no request must boot, not exit {outcome.exit_code!r}")
    assert recorder.calls == [], (
        f"a launch with no request must not even probe: {recorder.calls}")

    # A malformed group voids the whole command, running instance or not, and
    # whatever else the line carries.
    for argv in (["--change-state", "deck-a", "Alpha", "0,0", "not-a-number"],
                 ["--change-state", "deck-a", "Alpha", "nope", "1"],
                 ["--change-state", "deck-a", "Alpha", "0,-1", "1"],
                 # Wider than the int32 the bus method takes. Left to the
                 # transport this raises OverflowError while it packs the call,
                 # in a module body no exception hook covers.
                 ["--change-state", "deck-a", "Alpha", "0,0",
                  str(cli_forward.MAX_STATE_NUMBER + 1)],
                 ["--change-state", "deck-a", "Alpha", "0,0", "99999999999999"],
                 # Bytes argv carried that are not valid UTF-8. Python decodes
                 # them to a lone surrogate, which no encode accepts, so the
                 # transport raises UnicodeEncodeError the same way.
                 ["--change-page", "deck-a", NOT_UTF8],
                 ["--change-page", NOT_UTF8, "Alpha"],
                 ["--change-state", "deck-a", NOT_UTF8, "0,0", "1"],
                 ["--change-state", NOT_UTF8, "Alpha", "0,0", "1"],
                 # A malformed group must beat --close-running. The other order
                 # stops the running instance over a typo and then refuses the
                 # command that the typo is in.
                 ["--change-state", "deck-a", "Alpha", "0,0", "not-a-number",
                  "--close-running"]):
        recorder = Recorder(running=True)
        outcome = cli_fast_path.answer_from_running_instance(parse(argv), recorder)
        assert outcome.exit_code == 1, (
            f"{argv} is malformed and must fail, not exit {outcome.exit_code!r}")
        assert cli_forward.USAGE in outcome.failures, outcome.failures
        assert recorder.forwards() == [], (
            f"a malformed command applies nothing: {recorder.forwards()}")
        assert_nothing_parked("a malformed command")

    # No session bus to open. This runs at the very start of a launch, and a
    # bus still coming up at login answers the boot path's own attempt a moment
    # later, so the invocation is handed back rather than refused here.
    recorder = Recorder(running=True)
    with bus_transport_raising():
        outcome = cli_fast_path.answer_from_running_instance(parse(FORWARD_ARGV))
    assert outcome.exit_code is None, (
        f"an unreachable bus must hand the invocation back, not exit "
        f"{outcome.exit_code!r} -- the boot path builds the same transport and "
        f"reports the failure")
    assert recorder.forwards() == [], recorder.forwards()
    assert_nothing_parked("an unreachable bus")

    # The instance went away between the probe and the send. The requests are
    # already claimed by this process, so it reports rather than boots -- a
    # boot here would open a deck the other instance may still hold.
    broke = cli_forward.TransportError("the connection is closed")
    recorder = Recorder(running=True, raises=broke)
    outcome = cli_fast_path.answer_from_running_instance(parse(FORWARD_ARGV), recorder)
    assert outcome.exit_code == 1, (
        f"a send that failed must exit non-zero, not {outcome.exit_code!r} -- "
        f"a zero reads as applied")
    assert outcome.failures == (str(broke),), outcome.failures

    # An instance without the methods answers every request the same way.
    recorder = Recorder(running=True, raises=cli_forward.OlderInstance())
    outcome = cli_fast_path.answer_from_running_instance(parse(FORWARD_ARGV), recorder)
    assert outcome.exit_code == 1, outcome.exit_code
    assert outcome.failures == (cli_forward.SKEW_MESSAGE,), outcome.failures

    # One request refused and the rest applied is still a failed command.
    recorder = Recorder(running=True, answers={"deck-a": "no such page"})
    outcome = cli_fast_path.answer_from_running_instance(parse(FORWARD_ARGV), recorder)
    assert outcome.exit_code == 1, outcome.exit_code
    assert outcome.failures == ("no such page",), outcome.failures
    assert recorder.forwards() == FORWARDS, (
        f"a refused request must not stop the ones behind it: "
        f"{recorder.forwards()}")

    print("  PASS: the fast path finishes only what it can finish")


# 1b. The verb that cannot be parked

EMULATE_ARGV = ["--emulate-input", "deck-a", "Alpha", "0,0", "press"]
EMULATE_FORWARDS = [("emulate", "deck-a", "Alpha", "0,0", "press")]


def leg_press_needs_a_running_instance() -> None:
    """A press is forwarded, or the invocation ends. It is never handed back.

    Parking is what makes handing an invocation back harmless: the launch that
    boots applies its requests to the decks it opens. A press cannot be held
    for a deck that has not appeared, so a fall-through here would end in a
    launch that presses nothing and reports success.
    """
    print("leg 1b: an emulated input against a running instance, or nothing")

    # An instance is running. This is the whole reason the verb exists, and it
    # ends here without the application being imported at all.
    recorder = Recorder(running=True)
    outcome = cli_fast_path.answer_from_running_instance(parse(EMULATE_ARGV), recorder)
    assert outcome.exit_code == 0, (
        f"a press the instance accepted must end the invocation with a success "
        f"code, not {outcome.exit_code!r}")
    assert outcome.failures == (), outcome.failures
    assert recorder.forwards() == EMULATE_FORWARDS, recorder.forwards()
    assert_nothing_parked("a forwarded press")

    # Nothing running. The boot cannot carry it out either, so the invocation
    # ends here with the reason rather than starting an application to find
    # that out.
    recorder = Recorder(running=False)
    outcome = cli_fast_path.answer_from_running_instance(parse(EMULATE_ARGV), recorder)
    assert outcome.exit_code == 1, (
        f"with nothing running a press must end the invocation, not boot: "
        f"{outcome.exit_code!r}")
    assert outcome.failures == (cli_forward.NOT_RUNNING_MESSAGE,), outcome.failures
    assert recorder.forwards() == [], recorder.forwards()
    assert_nothing_parked("a press with nothing running")

    # --close-running makes this launch the instance, which is the same case.
    recorder = Recorder(running=True)
    outcome = cli_fast_path.answer_from_running_instance(
        parse([*EMULATE_ARGV, "--close-running"]), recorder)
    assert outcome.exit_code == 1, (
        f"--close-running cannot press either, and must not boot: "
        f"{outcome.exit_code!r}")
    assert outcome.failures == (cli_forward.CLOSE_RUNNING_MESSAGE,), outcome.failures
    assert recorder.forwards() == [], (
        f"nothing may be sent to the instance this launch is about to stop: "
        f"{recorder.forwards()}")

    # A press beside a parkable request refuses the whole command, so half of
    # it is never applied by a process the other half was not meant for.
    recorder = Recorder(running=False)
    outcome = cli_fast_path.answer_from_running_instance(
        parse([*FORWARD_ARGV, *EMULATE_ARGV]), recorder)
    assert outcome.exit_code == 1, outcome.exit_code
    assert outcome.failures == (cli_forward.NOT_RUNNING_MESSAGE,), outcome.failures
    assert_nothing_parked("a mixed command with nothing running")

    # A malformed press is malformed whether or not anything runs, and it says
    # so here rather than at the transport.
    for argv in (["--emulate-input", "deck-a", "Alpha", "0,0", "smash"],
                 ["--emulate-input", "deck-a", "Alpha", "nope", "press"],
                 ["--emulate-input", "deck-a", NOT_UTF8, "0,0", "press"]):
        recorder = Recorder(running=True)
        outcome = cli_fast_path.answer_from_running_instance(parse(argv), recorder)
        assert outcome.exit_code == 1, (
            f"{argv} is malformed and must fail, not exit {outcome.exit_code!r}")
        assert cli_forward.USAGE in outcome.failures, outcome.failures
        assert recorder.forwards() == [], recorder.forwards()

    # A listing verb is answered by main.py itself, which prints and returns
    # before anything else on the line runs. A press on such a line is applied
    # by nobody, so the line is refused rather than answered with a listing and
    # a successful exit. It holds whether or not an instance is running,
    # because the listing ends this process either way.
    for running in (True, False):
        for verb in ("--list-pages", "--list-devices"):
            recorder = Recorder(running=running)
            outcome = cli_fast_path.answer_from_running_instance(
                parse([*EMULATE_ARGV, verb]), recorder)
            assert outcome.exit_code == 1, (
                f"{verb} with a press must refuse the line, not exit "
                f"{outcome.exit_code!r} and drop the press")
            assert outcome.failures == (cli_forward.LISTING_MESSAGE,), outcome.failures
            assert recorder.calls == [], (
                f"a refused line must not go near the bus: {recorder.calls}")

    # A listing verb on its own is still that command, whatever else it carries
    # that the boot can apply.
    for verb in ("--list-pages", "--list-devices"):
        recorder = Recorder(running=True)
        outcome = cli_fast_path.answer_from_running_instance(
            parse([*FORWARD_ARGV, verb]), recorder)
        assert outcome.exit_code is None, (
            f"{verb} must boot so this process can answer it, not exit "
            f"{outcome.exit_code!r}")

    print("  PASS: a press is forwarded to a running instance or refused with a reason")


DUMP = ('{"decks": [{"serial": "deck-a", "active_page": "Main", '
        '"brightness": 60}], "pages": ["Main"]}')


def leg_instance_verbs() -> None:
    """The read-side and page verbs, decided by the fast path.

    Each needs a running instance and is never handed back: a read cannot be
    parked, and booting the application to answer one would spend the very
    imports this module exists to skip.
    """
    print("leg 1d: the read-side and page verbs against a running instance, or nothing")

    # --json prints the dump and ends with success, no application imported.
    recorder = Recorder(running=True, query=DUMP)
    outcome = cli_fast_path.answer_from_running_instance(parse(["--json"]), recorder)
    assert outcome.exit_code == 0, outcome
    assert outcome.output == (DUMP,), outcome.output
    assert ("query_state",) in recorder.calls, recorder.calls
    assert_nothing_parked("a state dump")

    # --get-brightness reads one deck out of the same dump.
    recorder = Recorder(running=True, query=DUMP)
    outcome = cli_fast_path.answer_from_running_instance(
        parse(["--get-brightness", "deck-a"]), recorder)
    assert outcome.exit_code == 0 and outcome.output == ("60",), outcome

    # Every command verb forwards and ends here.
    for argv, call in (
        (["--set-brightness", "deck-a", "60"], ("set-brightness", "deck-a", 60)),
        (["--sleep", "deck-a"], ("sleep", "deck-a")),
        (["--wake", "deck-a"], ("wake", "deck-a")),
        (["--rename-page", "Main", "Home"], ("rename-page", "Main", "Home")),
        (["--duplicate-page", "Main", "Home"], ("duplicate-page", "Main", "Home")),
        (["--list-actions", "Main"], ("list_actions", "Main", "")),
    ):
        recorder = Recorder(running=True, query=DUMP)
        outcome = cli_fast_path.answer_from_running_instance(parse(argv), recorder)
        assert outcome.exit_code == 0, (argv, outcome)
        assert call in recorder.calls, (argv, recorder.calls)

    # With nothing running, every verb refuses here rather than boots.
    for argv in (["--json"], ["--get-brightness", "deck-a"], ["--list-actions", "Main"],
                 ["--set-brightness", "deck-a", "60"], ["--sleep", "deck-a"],
                 ["--rename-page", "Main", "Home"]):
        recorder = Recorder(running=False)
        outcome = cli_fast_path.answer_from_running_instance(parse(argv), recorder)
        assert outcome.exit_code == 1, (argv, outcome)
        assert outcome.failures == (cli_forward._NOT_RUNNING_INSTANCE_MESSAGE,), (
            argv, outcome.failures)
        assert recorder.forwards() == [], (argv, recorder.forwards())
        assert_nothing_parked(f"a refused {argv[0]}")

    # A malformed verb voids the command before the bus, running or not.
    recorder = Recorder(running=True)
    outcome = cli_fast_path.answer_from_running_instance(
        parse(["--set-brightness", "deck-a", "nope"]), recorder)
    assert outcome.exit_code == 1 and cli_forward.USAGE in outcome.failures, outcome
    assert recorder.calls == [], recorder.calls

    # An unreachable bus ends a read verb with a reason. It is NOT handed back:
    # a read cannot be parked, and the boot would import the application to
    # answer a line.
    with bus_transport_raising():
        outcome = cli_fast_path.answer_from_running_instance(parse(["--json"]))
    assert outcome.exit_code == 1, outcome
    assert outcome.failures and "session bus" in outcome.failures[0], outcome.failures

    print("  PASS: the read-side and page verbs forward to a running instance or refuse")


def leg_both_halves_answer_alike() -> None:
    """One command line gets one answer, whichever half of the CLI sees it.

    The fast path answers before the application is imported and the boot path
    answers after, and both meet the same three situations. They read the
    situation from different places -- the boot path has a bus probe where the
    fast path has none -- so the answers are pinned against each other here
    rather than against a constant in each file. The pairing is what caught the
    two halves calling the same command line two different things.
    """
    print("leg 1c: the two halves answer one command line the same way")

    lines = [
        ("nothing running", EMULATE_ARGV, False, False),
        ("an instance running", EMULATE_ARGV, True, False),
        ("--close-running over an instance", EMULATE_ARGV, True, True),
        ("--close-running with nothing running", EMULATE_ARGV, False, True),
        ("a press beside a page change, nothing running",
         [*FORWARD_ARGV, *EMULATE_ARGV], False, False),
        ("a malformed press", ["--emulate-input", "deck-a", "Alpha", "0,0", "smash"],
         True, False),
        # A listing verb answers the whole line by itself, so neither half may
        # apply what is beside it, and a press beside it is refused rather than
        # dropped. Only main.py's own ordering keeps the boot path from seeing
        # these lines in the field, and nothing pins that ordering, so both
        # halves answer them the same way here.
        ("a press beside a listing, an instance running",
         [*EMULATE_ARGV, "--list-pages"], True, False),
        ("a press beside a listing, nothing running",
         [*EMULATE_ARGV, "--list-devices"], False, False),
        ("a page change beside a listing, an instance running",
         [*FORWARD_ARGV, "--list-pages"], True, False),
        ("a malformed press beside a listing",
         ["--emulate-input", "deck-a", "Alpha", "0,0", "smash", "--list-pages"],
         True, False),
        # The read-side and page verbs. Neither half parks or boots them, and
        # both read the same situation the same way.
        ("a state dump, an instance running", ["--json"], True, False),
        ("a state dump, nothing running", ["--json"], False, False),
        ("a brightness read, an instance running",
         ["--get-brightness", "deck-a"], True, False),
        ("a brightness read, nothing running",
         ["--get-brightness", "deck-a"], False, False),
        ("a sleep, an instance running", ["--sleep", "deck-a"], True, False),
        ("a page rename, an instance running",
         ["--rename-page", "Main", "Home"], True, False),
        ("a page duplicate, nothing running",
         ["--duplicate-page", "Main", "Home"], False, False),
        ("a malformed brightness",
         ["--set-brightness", "deck-a", "nope"], True, False),
        ("actions on a page, an instance running",
         ["--list-actions", "Main", "0,0"], True, False),
    ]

    for what, argv, running, close_running in lines:
        line = [*argv, "--close-running"] if close_running else argv
        gl.api_page_requests.clear()
        gl.api_state_requests.clear()

        fast_recorder = Recorder(running=running)
        boot_recorder = Recorder(running=running)
        fast = cli_fast_path.answer_from_running_instance(parse(line), fast_recorder)
        boot = cli_forward.forward_cli_requests(parse(line), boot_recorder)

        assert tuple(boot.failures) == fast.failures, (
            f"{what}: the fast path says {fast.failures} and the boot path says "
            f"{tuple(boot.failures)} for the same command line")
        assert tuple(boot.output) == fast.output, (
            f"{what}: the fast path prints {fast.output} and the boot path "
            f"prints {tuple(boot.output)} for the same command line")
        assert boot_recorder.forwards() == fast_recorder.forwards(), (
            f"{what}: the fast path sent {fast_recorder.forwards()} and the "
            f"boot path sent {boot_recorder.forwards()}")
        assert not gl.api_page_requests and not gl.api_state_requests, (
            f"{what}: a refused line parked {gl.api_page_requests} / "
            f"{gl.api_state_requests}")

    # A listing beside a request the boot could apply is still just a listing:
    # nothing is sent, nothing is parked, and the process goes on to print it.
    gl.api_page_requests.clear()
    gl.api_state_requests.clear()
    recorder = Recorder(running=False)
    boot = cli_forward.forward_cli_requests(
        parse([*FORWARD_ARGV, "--list-pages"]), recorder)
    assert not boot.handled and boot.failures == [], boot
    assert recorder.forwards() == [], recorder.forwards()
    assert not gl.api_page_requests and not gl.api_state_requests, (
        f"a listing line parked {gl.api_page_requests} / {gl.api_state_requests}")

    print("  PASS: both halves give one answer per command line")


# 2. The import fence

_FENCE_CHILD = r'''
import sys

# Every root the fast path exists to avoid. globals resolves the data path and
# creates the directory at import time, gi is the toolkit, cv2 is the first of
# the expensive imports, and the rest arrive behind them.
BANNED = ("globals", "gi", "cv2", "loguru", "PIL", "StreamDeck", "dasbus",
          "numpy", "setproctitle", "usbmonitor")
# What the module body may reach beyond the standard library. A root added
# here is a decision to make every forwarded call pay for it.
ALLOWED = {"src", "appinfo"}

before = set(sys.modules)
from src.backend import cli_fast_path


def report(stage):
    print("BANNED %s %s" % (stage, ",".join(n for n in BANNED if n in sys.modules)))
    roots = {name.split(".")[0] for name in set(sys.modules) - before}
    print("EXTRA %s %s" % (stage, ",".join(sorted(
        r for r in roots if r not in sys.stdlib_module_names and r not in ALLOWED))))


report("import")

import argparse


class Transport:
    """A running instance with no bus behind it."""

    def __init__(self):
        self.calls = []

    def is_running(self):
        return True

    def change_page(self, serial, page):
        self.calls.append((serial, page))
        return ""

    def change_state(self, serial, page, coords, state):
        self.calls.append((serial, page, coords, state))
        return ""

    def emulate_input(self, serial, page, coords, event):
        self.calls.append((serial, page, coords, event))
        return ""


# Every argv attribute the decision reads, written out. A flag added to the
# path and not to this list fails here rather than at a person's first launch.
args = argparse.Namespace(change_page=[["deck", "Page"]], change_state=None,
                          emulate_input=None, close_running=False,
                          list_devices=False, list_pages=False,
                          json=False, get_brightness=None, set_brightness=None,
                          sleep=None, wake=None, list_actions=None,
                          rename_page=None, duplicate_page=None)
transport = Transport()
outcome = cli_fast_path.answer_from_running_instance(args, transport)
print("DECIDED %r %r" % (outcome.exit_code, transport.calls))
report("decide")
'''


def leg_import_fence() -> None:
    print("leg 2: the module and its decision reach nothing expensive")
    proc = subprocess.run([sys.executable, "-c", _FENCE_CHILD], cwd=_REPO_ROOT,
                          capture_output=True, text=True, timeout=60)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"the fence child failed:\n{out}"

    lines = out.splitlines()
    for stage in ("import", "decide"):
        banned = next((ln for ln in lines if ln.startswith(f"BANNED {stage} ")), None)
        assert banned is not None, f"the child printed no BANNED line for {stage}:\n{out}"
        names = banned.split(" ", 2)[2]
        assert names == "", (
            f"after the {stage} stage the fast path had imported {names}. That "
            f"is the cost this module exists to skip, and every forwarded call "
            f"now pays it.\n{out}")

        extra = next((ln for ln in lines if ln.startswith(f"EXTRA {stage} ")), None)
        assert extra is not None, f"the child printed no EXTRA line for {stage}:\n{out}"
        roots = extra.split(" ", 2)[2]
        assert roots == "", (
            f"after the {stage} stage the fast path had imported the "
            f"non-standard-library roots {roots}. Widen ALLOWED in this "
            f"scenario only for something a forwarded call must have.\n{out}")

    decided = next((ln for ln in lines if ln.startswith("DECIDED ")), None)
    assert decided == "DECIDED 0 [('deck', 'Page')]", (
        f"the fenced decision did not forward the request: {decided!r}\n{out}")
    print("  PASS: the module body and the decision stay standard library, "
          "appinfo and src")


# 3. The real entry point

def make_cv2_sentinel(where: str) -> str:
    """A cv2 that ends the process, so an exit code says whether main.py
    reached its expensive imports. It shadows the real one through PYTHONPATH,
    which the re-exec guard in main.py carries across."""
    os.makedirs(where, exist_ok=True)
    with open(os.path.join(where, "cv2.py"), "w") as f:
        f.write("import sys\n\nsys.exit(%d)\n" % HEAVY_IMPORT_EXIT)
    return where


def make_gi_fault_injector(where: str) -> str:
    """A gi that builds a transport and then fails while it packs a call.

    The two arguments leg 3d covers used to raise exactly this way, out of
    GLib.Variant, in main.py's module body. They are refused before the
    transport sees them now, so this stub stands for whatever else may one day
    raise there and pins that the call site answers it with a sentence.

    It shadows the real gi through PYTHONPATH, which beats site-packages.
    Nothing before the fast path imports gi, so this reaches the transport and
    nothing else.
    """
    package = os.path.join(where, "gi", "repository")
    os.makedirs(package, exist_ok=True)
    with open(os.path.join(where, "gi", "__init__.py"), "w") as f:
        f.write("def require_version(*a, **k):\n    pass\n")
    with open(os.path.join(package, "__init__.py"), "w") as f:
        f.write(
            "class Error(Exception):\n"
            "    pass\n"
            "\n"
            "\n"
            "class GLib:\n"
            "    Error = Error\n"
            "\n"
            "    @staticmethod\n"
            "    def Variant(signature, values):\n"
            "        raise OverflowError('stub gi: not in range for ' + signature)\n"
            "\n"
            "    @staticmethod\n"
            "    def VariantType(signature):\n"
            "        return signature\n"
            "\n"
            "\n"
            "class _Connection:\n"
            "    def call_sync(self, *a, **k):\n"
            "        raise AssertionError('the stub never gets this far')\n"
            "\n"
            "\n"
            "class Gio:\n"
            "    class BusType:\n"
            "        SESSION = 0\n"
            "\n"
            "    class DBusCallFlags:\n"
            "        NO_AUTO_START = 0\n"
            "\n"
            "    @staticmethod\n"
            "    def bus_get_sync(bus_type, cancellable):\n"
            "        return _Connection()\n")
    return where


def reset_record(path: str) -> None:
    """Empty the stand-in instance's record, whether or not it wrote one."""
    if os.path.isfile(path):
        os.remove(path)


def start_stub_instance(record_path: str, refuse: str = "",
                        query_json: str = "") -> subprocess.Popen:
    env = dict(os.environ)
    env["DECKARD_STUB_APP_ID"] = appinfo.APP_ID
    env["DECKARD_STUB_RECORD"] = record_path
    env["DECKARD_STUB_REFUSE"] = refuse
    if query_json:
        env["DECKARD_STUB_QUERY_JSON"] = query_json
    proc = subprocess.Popen([sys.executable, STUB_INSTANCE], stdout=subprocess.PIPE,
                            text=True, env=env, preexec_fn=_die_with_parent)
    ready = (proc.stdout.readline() or "").strip()
    assert ready == "READY", (
        f"the stand-in instance never took the application name: {ready!r}")
    return proc


def stop_stub_instance(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    if proc.stdout is not None:
        proc.stdout.close()


def run_main(argv: list[str], shadow_path: str) -> tuple[subprocess.CompletedProcess, float]:
    env = dict(os.environ)
    # What scripts/Deckard exports, so main.py's startup guard skips its
    # self re-exec. That is the shape a user's forwarded call has.
    env["MALLOC_ARENA_MAX"] = "2"
    env["MALLOC_TRIM_THRESHOLD_"] = "131072"
    env["PYTHONPATH"] = os.pathsep.join(
        [shadow_path, env["PYTHONPATH"]] if env.get("PYTHONPATH") else [shadow_path])
    started = time.monotonic()
    proc = subprocess.run([sys.executable, MAIN, *argv], cwd=_REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=60)
    return proc, time.monotonic() - started


def read_record(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def leg_entry_point() -> None:
    print("leg 3: main.py against a stand-in instance on a private bus")
    sentinel = make_cv2_sentinel(os.path.join(gl.DATA_PATH, "cv2-sentinel"))
    record = os.path.join(gl.DATA_PATH, "stub-instance-calls.jsonl")
    # Not read by anything here. It is passed so the rename migration in
    # main.py's body takes its --data override and returns at once, and so a
    # run that got past the sentinel would touch this tree and not the user's.
    # It proves nothing on its own: the sentinel ends the process before
    # globals.py could create it either way.
    scratch_data = os.path.join(gl.DATA_PATH, "entry-point-data")

    argv = ["--data", scratch_data,
            "--change-page", SERIAL, "Alpha",
            "--change-state", SERIAL, "Beta", "1,2", "3"]

    # 3a. An instance is running: the requests go over the bus and the process
    # leaves without importing anything expensive.
    stub = start_stub_instance(record)
    try:
        proc, elapsed = run_main(argv, sentinel)
    finally:
        stop_stub_instance(stub)

    assert proc.returncode != HEAVY_IMPORT_EXIT, (
        f"a forwarded call imported cv2, which means it built the whole "
        f"application to send two bus messages:\n{proc.stdout}{proc.stderr}")
    assert proc.returncode == 0, (
        f"the forward failed with {proc.returncode}:\n{proc.stdout}{proc.stderr}")
    assert read_record(record) == [
        {"method": "ChangePage", "args": [SERIAL, "Alpha"]},
        {"method": "ChangeState", "args": [SERIAL, "Beta", "1,2", 3]},
    ], f"the instance was sent {read_record(record)}"
    print(f"  the forwarded call took {elapsed * 1000:.0f} ms end to end")

    # 3b. Nothing owns the name. The invocation must fall through to the
    # ordinary startup path, which parks its requests and boots.
    reset_record(record)
    proc, cold_elapsed = run_main(argv, sentinel)
    assert proc.returncode == HEAVY_IMPORT_EXIT, (
        f"with nothing running the invocation must boot, and it exited "
        f"{proc.returncode} instead:\n{proc.stdout}{proc.stderr}")
    assert read_record(record) == [], (
        f"nothing owns the name, so nothing may be sent: {read_record(record)}")
    print(f"  the cold call reached the expensive imports in "
          f"{cold_elapsed * 1000:.0f} ms")

    # 3c. The instance refuses. The command failed, and it still must not
    # boot a second instance on the way out.
    refusal = f"There is no deck with the serial number {SERIAL}"
    stub = start_stub_instance(record, refuse=refusal)
    try:
        proc, _ = run_main(argv, sentinel)
    finally:
        stop_stub_instance(stub)

    assert proc.returncode == 1, (
        f"a refused command must exit 1, not {proc.returncode}:"
        f"\n{proc.stdout}{proc.stderr}")
    assert refusal in proc.stderr, (
        f"the instance's own sentence never reached the person who typed the "
        f"command:\n{proc.stdout}{proc.stderr}")

    # 3d. The two typed arguments that used to raise out of the transport, in
    # the module body no exception hook covers. Each must reach a person as a
    # sentence with a non-zero exit, never as a traceback, and never as the
    # exit code of zero mainline gave them.
    stub = start_stub_instance(record)
    try:
        for what, bad_argv in (
            ("a state number wider than the wire's integer",
             ["--change-state", SERIAL, "Alpha", "0,0",
              str(cli_forward.MAX_STATE_NUMBER + 1)]),
            ("a page name argv carried as non-UTF-8 bytes",
             ["--change-page", SERIAL, NOT_UTF8]),
        ):
            reset_record(record)
            proc, _ = run_main(["--data", scratch_data, *bad_argv], sentinel)
            output = proc.stdout + proc.stderr
            assert proc.returncode == 1, (
                f"{what} must exit 1, not {proc.returncode}:\n{output}")
            assert "Traceback" not in output, (
                f"{what} reached a person as a traceback:\n{output}")
            assert "Error:" in proc.stderr, (
                f"{what} produced no sentence:\n{output}")
            assert read_record(record) == [], (
                f"{what} was sent to the instance anyway: {read_record(record)}")
    finally:
        stop_stub_instance(stub)

    # 3e. The backstop in main.py's body. gi is shadowed by a stub that raises
    # a plain OverflowError from GLib.Variant, which is the shape the two
    # arguments above had before they were checked, and stands for whatever
    # else may one day reach it. Nothing here is reachable with the real
    # toolkit; the point is that the call site turns anything into a sentence.
    fault = make_gi_fault_injector(os.path.join(gl.DATA_PATH, "gi-fault"))
    stub = start_stub_instance(record)
    try:
        reset_record(record)
        proc, _ = run_main(argv, os.pathsep.join([fault, sentinel]))
    finally:
        stop_stub_instance(stub)

    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, (
        f"an unforeseen failure in the fast path must exit 1, not "
        f"{proc.returncode}:\n{output}")
    assert "Traceback" not in output, (
        f"an unforeseen failure reached a person as a traceback:\n{output}")
    assert "could not carry out that command" in proc.stderr, (
        f"the call site printed no sentence for it:\n{output}")

    # 3f. The verb that cannot be parked, through the real entry point. With an
    # instance it forwards like the rest. With none, the invocation has to end
    # here: the fall-through would boot an application that presses nothing and
    # exits zero, and the expensive imports are where that shows.
    press_argv = ["--data", scratch_data,
                  "--emulate-input", SERIAL, "Alpha", "0,0", "press"]

    reset_record(record)
    stub = start_stub_instance(record)
    try:
        proc, _ = run_main(press_argv, sentinel)
    finally:
        stop_stub_instance(stub)

    assert proc.returncode == 0, (
        f"the forwarded press failed with {proc.returncode}:"
        f"\n{proc.stdout}{proc.stderr}")
    assert read_record(record) == [
        {"method": "EmulateInput", "args": [SERIAL, "Alpha", "0,0", "press"]},
    ], f"the instance was sent {read_record(record)}"

    # The instance refuses the press: the key is held, the page moved on, the
    # deck is not there. That sentence is the whole answer a person gets, and
    # dropped anywhere on the way it becomes a successful exit that printed
    # nothing.
    press_refusal = "Position (0,0) on device fastpath-deck-1 is already held down"
    reset_record(record)
    stub = start_stub_instance(record, refuse=press_refusal)
    try:
        proc, _ = run_main(press_argv, sentinel)
    finally:
        stop_stub_instance(stub)

    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, (
        f"a refused press must exit 1, not {proc.returncode}:\n{output}")
    assert press_refusal in proc.stderr, (
        f"the instance's own sentence about the press never reached the person "
        f"who typed the command:\n{output}")

    reset_record(record)
    proc, _ = run_main(press_argv, sentinel)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, (
        f"with nothing running a press must end the invocation with a reason, "
        f"and it exited {proc.returncode} instead:\n{output}")
    assert proc.returncode != HEAVY_IMPORT_EXIT, (
        f"the invocation booted the application to press a deck it has not "
        f"opened yet:\n{output}")
    assert "not running" in proc.stderr, (
        f"the refusal reached the person who typed the command without its "
        f"reason:\n{output}")
    assert read_record(record) == [], (
        f"nothing owns the name, so nothing may be sent: {read_record(record)}")

    print("  PASS: the entry point forwards, falls through, reports and never "
          "leaks a traceback")


def leg_read_verbs() -> None:
    """The read verbs through the real entry point, against a stand-in.

    --json and --get-brightness print the instance's answer to stdout and end
    without importing the application. With nothing running they refuse, and
    must not boot: a read has nothing to park, so a fall-through would build the
    whole app to answer one line.
    """
    print("leg 5: the read verbs through the real entry point")
    sentinel = make_cv2_sentinel(os.path.join(gl.DATA_PATH, "cv2-sentinel"))
    record = os.path.join(gl.DATA_PATH, "read-verb-calls.jsonl")
    scratch_data = os.path.join(gl.DATA_PATH, "read-verb-data")
    dump = ('{"decks": [{"serial": "%s", "active_page": "Main", '
            '"brightness": 55}], "pages": ["Main"]}') % SERIAL

    # --json prints the dump the instance gave, and imports nothing expensive.
    stub = start_stub_instance(record, query_json=dump)
    try:
        proc, _ = run_main(["--data", scratch_data, "--json"], sentinel)
    finally:
        stop_stub_instance(stub)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"--json failed with {proc.returncode}:\n{output}"
    assert proc.returncode != HEAVY_IMPORT_EXIT, (
        f"--json built the application to read one line:\n{output}")
    assert dump in proc.stdout, f"the dump never reached stdout:\n{proc.stdout!r}"
    assert read_record(record) == [{"method": "QueryState", "args": []}], (
        f"the instance was sent {read_record(record)}")

    # --get-brightness reads one deck's value out of the same dump. It calls
    # QueryState too, because the value lives in that one answer.
    reset_record(record)
    stub = start_stub_instance(record, query_json=dump)
    try:
        proc, _ = run_main(["--data", scratch_data, "--get-brightness", SERIAL], sentinel)
    finally:
        stop_stub_instance(stub)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"--get-brightness failed with {proc.returncode}:\n{output}"
    assert "55" in proc.stdout.splitlines(), f"the brightness was not printed:\n{proc.stdout!r}"
    assert read_record(record) == [{"method": "QueryState", "args": []}], (
        f"the instance was sent {read_record(record)}")

    # Nothing running. The read verb refuses with a reason and must not boot.
    reset_record(record)
    proc, _ = run_main(["--data", scratch_data, "--json"], sentinel)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, (
        f"a read verb with nothing running must exit 1, not {proc.returncode}:\n{output}")
    assert proc.returncode != HEAVY_IMPORT_EXIT, (
        f"a read verb booted the application to answer it:\n{output}")
    assert "not running" in proc.stderr, output
    assert read_record(record) == [], (
        f"nothing owns the name, so nothing may be sent: {read_record(record)}")

    print("  PASS: the read verbs print the instance's answer without building the app")


def main() -> int:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_cli_fast_path")

    leg_decision_table()
    leg_press_needs_a_running_instance()
    leg_instance_verbs()
    leg_both_halves_answer_alike()
    leg_import_fence()

    bus_proc, bus_address = start_private_bus()
    try:
        assert os.environ.get("DBUS_SESSION_BUS_ADDRESS") == bus_address, (
            "the private bus address is not in the environment the children "
            "inherit")
        assert bus_address != ORIGINAL_BUS, (
            "this scenario owns the app's real bus name, so it must never run "
            "on the developer's own session bus")
        leg_entry_point()
        leg_read_verbs()
    finally:
        stop_private_bus(bus_proc)

    print("ALL PASS: scenario_cli_fast_path")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
