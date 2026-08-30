"""Expose Deckard control at /io/github/nazbert/Deckard over io.github.nazbert.Deckard.
Each controller uses /io/github/nazbert/Deckard/controllers/<serial>."""

import contextlib
import json
import os
import re
from collections import namedtuple
from typing import Any, Tuple, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
from src.Signals import Signals
from loguru import logger as log

from dasbus.server.interface import dbus_interface
from dasbus.connection import SessionMessageBus
from dasbus.typing import Int, Str, List
from dasbus.error import DBusError
from gi.repository import GLib

import appinfo
import globals as gl
from src.backend import control_plane
from src.backend.atomic_json import require_containment

WindowInfo = namedtuple("WindowInfo", ["name", "wm_class"])

DBUS_OBJECT_PATH = appinfo.DBUS_OBJECT_PATH
CONTROLLER_BASE_PATH = DBUS_OBJECT_PATH + "/controllers"
TOP_IFACE = appinfo.APP_ID
CTRL_IFACE = f"{appinfo.APP_ID}.Controller"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
ERROR_PAGE_EXISTS = f"{appinfo.APP_ID}.Error.PageExists"


def _emit_properties_changed(object_path: str, interface: str,
                             changed: dict[str, Any], invalidated: list[str] | None = None) -> None:
    """Emit org.freedesktop.DBus.Properties.PropertiesChanged on the bus."""
    if _bus is None:
        return
    try:
        connection = _bus.connection
        body = GLib.Variant("(sa{sv}as)", (
            interface,
            changed,
            invalidated or [],
        ))
        connection.emit_signal(
            None,           # destination (broadcast)
            object_path,
            PROPS_IFACE,
            "PropertiesChanged",
            body,
        )
    except Exception as e:
        log.debug(f"DBus API: Failed to emit PropertiesChanged: {e}")


def _serial_to_dbus_path(serial: str) -> str:
    """Convert a serial number to a valid DBus object path component."""
    # DBus paths only allow [A-Za-z0-9_], so replace anything else with _
    return re.sub(r"[^A-Za-z0-9_]", "_", serial)


# Per-controller API, published at .../controllers/<serial>.

@dbus_interface(CTRL_IFACE)
class ControllerInstanceAPI:
    """DBus interface for a single StreamDeck controller."""

    def __init__(self, controller: "DeckController") -> None:
        self._controller = controller
        self._active_page_name: str = ""
        self._object_path: str = ""  # set by _publish_controller

    # Methods

    def SetActivePage(self, name: Str) -> None:
        """Set the active page through shared control rules; the active page is a no-op.
        Return nothing and log invalid page names without raising."""
        serial = self._controller.serial_number()
        log.info(f"DBus API [{serial}]: SetActivePage called – name={name!r}")
        try:
            result = control_plane.get().change_page_on(self._controller, name)
            if result.ok:
                self._active_page_name = name
            else:
                log.warning(f"DBus API [{serial}]: SetActivePage – {result.message}")
        except Exception as e:
            log.error(f"DBus API [{serial}]: SetActivePage error: {e}")

    # Properties

    @property
    def ActivePageName(self) -> Str:
        """The name of the currently active page on this controller."""
        return self._active_page_name

    @ActivePageName.setter
    def ActivePageName(self, value: Str) -> None:
        self._active_page_name = value
        log.debug(f"DBus API [{self._controller.serial_number()}]: ActivePageName changed to {value!r}")
        if self._object_path:
            _emit_properties_changed(
                self._object_path, CTRL_IFACE,
                {"ActivePageName": GLib.Variant("s", value)},
            )


# Top-level API, published at /io/github/nazbert/Deckard.

@dbus_interface(TOP_IFACE)
class DeckardAPI:
    """DBus interface for Deckard (top-level)."""

    def __init__(self) -> None:
        self._foreground_window: WindowInfo = WindowInfo("", "")

    # Methods

    @property
    def Pages(self) -> List[Str]:
        """Return a list of page names."""
        log.info("DBus API: Pages read")
        try:
            if gl.page_manager is not None:
                return gl.page_manager.get_page_names()
        except Exception as e:
            log.error(f"DBus API: Pages error: {e}")
        return []

    def AddPage(self, name: Str, json_contents: Str) -> None:
        """Add a new page with the given name and JSON contents."""
        log.info(f"DBus API: AddPage called – name={name!r}")
        try:
            page_dict = json.loads(json_contents) if json_contents else {}
            if gl.page_manager is not None:
                path = gl.page_manager.add_page(name, page_dict)
                gl.page_manager.refresh_document(path)
                gl.page_manager.reload_pages_with_path(path)
                gl.signal_manager.trigger_signal(Signals.PageAdd, path)
        except FileExistsError as e:
            raise DBusError(
                ERROR_PAGE_EXISTS,
                f"Page '{name}' already exists"
            )
        except json.JSONDecodeError as e:
            log.error(f"DBus API: AddPage – invalid JSON: {e}")
        except Exception as e:
            log.error(f"DBus API: AddPage error: {e}")

    def RemovePage(self, name: Str) -> None:
        """Remove the page with the given name."""
        log.info(f"DBus API: RemovePage called – name={name!r}")
        try:
            if gl.page_manager is not None:
                page_path = os.path.join(gl.page_manager.PAGE_PATH, f"{name}.json")
                if os.path.exists(page_path):
                    gl.page_manager.remove_page(page_path)
                    gl.signal_manager.trigger_signal(Signals.PageDelete, page_path)
                else:
                    log.warning(f"DBus API: RemovePage – page not found: {name}")
        except Exception as e:
            log.error(f"DBus API: RemovePage error: {e}")

    def ChangePage(self, serial: Str, page: Str) -> Str:
        """Show a page name or path on serial, returning empty for success or the reason.
        The active page counts as success; unexpected exceptions become D-Bus errors."""
        log.info(f"DBus API: ChangePage called – serial={serial!r} page={page!r}")
        result = control_plane.get().change_page(serial, page)
        if result.ok:
            return ""
        log.warning(f"DBus API: ChangePage – {result.message}")
        return result.message

    def ChangeState(self, serial: Str, page: Str, coords: Str, state: Int) -> Str:
        """Load page if needed and set its x,y input on serial to state.
        Return empty for success or the reason; the control plane parses coords."""
        log.info(f"DBus API: ChangeState called – serial={serial!r} page={page!r} "
                 f"coords={coords!r} state={state!r}")
        result = control_plane.get().change_state(serial, page, coords, state)
        if result.ok:
            log.info(f"DBus API: ChangeState – {result.message}")
            return ""
        log.warning(f"DBus API: ChangeState – {result.message}")
        return result.message

    def EmulateInput(self, serial: Str, page: Str, coords: Str, event: Str) -> Str:
        """Load page if needed and send press or long-press through the deck input path.
        Reply after the bounded press admission, not action completion or release; return empty or the reason."""
        log.info(f"DBus API: EmulateInput called – serial={serial!r} page={page!r} "
                 f"coords={coords!r} event={event!r}")
        result = control_plane.get().emulate_input(serial, page, coords, event)
        if result.ok:
            log.info(f"DBus API: EmulateInput – {result.message}")
            return ""
        log.warning(f"DBus API: EmulateInput – {result.message}")
        return result.message

    def QueryState(self) -> Str:
        """Return JSON with page names and each deck's serial, active page, and brightness.
        Unexpected failures use a top-level error key that valid state never uses."""
        log.info("DBus API: QueryState read")
        try:
            return json.dumps(control_plane.get().dump_state())
        except Exception as e:
            log.error(f"DBus API: QueryState error: {e}")
            return json.dumps({"error": f"Could not read the state: {e}"})

    def ListActions(self, page: Str, coords: Str) -> Str:
        """Return page actions as JSON for all inputs or one x,y coordinate.
        Missing pages and invalid coordinates return a top-level error object."""
        log.info(f"DBus API: ListActions called – page={page!r} coords={coords!r}")
        try:
            error, data = control_plane.get().list_page_actions(page, coords)
            if error is not None:
                log.warning(f"DBus API: ListActions – {error}")
                return json.dumps({"error": error})
            return json.dumps(data)
        except Exception as e:
            log.error(f"DBus API: ListActions error: {e}")
            return json.dumps({"error": f"Could not read the actions on page '{page}': {e}"})

    def SetDeckBrightness(self, serial: Str, value: Int) -> Str:
        """Set and persist serial brightness from 0 to 100, returning empty or the reason.
        A later page brightness override still takes precedence."""
        log.info(f"DBus API: SetDeckBrightness called – serial={serial!r} value={value!r}")
        result = control_plane.get().set_brightness(serial, value)
        if not result.ok:
            log.warning(f"DBus API: SetDeckBrightness – {result.message}")
            return result.message
        self._persist_deck_brightness(serial, value)
        return ""

    @staticmethod
    def _persist_deck_brightness(serial: str, value: int) -> None:
        """Best-effort persist the live brightness so page reload does not restore an old value.
        Clamp storage to 0..100; a write failure leaves the live device change in place."""
        try:
            if gl.settings_manager is None:
                return
            value = min(100, max(0, value))
            settings = gl.settings_manager.get_deck_settings(serial)
            settings.setdefault("brightness", {})["value"] = value
            gl.settings_manager.save_deck_settings(serial, settings)
        except Exception as e:
            log.error(f"DBus API: could not persist brightness for {serial}: {e}")

    def Sleep(self, serial: Str) -> Str:
        """Put the deck with serial to its screensaver. Empty on success and
        the reason otherwise. A press wakes it, as with an idle screensaver."""
        log.info(f"DBus API: Sleep called – serial={serial!r}")
        result = control_plane.get().sleep(serial)
        if result.ok:
            return ""
        log.warning(f"DBus API: Sleep – {result.message}")
        return result.message

    def Wake(self, serial: Str) -> Str:
        """Wake the deck with serial from its screensaver. Empty on success and
        the reason otherwise."""
        log.info(f"DBus API: Wake called – serial={serial!r}")
        result = control_plane.get().wake(serial)
        if result.ok:
            return ""
        log.warning(f"DBus API: Wake – {result.message}")
        return result.message

    def RenamePage(self, old: Str, new: Str) -> Str:
        """Rename old to a free contained name, returning empty or the reason.
        Live and default-page references follow; plugin pages cannot be renamed."""
        log.info(f"DBus API: RenamePage called – old={old!r} new={new!r}")
        try:
            page_manager = gl.page_manager
            if page_manager is None:
                return "Cannot rename a page: no page manager"
            old_path = page_manager.find_matching_page_path(old)
            if old_path is None:
                return f"Page '{old}' not found"
            # Absolute input can resolve outside pages, and move_page removes its source.
            # Require containment before moving any caller-selected path.
            try:
                require_containment(page_manager.PAGE_PATH, old_path)
            except ValueError:
                return f"Page '{old}' is not a page in the pages folder"
            if old_path in page_manager.custom_pages:
                return f"Page '{old}' is provided by a plugin and cannot be renamed"
            if not new:
                return "The new page name is empty"
            new_path = os.path.join(page_manager.PAGE_PATH, f"{new}.json")
            try:
                require_containment(page_manager.PAGE_PATH, new_path)
            except ValueError:
                return f"'{new}' is not a name a page can take"
            if os.path.exists(new_path):
                return f"A page named '{new}' already exists"
            page_manager.move_page(old_path, new_path)
            gl.signal_manager.trigger_signal(Signals.PageRename, old_path, new_path)
            return ""
        except Exception as e:
            log.error(f"DBus API: RenamePage error: {e}")
            return f"Could not rename page '{old}': {e}"

    def DuplicatePage(self, source: Str, new: Str) -> Str:
        """Copy the current on-disk source to a free contained name, returning empty or the reason.
        Reading flushes pending edits so a displayed page duplicates what it shows."""
        log.info(f"DBus API: DuplicatePage called – source={source!r} new={new!r}")
        try:
            page_manager = gl.page_manager
            if page_manager is None:
                return "Cannot duplicate a page: no page manager"
            source_path = page_manager.find_matching_page_path(source)
            if source_path is None:
                return f"Page '{source}' not found"
            # Confine absolute source paths before reading them into a new page.
            try:
                require_containment(page_manager.PAGE_PATH, source_path)
            except ValueError:
                return f"Page '{source}' is not a page in the pages folder"
            if not new:
                return "The new page name is empty"
            data = page_manager.get_page_data(source_path)
            try:
                new_path = page_manager.add_page(new, data)
            except FileExistsError:
                return f"A page named '{new}' already exists"
            except ValueError:
                return f"'{new}' is not a name a page can take"
            gl.signal_manager.trigger_signal(Signals.PageAdd, new_path)
            return ""
        except Exception as e:
            log.error(f"DBus API: DuplicatePage error: {e}")
            return f"Could not duplicate page '{source}': {e}"

    def NotifyForegroundWindow(self, name: Str, wm_class: Str) -> None:
        """Report a foreground window without kdotool and route it through the worker.
        Never route inline because page loading marshals back to this D-Bus main context."""
        win = WindowInfo(name, wm_class)
        log.info(f"DBus API: NotifyForegroundWindow called – {win!r}")
        try:
            if gl.window_grabber is not None:
                from src.backend.WindowGrabber.Window import Window
                window = Window(wm_class=win.wm_class, title=win.name)
                gl.window_grabber.report_active_window(window)
        except Exception as e:
            log.error(f"DBus API: NotifyForegroundWindow error: {e}")

    @property
    def IconPacks(self) -> List[Str]:
        """Return a list of icon pack IDs."""
        log.info("DBus API: IconPacks read")
        try:
            if gl.icon_pack_manager is not None:
                packs = gl.icon_pack_manager.get_icon_packs()
                return list(packs.keys())
        except Exception as e:
            log.error(f"DBus API: IconPacks error: {e}")
        return []

    def GetIconNames(self, icon_pack_id: Str) -> List[Str]:
        """Return a list of all icon names in the given icon pack."""
        log.info(f"DBus API: GetIconNames called – icon_pack_id={icon_pack_id!r}")
        try:
            if gl.icon_pack_manager is not None:
                packs = gl.icon_pack_manager.get_icon_packs()
                pack = packs.get(icon_pack_id)
                if pack is None:
                    log.warning(f"DBus API: GetIconNames – pack not found: {icon_pack_id}")
                    return []
                icons = pack.get_icons()
                return [icon.name for icon in icons]
        except Exception as e:
            log.error(f"DBus API: GetIconNames error: {e}")
        return []

    # Properties

    @property
    def DataPath(self) -> Str:
        """Return the base data path used to compose page and icon references."""
        return cast(str, gl.DATA_PATH)
    
    @property
    def Controllers(self) -> List[Str]:
        """Return addressable serials from the main-context published-object registry.
        Every listed serial therefore has its composed controller object path."""
        # A D-Bus read can overtake queued publication and temporarily omit a deck.
        # PropertiesChanged corrects it; never list a serial before its object exists.
        return list(_controller_instances)

    @property
    def ForegroundWindow(self) -> Tuple[Str, Str]:
        """The current foreground window as (name, wm_class)."""
        return cast("tuple[str, str]", (self._foreground_window.name, self._foreground_window.wm_class))

    @ForegroundWindow.setter
    def ForegroundWindow(self, value: Tuple[Str, Str]) -> None:
        window = WindowInfo(*value)
        if window == self._foreground_window:
            # Do not wake subscribers when rule reapplication reports the same window.
            return

        self._foreground_window = window
        log.debug(f"DBus API: ForegroundWindow changed to {self._foreground_window!r}")
        _emit_properties_changed(
            DBUS_OBJECT_PATH, TOP_IFACE,
            {"ForegroundWindow": GLib.Variant("(ss)", tuple(self._foreground_window))},
        )


# Helpers that start and stop the service.

_bus = None
_api_instance = None
_controller_instances: dict[str, ControllerInstanceAPI] = {}


def start_dbus_service() -> None:
    """Publish the Deckard API on the session bus."""
    global _bus, _api_instance
    # Commit globals only after publication succeeds.
    # A half-open _bus would look usable to controller publication and property reads.
    bus = None
    try:
        bus = SessionMessageBus()
        api_instance = DeckardAPI()
        bus.publish_object(DBUS_OBJECT_PATH, api_instance)
    except Exception as e:
        log.error(f"Failed to start DBus API service: {e}")
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.disconnect()
        _bus = None
        _api_instance = None
        return

    _bus = bus
    _api_instance = api_instance

    # Publish decks registered before service startup directly on the main context.
    # Isolate each failure so later decks still publish.
    if gl.deck_manager is not None:
        for controller in list(gl.deck_manager.deck_controller):
            _publish_on_main(controller)

    log.success(f"DBus API published at {DBUS_OBJECT_PATH}")


def publish_controller(controller: "DeckController") -> None:
    """Queue controller publication from USB, boot-rescan, or main registration threads.
    dasbus object registration runs on the GLib main context."""
    # Check the bus before reading a controller that can still be under construction.
    # Service startup later sweeps registered decks.
    if _bus is None:
        return
    GLib.idle_add(_publish_on_main, controller)


def unpublish_controller(controller: "DeckController") -> None:
    """Queue controller removal on the main context when its deck goes away.
    Its proxy becomes UnknownObject as the shared registry drops its serial."""
    if _bus is None:
        return
    GLib.idle_add(_unpublish_on_main, controller)


def _known_serial(controller: "DeckController") -> str:
    """Read the cached serial without device I/O for failure reporting."""
    return cast(str, getattr(controller, "_serial_number", None) or "<unknown>")


def _publish_on_main(controller: "DeckController") -> bool:
    """Idle worker for publish_controller. Main context only."""
    try:
        _publish_controller(controller)
    except Exception as e:
        log.error(
            f"DBus API: failed to publish controller {_known_serial(controller)}: "
            f"{e}. That deck is off the DBus API for this session."
        )
    return GLib.SOURCE_REMOVE


def _unpublish_on_main(controller: "DeckController") -> bool:
    """Idle worker for unpublish_controller. Main context only."""
    try:
        _unpublish_controller(controller)
    except Exception as e:
        log.error(
            f"DBus API: failed to unpublish controller {_known_serial(controller)}: "
            f"{e}. Its object stays on the bus, bound to a deck that is gone."
        )
    return GLib.SOURCE_REMOVE


def _publish_controller(controller: "DeckController") -> None:
    """Publish one controller API on the main context."""
    if _bus is None:
        return  # the service stopped between queuing this and running it
    if gl.deck_manager is None or controller not in gl.deck_manager.deck_controller:
        # The deck went away before this idle ran. A publish now leaves an
        # object that no removal clears.
        return
    serial = controller.serial_number()
    existing = _controller_instances.get(serial)
    if existing is not None:
        if existing._controller is controller or (
                gl.deck_manager is not None
                and existing._controller in gl.deck_manager.deck_controller):
            return  # already published
        # Publish can overtake unpublish because registration changes queue outside locks.
        # Replace only the dead object; later identity-based unpublish leaves this one.
        log.warning(
            f"DBus API: replacing the stale object for deck {serial} -- its "
            f"controller was removed, and this publish arrived first."
        )
        _bus.unpublish_object(existing._object_path)
        del _controller_instances[serial]
    obj_path = f"{CONTROLLER_BASE_PATH}/{_serial_to_dbus_path(serial)}"
    taken_by = _serial_published_at(obj_path)
    if taken_by is not None:
        log.error(
            f"DBus API: not publishing controller {serial}: {obj_path} already "
            f"belongs to {taken_by}. Two serials that differ only in characters "
            f"a DBus path cannot carry map to one path; that deck stays off the "
            f"API rather than taking over another deck's object."
        )
        return
    instance = ControllerInstanceAPI(controller)
    instance._object_path = obj_path
    # Seed the boot page because it can load before this object exists.
    active_page = controller.active_page
    instance._active_page_name = "" if active_page is None else active_page.get_name()
    _bus.publish_object(obj_path, instance)
    # Record only after the bus accepts the object, so a failed publish leaves
    # the serial free for another publish.
    _controller_instances[serial] = instance
    log.info(f"DBus API: published controller {serial} at {obj_path}")
    _emit_controllers_changed()


def _unpublish_controller(controller: "DeckController") -> None:
    """Remove one controller API on the main context."""
    if _bus is None:
        return  # already stopped, which unpublished everything
    serial = _serial_published_for(controller)
    if serial is None:
        # Identity matching preserves a new controller that inherited this serial.
        return
    instance = _controller_instances.pop(serial)
    _bus.unpublish_object(instance._object_path)
    log.info(f"DBus API: unpublished controller {serial} from {instance._object_path}")
    _emit_controllers_changed()


def _serial_published_at(obj_path: str) -> str | None:
    """The serial already published at obj_path, if there is one."""
    for serial, instance in _controller_instances.items():
        if instance._object_path == obj_path:
            return serial
    return None


def _serial_published_for(controller: "DeckController") -> str | None:
    """The serial this exact controller is published under, if any."""
    for serial, instance in _controller_instances.items():
        if instance._controller is controller:
            return serial
    return None


def _emit_controllers_changed() -> None:
    """Emit the current addressable controller set after registry changes.
    This corrects early reads without naming an unpublished object."""
    if _api_instance is None:
        return
    _emit_properties_changed(
        DBUS_OBJECT_PATH, TOP_IFACE,
        {"Controllers": GLib.Variant("as", _api_instance.Controllers)},
    )


def stop_dbus_service() -> None:
    """Disconnect from the session bus and clear all related state."""
    global _bus, _api_instance
    try:
        if _bus is not None:
            _bus.disconnect()
            log.info("DBus API service stopped")
    except Exception as e:
        log.error(f"Failed to stop DBus API service: {e}")
    finally:
        # Clear all service state even when disconnect fails.
        _bus = None
        _api_instance = None
        _controller_instances.clear()


def get_api_instance() -> DeckardAPI | None:
    """Return the active top-level API instance, or None if not started."""
    return _api_instance


def get_controller_instance(serial: str) -> ControllerInstanceAPI | None:
    """Return the API instance for a specific controller, or None."""
    return _controller_instances.get(serial)


def notify_active_page_changed(serial: str, page_name: str) -> None:
    """Publish a controller's active page name to D-Bus clients."""
    instance = _controller_instances.get(serial)
    if instance is not None:
        instance.ActivePageName = page_name


def notify_foreground_window_changed(name: str, wm_class: str) -> None:
    """Publish a WindowGrabber or NotifyForegroundWindow change to D-Bus clients."""
    # Track the desktop only while a page needs window auto-change rules.
    # Constant updates would poll only to maintain this property.
    if _api_instance is not None:
        _api_instance.ForegroundWindow = WindowInfo(name, wm_class)
