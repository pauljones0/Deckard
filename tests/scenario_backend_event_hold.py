"""Hold pre-registration backend events for a bounded window and release them when the backend registers.
The hold keeps the newest per holder and event ID; live dispatch and relaunch supersede entries, and expiry drops the rest with one log."""

import threading
import time
import types

import fixtures  # noqa: F401  (isolated --data tempdir; import first)

import globals as gl  # noqa: E402

# Stub only the registries touched by launch and teardown; a real PluginManager loads the plugin ecosystem.
gl.plugin_manager = types.SimpleNamespace(backends=[], backend_processes=[])

from src.backend.PluginManager import PluginBase as plugin_base_module  # noqa: E402
from src.backend.PluginManager import PluginManager as plugin_manager_module  # noqa: E402
from src.backend.PluginManager.backend_event_hold import BackendEventHold  # noqa: E402
from src.backend.PluginManager.EventHolder import EventHolder  # noqa: E402
from src.backend.PluginManager.PluginBase import PluginBase  # noqa: E402


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    """Poll predicate to a deadline. Used for the timer-wheel expiry alone,
    which is a real delay and not an interleaving."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class _Recorder:
    """An observer that records every delivery and counts them."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.got = threading.Event()

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.got.set()


def _holder(hold: BackendEventHold, event_id: str) -> EventHolder:
    """An EventHolder whose plugin carries the given hold."""
    plugin = types.SimpleNamespace(backend_event_hold=hold)
    return EventHolder(plugin_base=plugin, event_id=event_id)


def check_held_then_delivered_on_connect() -> None:
    """Release an unobserved event after its observer subscribes.
    This covers backend state sent before action listeners load."""
    hold = BackendEventHold(label="held", bound_s=30.0)
    holder = _holder(hold, "test::held")
    hold.arm()

    holder.trigger_event("payload")
    recorder = _Recorder()
    holder.add_listener(recorder)
    assert not recorder.calls, "the event was delivered before the backend registered"

    hold.release()
    assert recorder.got.wait(10), "the held event was never delivered after the release"
    args, kwargs = recorder.calls[0]
    assert args == ("test::held", "payload"), (
        f"the held event lost the event id contract of the first argument: {args}"
    )
    assert kwargs == {}
    assert not hold.armed, "the release left the window open"
    print("PASS: an event held while the backend connects is delivered on the release")


def check_observed_event_is_not_delayed() -> None:
    """Dispatch observed events immediately.
    The hold accepts only events that would otherwise have no observer."""
    hold = BackendEventHold(label="observed", bound_s=30.0)
    holder = _holder(hold, "test::observed")
    recorder = _Recorder()
    holder.add_listener(recorder)

    hold.arm()
    holder.trigger_event("now")
    assert recorder.got.wait(10), "an event with a live observer was held instead of dispatched"
    assert hold.armed, "dispatching an observed event closed the window"
    hold.release()
    assert len(recorder.calls) == 1, (
        f"the observed event was both dispatched and held: {recorder.calls}"
    )
    print("PASS: an event with an observer dispatches at once while the window is open")


def check_coalescing_and_multiple_ids() -> None:
    """One entry per event id, newest wins, and different ids all survive."""
    hold = BackendEventHold(label="coalesce", bound_s=30.0)
    first = _holder(hold, "test::coalesce-a")
    second = _holder(hold, "test::coalesce-b")
    hold.arm()

    first.trigger_event("stale")
    first.trigger_event("fresh")
    second.trigger_event("other")

    recorder_a = _Recorder()
    recorder_b = _Recorder()
    first.add_listener(recorder_a)
    second.add_listener(recorder_b)
    hold.release()

    assert recorder_a.got.wait(10) and recorder_b.got.wait(10), "a held event was lost"
    # Per-holder lanes are serialized; after one delivery, allow time for an erroneous second delivery before asserting the count.
    assert not _wait_until(lambda: len(recorder_a.calls) > 1, timeout=0.5), (
        f"the hold replayed every trigger of one event id: {recorder_a.calls}"
    )
    assert recorder_a.calls[0][0] == ("test::coalesce-a", "fresh"), (
        f"the hold kept the stale value of a coalesced event id: {recorder_a.calls}"
    )
    assert recorder_b.calls[0][0] == ("test::coalesce-b", "other"), (
        f"a second event id was coalesced away: {recorder_b.calls}"
    )
    print("PASS: the hold keeps one entry per event id, and the newest wins")


def check_a_live_dispatch_supersedes_the_held_value() -> None:
    """Do not replay a held value after a newer live dispatch.
    Otherwise release leaves the observer's final state stale."""
    hold = BackendEventHold(label="supersede", bound_s=30.0)
    holder = _holder(hold, "test::supersede")
    hold.arm()

    holder.trigger_event("stale")
    recorder = _Recorder()
    holder.add_listener(recorder)
    holder.trigger_event("fresh")
    assert recorder.got.wait(10), "the live event was not dispatched"

    hold.release()
    assert not _wait_until(lambda: len(recorder.calls) > 1, timeout=0.5), (
        f"the release delivered a value older than the one already dispatched: "
        f"{recorder.calls}"
    )
    assert recorder.calls[-1][0] == ("test::supersede", "fresh"), (
        f"the observer's last reading is not the newest value: {recorder.calls}"
    )
    print("PASS: a live dispatch supersedes what the window holds for that event")


def check_two_holders_sharing_an_event_id() -> None:
    """Keep separate held entries for holders that share one event ID.
    Keying by event ID alone would silently replace the first holder's event."""
    hold = BackendEventHold(label="shared-id", bound_s=30.0)
    first = _holder(hold, "test::shared")
    second = _holder(hold, "test::shared")
    hold.arm()

    first.trigger_event("from-first")
    second.trigger_event("from-second")

    first_recorder = _Recorder()
    second_recorder = _Recorder()
    first.add_listener(first_recorder)
    second.add_listener(second_recorder)
    hold.release()

    assert first_recorder.got.wait(10), (
        "the first holder's event was replaced by the second holder's; the hold "
        "keys on the event id alone"
    )
    assert second_recorder.got.wait(10), "the second holder's event was lost"
    assert first_recorder.calls[0][0] == ("test::shared", "from-first")
    assert second_recorder.calls[0][0] == ("test::shared", "from-second")
    print("PASS: two holders sharing an event id keep an entry each")


def check_the_deadline_does_not_move() -> None:
    """Keep the launch-time deadline fixed when later events arrive.
    Rearming per offer lets a chatty source hold events for the process lifetime."""
    hold = BackendEventHold(label="deadline", bound_s=0.1)
    holder = _holder(hold, "test::deadline")
    hold.arm()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        holder.trigger_event("offer")
        time.sleep(0.01)

    assert not hold.armed, (
        "the window is still open a long way past its bound; a later event moved "
        "the deadline instead of leaving it where the launch set it"
    )
    print("PASS: a later event does not move the window's deadline")


def check_bound_expiry_drops_and_shuts() -> None:
    """The window closes on its own, drops what it holds and stops holding."""
    hold = BackendEventHold(label="expiry", bound_s=0.05)
    holder = _holder(hold, "test::expiry")
    hold.arm()
    holder.trigger_event("lost")

    assert _wait_until(lambda: not hold.armed), (
        "the window never closed on its own; the bound is not armed on a timer"
    )

    recorder = _Recorder()
    holder.add_listener(recorder)
    hold.release()
    assert not _wait_until(lambda: recorder.calls, timeout=0.5), (
        f"an event the expired window dropped was delivered anyway: {recorder.calls}"
    )

    # A trigger after the window shut goes out the normal way.
    holder.trigger_event("after")
    assert recorder.got.wait(10), "a trigger after the expiry was still held"
    assert recorder.calls[0][0] == ("test::expiry", "after")
    print("PASS: the bound closes the window, drops what it holds and stops holding")


def check_generation_guard_across_reconnect() -> None:
    """Discard data held by a superseded backend launch.
    Each test leg uses a new holder so every trigger reaches the hold."""
    hold = BackendEventHold(label="generation", bound_s=30.0)

    superseded = _holder(hold, "test::generation-superseded")
    superseded_recorder = _Recorder()
    hold.arm()
    superseded.trigger_event("first-attempt")
    hold.arm()  # the backend was launched again
    superseded.add_listener(superseded_recorder)
    hold.release()
    assert not _wait_until(lambda: superseded_recorder.calls, timeout=0.5), (
        f"a relaunch replayed the events of the failed connection: "
        f"{superseded_recorder.calls}"
    )

    # Relaunch must clear payloads; a generation check alone prevents delivery but pins data and hides the drop.
    pinned = _holder(hold, "test::generation-pinned")
    hold.arm()
    pinned.trigger_event("pinned")
    assert hold._held, "the trigger never reached the hold"
    hold.arm()
    assert not hold._held, (
        "a relaunch left the previous connection's entries in the hold"
    )

    fresh = _holder(hold, "test::generation-fresh")
    fresh_recorder = _Recorder()
    hold.arm()
    fresh.trigger_event("second-attempt")
    fresh.add_listener(fresh_recorder)
    hold.release()
    assert fresh_recorder.got.wait(10), "the new connection's held event was not delivered"
    assert fresh_recorder.calls[0][0] == ("test::generation-fresh", "second-attempt")

    # Entry generation blocks stale delivery when release races a relaunch, even while the window is open.
    stamped = _holder(hold, "test::generation-stamped")
    stamped_recorder = _Recorder()
    hold.arm()
    stamped.trigger_event("stamped")
    stamped.add_listener(stamped_recorder)
    hold._held.update({key: (stamp - 1, deliver)
                       for key, (stamp, deliver) in hold._held.items()})
    hold.release()
    assert not _wait_until(lambda: stamped_recorder.calls, timeout=0.5), (
        f"the release delivered an entry stamped with an older launch: "
        f"{stamped_recorder.calls}"
    )
    print("PASS: a relaunch drops the hold of the connection it superseded")


def check_cancel_drops_without_delivery() -> None:
    """A teardown shuts the window and delivers nothing."""
    hold = BackendEventHold(label="cancel", bound_s=30.0)
    holder = _holder(hold, "test::cancel")
    recorder = _Recorder()
    hold.arm()
    holder.trigger_event("orphan")
    holder.add_listener(recorder)

    hold.cancel()
    assert not hold.armed, "cancel left the window open"
    assert not _wait_until(lambda: recorder.calls, timeout=0.5), (
        f"cancel delivered what it was asked to drop: {recorder.calls}"
    )
    holder.trigger_event("after")
    assert recorder.got.wait(10), "a trigger after the cancel was still held"
    print("PASS: a teardown shuts the window and drops what it holds")


def check_holder_without_a_plugin_still_dispatches() -> None:
    """An EventHolder built without a plugin keeps dispatching."""
    holder = EventHolder(plugin_base=None, event_id="test::no-plugin")
    recorder = _Recorder()
    holder.add_listener(recorder)
    holder.trigger_event("value")
    assert recorder.got.wait(10), "a holder without a plugin stopped dispatching"

    try:
        EventHolder(plugin_base=None, event_id_suffix="Suffix")
    except ValueError:
        pass
    else:
        raise AssertionError("a suffix without a plugin must be refused, not crash later")
    print("PASS: a holder without a plugin dispatches, and the suffix form is refused")


def _wiring_plugin(hold: BackendEventHold) -> PluginBase:
    """A PluginBase with the launch state alone. __init__ needs a real plugin
    directory that this contract never touches."""
    plugin = PluginBase.__new__(PluginBase)
    # backend_event_hold is a read-only property over this slot, so that a
    # plugin whose __init__ skipped super() still gets a hold.
    plugin._backend_event_hold = hold
    plugin.server = types.SimpleNamespace(port=1)
    plugin.backend = None
    plugin.backend_connection = None
    plugin.backend_process = None
    plugin._backend_launch_gen = 0
    plugin._backend_stop_requested = False
    plugin._backend_via_terminal = False
    plugin._backend_ready = threading.Event()
    return plugin


def check_hold_exists_without_a_full_init() -> None:
    """Provide one shared hold when a plugin skips PluginBase.__init__.
    Launch, registration, teardown, and event holders all access this property."""
    plugin = PluginBase.__new__(PluginBase)
    hold = plugin.backend_event_hold
    assert hold is not None, "a plugin that skipped __init__ has no event hold"
    assert plugin.backend_event_hold is hold, "each read built another hold"

    # Concurrent first reads must settle on one hold. Two would leave the
    # armed one and the consulted one different, and the hold inert.
    other = PluginBase.__new__(PluginBase)
    seen: list = []
    barrier = threading.Barrier(4)

    def grab() -> None:
        barrier.wait(30)
        seen.append(other.backend_event_hold)

    threads = [threading.Thread(target=grab, name=f"hold_race_{i}", daemon=True)
               for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(seen) == 4 and len({id(entry) for entry in seen}) == 1, (
        f"concurrent first reads built more than one hold: {len({id(e) for e in seen})}"
    )

    # Re-read the slot under lock because another thread can initialize it while this thread waits.
    contended = PluginBase.__new__(PluginBase)
    winner = BackendEventHold(label="winner")

    class _RacingLock:
        def __enter__(self):
            contended._backend_event_hold = winner

        def __exit__(self, *exc_info):
            return False

    real_lock = PluginBase._backend_event_hold_lock
    PluginBase._backend_event_hold_lock = _RacingLock()
    try:
        assert contended.backend_event_hold is winner, (
            "the build under the lock did not re-read the slot, so a thread "
            "that waited for the lock builds a second hold; the armed one and "
            "the consulted one then differ and the hold does nothing"
        )
    finally:
        PluginBase._backend_event_hold_lock = real_lock

    # The teardown of such a plugin must not raise either.
    other.server = None
    other.backend_connection = None
    other.backend_process = None
    other.on_disconnect(None)
    print("PASS: a plugin that skipped __init__ still has one shared event hold")


class _LaunchStop(Exception):
    """Ends launch_backend at the spawn, after the wiring under test ran."""


def check_launch_backend_opens_the_window() -> None:
    hold = BackendEventHold(label="wiring-arm", bound_s=30.0)
    plugin = _wiring_plugin(hold)
    real_subprocess = plugin_base_module.subprocess

    def _refuse(*args, **kwargs):
        raise _LaunchStop()

    plugin_base_module.subprocess = types.SimpleNamespace(Popen=_refuse)
    try:
        plugin.launch_backend(fixtures.__file__)
    except _LaunchStop:
        pass
    finally:
        plugin_base_module.subprocess = real_subprocess

    assert hold.armed, "launch_backend did not open the event hold window"
    print("PASS: launch_backend opens the hold window before it spawns the backend")


def check_register_backend_closes_the_window() -> None:
    hold = BackendEventHold(label="wiring-release", bound_s=30.0)
    plugin = _wiring_plugin(hold)
    holder = EventHolder(plugin_base=plugin, event_id="test::wiring")
    recorder = _Recorder()

    hold.arm()
    holder.trigger_event("during-connect")
    holder.add_listener(recorder)

    real_verify = plugin_manager_module.verify_backend_port
    real_rpyc = plugin_base_module.rpyc
    plugin_manager_module.verify_backend_port = lambda *args, **kwargs: "127.0.0.1"
    plugin_base_module.rpyc = types.SimpleNamespace(
        connect=lambda *args, **kwargs: types.SimpleNamespace(root=object()))
    try:
        plugin.register_backend(port=1)
    finally:
        plugin_manager_module.verify_backend_port = real_verify
        plugin_base_module.rpyc = real_rpyc

    assert not hold.armed, "register_backend did not close the hold window"
    assert recorder.got.wait(10), (
        "register_backend closed the window without delivering what it held"
    )
    assert recorder.calls[0][0] == ("test::wiring", "during-connect")
    print("PASS: register_backend closes the hold window and delivers what it held")


def check_release_happens_before_the_plugin_hook() -> None:
    """Close the window before on_backend_ready runs.
    Events fired while the hook synchronizes backend state must dispatch immediately."""
    hold = BackendEventHold(label="hook-order", bound_s=30.0)
    seen: list = []

    class _HookPlugin(PluginBase):
        def on_backend_ready(self) -> None:
            seen.append(self.backend_event_hold.armed)

    plugin = _HookPlugin.__new__(_HookPlugin)
    plugin._backend_event_hold = hold
    plugin.server = types.SimpleNamespace(port=1)
    plugin.backend = None
    plugin.backend_connection = None
    plugin.backend_process = None
    plugin._backend_launch_gen = 0
    plugin._backend_stop_requested = False
    plugin._backend_via_terminal = False
    plugin._backend_ready = threading.Event()

    hold.arm()
    real_verify = plugin_manager_module.verify_backend_port
    real_rpyc = plugin_base_module.rpyc
    plugin_manager_module.verify_backend_port = lambda *args, **kwargs: "127.0.0.1"
    plugin_base_module.rpyc = types.SimpleNamespace(
        connect=lambda *args, **kwargs: types.SimpleNamespace(root=object()))
    try:
        plugin.register_backend(port=1)
    finally:
        plugin_manager_module.verify_backend_port = real_verify
        plugin_base_module.rpyc = real_rpyc

    assert seen == [False], (
        f"on_backend_ready ran while the hold window was still open: {seen}"
    )
    print("PASS: the hold window is shut before the plugin's backend hook runs")


def check_teardown_closes_the_window() -> None:
    hold = BackendEventHold(label="wiring-cancel", bound_s=30.0)
    plugin = _wiring_plugin(hold)
    holder = EventHolder(plugin_base=plugin, event_id="test::teardown")
    recorder = _Recorder()

    hold.arm()
    holder.trigger_event("orphan")
    holder.add_listener(recorder)

    # Close the window before the early return for a backend with no server, connection, or process.
    plugin.server = None
    plugin.backend_connection = None
    plugin.backend_process = None
    plugin.on_disconnect(None)
    assert not hold.armed, "the teardown left the hold window open"
    assert not _wait_until(lambda: recorder.calls, timeout=0.5), (
        f"the teardown delivered what it was asked to drop: {recorder.calls}"
    )
    print("PASS: the backend teardown closes the hold window")


def main() -> None:
    # Below the per-scenario timeout of run_all.py, so a stall reports here
    # with a message instead of an opaque runner timeout.
    fixtures.start_watchdog(90, label="scenario_backend_event_hold")

    check_held_then_delivered_on_connect()
    check_observed_event_is_not_delayed()
    check_coalescing_and_multiple_ids()
    check_a_live_dispatch_supersedes_the_held_value()
    check_two_holders_sharing_an_event_id()
    check_the_deadline_does_not_move()
    check_bound_expiry_drops_and_shuts()
    check_generation_guard_across_reconnect()
    check_cancel_drops_without_delivery()
    check_holder_without_a_plugin_still_dispatches()
    check_hold_exists_without_a_full_init()
    check_launch_backend_opens_the_window()
    check_register_backend_closes_the_window()
    check_release_happens_before_the_plugin_hook()
    check_teardown_closes_the_window()

    print("PASS: scenario_backend_event_hold")


if __name__ == "__main__":
    main()
