"""Late and restarted StatusNotifierWatcher checks."""

import time

from gi.repository import Gio, GLib

from src.backend.trayicon import DBusMenu, DBusTrayIcon

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


def pump_until(condition, timeout: float, what: str) -> None:
    context = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        while context.iteration(False):
            pass
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out after {timeout}s: {what}")


class FakeWatcher:
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
        pump_until(
            lambda: self._name_acquired,
            timeout,
            "fake watcher never acquired the well-known name",
        )

    def crash(self) -> None:
        self.connection.close_sync(None)


def _bus_id(connection: Gio.DBusConnection) -> str:
    result = connection.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "GetId",
        None,
        GLib.VariantType.new("(s)"),
        Gio.DBusCallFlags.NONE,
        5_000,
        None,
    )
    assert result is not None, "D-Bus GetId returned no result"
    return result.unpack()[0]


def run_watcher_checks(bus_address: str) -> None:
    menu = DBusMenu()
    menu.add_menu_item(1, "Quit", callback=lambda: None)
    tray = DBusTrayIcon(
        menu=menu,
        app_id="com.example.HarnessTray",
        title="HarnessTray",
    )

    try:
        tray.register()
    except Exception as error:
        raise AssertionError(
            "register() must not fail without a StatusNotifierWatcher: "
            f"{error!r}"
        ) from error

    watcher = FakeWatcher(bus_address)
    tray_bus_id = _bus_id(tray.sni_service.bus)
    watcher_bus_id = _bus_id(watcher.connection)
    assert tray_bus_id == watcher_bus_id, (
        "tray and fake watcher connected to different D-Bus daemons: "
        f"tray={tray_bus_id}, watcher={watcher_bus_id}"
    )
    watcher.wait_until_owning_name()
    pump_until(
        lambda: len(watcher.registrations) >= 1,
        10.0,
        "item was never announced to a late-appearing watcher",
    )
    item_path = tray.sni_service.dbus_path
    assert watcher.registrations == [item_path], watcher.registrations

    watcher.crash()
    reborn = FakeWatcher(bus_address)
    reborn.wait_until_owning_name()
    pump_until(
        lambda: len(reborn.registrations) >= 1,
        10.0,
        "item was never re-announced after the watcher restarted",
    )
    assert reborn.registrations == [item_path], reborn.registrations
    tray.unregister()
