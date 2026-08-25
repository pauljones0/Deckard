"""Pins the CLI half of the control plane in src/backend/cli_forward.py.

Every page and state request is forwarded or parked, and every emulated input
is forwarded or refused, because a press cannot wait for a deck. Validation is
syntax only and all-or-nothing, and a failure never stops the requests behind
it.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import globals as gl  # noqa: E402

from cli_args import argparser  # noqa: E402

from src.backend import cli_forward  # noqa: E402

WATCHDOG_SECONDS = 60

# One command carrying three page requests and two state requests. Every one
# names a different deck, so a dropped request cannot hide behind
# last-write-wins parking.
ARGV = [
    "--change-page", "deck-a", "Alpha",
    "--change-page", "deck-b", "Beta",
    "--change-page", "deck-c", "Gamma",
    "--change-state", "deck-d", "Delta", "0,0", "1",
    "--change-state", "deck-e", "Epsilon", "1,2", "3",
]

EXPECTED_FORWARDS = [
    ("page", "deck-a", "Alpha"),
    ("page", "deck-b", "Beta"),
    ("page", "deck-c", "Gamma"),
    ("state", "deck-d", "Delta", "0,0", 1),
    ("state", "deck-e", "Epsilon", "1,2", 3),
]


def parse(argv: list[str]):
    """The app's own parser, so what is tested below is what argv produces."""
    return argparser.parse_args(argv)


def clear_parking() -> None:
    gl.api_page_requests.clear()
    gl.api_state_requests.clear()


class Recorder:
    """The running instance, without a bus.

    Records every call the forwarder makes, in order. It answers the way the
    real methods do, with an empty string on success and a sentence on failure.
    """

    def __init__(self, running: bool = True, answers: dict | None = None,
                 raises: Exception | None = None):
        self._running = running
        self._answers = answers or {}
        self._raises = raises
        self.calls: list[tuple] = []

    def is_running(self) -> bool:
        self.calls.append(("is_running",))
        return self._running

    def change_page(self, serial: str, page: str) -> str:
        self.calls.append(("page", serial, page))
        return self._answer(serial)

    def change_state(self, serial: str, page: str, coords: str, state: int) -> str:
        self.calls.append(("state", serial, page, coords, state))
        return self._answer(serial)

    def emulate_input(self, serial: str, page: str, coords: str, event: str) -> str:
        self.calls.append(("emulate", serial, page, coords, event))
        return self._answer(serial)

    def _answer(self, serial: str) -> str:
        if self._raises is not None:
            raise self._raises
        return self._answers.get(serial, "")

    def forwards(self) -> list[tuple]:
        return [call for call in self.calls if call[0] != "is_running"]


# 1. Every request is forwarded

def check_all_requests_are_forwarded() -> None:
    clear_parking()
    recorder = Recorder(running=True)

    verdict = cli_forward.forward_cli_requests(parse(ARGV), recorder)

    assert recorder.forwards() == EXPECTED_FORWARDS, (
        f"the running instance was sent {recorder.forwards()} instead of "
        f"{EXPECTED_FORWARDS} -- a command's later requests are being dropped, "
        f"which is what returning after the first forwarded request did")
    assert verdict.handled, (
        "the instance took the requests, so this invocation is finished -- "
        "returning otherwise boots a second instance")
    assert verdict.failures == [], verdict.failures
    assert not gl.api_page_requests and not gl.api_state_requests, (
        f"forwarded requests must not also be parked: {gl.api_page_requests} / "
        f"{gl.api_state_requests}")

    # The state travelled as an integer. The bus method signature says so, and
    # the conversion is the job of the CLI edge.
    for call in recorder.forwards():
        if call[0] == "state":
            assert isinstance(call[4], int) and not isinstance(call[4], bool), (
                f"the state reached the transport as {call[4]!r}, not an int")

    print("PASS: every page and state request on the command line is forwarded")


# 2 and 3. Parking

def check_nothing_running_parks_everything() -> None:
    clear_parking()
    recorder = Recorder(running=False)

    verdict = cli_forward.forward_cli_requests(parse(ARGV), recorder)

    assert not verdict.handled, (
        "with nothing running this invocation becomes the instance, so it must "
        "carry on booting")
    assert verdict.failures == [], verdict.failures
    assert recorder.forwards() == [], (
        f"nothing may be forwarded when nothing is running: {recorder.forwards()}")

    assert gl.api_page_requests == {
        "deck-a": "Alpha", "deck-b": "Beta", "deck-c": "Gamma",
    }, gl.api_page_requests
    # The exact dict the boot path reads back, with state already an int.
    assert gl.api_state_requests == {
        "deck-d": {"page_name": "Delta", "coords": "0,0", "state": 1},
        "deck-e": {"page_name": "Epsilon", "coords": "1,2", "state": 3},
    }, gl.api_state_requests

    print("PASS: with no instance running every request is parked for the boot")


def check_close_running_parks_requests() -> None:
    clear_parking()
    recorder = Recorder(running=True)

    verdict = cli_forward.forward_cli_requests(
        parse(ARGV + ["--close-running"]), recorder)

    assert not verdict.handled, (
        "--close-running boots over the instance it stops, so its requests are "
        "this process's to apply")
    assert recorder.forwards() == [], (
        f"--close-running must not hand requests to the instance it is about "
        f"to shut down: {recorder.forwards()}")
    assert len(gl.api_page_requests) == 3 and len(gl.api_state_requests) == 2, (
        f"{gl.api_page_requests} / {gl.api_state_requests}")

    print("PASS: --close-running parks its requests even with an instance running")


# 4 and 5. What the instance says comes back

def check_failures_surface_without_stopping() -> None:
    clear_parking()
    refusal = "Page 'Beta' not found. Available pages: Alpha, Gamma"
    unknown_deck = "StreamDeck with serial 'deck-d' not found. Available devices: deck-a"
    recorder = Recorder(running=True,
                        answers={"deck-b": refusal, "deck-d": unknown_deck})

    verdict = cli_forward.forward_cli_requests(parse(ARGV), recorder)

    assert verdict.failures == [refusal, unknown_deck], (
        f"the instance's own sentences are what the person who typed the "
        f"command has to see: {verdict.failures}")
    assert recorder.forwards() == EXPECTED_FORWARDS, (
        f"a request the instance refused must not stop the ones behind it: "
        f"{recorder.forwards()}")

    print("PASS: refusals come back in the verdict without stopping the command")


def check_older_instance_reports_once() -> None:
    clear_parking()
    recorder = Recorder(running=True, raises=cli_forward.OlderInstance())

    verdict = cli_forward.forward_cli_requests(parse(ARGV), recorder)

    assert verdict.failures == [cli_forward.SKEW_MESSAGE], verdict.failures
    assert "restart" in cli_forward.SKEW_MESSAGE, (
        "the message has to say what to do about it")
    assert "older build" in cli_forward.SKEW_MESSAGE, cli_forward.SKEW_MESSAGE
    for verb in ("page and state", "--change-page", "--change-state",
                 "--emulate-input"):
        assert verb not in cli_forward.SKEW_MESSAGE, (
            f"the message names {verb!r}, and any of the control methods can "
            f"be the one that meets an older instance: {cli_forward.SKEW_MESSAGE!r}")
    assert "shutting down" in cli_forward.SKEW_MESSAGE, (
        "an instance takes its interface off the bus before it lets go of the "
        "name, so it answers identically to one too old to have the methods -- "
        "and that is now the only other way to reach this")
    assert "finished starting" not in cli_forward.SKEW_MESSAGE, (
        "a starting instance publishes its objects BEFORE it takes the name, "
        "so it cannot answer this way at all any more; telling people to wait "
        "for a startup to finish would send them to wait for nothing")
    assert len(recorder.forwards()) == 1, (
        f"every request would fail identically, so the rest are pointless: "
        f"{recorder.forwards()}")
    assert verdict.handled, (
        "an instance IS running -- booting a second one over it would be worse "
        "than the failure being reported")
    assert not gl.api_page_requests and not gl.api_state_requests, (
        "a version-skewed forward must not silently fall back to parking, "
        "which would apply the requests to a DIFFERENT process later")

    print("PASS: an instance without the methods is reported once, and clearly")


def check_broken_conversation_reported() -> None:
    """A wedged instance, a dropped connection or a refused call reads back.

    The bus text must reach the terminal. Left as the GLib error it starts as,
    it escapes the CLI, prints a traceback and still exits zero.
    """
    clear_parking()
    broke = cli_forward.TransportError(
        "GDBus.Error:org.freedesktop.DBus.Error.NoReply: Message did not "
        "receive a reply (timeout by message bus)")
    recorder = Recorder(running=True, raises=broke)

    verdict = cli_forward.forward_cli_requests(parse(ARGV), recorder)

    assert verdict.failures == [str(broke)], (
        f"a broken conversation has to come back as something printable: "
        f"{verdict.failures}")
    assert verdict.handled, (
        "an instance is running -- booting a second one over a bus hiccup "
        "would be worse than the failure being reported")
    assert len(recorder.forwards()) == 1, (
        f"the remaining requests would each spend the call timeout again "
        f"against something that is not answering: {recorder.forwards()}")
    assert not gl.api_page_requests and not gl.api_state_requests, (
        "a failed forward must not silently fall back to parking, which would "
        "apply the requests to a DIFFERENT process later")

    print("PASS: a failed conversation with the instance is reported, not raised")


# 6 and 7. Syntax only

def check_validation_is_syntax_only() -> None:
    bad = [
        ["--change-state", "deck-a", "Alpha", "nope", "1"],       # no comma
        ["--change-state", "deck-a", "Alpha", "1,2,3", "1"],      # three of them
        ["--change-state", "deck-a", "Alpha", "", "1"],           # nothing at all
        ["--change-state", "deck-a", "Alpha", "x,y", "1"],        # not numbers
        ["--change-state", "deck-a", "Alpha", "-1,0", "1"],       # before the first key
        ["--change-state", "deck-a", "Alpha", "0,0", "one"],      # not a number
        ["--change-state", "deck-a", "Alpha", "0,0", "-1"],       # before the first state
        ["--change-state", "", "Alpha", "0,0", "1"],              # no deck named
        ["--change-state", "deck-a", "", "0,0", "1"],             # no page named
    ]
    for argv in bad:
        clear_parking()
        recorder = Recorder(running=True)
        verdict = cli_forward.forward_cli_requests(parse(argv), recorder)
        assert verdict.failures, f"{argv} was accepted"
        assert verdict.failures[0].startswith("Error: "), verdict.failures
        assert cli_forward.USAGE in verdict.failures, (
            f"a malformed command has to be shown the shape of a good one: "
            f"{verdict.failures}")
        assert not verdict.handled, argv
        assert recorder.calls == [], (
            f"{argv} reached the bus before it was read: {recorder.calls}")
        assert not gl.api_page_requests and not gl.api_state_requests, (
            f"{argv} parked something despite being rejected")

    # All-or-nothing. One malformed group leaves the whole command unapplied,
    # because a partly applied command is the worst of the three outcomes.
    clear_parking()
    recorder = Recorder(running=True)
    verdict = cli_forward.forward_cli_requests(
        parse(["--change-page", "deck-a", "Alpha",
               "--change-state", "deck-b", "Beta", "0,0", "1",
               "--change-state", "deck-c", "Gamma", "nope", "1"]), recorder)
    assert verdict.failures and recorder.calls == [], recorder.calls
    assert not gl.api_page_requests and not gl.api_state_requests, (
        f"a command with one bad group applied part of itself: "
        f"{gl.api_page_requests} / {gl.api_state_requests}")

    print("PASS: syntax failures reject the whole command before anything happens")


def check_large_decks_not_pre_rejected() -> None:
    """Large coordinates and state numbers travel to the instance untouched.

    A cap of x,y <= 10 and state <= 20 matches no device. It rejects valid
    requests for large decks before anything that knows a deck sees them.
    """
    argv = ["--change-state", "deck-a", "Alpha", "9,9", "19",
            "--change-state", "deck-b", "Beta", "14,7", "31"]

    clear_parking()
    recorder = Recorder(running=True)
    verdict = cli_forward.forward_cli_requests(parse(argv), recorder)
    assert verdict.failures == [], verdict.failures
    assert recorder.forwards() == [
        ("state", "deck-a", "Alpha", "9,9", 19),
        ("state", "deck-b", "Beta", "14,7", 31),
    ], recorder.forwards()

    # The same on the parking path, so no boot-time request is judged by
    # numbers the CLI made up.
    clear_parking()
    verdict = cli_forward.forward_cli_requests(parse(argv), Recorder(running=False))
    assert verdict.failures == [], verdict.failures
    assert gl.api_state_requests["deck-a"]["state"] == 19, gl.api_state_requests
    assert gl.api_state_requests["deck-b"]["coords"] == "14,7", gl.api_state_requests

    print("PASS: coordinates and states the device decides on are passed through")


# The emulated inputs, which forward like the rest and park unlike it

EMULATE_ARGV = [
    "--emulate-input", "deck-f", "Zeta", "0,0", "press",
    "--emulate-input", "deck-g", "Eta", "1,2", "long-press",
]

EXPECTED_EMULATE_FORWARDS = [
    ("emulate", "deck-f", "Zeta", "0,0", "press"),
    ("emulate", "deck-g", "Eta", "1,2", "long-press"),
]


def check_emulate_requests_are_forwarded() -> None:
    """Every press goes to the instance, after the changes on the same line.

    A command that sets a state and then presses that input has to press the
    state it just set, which is what the order of the sends decides.
    """
    clear_parking()
    recorder = Recorder(running=True)

    verdict = cli_forward.forward_cli_requests(parse(ARGV + EMULATE_ARGV), recorder)

    assert verdict.handled and verdict.failures == [], verdict
    assert recorder.forwards() == EXPECTED_FORWARDS + EXPECTED_EMULATE_FORWARDS, (
        f"the instance was sent {recorder.forwards()} instead of the page and "
        f"state requests followed by the presses")
    assert not gl.api_page_requests and not gl.api_state_requests, (
        f"forwarded requests must not also be parked: {gl.api_page_requests} / "
        f"{gl.api_state_requests}")

    print("PASS: every emulated input is forwarded, after the page and state requests")


def check_emulate_refusal_comes_back() -> None:
    """What the instance says about a press is what the terminal shows.

    A press the instance refuses, because the deck moved on or the key is held
    or the serial is not there, has to reach the person who typed the command.
    Dropped here it exits zero with nothing printed, which reads as pressed.
    """
    clear_parking()
    refusal = "Position (0,0) on device deck-f is already held down"
    recorder = Recorder(running=True, answers={"deck-f": refusal})

    verdict = cli_forward.forward_cli_requests(parse(EMULATE_ARGV), recorder)

    assert verdict.failures == [refusal], (
        f"the instance's own sentence about the press never came back: "
        f"{verdict.failures}")
    assert verdict.handled, (
        "an instance is running and took the command, refusal and all")
    assert recorder.forwards() == EXPECTED_EMULATE_FORWARDS, (
        f"a refused press must not stop the one behind it: {recorder.forwards()}")

    print("PASS: a press the instance refuses comes back as its own sentence")


def check_emulate_cannot_be_parked() -> None:
    """With nothing running, a press ends the command instead of parking.

    Parking exists so an invocation that finds nothing running can boot and
    apply its requests to the decks it opens. A press has no such meaning: it
    would fire at whatever moment the deck turned up. So the whole command is
    refused, and nothing on it is applied, because a command applies all of
    itself or none of it.
    """
    clear_parking()
    recorder = Recorder(running=False)

    verdict = cli_forward.forward_cli_requests(parse(EMULATE_ARGV), recorder)

    assert not verdict.handled, verdict
    assert verdict.failures == [cli_forward.NOT_RUNNING_MESSAGE], verdict.failures
    assert "not running" in cli_forward.NOT_RUNNING_MESSAGE, cli_forward.NOT_RUNNING_MESSAGE
    assert "Start Deckard" in cli_forward.NOT_RUNNING_MESSAGE, (
        "the message has to say what to do about it")
    assert recorder.forwards() == [], (
        f"nothing is running, so nothing may be sent: {recorder.forwards()}")
    assert not gl.api_page_requests and not gl.api_state_requests, (
        f"a press must not be parked, and must not drag the rest of its "
        f"command into the parking either: {gl.api_page_requests} / "
        f"{gl.api_state_requests}")

    # The same command with a parkable request beside the press. The press is
    # what the boot cannot carry out, so the page change parks nothing either.
    clear_parking()
    verdict = cli_forward.forward_cli_requests(
        parse(["--change-page", "deck-a", "Alpha", *EMULATE_ARGV]), Recorder(running=False))
    assert verdict.failures == [cli_forward.NOT_RUNNING_MESSAGE], verdict.failures
    assert not gl.api_page_requests and not gl.api_state_requests, (
        f"a refused command applied part of itself: {gl.api_page_requests} / "
        f"{gl.api_state_requests}")

    print("PASS: with nothing running a press ends the command and parks nothing")


def check_emulate_refuses_close_running() -> None:
    """--close-running is the same case, and says so in its own words.

    That launch stops the instance and becomes it, so its requests belong to
    the decks it opens next. A press cannot wait for those either.
    """
    clear_parking()
    recorder = Recorder(running=True)

    verdict = cli_forward.forward_cli_requests(
        parse([*EMULATE_ARGV, "--close-running"]), recorder)

    assert not verdict.handled, verdict
    assert verdict.failures == [cli_forward.CLOSE_RUNNING_MESSAGE], verdict.failures
    assert "--close-running" in cli_forward.CLOSE_RUNNING_MESSAGE, (
        cli_forward.CLOSE_RUNNING_MESSAGE)
    assert recorder.forwards() == [], (
        f"the instance is about to be stopped, so nothing may be sent to it: "
        f"{recorder.forwards()}")
    assert not gl.api_page_requests and not gl.api_state_requests

    # The same answer with nothing running at all. That launch opens the decks
    # either way, so the situation does not depend on the probe, and the fast
    # path, which has no probe at that point, gives this same sentence.
    clear_parking()
    verdict = cli_forward.forward_cli_requests(
        parse([*EMULATE_ARGV, "--close-running"]), Recorder(running=False))
    assert verdict.failures == [cli_forward.CLOSE_RUNNING_MESSAGE], verdict.failures
    assert not gl.api_page_requests and not gl.api_state_requests

    print("PASS: --close-running refuses a press, and names its own reason")


def check_emulate_validation_is_syntax_only() -> None:
    bad = [
        ["--emulate-input", "deck-a", "Alpha", "0,0", "smash"],   # no such event
        ["--emulate-input", "deck-a", "Alpha", "0,0", ""],        # no event at all
        ["--emulate-input", "deck-a", "Alpha", "0,0", "Press"],   # the words are exact
        ["--emulate-input", "deck-a", "Alpha", "nope", "press"],  # no comma
        ["--emulate-input", "deck-a", "Alpha", "-1,0", "press"],  # before the first key
        ["--emulate-input", "", "Alpha", "0,0", "press"],         # no deck named
        ["--emulate-input", "deck-a", "", "0,0", "press"],        # no page named
    ]
    for argv in bad:
        clear_parking()
        recorder = Recorder(running=True)
        verdict = cli_forward.forward_cli_requests(parse(argv), recorder)
        assert verdict.failures, f"{argv} was accepted"
        assert verdict.failures[0].startswith("Error: "), verdict.failures
        assert cli_forward.USAGE in verdict.failures, (
            f"a malformed command has to be shown the shape of a good one: "
            f"{verdict.failures}")
        assert not verdict.handled, argv
        assert recorder.calls == [], (
            f"{argv} reached the bus before it was read: {recorder.calls}")
        assert not gl.api_page_requests and not gl.api_state_requests, (
            f"{argv} parked something despite being rejected")

    # The large-deck rule holds here too: coordinates the CLI cannot judge go
    # to the instance, which has the device to judge them against.
    clear_parking()
    recorder = Recorder(running=True)
    verdict = cli_forward.forward_cli_requests(
        parse(["--emulate-input", "deck-a", "Alpha", "14,7", "press"]), recorder)
    assert verdict.failures == [], verdict.failures
    assert recorder.forwards() == [("emulate", "deck-a", "Alpha", "14,7", "press")], (
        recorder.forwards())

    print("PASS: a malformed press rejects the whole command before anything happens")


def check_event_words_match_the_control_plane() -> None:
    """The CLI's copy of the vocabulary is the control plane's list.

    This module stays importable before globals and the control plane is not,
    so the words are written out twice. A word added on one side and not the
    other is either a command the CLI refuses and the instance would have run,
    or one it sends for the instance to refuse.
    """
    from src.backend import control_plane

    assert cli_forward.EMULATE_EVENTS == control_plane.EMULATED_EVENTS, (
        f"the CLI knows {cli_forward.EMULATE_EVENTS} and the control plane "
        f"knows {control_plane.EMULATED_EVENTS}")
    for word in cli_forward.EMULATE_EVENTS:
        assert word in cli_forward.USAGE, (
            f"the usage text must show every word that works: {word!r}")

    print("PASS: the CLI accepts exactly the event words the control plane runs")


def check_coordinate_failures_read_alike() -> None:
    """Both verbs that carry coordinates refuse them with one sentence.

    Only the flag named in the failure may differ. A copy of the check per verb
    drifts, and one mistake then reads two ways.
    """
    for coords in ("nope", "1,2,3", "", "x,y", "-1,0"):
        state = cli_forward.forward_cli_requests(
            parse(["--change-state", "deck-a", "Alpha", coords, "1"]), Recorder())
        press = cli_forward.forward_cli_requests(
            parse(["--emulate-input", "deck-a", "Alpha", coords, "press"]), Recorder())
        assert state.failures and press.failures, coords
        assert press.failures[0] == state.failures[0].replace(
            "--change-state argument", "--emulate-input argument"), (
            f"{coords!r} reads two ways:\n  {state.failures[0]}\n  {press.failures[0]}")

    print("PASS: both verbs refuse the same coordinates with the same sentence")


def check_call_timeout_outlasts_the_instances_own_waits() -> None:
    """A control call waits longer than the instance can take to answer it.

    The instance waits for a page's inputs to rebuild, and then for the press
    to reach the deck, before it replies. A call timeout below their sum gives
    up on work the instance goes on to do: a state change survives that,
    because asking twice sets the same state, but a press does not, and a
    command that reports a timeout and presses anyway invites the retry that
    presses twice.

    The two constants live in modules that cannot import each other, because
    this one stays importable before globals. This is where they meet.
    """
    from src.backend import control_plane

    instance_waits = (control_plane._INPUT_LOAD_WAIT_S
                      + control_plane._PRESS_START_WAIT_S)
    assert cli_forward.CONTROL_CALL_TIMEOUT_MS / 1000 > instance_waits, (
        f"a control call gives up after "
        f"{cli_forward.CONTROL_CALL_TIMEOUT_MS / 1000}s, and the instance can "
        f"take up to {instance_waits}s to answer one")
    assert cli_forward.DBUS_CALL_TIMEOUT_MS < cli_forward.CONTROL_CALL_TIMEOUT_MS, (
        "the probe that decides whether to boot must not wait as long as a "
        "call that does work a person asked for")

    print("PASS: a control call outwaits the longest answer the instance can give")


class _Reply:
    def __init__(self, text: str = ""):
        self._text = text

    def unpack(self):
        return (self._text,)


class _RecordingConnection:
    """A bus connection that answers everything and remembers the timeouts."""

    def __init__(self, running: bool = True):
        self.calls: list[tuple[str, int]] = []
        self._running = running

    def call_sync(self, destination, path, interface, method, params, reply_type,
                  flags, timeout_ms, cancellable):
        self.calls.append((method, timeout_ms))
        if method == "NameHasOwner":
            return _Reply(self._running)
        return _Reply("")

    def timeout_for(self, method: str) -> int:
        return next(ms for (name, ms) in self.calls if name == method)


class _StubGLib:
    class Error(Exception):
        pass

    @staticmethod
    def Variant(signature, values):
        return (signature, values)

    @staticmethod
    def VariantType(signature):
        return signature


class _StubGio:
    class DBusCallFlags:
        NO_AUTO_START = 0

    class DBusError:
        @staticmethod
        def get_remote_error(error):
            return ""


def check_control_calls_take_the_longer_timeout() -> None:
    """The transport spends the control timeout on a control call.

    The number above is only worth having if the call site uses it. The probe
    keeps the short one, because it decides whether to boot and nothing a
    person typed waits on its answer.
    """
    transport = object.__new__(cli_forward._BusTransport)
    connection = _RecordingConnection()
    transport._gio = _StubGio
    transport._glib = _StubGLib
    transport._connection = connection

    assert transport.is_running() is True
    assert transport.change_page("deck-a", "Alpha") == ""
    assert transport.change_state("deck-a", "Alpha", "0,0", 1) == ""
    assert transport.emulate_input("deck-a", "Alpha", "0,0", "press") == ""

    assert connection.timeout_for("NameHasOwner") == cli_forward.DBUS_CALL_TIMEOUT_MS, (
        f"the probe spent {connection.timeout_for('NameHasOwner')}ms")
    for method in ("ChangePage", "ChangeState", "EmulateInput"):
        assert connection.timeout_for(method) == cli_forward.CONTROL_CALL_TIMEOUT_MS, (
            f"{method} gave up after {connection.timeout_for(method)}ms, which is "
            f"not the control timeout of {cli_forward.CONTROL_CALL_TIMEOUT_MS}ms")

    print("PASS: a control call spends the control timeout and a probe does not")


def check_unparkable_is_the_one_rule() -> None:
    """One function answers "can a boot apply this?" for both halves of the CLI.

    The fast path and the boot path both ask it, which is what keeps park()
    free of a kind it has no queue for.
    """
    situations = (cli_forward.NOT_RUNNING_MESSAGE,
                  cli_forward.CLOSE_RUNNING_MESSAGE,
                  cli_forward.LISTING_MESSAGE)

    parkable = cli_forward.Plan(page_requests=[("deck-a", "Alpha")],
                                state_requests=[("deck-b", "Beta", "0,0", 1)])
    for message in situations:
        assert cli_forward.unparkable(parkable, message) == [], (
            "a page or state change is exactly what parking is for")

    pressing = cli_forward.Plan(emulate_requests=[("deck-f", "Zeta", "0,0", "press")])
    for message in situations:
        assert cli_forward.unparkable(pressing, message) == [message], (
            "the caller knows which situation it is in, and the answer is its "
            "own sentence")
    assert len(set(situations)) == len(situations), (
        "the three situations must read differently, or a person cannot tell "
        "which one they are in")

    assert not cli_forward.Plan(
        emulate_requests=[("deck-f", "Zeta", "0,0", "press")]).empty, (
        "a command carrying only presses asks for something, and an empty plan "
        "would boot as an ordinary launch and lose them")

    # Nothing about a press reaches the startup queue, so no slot on it holds
    # one and nothing has to sweep one that arrived too early.
    clear_parking()
    cli_forward.park(parkable)
    assert set(gl.api_page_requests) == {"deck-a"} and set(gl.api_state_requests) == {"deck-b"}, (
        f"{gl.api_page_requests} / {gl.api_state_requests}")

    print("PASS: one rule decides what a boot cannot apply, and presses are it")


# 8. Nothing asked for

def check_no_requests_touches_nothing() -> None:
    clear_parking()
    recorder = Recorder(running=True)

    verdict = cli_forward.forward_cli_requests(parse(["--devel"]), recorder)

    assert not verdict.handled and verdict.failures == [], verdict
    assert recorder.calls == [], (
        f"a launch that asks for no page or state change must not go near the "
        f"bus: {recorder.calls}")
    assert not gl.api_page_requests and not gl.api_state_requests

    print("PASS: an invocation with no requests probes nothing and parks nothing")


# 9. No session bus to open

def check_unreachable_bus_is_reported() -> None:
    """A bus that cannot be opened ends the command, and says so.

    The transport's constructor raises when there is no session bus. That
    error used to leave this module, reach main()'s @log.catch, print a
    traceback and exit zero, which reads as applied while the request was
    dropped. This process also cannot tell whether an instance runs, so it must
    not park and boot either: that opens a deck on a guess, next to an instance
    that may hold it.
    """
    clear_parking()
    original = cli_forward.bus_transport

    def refuse():
        raise cli_forward.TransportError("Could not open the session bus, so "
                                         "nothing was applied: no bus here")

    cli_forward.bus_transport = refuse
    try:
        verdict = cli_forward.forward_cli_requests(parse(ARGV))
    finally:
        cli_forward.bus_transport = original

    assert not verdict.handled, (
        "nothing was applied, so this invocation is not handled")
    assert verdict.failures == [
        "Could not open the session bus, so nothing was applied: no bus here"
    ], verdict.failures
    assert not gl.api_page_requests and not gl.api_state_requests, (
        f"a launch that cannot see the bus must not park and boot on a guess: "
        f"{gl.api_page_requests} / {gl.api_state_requests}")

    print("PASS: an unreachable session bus ends the command with a reason")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_cli_forward_all")
    try:
        check_all_requests_are_forwarded()
        check_nothing_running_parks_everything()
        check_close_running_parks_requests()
        check_failures_surface_without_stopping()
        check_older_instance_reports_once()
        check_broken_conversation_reported()
        check_validation_is_syntax_only()
        check_large_decks_not_pre_rejected()
        check_emulate_requests_are_forwarded()
        check_emulate_refusal_comes_back()
        check_emulate_cannot_be_parked()
        check_emulate_refuses_close_running()
        check_emulate_validation_is_syntax_only()
        check_event_words_match_the_control_plane()
        check_coordinate_failures_read_alike()
        check_call_timeout_outlasts_the_instances_own_waits()
        check_control_calls_take_the_longer_timeout()
        check_unparkable_is_the_one_rule()
        check_no_requests_touches_nothing()
        check_unreachable_bus_is_reported()
    finally:
        clear_parking()

    print("ALL PASS: scenario_cli_forward_all")


if __name__ == "__main__":
    main()
