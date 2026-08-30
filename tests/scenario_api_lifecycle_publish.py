"""Verify D-Bus deck publication follows the deck-manager lifecycle.
Every lifecycle leg reads over a separate bus connection."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import ctypes  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402

from gi.repository import Gio, GLib  # noqa: E402

import globals as gl  # noqa: E402

import src.api as api  # noqa: E402
import src.backend.DeckManagement.DeckManager as dm_module  # noqa: E402

from faulty_fake_deck import FaultyFakeDeck  # noqa: E402

WATCHDOG_SECONDS = 80

PROPS_IFACE = "org.freedesktop.DBus.Properties"

SERIAL_BOOT = "api-boot-1"
SERIAL_HOT = "api-hotplug-1"
SERIAL_REMOTE = "remote-deck-api-1"
SERIAL_LATE = "api-late-1"

# Hardcoded instead of run through _serial_to_dbus_path, so the sanitization a
# client needs to compose a path from a serial is pinned here too.
PATH_BOOT = f"{api.CONTROLLER_BASE_PATH}/api_boot_1"
PATH_HOT = f"{api.CONTROLLER_BASE_PATH}/api_hotplug_1"
PATH_REMOTE = f"{api.CONTROLLER_BASE_PATH}/remote_deck_api_1"
PATH_LATE = f"{api.CONTROLLER_BASE_PATH}/api_late_1"

PAGE = "Main"


# Stub environment-facing collaborators before constructing the real manager.
# The boot and lifecycle scenarios then drive the same manager code.

class StubUSBMonitor:
    """usbmonitor.USBMonitor stand-in with no udev and no threads."""

    def __init__(self, *args, **kwargs):
        self.on_connect = None
        self.on_disconnect = None

    def start_monitoring(self, on_connect=None, on_disconnect=None):
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

    def stop_monitoring(self, timeout=None):
        pass


class StubPortal:
    def running_under_flatpak(self):
        return False


class ScriptedDeviceManager:
    """Return the current class-level enumeration script."""

    results: list = []
    _lock = threading.Lock()

    def enumerate(self):
        with ScriptedDeviceManager._lock:
            return list(ScriptedDeviceManager.results)


BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


PR_SET_PDEATHSIG = 1
# Resolve before the fork, because the child runs _die_with_parent between
# fork and exec, where loading a library is a hazard.
_LIBC = ctypes.CDLL("libc.so.6", use_errno=True)


def _die_with_parent() -> None:
    """Kill the private bus if the harness exits without Python cleanup."""
    _LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)


def start_private_bus() -> tuple[subprocess.Popen, str]:
    """Start an owned bus; Gio.TestDBus otherwise waits its full 30 s timeout."""
    assert shutil.which("dbus-daemon") is not None, (
        "dbus-daemon is not on PATH, so this scenario cannot start an isolated "
        "session bus and would prove nothing about the DBus API. Install it "
        "(the 'dbus' package) and re-run -- it is a runtime dependency of the "
        "app itself, not just of this test."
    )
    config_path = os.path.join(gl.DATA_PATH, "harness-session-bus.conf")
    with open(config_path, "w") as f:
        f.write(BUS_CONFIG)
    proc = subprocess.Popen(
        ["dbus-daemon", "--nofork", "--print-address", "--config-file", config_path],
        stdout=subprocess.PIPE, text=True, preexec_fn=_die_with_parent,
    )
    address = (proc.stdout.readline() or "").strip()
    assert address, "dbus-daemon started without printing a bus address"
    # Read before any bus connection is made anywhere in this process.
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = address
    return proc, address


def stop_private_bus(proc: subprocess.Popen) -> None:
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    # GDBus ends the process when its bus connection closes unless told
    # otherwise, and the daemon is about to go away.
    connection.set_exit_on_close(False)
    proc.terminate()
    proc.wait(timeout=10)


def pump(seconds: float = 0.0) -> None:
    """Run queued publication work without a GLib main loop."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while True:
        while context.iteration(False):
            pass
        if time.monotonic() >= deadline:
            return
        time.sleep(0.005)


def pump_until(condition, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while True:
        pump(0.01)
        if condition():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s: {what}")


class Observer:
    """Observe the API through a connection separate from the publisher."""

    def __init__(self, bus_address: str, destination: str):
        self.connection = Gio.DBusConnection.new_for_address_sync(
            bus_address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        self.destination = destination
        self.property_changes: list[tuple[str, str, dict]] = []
        self.connection.signal_subscribe(
            None, PROPS_IFACE, "PropertiesChanged", None, None,
            Gio.DBusSignalFlags.NONE, self._on_properties_changed,
        )

    def _on_properties_changed(self, _connection, _sender, object_path,
                               _interface, _signal, parameters, *_user_data):
        interface, changed, _invalidated = parameters.unpack()
        self.property_changes.append((object_path, interface, changed))

    def controller_changes(self) -> list[list[str]]:
        """Every Controllers value the top-level object has announced."""
        return [
            changed["Controllers"]
            for path, interface, changed in self.property_changes
            if path == api.DBUS_OBJECT_PATH and interface == api.TOP_IFACE
            and "Controllers" in changed
        ]

    def call(self, object_path: str, interface: str, method: str,
             params=None, timeout: float = 5.0):
        """Call asynchronously and pump the service context to avoid deadlock."""
        box: dict = {}

        def on_done(source, result, *_user_data):
            try:
                box["value"] = source.call_finish(result)
            except GLib.Error as e:
                box["error"] = e

        self.connection.call(
            self.destination, object_path, interface, method, params, None,
            Gio.DBusCallFlags.NONE, int(timeout * 1000), None, on_done, None,
        )
        pump_until(lambda: bool(box), timeout + 5,
                   f"no reply to {interface}.{method} at {object_path}")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def get_property(self, object_path: str, interface: str, name: str):
        reply = self.call(object_path, PROPS_IFACE, "Get",
                          GLib.Variant("(ss)", (interface, name)))
        return reply.unpack()[0]

    def controllers(self) -> list[str]:
        return self.get_property(api.DBUS_OBJECT_PATH, api.TOP_IFACE,
                                 "Controllers")

    def published_paths(self) -> list[str]:
        """Return controller paths from GDBus's registration table."""
        xml = self.call(api.CONTROLLER_BASE_PATH,
                        "org.freedesktop.DBus.Introspectable",
                        "Introspect", None).unpack()[0]
        return sorted(f"{api.CONTROLLER_BASE_PATH}/{name}"
                      for name in re.findall(r'<node\s+name="([^"]+)"', xml))

    def active_page(self, object_path: str) -> str:
        return self.get_property(object_path, api.CTRL_IFACE, "ActivePageName")


def assert_agreement(observer: Observer, where: str) -> None:
    """Compare advertised serials with addressable objects over the wire.
    Compose each path with the application's serial sanitizer."""
    listed = sorted(api._serial_to_dbus_path(serial)
                    for serial in observer.controllers())
    published = sorted(os.path.basename(path)
                       for path in observer.published_paths())
    assert listed == published, (
        f"{where}: Controllers names {listed} while the bus carries objects "
        f"for {published} -- a client composing a path from this property "
        f"reaches something that is not there"
    )


def wait_for_object(observer: Observer, object_path: str,
                    timeout: float = 15.0) -> str:
    """Poll while pumping and return the controller's ActivePageName."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while True:
        try:
            return observer.active_page(object_path)
        except GLib.Error as e:
            last = e
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{object_path} never appeared on the bus within {timeout}s "
                f"(last reply: {last})"
            )
        pump(0.05)


def expect_gone(observer: Observer, object_path: str) -> str:
    """Assert the object stopped answering, and return the remote error."""
    try:
        observer.active_page(object_path)
    except GLib.Error as e:
        remote = Gio.DBusError.get_remote_error(e) or ""
        assert "Unknown" in remote or "unknown" in str(e), (
            f"{object_path} failed with {remote or e}, which is not the "
            f"'this object is not here' answer a client must get"
        )
        return remote or str(e)
    raise AssertionError(
        f"{object_path} still answers calls -- the object was never taken off "
        f"the bus, so a client keeps driving a controller that is gone"
    )


# Record registration threads because dasbus mutates an unlocked dictionary.
# Every mutation must share the GLib context that dispatches GDBus calls.

MAIN_IDENT = threading.main_thread().ident
BUS_CALLS: list[tuple[str, str, int]] = []


def record_bus_threads() -> None:
    """Wrap the live bus so every publish/unpublish records its thread."""
    bus = api._bus
    real_publish = bus.publish_object
    real_unpublish = bus.unpublish_object

    def publish_object(object_path, obj, *args, **kwargs):
        BUS_CALLS.append(("publish", object_path, threading.get_ident()))
        return real_publish(object_path, obj, *args, **kwargs)

    def unpublish_object(object_path):
        BUS_CALLS.append(("unpublish", object_path, threading.get_ident()))
        return real_unpublish(object_path)

    bus.publish_object = publish_object
    bus.unpublish_object = unpublish_object


def assert_on_main(op: str, object_path: str) -> None:
    calls = [c for c in BUS_CALLS if c[0] == op and c[1] == object_path]
    assert calls, f"nothing ever ran {op} for {object_path}"
    for _op, _path, ident in calls:
        assert ident == MAIN_IDENT, (
            f"{op} of {object_path} ran on thread {ident}, not the main "
            f"context ({MAIN_IDENT}) -- dasbus's registration bookkeeping is "
            f"unlocked and GDBus dispatches on that context, so this work is "
            f"racing the dispatch it is registering with"
        )


class Exploding:
    """Fail attribute access to verify the no-bus guard runs first."""

    def __getattr__(self, name):
        raise AssertionError(
            f"publish/unpublish dereferenced {name!r} before checking whether "
            f"a bus exists"
        )


def make_deck_manager() -> "dm_module.DeckManager":
    manager = dm_module.DeckManager()
    manager.BOOT_RESCAN_DELAYS = (0.1, 0.15, 0.2)
    return manager


def controller_for(manager, serial: str):
    for controller in manager.deck_controller:
        if controller.serial_number() == serial:
            return controller
    return None


def connect_deck_on_worker(manager, serial: str) -> None:
    """Register a hotplugged deck from a worker thread."""
    deck = FaultyFakeDeck(serial_number=serial, deck_type="Fake Deck")
    ScriptedDeviceManager.results = [deck]
    thread = threading.Thread(target=manager.connect_new_decks,
                              name=f"hotplug-{serial}")
    thread.start()
    pump_until(lambda: controller_for(manager, serial) is not None, 20.0,
               f"{serial} was never registered with the deck manager")
    thread.join(timeout=20)
    assert not thread.is_alive(), f"connect_new_decks for {serial} did not return"
    ScriptedDeviceManager.results = []


def leg_boot_sweep(manager, bus_address: str) -> Observer:
    """Publish decks that registered before the service started."""
    deck = FaultyFakeDeck(serial_number=SERIAL_BOOT, deck_type="Fake Deck")
    manager.load_hardware_deck(deck)
    assert controller_for(manager, SERIAL_BOOT) is not None, \
        "the pre-service deck never registered -- nothing below is meaningful"
    assert api.get_controller_instance(SERIAL_BOOT) is None, \
        "a controller was published with no service running"

    # Nothing to dereference and nothing queued, because the guard returns first.
    api.publish_controller(Exploding())
    api.unpublish_controller(Exploding())
    pump(0.05)

    api.start_dbus_service()
    assert api._bus is not None, "the DBus service did not start"
    record_bus_threads()

    observer = Observer(bus_address, api._bus.connection.get_unique_name())
    assert wait_for_object(observer, PATH_BOOT) == PAGE, \
        "the pre-service deck is on the bus but not showing its loaded page"
    assert observer.controllers() == [SERIAL_BOOT], \
        f"Controllers disagrees with the published objects: {observer.controllers()}"
    print("  PASS: the boot sweep publishes decks registered before the service")
    return observer


def leg_worker_thread_arrival(manager, observer: Observer) -> None:
    """Publish a runtime deck that arrives on a worker thread."""
    observer.property_changes.clear()
    connect_deck_on_worker(manager, SERIAL_HOT)

    assert wait_for_object(observer, PATH_HOT) == PAGE, (
        "a deck registered by a worker thread never reached the bus -- "
        "publishing is still the one-shot boot sweep"
    )
    pump_until(lambda: SERIAL_HOT in observer.controllers(), 10.0,
               "Controllers never listed the newly arrived deck")
    pump_until(
        lambda: any(SERIAL_HOT in value
                    for value in observer.controller_changes()),
        10.0,
        "no PropertiesChanged for Controllers reached the second connection "
        "when a deck arrived",
    )
    # Registered on the hotplug thread, published on the main context.
    assert_on_main("publish", PATH_HOT)
    print("  PASS: a deck arriving on a worker thread is published, on main")

    # The third registration site is remote decks. Same call, driven with a
    # stubbed remote manager, so the append path runs for real.
    remote_deck = FaultyFakeDeck(serial_number=SERIAL_REMOTE, deck_type="Fake Deck")
    remote_controller = manager._init_deck_controller_with_retry(remote_deck)
    assert remote_controller is not None, "the remote controller failed to build"
    manager.remote_deck_manager = types.SimpleNamespace(
        # start() answers whether the server bound; False registers nothing.
        start=lambda: True,
        deck_controllers=[remote_controller],
        stop=lambda: None,
    )
    manager.load_remote_decks()
    assert wait_for_object(observer, PATH_REMOTE) == PAGE, \
        "a remote deck registered at runtime never reached the bus"
    print("  PASS: a remote deck registered at runtime is published")


def leg_active_page_seeded(manager, observer: Observer) -> None:
    """Seed ActivePageName because boot selects a page before API publication."""
    for serial, path in ((SERIAL_BOOT, PATH_BOOT), (SERIAL_HOT, PATH_HOT)):
        controller = controller_for(manager, serial)
        assert controller.active_page is not None, \
            f"{serial} has no active page -- this leg would prove nothing"
        expected = controller.active_page.get_name()
        seen = observer.active_page(path)
        assert seen == expected, (
            f"{serial} reports ActivePageName {seen!r} over DBus while it is "
            f"showing {expected!r}"
        )
        assert seen != "", f"{serial} published with an empty ActivePageName"
    print("  PASS: ActivePageName is seeded from the controller at publish")


def leg_removal_unpublishes(manager, observer: Observer):
    """Unpublish a removed controller and return it for stale-call checks."""
    observer.property_changes.clear()
    controller = controller_for(manager, SERIAL_HOT)

    # Removal reaches DeckManager from the USB monitor thread, the Flatpak poll
    # thread and the media error paths, never from main alone.
    remover = threading.Thread(target=manager.remove_controller,
                               args=(controller,), name="unplug-sim")
    remover.start()
    remover.join(timeout=20)
    assert not remover.is_alive(), "remove_controller did not return"

    pump_until(lambda: api.get_controller_instance(SERIAL_HOT) is None, 10.0,
               "the removed deck's API object was never dropped")
    remote_error = expect_gone(observer, PATH_HOT)

    # Several threads race to remove the same controller, so a second removal,
    # and the unpublish behind it, must be a no-op rather than an error.
    manager.remove_controller(controller)
    api.unpublish_controller(controller)
    pump(0.2)

    controllers = observer.controllers()
    assert SERIAL_HOT not in controllers, \
        f"Controllers still lists the removed deck: {controllers}"
    assert SERIAL_BOOT in controllers, \
        f"removing one deck unpublished another: {controllers}"
    assert wait_for_object(observer, PATH_BOOT) == PAGE, \
        "the surviving deck's object stopped answering when another was removed"

    announced = observer.controller_changes()
    assert announced, ("no PropertiesChanged for Controllers reached the "
                       "second connection when a deck was removed")
    assert all(SERIAL_HOT not in value for value in announced), (
        f"the removal was announced with the removed deck still listed: "
        f"{announced}"
    )
    # Removed on the unplug thread, unpublished on the main context.
    assert_on_main("unpublish", PATH_HOT)
    print(f"  PASS: removal unpublishes the deck, on main ({remote_error})")
    return controller


def leg_replug(manager, observer: Observer, stale) -> None:
    """Rebind a replugged deck to a fresh object at its serial-derived path."""
    connect_deck_on_worker(manager, SERIAL_HOT)
    fresh = controller_for(manager, SERIAL_HOT)

    assert wait_for_object(observer, PATH_HOT) == PAGE, \
        "a replugged deck never came back on its object path"
    instance = api.get_controller_instance(SERIAL_HOT)
    assert instance is not None, "the replugged deck has no API instance"
    assert instance._controller is fresh, (
        "the replugged deck's object is still bound to the closed controller"
    )
    assert instance._object_path == PATH_HOT, (
        f"the replugged deck moved to {instance._object_path}, so clients that "
        f"cache paths by serial break"
    )

    observer.call(PATH_HOT, api.CTRL_IFACE, "SetActivePage",
                  GLib.Variant("(s)", (PAGE,)))

    # A stale unpublish must use controller identity and preserve the replug.
    api.unpublish_controller(stale)
    pump(0.2)
    assert wait_for_object(observer, PATH_HOT, timeout=5) == PAGE, (
        "a late unpublish for the PREVIOUS controller at this serial took the "
        "replugged deck's object off the bus"
    )
    print("  PASS: a replugged deck returns at the same path, freshly bound")


def leg_inverted_replug_order(manager, observer: Observer) -> None:
    """Preserve a replug whose publish runs before the stale unpublish.
    Registration and removal workers do not order their queued operations."""
    stale = controller_for(manager, SERIAL_HOT)
    assert api.get_controller_instance(SERIAL_HOT) is not None, \
        "nothing is published at this serial -- the race below cannot happen"

    manager.remove_controller(stale)  # queues an unpublish; left unpumped

    deck = FaultyFakeDeck(serial_number=SERIAL_HOT, deck_type="Fake Deck")
    fresh = manager._init_deck_controller_with_retry(deck)
    assert fresh is not None, "the replacement controller failed to build"
    manager.deck_controller.append(fresh)

    api._publish_on_main(fresh)
    api._unpublish_on_main(stale)
    pump(0.2)  # and the queued unpublish lands too

    assert wait_for_object(observer, PATH_HOT, timeout=5) == PAGE, (
        "the replugged deck is registered and working but has no object on "
        "the bus: its publish found the serial still claimed by the removed "
        "controller and gave up, and the unpublish behind it then dropped "
        "that claim -- nothing publishes the deck again for the rest of the "
        "session"
    )
    instance = api.get_controller_instance(SERIAL_HOT)
    assert instance is not None and instance._controller is fresh, \
        "the surviving object is not the fresh controller's"
    assert SERIAL_HOT in observer.controllers(), \
        "Controllers lost the replugged deck"
    print("  PASS: a publish queued before the old deck's unpublish survives it")


def serials_registered(manager) -> list[str]:
    """The serials the deck manager holds."""
    return [controller.serial_number() for controller in manager.deck_controller]


def leg_publish_lag_direction(manager, observer: Observer) -> None:
    """Make Controllers follow published objects rather than manager contents.
    Main-context marshaling creates both publication and removal lag windows."""
    deck = FaultyFakeDeck(serial_number=SERIAL_LATE, deck_type="Fake Deck")
    controller = manager._init_deck_controller_with_retry(deck)
    assert controller is not None, "the late controller failed to build"
    manager.deck_controller.append(controller)

    observer.property_changes.clear()
    api.publish_controller(controller)  # queued, and left unpumped

    top = api.get_api_instance()
    # Assert the absence in-process. A read over the bus pumps this context and
    # would dispatch the very publish the window is made of.
    assert SERIAL_LATE in serials_registered(manager), \
        "the deck never registered, so there is no disagreement to have"
    assert api.get_controller_instance(SERIAL_LATE) is None, \
        "the publish ran anyway -- this leg needs the queued, undispatched one"
    assert SERIAL_LATE not in top.Controllers, (
        f"the property named a deck with no object on the bus: read from the "
        f"deck manager it announces decks a client cannot address, which is "
        f"the one direction this must never fail in ({top.Controllers})"
    )

    pump_until(lambda: api.get_controller_instance(SERIAL_LATE) is not None,
               10.0, "the queued publish never ran")
    assert SERIAL_LATE in top.Controllers, \
        "the deck is published and the property still does not name it"
    assert wait_for_object(observer, PATH_LATE) == PAGE, \
        "the late deck's object never answered"
    assert_agreement(observer, "after a queued publish dispatched")

    healed = [value for value in observer.controller_changes()
              if SERIAL_LATE in value]
    assert healed, (
        "nothing announced the new deck, so a client that read the property "
        "in the window above has no way to learn it was wrong except by "
        "polling -- the signal is what closes a window that cannot be ordered "
        "away"
    )

    # During removal lag, Controllers must keep naming the still-addressable
    # object until the queued unpublish runs.
    observer.property_changes.clear()
    manager.remove_controller(controller)  # queues the unpublish
    assert SERIAL_LATE not in serials_registered(manager), \
        "the removal did not happen, so there is no window to be in"
    assert SERIAL_LATE in top.Controllers, (
        f"the property dropped a deck whose object is still on the bus: read "
        f"from the deck manager it stops naming a deck a client can still "
        f"drive ({top.Controllers})"
    )

    pump_until(lambda: api.get_controller_instance(SERIAL_LATE) is None, 10.0,
               "the queued unpublish never ran")
    expect_gone(observer, PATH_LATE)
    assert_agreement(observer, "after a queued unpublish dispatched")
    print("  PASS: the property lags the decks, never the objects")


def leg_stop_service(manager, observer: Observer) -> None:
    """Unpublish all objects on stop and ignore later lifecycle calls."""
    # Dispatch queued work after stop on the same serialized main context.
    # The worker must find no bus and return quietly.
    survivor = controller_for(manager, SERIAL_BOOT)
    api.unpublish_controller(survivor)
    assert api.get_api_instance() is not None, \
        "the top-level API instance was missing before stop -- this leg would prove nothing"
    api.stop_dbus_service()
    assert api._bus is None, "stop_dbus_service left the bus in place"
    assert api.get_api_instance() is None, \
        "stop_dbus_service left a stale top-level API instance behind"
    pump(0.2)

    expect_gone(observer, PATH_BOOT)
    expect_gone(observer, PATH_HOT)
    assert api.get_controller_instance(SERIAL_BOOT) is None, \
        "a published instance survived the service stopping"

    # Late arrivals and removals find no bus, so nothing to dereference and
    # nothing to publish.
    api.publish_controller(survivor)
    api.unpublish_controller(survivor)
    api.publish_controller(Exploding())
    api.unpublish_controller(Exploding())
    pump(0.2)
    assert api.get_controller_instance(SERIAL_BOOT) is None, (
        "publish_controller published against a stopped service"
    )

    # Whole-run sweep. No registration mutation ran off the main context.
    off_main = [c for c in BUS_CALLS if c[2] != MAIN_IDENT]
    assert not off_main, (
        f"registrations were mutated off the main context: {off_main}"
    )
    print("  PASS: stopping unpublishes everything; later calls are no-ops")


def leg_failed_publish_commits_nothing() -> None:
    """Leave both API globals unset when initial object publication fails."""
    assert api._bus is None and api.get_api_instance() is None

    real_bus = api.SessionMessageBus
    real_publish_object = real_bus.publish_object

    def exploding_publish(self, *args, **kwargs):
        raise RuntimeError("simulated publish failure")

    real_bus.publish_object = exploding_publish
    try:
        api.start_dbus_service()
    finally:
        real_bus.publish_object = real_publish_object

    assert api._bus is None, "a failed publish left a half-open bus in _bus"
    assert api.get_api_instance() is None, \
        "a failed publish left a stale top-level API instance behind"
    print("  PASS: a failed publish commits neither global")


def run_legs(bus_address: str) -> None:
    gl.deck_manager = manager = make_deck_manager()
    observer = None
    try:
        observer = leg_boot_sweep(manager, bus_address)
        assert_agreement(observer, "after the boot sweep")
        leg_worker_thread_arrival(manager, observer)
        assert_agreement(observer, "after a deck arrived on a worker thread")
        leg_active_page_seeded(manager, observer)
        stale = leg_removal_unpublishes(manager, observer)
        assert_agreement(observer, "after a deck was removed")
        leg_replug(manager, observer, stale)
        assert_agreement(observer, "after a replug")
        leg_inverted_replug_order(manager, observer)
        assert_agreement(observer, "after a publish and unpublish crossed")
        leg_publish_lag_direction(manager, observer)
        leg_stop_service(manager, observer)
        leg_failed_publish_commits_nothing()
    finally:
        api.stop_dbus_service()
        for controller in list(manager.deck_controller):
            fixtures.teardown(controller)
        if observer is not None:
            observer.connection.close_sync(None)


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, "scenario_api_lifecycle_publish")
    fixtures._install_integration_globals()
    fixtures.seed_page(PAGE)

    dm_module.USBMonitor = StubUSBMonitor
    dm_module.Xdp = types.SimpleNamespace(
        Portal=types.SimpleNamespace(new=lambda: StubPortal()))
    dm_module.DeviceManager = ScriptedDeviceManager

    bus_proc, bus_address = start_private_bus()
    try:
        run_legs(bus_address)
    finally:
        stop_private_bus(bus_proc)

    print("PASS: scenario_api_lifecycle_publish")


if __name__ == "__main__":
    main()
