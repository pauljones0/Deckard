"""Exercise the uniqueness gate with real D-Bus and Gio applications.
Cover publish order, contention, forwarding, handoff, and busless startup."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

from gi.repository import Gio, GLib  # noqa: E402

import globals as gl  # noqa: E402

import appinfo  # noqa: E402
import src.api as api  # noqa: E402
from cli_args import argparser  # noqa: E402
from src.backend import cli_forward, instance_gate  # noqa: E402

from scenario_api_lifecycle_publish import (  # noqa: E402
    Observer, _die_with_parent, pump, pump_until, start_private_bus,
    stop_private_bus,
)
# The running instance as a recorder, shared rather than copied. Leg 11 needs
# the object the forwarding rules are already pinned against.
from scenario_cli_forward_all import Recorder  # noqa: E402

WATCHDOG_SECONDS = 60

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "instance_gate_child.py")

# Use a unique non-production name per leg to avoid inherited bus owners.
ID_ORDERING = "io.github.nazbert.DeckardGateOrdering"
ID_RACE = "io.github.nazbert.DeckardGateRace"
ID_FORWARD = "io.github.nazbert.DeckardGateForward"
ID_CLOSE = "io.github.nazbert.DeckardGateClose"
ID_DEAF = "io.github.nazbert.DeckardGateDeaf"
ID_MIDBOOT = "io.github.nazbert.DeckardGateMidBoot"
ID_SILENT = "io.github.nazbert.DeckardGateSilent"
ID_FAILOPEN = "io.github.nazbert.DeckardGateFailOpen"
ID_BLOCKED = "io.github.nazbert.DeckardGateBlocked"
ID_OLDGUARD = "io.github.nazbert.DeckardGateOldGuard"
ID_PARKED = "io.github.nazbert.DeckardGateParked"

RACE_CONTENDERS = 8

# One command carrying both kinds, each naming its own deck, so a request lost
# on the way out cannot hide behind last-write-wins parking.
PARKED_ARGV = [
    "--change-page", "gate-deck-a", "Alpha",
    "--change-page", "gate-deck-b", "Beta",
    "--change-state", "gate-deck-c", "Gamma", "1,2", "3",
]
PARKED_FORWARDS = [
    ("page", "gate-deck-a", "Alpha"),
    ("page", "gate-deck-b", "Beta"),
    ("state", "gate-deck-c", "Gamma", "1,2", 3),
]


# Children

def spawn(mode: str, app_id: str, **env_extra) -> subprocess.Popen:
    env = dict(os.environ)
    env["DECKARD_GATE_MODE"] = mode
    env["DECKARD_GATE_APP_ID"] = app_id
    # Keep os._exit child data inside the scenario's temporary directory.
    env["DECKARD_GATE_DATA"] = gl.DATA_PATH
    env.update({k: str(v) for k, v in env_extra.items()})
    return subprocess.Popen(
        [sys.executable, CHILD], stdout=subprocess.PIPE, text=True,
        env=env, preexec_fn=_die_with_parent,
    )


def wait_for_line(proc: subprocess.Popen, expected: str,
                  timeout: float = 30.0) -> None:
    """Block until the child prints expected. It flushes every line."""
    deadline = time.monotonic() + timeout
    while True:
        line = (proc.stdout.readline() or "").strip()
        if line == expected:
            return
        if not line and proc.poll() is not None:
            raise AssertionError(
                f"child exited (rc={proc.returncode}) before printing "
                f"{expected!r}")
        if time.monotonic() >= deadline:
            raise AssertionError(f"child never printed {expected!r}")


def _said_lines(proc: subprocess.Popen, timeout: float = 60.0) -> list[tuple[str, str]]:
    """Return each child line as an ordered (first word, rest) pair.
    Cache the result because communicate() drains the pipe once."""
    said = getattr(proc, "_said_lines", None)
    if said is None:
        out, _ = proc.communicate(timeout=timeout)
        said = [(key, value) for key, _, value in
                (line.strip().partition(" ") for line in out.splitlines())
                if key]
        proc._said_lines = said
    return said


def transcript(proc: subprocess.Popen, timeout: float = 60.0) -> list[str]:
    """Return line keys in order, unlike records(), which loses ordering."""
    return [key for key, _ in _said_lines(proc, timeout)]


def records(proc: subprocess.Popen, timeout: float = 60.0) -> dict[str, str]:
    """Return child output keyed by the first word of each line."""
    return dict(_said_lines(proc, timeout))


def kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


# Bus helpers, kept apart from the gate's own

def has_owner(connection: Gio.DBusConnection, name: str) -> bool:
    """Ask the daemon directly without using the gate helper under test."""
    return connection.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "NameHasOwner", GLib.Variant("(s)", (name,)), GLib.VariantType("(b)"),
        Gio.DBusCallFlags.NO_AUTO_START, 5000, None,
    ).unpack()[0]


def wait_for_owner(connection: Gio.DBusConnection, name: str,
                   timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not has_owner(connection, name):
        if time.monotonic() >= deadline:
            raise AssertionError(f"{name} was never owned")
        time.sleep(0.02)


# Legs

def leg_ordering(observer: Observer) -> None:
    """publish() runs before register(), so no name appears before objects."""
    app = Gio.Application(application_id=ID_ORDERING)
    seen: list[bool] = []

    def publish():
        # Publish objects before ownership makes the instance discoverable.
        seen.append(has_owner(observer.connection, ID_ORDERING))
        api.start_dbus_service()

    decision = instance_gate.establish(app, publish=publish, close_running=False)

    assert decision is instance_gate.Decision.PRIMARY, decision
    assert seen == [False], (
        f"the application name was ALREADY owned when publish() ran ({seen}): "
        f"registration happened first, which reopens the window where a "
        f"client sees an instance whose objects are not there yet"
    )
    assert has_owner(observer.connection, ID_ORDERING), (
        "the gate returned PRIMARY without the name being owned"
    )

    # The other half. An external client that addresses the well-known name the
    # instant it exists reaches the object that went up before it.
    observer.destination = ID_ORDERING
    data_path = observer.get_property(api.DBUS_OBJECT_PATH, api.TOP_IFACE,
                                      "DataPath")
    assert data_path == gl.DATA_PATH, (
        f"the published object answered {data_path!r} instead of "
        f"{gl.DATA_PATH!r}"
    )
    api.stop_dbus_service()
    print("  PASS: objects are published before the name is owned")


def contend(app_id: str) -> list[str]:
    """Return verdicts from simultaneous launches.
    Synchronize immediately before establish(), including connection time."""
    start_at = time.time() + 1.0
    children = [
        spawn("establish", app_id, DECKARD_GATE_START_AT=start_at,
              DECKARD_GATE_HOLD=2.0)
        for _ in range(RACE_CONTENDERS)
    ]
    try:
        return [records(child).get("VERDICT") for child in children]
    finally:
        for child in children:
            kill(child)


def leg_race(observer: Observer) -> None:
    """Simultaneous launches. Exactly one is the instance, every time."""
    for round_number in (1, 2):
        app_id = f"{ID_RACE}{round_number}"
        verdicts = contend(app_id)

        primaries = [v for v in verdicts if v == "primary"]
        remotes = [v for v in verdicts if v == "remote"]
        assert len(primaries) == 1, (
            f"round {round_number}: {len(primaries)} of {RACE_CONTENDERS} "
            f"simultaneous launches think they are the instance ({verdicts}) "
            f"-- two of them would open the same decks"
        )
        assert len(remotes) == RACE_CONTENDERS - 1, verdicts
        assert not has_owner(observer.connection, app_id), (
            "the winner's name outlived its process"
        )
    print(f"  PASS: {RACE_CONTENDERS} simultaneous launches x2 rounds, exactly "
          f"one primary each")


def leg_forwarding(observer: Observer) -> None:
    """The activate() of a remote lands on the activate signal of the primary."""
    app = Gio.Application(application_id=ID_FORWARD)
    activations: list[float] = []
    app.connect("activate", lambda _app: activations.append(time.monotonic()))

    decision = instance_gate.establish(app, publish=lambda: None,
                                       close_running=False)
    assert decision is instance_gate.Decision.PRIMARY, decision
    assert not activations, "registering must not activate anything"

    child = spawn("activate", ID_FORWARD)
    try:
        pump_until(lambda: bool(activations), 30.0,
                   "the remote launch's activation never reached the primary")
        said = records(child)
    finally:
        kill(child)

    assert said.get("VERDICT") == "remote", said
    assert said.get("ACTIVATED") == "", said
    print("  PASS: a second launch presents the running instance through the "
          "framework")


def leg_close_running(observer: Observer) -> None:
    """Against an instance that quits, the gate sees the release, then registers."""
    child = spawn("primary-quit", ID_CLOSE)
    try:
        wait_for_line(child, "READY")
        assert has_owner(observer.connection, ID_CLOSE)

        app = Gio.Application(application_id=ID_CLOSE)
        started = time.monotonic()
        decision = instance_gate.establish(app, publish=lambda: None,
                                           close_running=True)
        took = time.monotonic() - started

        assert decision is instance_gate.Decision.PRIMARY, decision
        said = records(child)
        assert said.get("QUIT-RECEIVED") == "", (
            f"the running instance was never asked to quit: {said}")
        assert child.returncode == 0, child.returncode
        assert took < instance_gate.CLOSE_GRACE_SECONDS, (
            f"waiting for the release took {took:.1f}s -- the poll is not "
            f"reacting to the release, it is waiting out its grace"
        )
    finally:
        kill(child)
    print(f"  PASS: --close-running closed the instance and took over "
          f"({took:.2f}s)")


def leg_close_running_mid_boot(observer: Observer) -> None:
    """Delay close-running until a booting instance can dispatch the request.
    A queued quit after handoff timeout could leave no instance running."""
    dispatch_delay = 2.0
    child = spawn("primary-quit", ID_MIDBOOT,
                  DECKARD_GATE_DISPATCH_DELAY=dispatch_delay)
    try:
        wait_for_line(child, "READY")
        assert has_owner(observer.connection, ID_MIDBOOT)

        app = Gio.Application(application_id=ID_MIDBOOT)
        started = time.monotonic()
        decision = instance_gate.establish(app, publish=lambda: None,
                                           close_running=True)
        took = time.monotonic() - started

        assert decision is instance_gate.Decision.PRIMARY, decision
        said = records(child)
        assert said.get("QUIT-RECEIVED") == "", (
            f"the instance was never asked once it could answer: {said}")
        assert child.returncode == 0, child.returncode
        # Compare child events because parent and child elapsed clocks start apart.
        # DISPATCHING must precede the GDBus reader's WIRE-ACTIVATE event.
        order = transcript(child)
        assert "DISPATCHING" in order, (
            f"the instance never reached its main loop: {order}")
        assert "WIRE-ACTIVATE" in order, (
            f"no quit ever arrived at the instance: {order}")
        assert order.index("DISPATCHING") < order.index("WIRE-ACTIVATE"), (
            f"the quit arrived while the instance was still booting ({order}) "
            f"-- it went into its queue, not to a loop that could act on it, "
            f"and it would have killed the instance a moment after this "
            f"launch had already given up"
        )
    finally:
        kill(child)
    print(f"  PASS: --close-running waits for a booting instance to answer, "
          f"then closes it ({took:.2f}s)")


def leg_close_running_never_answers(observer: Observer) -> None:
    """Leave an instance that never dispatches running and unasked.
    Verify at the GDBus worker because its main-context handler never runs."""
    child = spawn("primary-quit", ID_SILENT, DECKARD_GATE_DISPATCH_DELAY=300)
    grace = instance_gate.CLOSE_GRACE_SECONDS
    try:
        wait_for_line(child, "READY")
        app = Gio.Application(application_id=ID_SILENT)
        instance_gate.CLOSE_GRACE_SECONDS = 1.5
        started = time.monotonic()
        try:
            instance_gate.establish(app, publish=lambda: None,
                                    close_running=True)
        except instance_gate.CloseRunningFailed as e:
            took = time.monotonic() - started
            assert "still starting up" in str(e), e
            assert "stuck" in str(e), (
                f"the message must name the wedged case it cannot rule out: {e}")
            assert "left running" in str(e), (
                f"the message must not read as though the quit was sent: {e}")
        else:
            raise AssertionError(
                "--close-running took over from an instance it never managed "
                "to speak to"
            )
        assert 1.5 <= took < 6.0, f"the grace was not honoured ({took:.1f}s)"
        assert child.poll() is None, (
            "the instance was killed by a quit it could not answer -- the "
            "outcome this ordering exists to prevent"
        )
        assert has_owner(observer.connection, ID_SILENT), (
            "the instance lost its name, so it did not survive this leg")
    finally:
        instance_gate.CLOSE_GRACE_SECONDS = grace
        kill(child)
    said = records(child)
    assert "WIRE-ACTIVATE" not in said, (
        f"a quit reached the instance at the wire: it would have sat in the "
        f"queue and killed it whenever its loop finally started, a moment "
        f"after this launch had already given up -- {said}"
    )
    assert "QUIT-RECEIVED" not in said, said
    print(f"  PASS: an instance that never answers is left running, unasked "
          f"(nothing on the wire, {took:.2f}s)")


def leg_close_running_refused(observer: Observer) -> None:
    """Against an instance that ignores the quit, the launch fails, not boots."""
    child = spawn("primary-deaf", ID_DEAF)
    grace = instance_gate.CLOSE_GRACE_SECONDS
    try:
        wait_for_line(child, "READY")
        app = Gio.Application(application_id=ID_DEAF)

        # Shorten the grace while preserving the bounded-failure condition.
        instance_gate.CLOSE_GRACE_SECONDS = 1.5
        started = time.monotonic()
        try:
            instance_gate.establish(app, publish=lambda: None,
                                    close_running=True)
        except instance_gate.CloseRunningFailed as e:
            took = time.monotonic() - started
            # Distinguish a refused quit from an instance that was never asked.
            assert "asked to quit" in str(e), e
            assert "had not exited" in str(e), e
        else:
            raise AssertionError(
                "--close-running reported success against an instance that is "
                "still running (and this launch would now boot a second one)"
            )
        assert 1.5 <= took < 6.0, f"the grace was not honoured ({took:.1f}s)"
        assert has_owner(observer.connection, ID_DEAF), (
            "the deaf instance lost its name, so this leg proved nothing")
    finally:
        instance_gate.CLOSE_GRACE_SECONDS = grace
        kill(child)
    said = records(child)
    # Use the same wire witness to distinguish arrival from the silent case.
    assert said.get("WIRE-ACTIVATE") == "quit", (
        f"the quit never reached this instance, so it did not refuse anything "
        f"-- and the witness the unasked leg trusts reports nothing: {said}"
    )
    print(f"  PASS: --close-running that cannot close fails after its grace "
          f"(asked at the wire, {took:.2f}s)")


def leg_fail_open() -> None:
    """With no bus at all the launch still boots, degraded and honest."""
    child = spawn("establish", ID_FAILOPEN,
                  DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent/deckard-gate")
    try:
        said = records(child)
    finally:
        kill(child)
    assert said.get("VERDICT") == "primary-unregistered", (
        f"a launch with no session bus must still boot: {said}")
    assert said.get("NON_UNIQUE") == "True", (
        f"the application was left claiming to be unique with no bus to be "
        f"unique on: {said}")
    assert said.get("PUBLISHED") == "1", (
        f"the API publish was skipped on the busless path: {said}")
    print("  PASS: a launch with no session bus boots non-unique")


def leg_old_name_guard(observer: Observer) -> None:
    """A pre-rename instance is asked to quit before this launch opens a deck."""
    # Delay exit after quit so the guard must wait for name release.
    teardown = 0.6
    child = spawn("old-name", ID_OLDGUARD, DECKARD_GATE_QUIT_DELAY=teardown)
    try:
        wait_for_line(child, "READY")
        # Confirm that the old name belongs to this scenario's private bus.
        assert has_owner(observer.connection, appinfo.OLD_APP_ID), (
            "the stand-in never took the pre-rename name")

        app = Gio.Application(application_id=ID_OLDGUARD)
        started = time.monotonic()
        decision = instance_gate.establish(app, publish=lambda: None,
                                           close_running=False)
        took = time.monotonic() - started

        assert decision is instance_gate.Decision.PRIMARY, decision
        said = records(child)
        assert said.get("QUIT-RECEIVED") == "", (
            f"the pre-rename instance was never asked to quit: {said}")
        assert not has_owner(observer.connection, appinfo.OLD_APP_ID), (
            "the gate returned before the pre-rename instance let go, so this "
            "launch would open decks that instance still holds"
        )
        assert teardown <= took < instance_gate.CLOSE_GRACE_SECONDS, (
            f"the guard returned after {took:.2f}s: it must wait for the "
            f"release (>= {teardown}s here) and must not sleep out its bound "
            f"({instance_gate.CLOSE_GRACE_SECONDS}s)"
        )
    finally:
        kill(child)
    print(f"  PASS: a pre-rename instance is quit and waited for ({took:.2f}s)")


def leg_parked_requests_follow_handoff(observer: Observer) -> None:
    """Forward parked requests after the launch loses the ownership race.
    Registration can report remote only after requests are already parked."""
    child = spawn("primary-deaf", ID_PARKED)
    try:
        wait_for_line(child, "READY")
        assert has_owner(observer.connection, ID_PARKED)

        parked = cli_forward.route_cli_requests(
            argparser.parse_args(PARKED_ARGV), Recorder(running=False))
        assert not parked.handled and not parked.failures, parked
        assert gl.api_page_requests and gl.api_state_requests, (
            "nothing was parked, so the loss this leg is about cannot happen")

        app = Gio.Application(application_id=ID_PARKED)
        decision = instance_gate.establish(app, publish=lambda: None,
                                           close_running=False)
        assert decision is instance_gate.Decision.REMOTE, decision

        refusal = "Page 'Beta' not found. Available pages: Alpha"
        instance = Recorder(running=True, answers={"gate-deck-b": refusal})
        failures = cli_forward.forward_parked_requests(instance)

        assert instance.forwards() == PARKED_FORWARDS, (
            f"the requests this launch parked never reached the instance that "
            f"owns the name -- they die here, silently, when nothing sends "
            f"them: {instance.forwards()}"
        )
        assert failures == [refusal], (
            f"what the instance says about a request has to come back to the "
            f"terminal that typed it, on this path as on any other: {failures}"
        )
        assert not gl.api_page_requests and not gl.api_state_requests, (
            f"the parking still holds requests that have been handed over: "
            f"{gl.api_page_requests} / {gl.api_state_requests}"
        )
    finally:
        kill(child)
        gl.api_page_requests.clear()
        gl.api_state_requests.clear()
    print("  PASS: requests parked by the launch that lost the race are handed "
          "to the one that won")


class RefusingApp:
    """Fail registration immediately instead of waiting for GIO's timeout."""

    def __init__(self, app_id: str):
        self._app_id = app_id
        self.flags = Gio.ApplicationFlags.DEFAULT_FLAGS

    def get_application_id(self):
        return self._app_id

    def register(self):
        raise GLib.Error.new_literal(Gio.io_error_quark(),
                                     "Timeout was reached",
                                     int(Gio.IOErrorEnum.TIMED_OUT))

    def get_is_remote(self):
        raise AssertionError("a registration that failed has no verdict to give")

    def get_flags(self):
        return self.flags

    def set_flags(self, flags):
        self.flags = flags


def leg_registration_failure_arms(observer: Observer) -> None:
    """A failed registration means opposite things depending on the name."""
    child = spawn("primary-quit", ID_BLOCKED)
    try:
        wait_for_line(child, "READY")
        assert has_owner(observer.connection, ID_BLOCKED)

        # An owned name makes registration failure a blocked second launch.
        blocked = RefusingApp(ID_BLOCKED)
        try:
            instance_gate.establish(blocked, publish=lambda: None,
                                    close_running=False)
        except instance_gate.HandoffFailed as e:
            assert "did not answer" in str(e), e
        else:
            raise AssertionError(
                "a launch that could not join the running instance decided it "
                "was the instance instead"
            )
        assert not (blocked.flags & Gio.ApplicationFlags.NON_UNIQUE), (
            "the second launch was quietly made non-unique, which is the "
            "double-instance this gate exists to prevent"
        )
    finally:
        kill(child)

    # Without an owner, registration failure permits degraded non-unique startup.
    lonely = RefusingApp("io.github.nazbert.DeckardGateNobody")
    decision = instance_gate.establish(lonely, publish=lambda: None,
                                       close_running=False)
    assert decision is instance_gate.Decision.PRIMARY_UNREGISTERED, decision
    assert lonely.flags & Gio.ApplicationFlags.NON_UNIQUE, lonely.flags
    print("  PASS: a failed registration boots only when nothing owns the name")


def run_legs(bus_address: str) -> None:
    observer = Observer(bus_address, ID_ORDERING)
    try:
        assert instance_gate.CLOSE_GRACE_SECONDS == 10.0, (
            "the shipped grace changed; every timing assertion below is "
            "written against 10s"
        )
        leg_ordering(observer)
        leg_race(observer)
        leg_forwarding(observer)
        leg_close_running(observer)
        leg_close_running_mid_boot(observer)
        leg_close_running_never_answers(observer)
        leg_close_running_refused(observer)
        leg_fail_open()
        leg_registration_failure_arms(observer)
        leg_old_name_guard(observer)
        leg_parked_requests_follow_handoff(observer)
    finally:
        api.stop_dbus_service()
        observer.connection.close_sync(None)
        pump(0.1)


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, "scenario_instance_gate")

    bus_proc, bus_address = start_private_bus()
    assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == bus_address, (
        "this scenario is not pointed at its own daemon, and everything it "
        "does -- including asking a pre-rename instance to quit -- would go to "
        "the developer's real session bus"
    )
    try:
        run_legs(bus_address)
    finally:
        stop_private_bus(bus_proc)

    print("PASS: scenario_instance_gate")


if __name__ == "__main__":
    main()
