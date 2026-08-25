# Inspired by code of deltragon/SafeEyes repo.
# Link: https://github.com/deltragon/SafeEyes/blob/f25f554585c79a11621e3a505cc6ce5af08a3d58/safeeyes/plugins/trayicon/plugin.py

from typing import Any, Callable, TypedDict, override

from gi.repository import Gio, GLib

from loguru import logger as log

# One tray menu entry. The hyphenated D-Bus property names force the
# functional syntax. Every key is optional except that add_menu_item always
# sets id.
MenuItem = TypedDict("MenuItem", {
    "id": int,
    "label": str,
    "enabled": bool,
    "hidden": bool,
    "type": str,
    "icon-name": str,
    "children-display": str,
    "children": list["MenuItem"],
    "callback": Callable[[], object],
}, total=False)


class _SNIKwargs(TypedDict, total=False):
    """The staged keyword arguments of StatusNotifierItemService."""

    session_bus: Gio.DBusConnection
    menu_items: list[MenuItem]
    path: str
    menu_path: str

SNI_NODE_INFO = Gio.DBusNodeInfo.new_for_xml("""
<?xml version="1.0" encoding="UTF-8"?>
<node>
    <interface name="org.kde.StatusNotifierItem">
        <property name="Category" type="s" access="read"/>
        <property name="Id" type="s" access="read"/>
        <property name="Title" type="s" access="read"/>
        <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
        <property name="Menu" type="o" access="read"/>
        <property name="ItemIsMenu" type="b" access="read"/>
        <property name="IconName" type="s" access="read"/>
        <property name="IconThemePath" type="s" access="read"/>
        <property name="Status" type="s" access="read"/>
        <signal name="NewIcon"/>
        <signal name="NewTooltip"/>

        <property name="XAyatanaLabel" type="s" access="read"/>
        <signal name="XAyatanaNewLabel">
            <arg type="s" name="label" direction="out" />
            <arg type="s" name="guide" direction="out" />
        </signal>
    </interface>
</node>""").interfaces[0]

MENU_NODE_INFO = Gio.DBusNodeInfo.new_for_xml("""
<?xml version="1.0" encoding="UTF-8"?>
<node>
    <interface name="com.canonical.dbusmenu">
        <method name="GetLayout">
            <arg type="i" direction="in"/>
            <arg type="i" direction="in"/>
            <arg type="as" direction="in"/>
            <arg type="u" direction="out"/>
            <arg type="(ia{sv}av)" direction="out"/>
        </method>
        <method name="GetGroupProperties">
                        <arg type="ai" name="ids" direction="in"/>
                        <arg type="as" name="propertyNames" direction="in" />
                        <arg type="a(ia{sv})" name="properties" direction="out" />
                </method>
        <method name="GetProperty">
                        <arg type="i" name="id" direction="in"/>
                        <arg type="s" name="name" direction="in"/>
                        <arg type="v" name="value" direction="out"/>
                </method>
        <method name="Event">
            <arg type="i" direction="in"/>
            <arg type="s" direction="in"/>
            <arg type="v" direction="in"/>
            <arg type="u" direction="in"/>
        </method>
        <method name="EventGroup">
                        <arg type="a(isvu)" name="events" direction="in" />
                        <arg type="ai" name="idErrors" direction="out" />
                </method>
        <method name="AboutToShow">
            <arg type="i" direction="in"/>
            <arg type="b" direction="out"/>
        </method>
        <method name="AboutToShowGroup">
                        <arg type="ai" name="ids" direction="in" />
                        <arg type="ai" name="updatesNeeded" direction="out" />
                        <arg type="ai" name="idErrors" direction="out" />
                </method>
        <signal name="LayoutUpdated">
            <arg type="u"/>
            <arg type="i"/>
        </signal>
    </interface>
</node>""").interfaces[0]

class DBusService:
    def __init__(self, interface_info: Gio.DBusInterfaceInfo, object_path: str, bus: Gio.DBusConnection) -> None:
        self.interface_info = interface_info
        self.object_path = object_path
        self.bus = bus
        self.registration_id: int | None = None

    def register(self) -> None:
        if self.registration_id is not None:
            # This object already registered. A second register() with no
            # unregister() between them, which TrayIcon.initialize() and the
            # Settings-panel start() together produce, orphans the earlier
            # object registration on the connection. Return early and keep the
            # existing registration. Do not unregister and register again
            # here. self.unregister() dispatches to
            # StatusNotifierItemService.unregister(), which also calls
            # self._menu.unregister() and leaves the tray menu object dead,
            # because the base register() registers the SNI object alone.
            return
        self.registration_id = self.bus.register_object(
            object_path=self.object_path,
            interface_info=self.interface_info,
            method_call_closure=self.on_method_call,
            get_property_closure=self.on_get_property
        )

        if not self.registration_id:
            raise GLib.Error(f"Failed to register object with path {self.object_path}")

        self.interface_info.cache_build()

    def unregister(self) -> None:
        self.interface_info.cache_release()

        if self.registration_id is not None:
            self.bus.unregister_object(self.registration_id)
            self.registration_id = None

    def on_method_call(self, _connection: Gio.DBusConnection, _sender: str, _path: str, _interface_name: str, method_name: str, parameters: GLib.Variant, invocation: Gio.DBusMethodInvocation) -> None:
        method_info = self.interface_info.lookup_method(method_name)
        if method_info is None:
            log.error(f"D-Bus call for {method_name!r}, which {self.object_path} does not declare")
            invocation.return_value(None)
            return
        method = getattr(self, method_name)
        result = method(*parameters.unpack())
        out_arg_types = "".join([arg.signature for arg in method_info.out_args])
        return_value = None

        if method_info.out_args:
            return_value = GLib.Variant(f"({out_arg_types})", result)

        invocation.return_value(return_value)

    def on_get_property(self, _connection: Gio.DBusConnection, _sender: str, _path: str, _interface: str, property_name: str) -> GLib.Variant:
        property_info = self.interface_info.lookup_property(property_name)
        if property_info is None:
            raise GLib.Error(f"no such property {property_name!r} on {self.object_path}")
        return GLib.Variant(property_info.signature, getattr(self, property_name))

    def emit_signal(self, signal_name: str, args: tuple[Any, ...] | None = None) -> None:
        signal_info = self.interface_info.lookup_signal(signal_name)
        if signal_info is None:
            log.error(f"emit of {signal_name!r}, which {self.object_path} does not declare")
            return
        if len(signal_info.args) == 0:
            parameters = None
        else:
            arg_types = "".join([arg.signature for arg in signal_info.args])
            parameters = GLib.Variant(f"({arg_types})", args)

        self.bus.emit_signal(
            destination_bus_name=None,
            object_path=self.object_path,
            interface_name=self.interface_info.name,
            signal_name=signal_name,
            parameters=parameters
        )

class DBusMenuService(DBusService):
    DBusPath = "/com/example/TrayApp/Menu"

    revision = 0

    # idToItems is the flat index from id to item that getItemsFlat()
    # builds.
    items: "list[MenuItem]" = []
    idToItems: "dict[int, MenuItem]" = {}

    def __init__(self, session_bus: Gio.DBusConnection, items: "list[MenuItem]", path: str = DBusPath) -> None:
        super().__init__(
            interface_info=MENU_NODE_INFO,
            object_path=path,
            bus=session_bus
        )

        self.dbus_path = path

        self.set_items(items)

    def set_items(self, items: "list[MenuItem]") -> None:
        self.items = items

        self.idToItems = self.getItemsFlat(items, {})

        self.revision += 1

        self.LayoutUpdate(self.revision, 0)

    @staticmethod
    def getItemsFlat(items: "list[MenuItem]", idToItems: "dict[int, MenuItem]") -> "dict[int, MenuItem]":
        for item in items:
            if item.get('hidden', False):
                continue

            idToItems[item['id']] = item

            if 'children' in item:
                idToItems = DBusMenuService.getItemsFlat(item['children'], idToItems)

        return idToItems

    @staticmethod
    def singleItemToDbus(item: "MenuItem") -> tuple[int, dict[str, GLib.Variant]]:
        props = DBusMenuService.itemPropsToDbus(item)

        return (item['id'], props)

    @staticmethod
    def itemPropsToDbus(item: "MenuItem") -> dict[str, GLib.Variant]:
        result = {}

        # Spelled out per key: a TypedDict reads only literal keys.
        if 'label' in item:
            result['label'] = GLib.Variant('s', item['label'])
        if 'icon-name' in item:
            result['icon-name'] = GLib.Variant('s', item['icon-name'])
        if 'type' in item:
            result['type'] = GLib.Variant('s', item['type'])
        if 'children-display' in item:
            result['children-display'] = GLib.Variant('s', item['children-display'])
        if 'enabled' in item:
            result['enabled'] = GLib.Variant('b', item['enabled'])

        return result

    @staticmethod
    def itemToDbus(item: "MenuItem", recursion_depth: int) -> GLib.Variant | None:
        if item.get('hidden', False):
            return None

        props = DBusMenuService.itemPropsToDbus(item)

        children = []
        if recursion_depth > 1 or recursion_depth == -1:
            if "children" in item:
                children = [DBusMenuService.itemToDbus(item, recursion_depth - 1) for item in item['children']]
                children = [i for i in children if i is not None]

        return GLib.Variant("(ia{sv}av)", (item['id'], props, children))

    def findItemWithParent(self, parent_id: int, items: "list[MenuItem]") -> "list[MenuItem] | None":
        for item in items:
            if item.get('hidden', False):
                continue
            if 'children' in item:
                if item['id'] == parent_id:
                    return item['children']
                else:
                    ret = self.findItemWithParent(parent_id, item['children'])
                    if ret is not None:
                        return ret
        return None

    def GetLayout(self, parent_id: int, recursion_depth: int, property_name: list[str]) -> tuple[int, tuple[int, dict[str, GLib.Variant], list[GLib.Variant]]]:
        source: "list[MenuItem]"
        if parent_id == 0:
            source = self.items
        else:
            found = self.findItemWithParent(parent_id, self.items)
            source = found if found is not None else []

        children = [variant
                    for variant in (self.itemToDbus(item, recursion_depth) for item in source)
                    if variant is not None]

        ret = (
            self.revision,
            (
                0,
                {'children-display': GLib.Variant('s', 'submenu')},
                children
            )
        )

        return ret

    def GetGroupProperties(self, ids: list[int], property_names: list[str]) -> tuple[list[tuple[int, dict[str, GLib.Variant]]]]:
        ret = []

        for idx in ids:
            if idx in self.idToItems:
                props = DBusMenuService.singleItemToDbus(self.idToItems[idx])
                if props is not None:
                    ret.append(props)
        return (ret,)

    def GetProperty(self, idx: int, name: str) -> tuple[GLib.Variant]:
        # A one-tuple, like every other method here: on_method_call packs the
        # result into a variant of the out-arg signature, which for this method
        # is "(v)". A bare variant does not fit that and the pack raises, so no
        # reply ever reached the caller.
        if idx in self.idToItems:
            props = DBusMenuService.itemPropsToDbus(self.idToItems[idx])
            if name in props:
                return (props[name],)
        # The interface declares one out arg and no absent value, so there is
        # nothing truthful to answer here. Name the cause rather than fail
        # later inside the variant pack.
        raise GLib.Error(f"menu item {idx} has no property {name!r}")

    def Event(self, idx: int, event_id: str, data: Any, timestamp: int) -> None:
        if event_id != "clicked":
            return

        if idx in self.idToItems:
            item = self.idToItems[idx]
            if 'callback' in item:
                item['callback']()

    def EventGroup(self, events: list[tuple[int, str, Any, int]]) -> list[int]:
        not_found = []

        for (idx, event_id, data, timestamp) in events:
            if idx not in self.idToItems:
                not_found.append(idx)
                continue

            if event_id != "clicked":
                continue

            item = self.idToItems[idx]
            if 'callback' in item:
                item['callback']()

        return not_found

    def AboutToShow(self, item_id: int) -> tuple[bool]:
        return (False,)

    def AboutToShowGroup(self, ids: list[int]) -> tuple[list[int], list[int]]:
        not_found = []

        for idx in ids:
            if idx not in self.idToItems:
                not_found.append(idx)
                continue

        return ([], not_found)

    def LayoutUpdate(self, revision: int, parent: int) -> None:
        self.emit_signal(
            'LayoutUpdated',
            (revision, parent)
        )

class StatusNotifierItemService(DBusService):
    DBusPath = "/org/ayatana/NotificationItem/com_example_TrayApp"
    Category = 'ApplicationStatus'
    Id = 'com.example.TrayApp'
    Title = 'Safe Eyes'
    Status = 'Active'
    IconName = 'alienarena'
    IconThemePath = ''
    ToolTip: "tuple[str, list[tuple[int, int, bytes]], str, str]" = ('', [], 'Safe Eyes', '')  # DBus (sa(iiay)ss); the icon array is always empty
    XAyatanaLabel = ""
    ItemIsMenu = True
    Menu = None

    def __init__(self, session_bus: Gio.DBusConnection, menu_items: "list[MenuItem]", path: str = DBusPath, menu_path: str = "") -> None:
        super().__init__(
            interface_info=SNI_NODE_INFO,
            object_path=path,
            bus=session_bus
        )

        self.bus = session_bus
        self.dbus_path = path
        self._watcher_watch_id: int | None = None

        if menu_path == "":
            self._menu = DBusMenuService(session_bus, menu_items)
        else:
            self._menu = DBusMenuService(session_bus, menu_items, menu_path)
        self.Menu = self._menu.dbus_path

    @override
    def register(self) -> None:
        self._menu.register()
        super().register()

        # A single RegisterStatusNotifierItem call loses the icon for the
        # rest of the app's life whenever the StatusNotifierWatcher restarts,
        # after a plasmashell or waybar crash, or appears late, as GNOME's
        # AppIndicator support does. A fresh watcher instance knows no item
        # that an earlier instance registered. Watch the well-known name
        # instead, and announce the item again each time the name gains an
        # owner.
        if self._watcher_watch_id is None:
            self._watcher_watch_id = Gio.bus_watch_name_on_connection(
                self.bus,
                'org.kde.StatusNotifierWatcher',
                Gio.BusNameWatcherFlags.NONE,
                self._on_watcher_appeared,
                self._on_watcher_vanished,
            )

    def _on_watcher_appeared(self, connection: Gio.DBusConnection, name: str, name_owner: str) -> None:
        log.info(f"StatusNotifierWatcher appeared (owner: {name_owner}), announcing tray icon")
        connection.call(
            'org.kde.StatusNotifierWatcher',
            '/StatusNotifierWatcher',
            'org.kde.StatusNotifierWatcher',
            'RegisterStatusNotifierItem',
            GLib.Variant('(s)', (self.dbus_path,)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_announce_finished,
        )

    def _on_announce_finished(self, connection: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
        try:
            connection.call_finish(result)
        except GLib.Error as e:
            log.warning(f"Failed to register the tray icon with the StatusNotifierWatcher: {e}")

    def _on_watcher_vanished(self, connection: Gio.DBusConnection, name: str) -> None:
        log.info("StatusNotifierWatcher vanished, re-announcing the tray icon once it returns")

    @override
    def unregister(self) -> None:
        if self._watcher_watch_id is not None:
            Gio.bus_unwatch_name(self._watcher_watch_id)
            self._watcher_watch_id = None
        super().unregister()
        self._menu.unregister()

    def set_items(self, items: "list[MenuItem]") -> None:
        self._menu.set_items(items)

    def set_icon(self, icon: str, path: str = "") -> None:
        self.IconName = icon
        self.IconThemePath = path

        self.emit_signal(
            'NewIcon'
        )

    def set_tooltip(self, title: str, description: str) -> None:
        self.ToolTip = ('', [], title, description)

        self.emit_signal(
            'NewTooltip'
        )

    def set_xayatanalabel(self, label: str) -> None:
        self.XAyatanaLabel = label

        self.emit_signal(
            "XAyatanaNewLabel",
            (label, "")
        )

class DBusTrayIcon:
    def __init__(self, menu: "DBusMenu", path: str = "", menu_path: str = "", app_id: str = "", title: str = "") -> None:
        session_bus = Gio.bus_get_sync(Gio.BusType.SESSION)

        self.menu = menu

        kwargs: "_SNIKwargs" = {
            "session_bus": session_bus,
            "menu_items": self.menu.get_items()
        }
        if path != "":
            kwargs["path"] = path

        if menu_path != "":
            kwargs["menu_path"] = menu_path

        self.sni_service = StatusNotifierItemService(**kwargs)
        if app_id != "":
            self.sni_service.Id = app_id

        if title != "":
            self.sni_service.Title = title

    def set_icon(self, icon: str, path: str = "") -> None:
        self.sni_service.set_icon(icon, path)

    def set_tooltip(self, title: str, description: str = "") -> None:
        self.sni_service.set_tooltip(title, description)

    def set_label(self, label: str) -> None:
        self.sni_service.set_xayatanalabel(label)

    def update_menu(self) -> None:
        self.sni_service.set_items(self.menu.get_items())

    def register(self) -> None:
        self.sni_service.register()

    def unregister(self) -> None:
        self.sni_service.unregister()

class DBusMenu:
    def __init__(self) -> None:
        # Each entry is the id plus whichever of label, type, icon-name and
        # callback the caller supplied.
        self.menu_items: "list[MenuItem]" = []

    def add_menu_item(self, menu_id: int, menu_label: str = "", menu_type: str = "",
                      icon_name: str = "", callback: "Callable[[], object] | None" = None) -> None:
        item: "MenuItem" = {'id': menu_id}
        if menu_label != "":
            item['label'] = menu_label
        if menu_type != "":
            item['type'] = menu_type
        if icon_name != "":
            item['icon-name'] = icon_name
        if callback:
            item['callback'] = callback

        self.menu_items.append(item)

    def get_items(self) -> "list[MenuItem]":
        return self.menu_items
