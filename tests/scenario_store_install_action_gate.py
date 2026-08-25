"""Pins what the exported store actions let a session peer do.

The application publishes its action group on the session bus, so a peer
that never touched this window can name install-plugin and pass a plugin
id, or name update-all-assets and pass nothing. These legs run a real
dbus-daemon, register a real Gio.Application on it, and drive both actions
from a separate bus connection, which is what an outside program has.

The property under test: an activation from outside reaches a
confirmation and never the worker on its own, a target that is not a
store id never reaches a dialog at all, a run of refusals makes an action
quiet, and the install path the app's own windows use, which calls the
store backend directly, still installs with nothing to confirm.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import threading  # noqa: E402

from gi.repository import Gio, GLib  # noqa: E402

import globals as gl  # noqa: E402

from src.backend.Store import install_request  # noqa: E402
from src.backend.Store.install_request import (  # noqa: E402
    INSTALL_ACTION,
    UPDATE_ACTION,
    ConfirmedActionGate,
    is_store_id,
)
from src.backend.Store.store_result import Err, Ok  # noqa: E402
from src.windows.Store.StoreData import PluginData  # noqa: E402

from scenario_api_lifecycle_publish import (  # noqa: E402
    pump, pump_until, start_private_bus, stop_private_bus,
)

WATCHDOG_SECONDS = 120

# An id of this scenario's own. The app's real id would let a Deckard
# running on the developer's session answer these calls if the daemon
# isolation ever broke.
APP_ID = "io.github.nazbert.DeckardInstallGate"

ACTIONS_IFACE = "org.gtk.Actions"

GOOD_ID = "com.example.Plugin"


class Recorder:
    """What a gate called, in the order it called it."""

    def __init__(self, agree: bool = False):
        self.events: list[tuple[str, str]] = []
        self.agree = agree
        self.raise_in_confirm = False
        self.confirm_block: "threading.Event | None" = None
        self.worker_block: "threading.Event | None" = None

    def confirm(self, subject: str) -> bool:
        self.events.append(("confirm", subject))
        if self.raise_in_confirm:
            raise RuntimeError("the confirmation dialog could not be built")
        if self.confirm_block is not None:
            self.confirm_block.wait(timeout=30)
        return self.agree

    def worker(self, subject: str) -> None:
        self.events.append(("work", subject))
        if self.worker_block is not None:
            self.worker_block.wait(timeout=30)

    def clear(self) -> None:
        self.events.clear()

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]


def off_thread(work, timeout: float = 25.0):
    """Run one blocking bus call on another thread while this one pumps.

    The application dispatches its incoming calls on the default main
    context, which is this thread. A call_sync from here would wait for a
    reply that only this thread can produce, so it would time out rather
    than prove anything.
    """
    box: dict = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = work()
        except Exception as e:  # re-raised on the calling thread below
            box["error"] = e
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    pump_until(done.is_set, timeout, "the peer's bus call to return")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class Peer:
    """A separate bus connection, standing in for any other program on the
    session bus. Nothing here passes by talking to the app in-process."""

    def __init__(self, bus_address: str, object_path: str):
        self.connection = Gio.DBusConnection.new_for_address_sync(
            bus_address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None,
        )
        self.object_path = object_path

    def list_actions(self) -> list[str]:
        reply = off_thread(lambda: self.connection.call_sync(
            APP_ID, self.object_path, ACTIONS_IFACE, "List", None,
            GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 10_000, None))
        return list(reply.unpack()[0])

    def _activate(self, name: str, arguments: list) -> None:
        # The reply is waited for, so the call returns only once the exported
        # action group has handled the activation, and the assertions below
        # a call are not racing it.
        off_thread(lambda: self.connection.call_sync(
            APP_ID, self.object_path, ACTIONS_IFACE, "Activate",
            GLib.Variant("(sava{sv})", (name, arguments, {})),
            None, Gio.DBusCallFlags.NONE, 10_000, None))

    def activate(self, name: str, target: str) -> None:
        self._activate(name, [GLib.Variant("s", target)])

    def activate_bare(self, name: str) -> None:
        """Activate an action that declares no parameter. Sending one to
        such an action is what a peer cannot do, so nothing is sent."""
        self._activate(name, [])

    def close(self) -> None:
        self.connection.close_sync(None)


def wait_for(recorder: Recorder, count: int, what: str, timeout: float = 20.0) -> None:
    pump_until(lambda: len(recorder.events) >= count, timeout, what)


def settle(seconds: float = 1.0) -> None:
    """Pump for a while, so a call that should never arrive has had its
    chance. A leg that asserts nothing happened is worthless without it."""
    pump(seconds)


# Bus legs

def leg_surface_carries_both_actions(peer: Peer) -> None:
    """The exported surface really does carry both actions today. Every leg
    below this one would pass vacuously if it did not."""
    names = peer.list_actions()
    for action in (INSTALL_ACTION, UPDATE_ACTION):
        assert action in names, (
            f"the peer must see {action} on the exported action group, "
            f"otherwise these legs prove nothing about it; got {names}")


def leg_external_activation_needs_a_yes(peer: Peer, recorder: Recorder) -> None:
    recorder.clear()
    recorder.agree = False

    peer.activate(INSTALL_ACTION, GOOD_ID)
    wait_for(recorder, 1, "the peer's activation to reach the confirmation")
    settle()

    assert recorder.events == [("confirm", GOOD_ID)], (
        "an activation from a bus peer must reach the confirmation and "
        f"nothing else while it is refused, got {recorder.events}")


def leg_confirmed_activation_installs(peer: Peer, recorder: Recorder) -> None:
    recorder.clear()
    recorder.agree = True

    peer.activate(INSTALL_ACTION, GOOD_ID)
    wait_for(recorder, 2, "the confirmed activation to reach the worker")
    settle()

    assert recorder.events == [("confirm", GOOD_ID), ("work", GOOD_ID)], (
        "a confirmed activation must install exactly the plugin it named, "
        f"and only after the confirmation, got {recorder.events}")


def leg_update_action_is_gated(peer: Peer, recorder: Recorder) -> None:
    """The twin of the install action. It carries no target, and an update
    reinstalls every out-of-date asset, so it must not run unconfirmed."""
    recorder.clear()
    recorder.agree = False

    peer.activate_bare(UPDATE_ACTION)
    wait_for(recorder, 1, "the update activation to reach the confirmation")
    settle()
    assert recorder.events == [("confirm", UPDATE_ACTION)], (
        "an update-all activation from a bus peer must reach the "
        f"confirmation and stop there while it is refused, got {recorder.events}")

    recorder.clear()
    recorder.agree = True
    peer.activate_bare(UPDATE_ACTION)
    wait_for(recorder, 2, "the confirmed update to reach the worker")
    settle()
    assert recorder.events == [("confirm", UPDATE_ACTION), ("work", UPDATE_ACTION)], (
        f"a confirmed update must run, and only after the answer, got {recorder.events}")


def leg_a_bad_target_never_reaches_a_dialog(peer: Peer, recorder: Recorder,
                                            gate: ConfirmedActionGate) -> None:
    """A target is attacker-controlled and ends up in a dialog heading. Only
    a store id may get that far: no traversal, no newline, no unbounded
    length."""
    recorder.clear()
    recorder.agree = True

    hostile = [
        "",
        "   ",
        "../../../etc/passwd",
        "com.example.Plugin\nThis app asks you to allow the next step.",
        "x" * 4096,
        ".hidden",
        "com example",
        "/absolute/path",
    ]
    for target in hostile:
        assert not is_store_id(target) or not target.strip(), (
            f"{target!r} must not read as a store id, or this leg proves nothing")
        peer.activate(INSTALL_ACTION, target)

    # An activation with no target at all cannot cross the bus for an action
    # that declares a string parameter, so it is driven here directly.
    gate.on_activate(None, None)
    settle(2.0)

    assert recorder.events == [], (
        "an activation whose target is not a store id must not even raise a "
        f"dialog, got {recorder.events}")


def leg_one_request_at_a_time(peer: Peer, recorder: Recorder) -> None:
    recorder.clear()
    recorder.agree = True
    recorder.confirm_block = threading.Event()
    try:
        peer.activate(INSTALL_ACTION, "com.example.First")
        wait_for(recorder, 1, "the first confirmation to open")
        # A peer that activates in a loop must not stack dialogs, and must
        # not start a second install beside the first.
        for _ in range(5):
            peer.activate(INSTALL_ACTION, "com.example.Second")
        settle(2.0)
        assert recorder.events == [("confirm", "com.example.First")], (
            "a second activation must be dropped while one request is still "
            f"waiting for its answer, got {recorder.events}")
    finally:
        recorder.confirm_block.set()
        recorder.confirm_block = None
    wait_for(recorder, 2, "the first request to finish")
    settle()
    assert recorder.events == [("confirm", "com.example.First"),
                               ("work", "com.example.First")], (
        f"the request that held the gate must still complete, got {recorder.events}")

    # The gate reopens once the request that held it ends.
    recorder.clear()
    recorder.agree = True
    peer.activate(INSTALL_ACTION, "com.example.Third")
    wait_for(recorder, 2, "the gate to reopen for a later activation")
    assert recorder.events == [("confirm", "com.example.Third"),
                               ("work", "com.example.Third")], (
        f"the gate must accept a later activation, got {recorder.events}")


# In-process legs, each on a gate of its own, because they leave the gate in
# a state a later leg would inherit. request() is the same entry point
# on_activate uses, so nothing is bypassed.

def leg_the_slot_is_held_until_the_work_ends() -> None:
    """The slot must cover the install and not only the dialog. Two installs
    of one id would swap the same directory under each other."""
    recorder = Recorder(agree=True)
    recorder.worker_block = threading.Event()
    gate = ConfirmedActionGate(INSTALL_ACTION, recorder.worker, recorder.confirm,
                               target_type="s", validate=is_store_id)
    try:
        assert gate.request(GOOD_ID) is True, "the first request must be accepted"
        wait_for(recorder, 2, "the worker to start")
        assert recorder.events == [("confirm", GOOD_ID), ("work", GOOD_ID)], recorder.events

        # The dialog is answered and gone; the install is still running.
        for _ in range(5):
            assert gate.request("com.example.Other") is False, (
                "a request must be refused while the install it would race is "
                "still running")
        settle(1.0)
        assert recorder.events == [("confirm", GOOD_ID), ("work", GOOD_ID)], (
            "no second confirmation or install may start while the first "
            f"install is still running, got {recorder.events}")
    finally:
        recorder.worker_block.set()
    settle(1.0)
    assert gate.request("com.example.Later") is True, (
        "the gate must reopen once the install finishes")


def leg_a_failed_confirmation_is_not_an_agreement() -> None:
    """A confirmation that raises answers no. A peer that makes the dialog
    fail must not get an install out of it."""
    recorder = Recorder(agree=True)
    recorder.raise_in_confirm = True
    gate = ConfirmedActionGate(INSTALL_ACTION, recorder.worker, recorder.confirm,
                               target_type="s", validate=is_store_id)
    gate.request(GOOD_ID)
    wait_for(recorder, 1, "the raising confirmation to run")
    settle(1.5)
    assert recorder.events == [("confirm", GOOD_ID)], (
        "a confirmation that raised must never reach the worker, got "
        f"{recorder.events}")


def leg_refusals_make_the_action_quiet() -> None:
    """A looping peer must not get a fresh modal for every Cancel. One
    mistaken Cancel still leaves the user their retry."""
    recorder = Recorder(agree=False)
    gate = ConfirmedActionGate(INSTALL_ACTION, recorder.worker, recorder.confirm,
                               target_type="s", validate=is_store_id)

    assert gate.request(GOOD_ID) is True, "the first request must be accepted"
    wait_for(recorder, 1, "the first refusal")
    settle(0.5)
    assert gate.request(GOOD_ID) is True, (
        "one refusal must not lock the user out of a retry")
    wait_for(recorder, 2, "the second refusal")
    settle(0.5)

    for _ in range(5):
        assert gate.request(GOOD_ID) is False, (
            "a run of refusals must make the action quiet")
    settle(1.0)
    assert len(recorder.events) == 2, (
        f"exactly two dialogs may come out of a refusing loop, got {recorder.events}")

    # An agreement clears the run, so the quiet period is about refusals and
    # not about use.
    fresh = Recorder(agree=True)
    ok_gate = ConfirmedActionGate(INSTALL_ACTION, fresh.worker, fresh.confirm,
                                  target_type="s", validate=is_store_id)
    for round_index in range(3):
        # Counted up front. The gate's thread appends while this runs, so a
        # target read after the request could already be behind.
        expected = (round_index + 1) * 2
        assert ok_gate.request(GOOD_ID) is True, (
            "an action nobody refused must stay open")
        wait_for(fresh, expected, "the confirmed request to finish")
        settle(0.3)
    assert fresh.kinds() == ["confirm", "work"] * 3, (
        f"each agreed request must confirm then work, got {fresh.events}")


def leg_internal_path_installs_unprompted(recorder: Recorder) -> None:
    """The path the store window, the missing-action row and the onboarding
    page use: a direct call on the store backend. It installs, and it asks
    the exported action's confirmation nothing."""
    from src.backend.Store.StoreBackend import StoreBackend
    from src.backend.Store.StoreCache import StoreCache

    recorder.clear()
    fixtures.install_stub_globals()

    class RecordingPluginManager:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def load_plugins(self) -> None: self.calls.append("load_plugins")
        def init_plugins(self) -> None: self.calls.append("init_plugins")
        def generate_action_index(self) -> None: self.calls.append("generate_action_index")
        def get_plugins(self) -> dict: return {}
        def get_plugin_by_id(self, plugin_id, include_disabled=True): return None

    class RecordingSignalManager:
        def __init__(self) -> None:
            self.signals: list[tuple] = []

        def trigger_signal(self, signal, *args) -> None:
            self.signals.append((signal, args))

    gl.plugin_manager = RecordingPluginManager()
    gl.signal_manager = RecordingSignalManager()

    backend = StoreBackend.__new__(StoreBackend)  # __init__ would spawn a fetch thread
    backend.store_cache = StoreCache()
    downloaded: list[str] = []

    def fake_download(**kwargs):
        downloaded.append(kwargs["directory"])
        return Ok(None)

    backend.download_repo = fake_download
    data = PluginData(github="https://github.com/test/test", plugin_id="com_test_Direct")

    result = backend.install_plugin(data)
    assert not isinstance(result, Err), (
        f"the app's own install path must still install, got {result!r}")
    assert len(downloaded) == 1, (
        f"the direct install must download exactly once, got {downloaded}")
    assert recorder.events == [], (
        "the direct install path must not go through the exported action's "
        f"confirmation, got {recorder.events}")


def leg_the_app_wires_both_actions_through_a_gate() -> None:
    """The legs above build their own gates, so they would all pass while
    the application itself exported a raw action. This drives the
    application's own wiring over a duck-typed self, and activates what it
    produced."""
    import types

    import src.app as app_mod

    added: dict = {}
    worked: list[str] = []
    confirmed: list[str] = []

    fake = types.SimpleNamespace(
        add_action=lambda action: added.__setitem__(action.get_name(), action),
        _install_plugin=lambda subject: worked.append(f"install:{subject}"),
        _update_all_assets=lambda subject="": worked.append(f"update:{subject}"),
        _confirm_install_request=lambda subject: confirmed.append(subject) or False,
        _confirm_update_request=lambda subject: confirmed.append(subject) or False,
    )
    app_mod.App.add_signals(fake)

    for name in (INSTALL_ACTION, UPDATE_ACTION):
        assert name in added, f"the application must export {name}, got {sorted(added)}"
    for attribute in ("install_gate", "update_assets_gate"):
        gate = getattr(fake, attribute, None)
        assert isinstance(gate, ConfirmedActionGate), (
            f"the application must wire {attribute} through the gate, got {gate!r}")

    # Activate what the application actually exported. A raw action would run
    # the worker here; a gated one reaches the confirmation and stops.
    added[UPDATE_ACTION].activate(None)
    added[INSTALL_ACTION].activate(GLib.Variant("s", GOOD_ID))
    pump_until(lambda: len(confirmed) >= 2, 20.0,
               "both exported actions to reach their confirmation")
    settle(1.5)

    assert worked == [], (
        "an activation of an action the application exported must not reach "
        f"its worker while it is refused, got {worked}")
    assert sorted(confirmed) == sorted([UPDATE_ACTION, GOOD_ID]), (
        f"both actions must confirm, got {confirmed}")


def leg_the_id_gate_is_the_installers_own() -> None:
    """The target check must be the one the installer applies, so a target
    it accepts is one the install would accept."""
    from src.backend.Store.StoreBackend import StoreBackend

    assert is_store_id is not None
    for value in (GOOD_ID, "com_test_Direct", "a"):
        assert is_store_id(value) is StoreBackend.is_safe_asset_id(value) is True, (
            f"{value!r} must read as a store id on both checks")
    assert install_request.REFUSALS_BEFORE_QUIET >= 2, (
        "one mistaken Cancel must not arm the quiet period")


def run_legs(bus_address: str) -> None:
    install_recorder = Recorder()
    install_gate = ConfirmedActionGate(
        INSTALL_ACTION, install_recorder.worker, install_recorder.confirm,
        target_type="s", validate=is_store_id)
    update_recorder = Recorder()
    update_gate = ConfirmedActionGate(
        UPDATE_ACTION, update_recorder.worker, update_recorder.confirm)

    application = Gio.Application(application_id=APP_ID,
                                  flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    install_gate.add_to(application)
    update_gate.add_to(application)
    assert application.register(None), (
        "the application must register on the private bus, or nothing here "
        "exercises the exported surface")
    object_path = application.get_dbus_object_path()
    assert object_path, "a registered application must carry a dbus object path"

    peer = Peer(bus_address, object_path)
    try:
        leg_surface_carries_both_actions(peer)
        leg_external_activation_needs_a_yes(peer, install_recorder)
        leg_confirmed_activation_installs(peer, install_recorder)
        leg_update_action_is_gated(peer, update_recorder)
        leg_a_bad_target_never_reaches_a_dialog(peer, install_recorder, install_gate)
        leg_one_request_at_a_time(peer, install_recorder)
        leg_the_slot_is_held_until_the_work_ends()
        leg_a_failed_confirmation_is_not_an_agreement()
        leg_refusals_make_the_action_quiet()
        leg_the_app_wires_both_actions_through_a_gate()
        leg_the_id_gate_is_the_installers_own()
        leg_internal_path_installs_unprompted(install_recorder)
    finally:
        peer.close()


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, "scenario_store_install_action_gate")
    bus_proc, bus_address = start_private_bus()
    try:
        run_legs(bus_address)
    finally:
        stop_private_bus(bus_proc)

    print("PASS: scenario_store_install_action_gate")


if __name__ == "__main__":
    main()
