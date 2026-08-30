"""Verify late and restarted tray watchers, idempotent D-Bus registration,
separate object paths, and valid host-aware icon-theme discovery."""

# This scenario runs an isolated session bus, registers the tray icon with no
# watcher present, then starts one, kills it, and starts a fresh one.
import fixtures  # noqa: F401  (must be imported first: isolates DATA_PATH)

import configparser
import gc
import os
import re
import sys
import tempfile
import time
import traceback

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

import appinfo
import globals as gl
from src.backend.trayicon import DBusTrayIcon, DBusMenu


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BUNDLED_ICON_DIR = os.path.join(REPO_ROOT, "Assets", "icons")
ICON_THEME_DIR = os.path.join(BUNDLED_ICON_DIR, "hicolor")
FLATPAK_MANIFEST = os.path.join(REPO_ROOT, f"{appinfo.APP_ID}.yml")
ICON_SUFFIXES = (".png", ".svg", ".xpm")

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
    """Repeated DBusService registration keeps one live object and no orphan."""
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
    """Repeated SNI registration keeps the item and nested menu ids live."""
    import src.backend.trayicon as trayicon_mod
    from src.backend.trayicon import StatusNotifierItemService

    # Stub the real-connection name watch to isolate object registration.
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
        # Both objects must retain their registration ids.
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


def shipped_icon_dirs() -> "set[str]":
    """Every dir under the shipped hicolor theme that holds an icon file,
    named the way an index.theme names it."""
    found = set()
    for dirpath, _dirnames, filenames in os.walk(ICON_THEME_DIR):
        if any(name.endswith(ICON_SUFFIXES) for name in filenames):
            found.add(os.path.relpath(dirpath, ICON_THEME_DIR))
    return found


def check_shipped_icon_theme_resolves() -> None:
    """The shipped theme has a valid index and lists every icon directory."""
    index_path = os.path.join(ICON_THEME_DIR, "index.theme")
    assert os.path.isfile(index_path), (
        f"{index_path} is missing; a shell whose icon loader takes the icon "
        "theme spec strictly reads no theme from a dir without it, and draws "
        "a placeholder instead of the app icon"
    )

    parser = configparser.ConfigParser()
    parser.optionxform = str  # keep the key case the icon theme spec uses
    parser.read(index_path, encoding="utf-8")

    assert parser.has_section("Icon Theme"), (
        f"{index_path} has no [Icon Theme] section, so it names no theme"
    )
    assert parser.get("Icon Theme", "Name", fallback="") != "", (
        f"{index_path} has no Name, which every theme needs"
    )

    listed = [entry.strip() for entry
              in parser.get("Icon Theme", "Directories", fallback="").split(",")
              if entry.strip() != ""]
    assert listed, f"{index_path} lists no Directories"

    shipped = shipped_icon_dirs()
    missing = sorted(shipped - set(listed))
    assert not missing, (
        f"{index_path} does not list {missing}, and a desktop shell that "
        "reads this theme cannot see an icon in a dir the theme leaves out. "
        "Add each dir to Directories and give it its own group."
    )

    for entry in listed:
        entry_path = os.path.join(ICON_THEME_DIR, *entry.split("/"))
        assert os.path.isdir(entry_path), (
            f"{index_path} lists {entry!r}, which is not a dir on disk"
        )
        assert parser.has_section(entry), (
            f"{index_path} lists {entry!r} in Directories with no [{entry}] "
            "group, so the theme states no size for it"
        )
        for key in ("Size", "Type", "Context"):
            assert parser.get(entry, key, fallback="") != "", (
                f"[{entry}] in {index_path} has no {key}"
            )
    # The application id must resolve through the shipped theme.
    theme = Gtk.IconTheme.new()
    theme.set_search_path([BUNDLED_ICON_DIR])
    theme.set_theme_name("hicolor")
    assert theme.has_icon(appinfo.APP_ID), (
        f"{appinfo.APP_ID} does not resolve in the theme under "
        f"{BUNDLED_ICON_DIR}; that is the name the tray gives the desktop "
        "shell, so a copy run from a source tree would hand over a dir that "
        "holds no icon under it"
    )
    print(f"PASS: the shipped icon theme resolves {appinfo.APP_ID} and lists "
          f"all {len(shipped)} icon dirs")


def check_flatpak_manifest_installs_the_app_icon() -> None:
    """The Flatpak manifest installs the app icon under /app/share icons."""
    with open(FLATPAK_MANIFEST, encoding="utf-8") as f:
        manifest = f.read()
    pattern = (r"/app/share/icons/hicolor/[^/\s]+/apps/"
               + re.escape(appinfo.APP_ID)
               + r"\.(?:png|svg|xpm)\b")
    assert re.search(pattern, manifest), (
        f"{FLATPAK_MANIFEST} installs no {appinfo.APP_ID} icon under "
        "/app/share/icons/hicolor/<size>/apps/. The tray reads the sandbox "
        "data dirs to decide it needs no icon theme path, so without that "
        "install a sandboxed copy hands the desktop shell a sandbox path it "
        "cannot read, and the tray shows a placeholder."
    )
    print("PASS: the flatpak manifest installs the app icon where the icon "
          "search reads it")


def check_item_and_menu_take_their_own_paths() -> None:
    """The tray item registers and announces its item path while Menu names the
    separate menu object path."""
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


def check_icon_theme_path_only_when_the_host_lacks_the_icon() -> None:
    """Use the bundled theme path only when host icon roots lack the app icon."""
    import appinfo
    from src.tray import TrayIcon, icon_search_roots, tray_icon_theme_path

    bundled = os.path.join(REPO_ROOT, "Assets", "icons")
    saved = {name: os.environ.get(name)
             for name in ("XDG_DATA_HOME", "XDG_DATA_DIRS")}

    def restore() -> None:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    try:
        # Unset XDG variables must retain the standard system roots.
        os.environ.pop("XDG_DATA_HOME", None)
        os.environ.pop("XDG_DATA_DIRS", None)
        assert "/usr/share" in icon_search_roots(), (
            f"an unset XDG_DATA_DIRS must fall back to the system dirs, got "
            f"{icon_search_roots()}"
        )

        # A relative entry names a dir under wherever the app was started.
        # The spec drops such an entry, and so must the search.
        os.environ["XDG_DATA_DIRS"] = os.pathsep.join(("share", "", "/usr/share"))
        assert icon_search_roots()[1:] == ["/usr/share"], (
            f"a relative or empty data dir must be dropped, got "
            f"{icon_search_roots()}"
        )
        os.environ.pop("XDG_DATA_DIRS", None)

        with tempfile.TemporaryDirectory(prefix="sc_tray_xdg_") as root:
            data_home = os.path.join(root, "home")
            data_dir = os.path.join(root, "system")
            os.makedirs(data_home)
            os.makedirs(data_dir)
            os.environ["XDG_DATA_HOME"] = data_home
            os.environ["XDG_DATA_DIRS"] = data_dir

            # 1. Nothing installed: the shell gets the icons that ship with
            #    the app.
            assert tray_icon_theme_path(appinfo.APP_ID, REPO_ROOT) == bundled, (
                "a copy whose icon the host theme does not hold must name the "
                f"icon dir that ships with it ({bundled})"
            )
            tray = TrayIcon()
            assert tray.sni_service.IconThemePath == bundled, (
                f"the tray handed the shell "
                f"{tray.sni_service.IconThemePath!r}, not {bundled!r}"
            )
            assert tray.sni_service.IconName == appinfo.APP_ID, (
                f"the tray names icon {tray.sni_service.IconName!r}"
            )

            # 2. Installed under a system data dir: the shell keeps its own
            #    theme.
            installed = os.path.join(data_dir, "icons", "hicolor", "256x256", "apps")
            os.makedirs(installed)
            icon_file = os.path.join(installed, f"{appinfo.APP_ID}.png")
            with open(icon_file, "wb") as f:
                f.write(b"")
            assert tray_icon_theme_path(appinfo.APP_ID, REPO_ROOT) == "", (
                "a copy whose icon sits in the host theme must name no theme "
                "path, so the shell keeps the user's icon theme"
            )
            tray = TrayIcon()
            assert tray.sni_service.IconThemePath == "", (
                f"the tray handed the shell "
                f"{tray.sni_service.IconThemePath!r} although the host theme "
                "already holds the icon"
            )
            os.remove(icon_file)

            # 3. The user data dir counts as well, and so does the older
            #    pixmaps dir.
            for relative in (("icons", "hicolor", "scalable", "apps",
                              f"{appinfo.APP_ID}.svg"),
                             ("pixmaps", f"{appinfo.APP_ID}.png")):
                path = os.path.join(data_home, *relative)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(b"")
                assert tray_icon_theme_path(appinfo.APP_ID, REPO_ROOT) == "", (
                    f"an icon at {path} is one the desktop shell finds, so "
                    "the tray must name no theme path"
                )
                os.remove(path)

            # 4. A data dir whose name holds a pattern character is still
            #    searched for the icon it holds, not for a pattern.
            odd_home = os.path.join(root, "we[i]rd")
            odd_apps = os.path.join(odd_home, "icons", "hicolor", "48x48", "apps")
            os.makedirs(odd_apps)
            odd_icon = os.path.join(odd_apps, f"{appinfo.APP_ID}.png")
            with open(odd_icon, "wb") as f:
                f.write(b"")
            os.environ["XDG_DATA_HOME"] = odd_home
            assert tray_icon_theme_path(appinfo.APP_ID, REPO_ROOT) == "", (
                f"the icon at {odd_icon} sits in a dir whose name holds a "
                "pattern character; the search must read the name and not a "
                "pattern"
            )
            odd_name = "we[i]rd-icon"
            with open(os.path.join(odd_apps, f"{odd_name}.png"), "wb") as f:
                f.write(b"")
            assert tray_icon_theme_path(odd_name, REPO_ROOT) == "", (
                f"the icon named {odd_name!r} holds a pattern character; the "
                "search must look for that name and not for a pattern"
            )
            os.environ["XDG_DATA_HOME"] = data_home

            # 5. An icon of another name is not this app's icon.
            other = os.path.join(installed, "some.other.App.png")
            os.makedirs(installed, exist_ok=True)
            with open(other, "wb") as f:
                f.write(b"")
            assert tray_icon_theme_path(appinfo.APP_ID, REPO_ROOT) == bundled, (
                "another application's icon must not pass for this one"
            )
    finally:
        restore()
    print("PASS: the tray names an icon theme path only when the host theme "
          "lacks the app icon")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_tray_reregister")
    check_base_double_register_no_orphan()
    check_sni_double_register_keeps_menu_live()
    check_shipped_icon_theme_resolves()
    check_flatpak_manifest_installs_the_app_icon()

    gl.MAIN_PATH = REPO_ROOT  # install root; the shipped icon dir sits under it

    test_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
    test_bus.up()  # also exports DBUS_SESSION_BUS_ADDRESS for bus_get_sync
    try:
        check_item_and_menu_take_their_own_paths()
        check_icon_theme_path_only_when_the_host_lacks_the_icon()
        run_checks(test_bus.get_bus_address())
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        # Traceback frames retain the bus, so stop it without waiting on teardown.
        test_bus.stop()
        os._exit(1)
    finally:
        # Collect the tray-menu cycle before waiting for the session bus.
        gc.collect()
        test_bus.down()
    print("PASS: scenario_tray_reregister")


def run_checks(bus_address: str) -> None:
    menu = DBusMenu()
    menu.add_menu_item(1, "Quit", callback=lambda: None)
    tray = DBusTrayIcon(menu=menu, app_id="com.example.HarnessTray",
                        title="HarnessTray")

    # Registration without a watcher must announce when one appears later.
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
