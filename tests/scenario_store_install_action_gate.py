"""Pins what the exported install-plugin action lets a session peer do.

The application publishes its action group on the session bus, so a peer
that never touched this window can name install-plugin and pass a plugin
id. These legs run a real dbus-daemon, register a real Gio.Application on
it, and drive the action from a separate bus connection, which is what an
outside program has.

The property under test: an activation from outside reaches a
confirmation and never the install worker on its own, while the install
path the app's own windows use, which calls the store backend directly,
still installs with nothing to confirm.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import threading  # noqa: E402

from gi.repository import Gio, GLib  # noqa: E402

import globals as gl  # noqa: E402

from src.backend.Store.install_request import ACTION_NAME, InstallActionGate  # noqa: E402
from src.backend.Store.store_result import Err, Ok  # noqa: E402
from src.windows.Store.StoreData import PluginData  # noqa: E402

from scenario_api_lifecycle_publish import (  # noqa: E402
    pump, pump_until, start_private_bus, stop_private_bus,
)

WATCHDOG_SECONDS = 90

# An id of this scenario's own. The app's real id would let a Deckard
# running on the developer's session answer these calls if the daemon
# isolation ever broke.
APP_ID = "io.github.nazbert.DeckardInstallGate"

ACTIONS_IFACE = "org.gtk.Actions"


class Recorder:
    """What the gate called, in the order it called it."""

    def __init__(self, agree: bool = False):
        self.events: list[tuple[str, str]] = []
        self.agree = agree
        self.block: "threading.Event | None" = None

    def confirm(self, plugin_id: str) -> bool:
        self.events.append(("confirm", plugin_id))
        if self.block is not None:
            self.block.wait(timeout=30)
        return self.agree

    def worker(self, plugin_id: str) -> None:
        self.events.append(("install", plugin_id))

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

    def activate(self, name: str, target: str) -> None:
        # The reply is waited for, so the call returns only once the exported
        # action group has handled the activation, and the assertions below
        # a call are not racing it.
        off_thread(lambda: self.connection.call_sync(
            APP_ID, self.object_path, ACTIONS_IFACE, "Activate",
            GLib.Variant("(sava{sv})", (name, [GLib.Variant("s", target)], {})),
            None, Gio.DBusCallFlags.NONE, 10_000, None))

    def close(self) -> None:
        self.connection.close_sync(None)


def wait_for(recorder: Recorder, count: int, what: str, timeout: float = 20.0) -> None:
    pump_until(lambda: len(recorder.events) >= count, timeout, what)


def settle(seconds: float = 1.0) -> None:
    """Pump for a while, so a call that should never arrive has had its
    chance. A leg that asserts nothing happened is worthless without it."""
    pump(seconds)


def leg_surface_carries_the_action(peer: Peer) -> None:
    """The exported surface really does carry install-plugin today. Every
    leg below this one would pass vacuously if it did not."""
    names = peer.list_actions()
    assert ACTION_NAME in names, (
        f"the peer must see {ACTION_NAME} on the exported action group, "
        f"otherwise these legs prove nothing about it; got {names}")


def leg_external_activation_needs_a_yes(peer: Peer, recorder: Recorder) -> None:
    recorder.events.clear()
    recorder.agree = False

    peer.activate(ACTION_NAME, "com.example.Refused")
    wait_for(recorder, 1, "the peer's activation to reach the confirmation")
    settle()

    assert recorder.events == [("confirm", "com.example.Refused")], (
        "an activation from a bus peer must reach the confirmation and "
        f"nothing else while it is refused, got {recorder.events}")


def leg_confirmed_activation_installs(peer: Peer, recorder: Recorder) -> None:
    recorder.events.clear()
    recorder.agree = True

    peer.activate(ACTION_NAME, "com.example.Agreed")
    wait_for(recorder, 2, "the confirmed activation to reach the worker")
    settle()

    assert recorder.events == [("confirm", "com.example.Agreed"),
                               ("install", "com.example.Agreed")], (
        "a confirmed activation must install exactly the plugin it named, "
        f"and only after the confirmation, got {recorder.events}")


def leg_malformed_target_is_dropped(peer: Peer, recorder: Recorder,
                                    gate: InstallActionGate) -> None:
    recorder.events.clear()
    recorder.agree = True

    peer.activate(ACTION_NAME, "")
    peer.activate(ACTION_NAME, "   ")
    # An activation with no target at all cannot cross the bus for an action
    # that declares a string parameter, so it is driven here directly.
    assert gate.on_activate(None, None) is None, (
        "the activation handler answers nothing either way")
    settle(2.0)

    assert recorder.events == [], (
        "an activation whose target names no plugin must not even raise a "
        f"dialog, got {recorder.events}")


def leg_one_request_at_a_time(peer: Peer, recorder: Recorder) -> None:
    recorder.events.clear()
    recorder.agree = True
    recorder.block = threading.Event()
    try:
        peer.activate(ACTION_NAME, "com.example.First")
        wait_for(recorder, 1, "the first confirmation to open")
        # A peer that activates in a loop must not stack dialogs, and must
        # not start a second install beside the first.
        for _ in range(5):
            peer.activate(ACTION_NAME, "com.example.Second")
        settle(2.0)
        assert recorder.events == [("confirm", "com.example.First")], (
            "a second activation must be dropped while one request is still "
            f"waiting for its answer, got {recorder.events}")
    finally:
        recorder.block.set()
        recorder.block = None
    wait_for(recorder, 2, "the first request to finish")
    settle()
    assert recorder.events == [("confirm", "com.example.First"),
                               ("install", "com.example.First")], (
        f"the request that held the gate must still complete, got {recorder.events}")

    # The gate reopens once the request that held it ends.
    recorder.events.clear()
    recorder.agree = False
    peer.activate(ACTION_NAME, "com.example.Third")
    wait_for(recorder, 1, "the gate to reopen for a later activation")
    assert recorder.events == [("confirm", "com.example.Third")], (
        f"the gate must accept a later activation, got {recorder.events}")


def leg_internal_path_installs_unprompted(recorder: Recorder) -> None:
    """The path the store window, the missing-action row and the onboarding
    page use: a direct call on the store backend. It installs, and it asks
    the exported action's confirmation nothing."""
    from src.backend.Store.StoreBackend import StoreBackend
    from src.backend.Store.StoreCache import StoreCache

    recorder.events.clear()
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


def run_legs(bus_address: str) -> None:
    recorder = Recorder()
    gate = InstallActionGate(recorder.worker, recorder.confirm)

    application = Gio.Application(application_id=APP_ID,
                                  flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    gate.add_to(application)
    assert application.register(None), (
        "the application must register on the private bus, or nothing here "
        "exercises the exported surface")
    object_path = application.get_dbus_object_path()
    assert object_path, "a registered application must carry a dbus object path"

    peer = Peer(bus_address, object_path)
    try:
        leg_surface_carries_the_action(peer)
        leg_external_activation_needs_a_yes(peer, recorder)
        leg_confirmed_activation_installs(peer, recorder)
        leg_malformed_target_is_dropped(peer, recorder, gate)
        leg_one_request_at_a_time(peer, recorder)
        leg_internal_path_installs_unprompted(recorder)
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
