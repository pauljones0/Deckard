"""
Regression scenario. Tray icon registration must not be one-shot.

A watcher that appears late must still receive the item, and the SNI spec
requires an item to re-register with a watcher that restarted. The tray item
and its menu must also take the D-Bus object paths their names state.
"""

# This scenario runs an isolated session bus, registers the tray icon with no
# watcher present, then starts one, kills it, and starts a fresh one.
import fixtures  # noqa: F401  (must be imported first: isolates DATA_PATH)

import gc
import os
import sys
import time
import traceback

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib

import globals as gl
from src.backend.trayicon import DBusTrayIcon, DBusMenu


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

WATCHER_NAME = "org.kde.StatusNotifierWatcher"

WATCHER_NODE_INFO = Gio.DBusNodeInfo.new_for_xml("""
<?xml version="1.0" encoding="UTF-8"?>
<node>
    <interface name="org.kde.StatusNotifierWatcher">
        <method name="RegisterStatusNotifierItem">
            <arg type="s" direction="in"/>
        </method>
    </interface>
</node>""")


class FakeWatcher:
    """A minimal StatusNotifierWatcher on its own bus connection, so
    closing the connection mimics the hosting shell crashing."""

    def __init__(self, bus_address: str):
        self.registrations: list[str] = []
        self._name_acquired = False
        self.connection = Gio.DBusConnection.new_for_address_sync(
            bus_address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        self.connection.register_object(
            object_path="/StatusNotifierWatcher",
            interface_info=WATCHER_NODE_INFO.interfaces[0],
            method_call_closure=self._on_method_call,
        )
        self._own_id = Gio.bus_own_name_on_connection(
            self.connection,
            WATCHER_NAME,
            Gio.BusNameOwnerFlags.NONE,
            self._on_name_acquired,
            None,
        )

    def _on_name_acquired(self, connection, name):
        self._name_acquired = True

    def _on_method_call(self, _connection, _sender, _path, _interface_name,
                        method_name, parameters, invocation):
        if method_name == "RegisterStatusNotifierItem":
            self.registrations.append(parameters.unpack()[0])
        invocation.return_value(None)

    def wait_until_owning_name(self, timeout: float = 10.0) -> None:
        pump_until(lambda: self._name_acquired, timeout,
                   "fake watcher never acquired the well-known name")

    def crash(self) -> None:
        """Drop the well-known name the hard way. Close the connection,
        like a crashing shell would."""
        self.connection.close_sync(None)


def pump_until(condition, timeout: float, what: str) -> None:
    """Iterate the default GLib main context until condition() or a timeout."""
    context = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        while context.iteration(False):
            pass
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out after {timeout}s: {what}")


class _StubBus:
    """Counts object registrations so a double-register leak (a registration
    that is never unregistered) is observable without a real D-Bus daemon."""

    def __init__(self):
        self.registered: list[int] = []
        self.unregistered: list[int] = []
        self._next_id = 1

    def register_object(self, object_path, interface_info,
                        method_call_closure, get_property_closure):
        reg_id = self._next_id
        self._next_id += 1
        self.registered.append(reg_id)
        return reg_id

    def unregister_object(self, reg_id):
        self.unregistered.append(reg_id)

    def emit_signal(self, **kwargs):
        # DBusMenuService.set_items calls LayoutUpdate, which emits a signal
        # at construction. The double-register accounting does not read it.
        pass

    @property
    def live(self) -> set:
        return set(self.registered) - set(self.unregistered)


class _StubInterfaceInfo:
    def cache_build(self):
        pass

    def cache_release(self):
        pass


def check_base_double_register_no_orphan() -> None:
    """DBusService.register with no intervening unregister must not orphan
    the previous object registration on the connection. Exactly one live
    registration survives any number of register calls, and none survives an
    unregister."""
    from src.backend.trayicon import DBusService

    bus = _StubBus()
    service = DBusService(_StubInterfaceInfo(), "/StubPath", bus)
    service.register()
    service.register()  # double-register with no stop()

    assert len(bus.live) == 1, (
        f"double register() leaked object registrations: registered "
        f"{bus.registered}, unregistered {bus.unregistered} -- "
        f"{len(bus.live)} left live (expected 1)"
    )
    service.unregister()
    assert not bus.live, f"unregister() left registrations live: {bus.live}"
    print("PASS: base DBusService double register() keeps exactly one live "
          "registration")


def check_sni_double_register_keeps_menu_live() -> None:
    """A second register() over the real TrayIcon path must keep both objects.

    StatusNotifierItemService.register registers the SNI object and a nested
    menu object, and its unregister cascades to the menu. Both registration
    ids must stay live across a second register, with nothing orphaned.
    """
    import src.backend.trayicon as trayicon_mod
    from src.backend.trayicon import StatusNotifierItemService

    # An unregister-then-reregister remedy inside the base register()
    # dispatches virtually to the SNI unregister override, which tears the
    # menu down, and the base then re-registers the SNI object alone. That
    # leaves the tray menu dead on the bus, which is what this check fails on.
    # register() also watches org.kde.StatusNotifierWatcher through
    # Gio.bus_watch_name_on_connection, which type-checks its first argument
    # against a real connection. That name-watch is orthogonal to the
    # object-registration leak, so it is stubbed out.
    orig_watch = trayicon_mod.Gio.bus_watch_name_on_connection
    orig_unwatch = trayicon_mod.Gio.bus_unwatch_name
    trayicon_mod.Gio.bus_watch_name_on_connection = (
        lambda *a, **k: 12345  # a plausible watch id; never a real watch
    )
    trayicon_mod.Gio.bus_unwatch_name = lambda *a, **k: None
    try:
        bus = _StubBus()
        sni = StatusNotifierItemService(session_bus=bus, menu_items=[])

        sni.register()                       # TrayIcon.initialize()
        sni_id = sni.registration_id
        menu_id = sni._menu.registration_id
        assert sni_id is not None, "SNI object failed to register"
        assert menu_id is not None, "menu object failed to register"

        sni.register()                       # Settings-panel start(), no stop()

        assert sni.registration_id is not None, (
            "SNI object registration lost after double register()"
        )
        assert sni._menu.registration_id is not None, (
            "double register() left the tray MENU object unregistered "
            f"(menu.registration_id={sni._menu.registration_id!r}); the base "
            "register() must not tear the menu down via the SNI unregister() "
            "override -- that cascades self._menu.unregister() and the base "
            "only re-registers the SNI object, leaving the menu dead. "
            f"registered={bus.registered} unregistered={bus.unregistered}"
        )
        # Exactly two live registrations. The SNI item + its menu, no orphans.
        assert len(bus.live) == 2, (
            f"double register() must keep exactly the SNI + menu objects live "
            f"(no leak, no teardown): registered={bus.registered}, "
            f"unregistered={bus.unregistered}, live={bus.live} (expected 2)"
        )
        # A double register changes nothing. Each object keeps its original
        # registration id, so nothing was unregistered and re-registered,
        # which would churn the id and kill the menu on this path.
        assert sni.registration_id == sni_id, (
            f"SNI object id churned on double register(): {sni_id} -> "
            f"{sni.registration_id}; register() must be a no-op when already "
            "registered, not unregister-then-reregister"
        )
        assert sni._menu.registration_id == menu_id, (
            f"menu object id churned on double register(): {menu_id} -> "
            f"{sni._menu.registration_id}"
        )

        # A legitimate stop()/start() cycle must still re-register cleanly.
        sni.unregister()
        assert not bus.live, f"unregister() left registrations live: {bus.live}"
        sni.register()
        assert sni.registration_id is not None and sni._menu.registration_id is not None, (
            "stop()/start() cycle failed to re-register SNI + menu"
        )
        assert len(bus.live) == 2, (
            f"stop()/start() cycle leaked registrations: live={bus.live} "
            f"(expected 2)"
        )
        sni.unregister()
    finally:
        trayicon_mod.Gio.bus_watch_name_on_connection = orig_watch
        trayicon_mod.Gio.bus_unwatch_name = orig_unwatch
    print("PASS: StatusNotifierItemService double register() keeps both the "
          "SNI and menu objects live (no leak, no menu teardown)")


def check_item_and_menu_take_their_own_paths() -> None:
    """The tray item and its menu must take the paths their names state.

    The item registers at the item path and announces that path to the
    StatusNotifierWatcher. The menu registers at the menu path, which the
    item's Menu property carries. Passing the two the wrong way round keeps
    the pair consistent on the wire, because both readers follow the same
    two fields, and leaves every name in the code stating the opposite of
    what it holds.
    """
    from src.tray import TrayIcon

    tray = TrayIcon()
    sni = tray.sni_service

    assert TrayIcon.IndicatorPath != TrayIcon.MenuPath, (
        "the item and the menu must take separate object paths"
    )
    assert sni.dbus_path == TrayIcon.IndicatorPath, (
        f"the tray item took {sni.dbus_path!r}, which is the menu path; it "
        f"must take the item path {TrayIcon.IndicatorPath!r}. The two "
        "constructor arguments are the wrong way round."
    )
    assert sni.object_path == TrayIcon.IndicatorPath, (
        f"the tray item registered at {sni.object_path!r}, not at "
        f"{TrayIcon.IndicatorPath!r}"
    )
    assert sni._menu.dbus_path == TrayIcon.MenuPath, (
        f"the tray menu took {sni._menu.dbus_path!r}, not the menu path "
        f"{TrayIcon.MenuPath!r}"
    )
    assert sni._menu.object_path == TrayIcon.MenuPath, (
        f"the tray menu registered at {sni._menu.object_path!r}, not at "
        f"{TrayIcon.MenuPath!r}"
    )
    assert sni.Menu == TrayIcon.MenuPath, (
        f"the Menu property points at {sni.Menu!r}, not at the menu path "
        f"{TrayIcon.MenuPath!r}"
    )
    # The remaining two constructor arguments, pinned in the same order.
    assert sni.Id == TrayIcon.AppId, (
        f"the item id is {sni.Id!r}, not {TrayIcon.AppId!r}"
    )
    assert sni.Title == "Deckard", f"the item title is {sni.Title!r}"
    print("PASS: the tray item and its menu take their own D-Bus paths")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_tray_reregister")
    check_base_double_register_no_orphan()
    check_sni_double_register_keeps_menu_live()

    gl.MAIN_PATH = REPO_ROOT  # install root; the shipped icon dir sits under it

    test_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
    test_bus.up()  # also exports DBUS_SESSION_BUS_ADDRESS for bus_get_sync
    try:
        check_item_and_menu_take_their_own_paths()
        run_checks(test_bus.get_bus_address())
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        # The traceback holds the frames of the failing check, which hold the
        # TrayIcon, which holds the session bus. No collection can free that
        # while the traceback lives, so the teardown below would wait its
        # full 30 seconds and the failure would read as a timeout. Kill the
        # daemon, which takes no wait, and leave at once.
        test_bus.stop()
        os._exit(1)
    finally:
        # A TrayIcon holds its menu, whose items hold bound methods back to
        # the TrayIcon, so reference counting alone never drops the tray or
        # the session bus it holds. The bus below then waits 30 seconds for a
        # reference that a collection releases at once.
        gc.collect()
        test_bus.down()
    print("PASS: scenario_tray_reregister")


def run_checks(bus_address: str) -> None:
    menu = DBusMenu()
    menu.add_menu_item(1, "Quit", callback=lambda: None)
    tray = DBusTrayIcon(menu=menu, app_id="com.example.HarnessTray",
                        title="HarnessTray")

    # 1. A late watcher. Registering while no watcher exists must neither
    #    raise nor lose the icon, and the announcement must arrive as soon
    #    as a watcher shows up.
    try:
        tray.register()
    except Exception as e:
        raise AssertionError(
            f"register() must not fail when the StatusNotifierWatcher "
            f"is not (yet) on the bus: {e!r}"
        )

    watcher = FakeWatcher(bus_address)
    watcher.wait_until_owning_name()
    pump_until(lambda: len(watcher.registrations) >= 1, 10.0,
               "item was never announced to a late-appearing watcher")
    item_path = tray.sni_service.dbus_path
    assert watcher.registrations == [item_path], (
        f"expected the item's object path {item_path!r} to be announced, "
        f"got {watcher.registrations}"
    )

    # 2) Watcher restart. A fresh watcher instance knows nothing about
    #    the items an earlier watcher held, so the item re-announces itself.
    watcher.crash()
    reborn = FakeWatcher(bus_address)
    reborn.wait_until_owning_name()
    pump_until(lambda: len(reborn.registrations) >= 1, 10.0,
               "item was never re-announced after the watcher restarted")
    assert reborn.registrations == [item_path], (
        f"expected re-announcement of {item_path!r} to the restarted "
        f"watcher, got {reborn.registrations}"
    )

    # The tray can still be unregistered cleanly afterwards (Settings
    # toggle / app shutdown path).
    tray.unregister()


if __name__ == "__main__":
    main()
