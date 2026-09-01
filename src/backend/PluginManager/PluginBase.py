import contextlib
import importlib
import os
import inspect
import json
import threading
import time
import subprocess
from collections.abc import Callable
from typing import cast, Any, TypedDict, NotRequired, override

from packaging import version

from loguru import logger as log

import rpyc
from rpyc.utils.server import ThreadedServer
from rpyc.core.protocol import Connection

import gi

from locales.LocaleManager import LocaleManager
from src.backend.PluginManager.ActionHolderGroup import ActionHolderGroup
from src.backend.PluginManager.PluginSettings.Asset import Icon, Color
from src.backend.PluginManager.PluginSettings.PluginAssetManager import AssetManager

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk

import globals as gl

from locales.LegacyLocaleManager import LegacyLocaleManager
from locales.translator import Translator
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.backend_event_hold import BackendEventHold
from src.backend.PluginManager.EventHolder import EventHolder
from src.backend.settings_store import PluginSettings


class PluginRegistration(TypedDict):
    """Registry entry; register() is the sole writer and always sets object.
    Warm-up tolerates malformed entries without it, so the key stays optional."""

    object: NotRequired["PluginBase"]
    plugin_version: "str | None"
    minimum_app_version: "str | None"
    github: "str | None"
    folder_path: str
    file_name: str


class DisabledPluginRegistration(PluginRegistration):
    """A disabled plugin's entry; reason names why register() refused it."""

    reason: "str | None"


class PluginBase(rpyc.Service):
    """The base class of every plugin."""

    # {plugin_id: registration}. See register().
    plugins: "dict[str, PluginRegistration]" = {}
    disabled_plugins: "dict[str, DisabledPluginRegistration]" = {}
    # The app-ready warm-up sets this per instance so later plugin loads do not
    # fire on_app_ready again; instances use the class default before warm-up.
    _on_app_ready_fired: bool = False

    # The backing slot of backend_event_hold below, and the lock that keeps
    # two threads from building two holds for one plugin.
    _backend_event_hold: "BackendEventHold | None" = None
    _backend_event_hold_lock = threading.Lock()

    @property
    def backend_event_hold(self) -> "BackendEventHold":
        """Bounded buffer for observerless EventHolder events, only while backend connects.
        Lazy if no super(); launch/register/teardown use it; class name fallback without PATH."""
        hold = self._backend_event_hold
        if hold is not None:
            return hold
        with PluginBase._backend_event_hold_lock:
            hold = self._backend_event_hold
            if hold is None:
                path = getattr(self, "PATH", "") or ""
                hold = BackendEventHold(label=os.path.basename(path) or type(self).__name__)
                self._backend_event_hold = hold
            return hold

    def __init__(self, use_legacy_locale: bool = True, legacy_dir: str = "locales"):
        self.backend_connection: "Connection | None" = None
        # A plugin can store an in-process backend or the rpyc netref created by
        # launch_backend(), so this attribute must accept both shapes.
        self.backend: Any = None
        self.server: "ThreadedServer | None" = None
        self.backend_process: subprocess.Popen[bytes] | None = None
        # The generation disarms watchdogs superseded by relaunch; the stop flag
        # suppresses errors when deactivation or unload requested the exit.
        self._backend_launch_generation: int = 0
        self._backend_stop_requested: bool = False
        # register_backend relaxes its port-ownership check for a terminal
        # launch, where the backend is not a child of the Popen handle.
        self._backend_via_terminal: bool = False
        # The backend's rpyc service thread sets this and wakes
        # wait_for_backend on the launching thread.
        self._backend_ready = threading.Event()

        self.logger = gl.loggers.get("plugins", None)

        self.PATH = os.path.dirname(inspect.getfile(self.__class__))
        self.settings_path: str = self._resolve_settings_path()
        # Serialize settings read-modify-write cycles that concurrent on_ready calls can lose.
        # Use a plain outer lock; accessors do not re-enter, and the store cache lock is inner.
        self._settings_lock = threading.Lock()

        # The two storage adapters share the Translator surface, which is
        # all a plugin reads through this attribute.
        self.locale_manager: Translator
        if use_legacy_locale:
            self.locale_manager = LegacyLocaleManager(os.path.join(self.PATH, legacy_dir))
        else:
            self.locale_manager = LocaleManager(os.path.join(self.PATH, "locales.csv"))
        self.locale_manager.set_to_os_default()

        self.action_holders: "dict[str, ActionHolder]" = {}

        self.action_holder_groups: set[ActionHolderGroup] = set()

        self.event_holders: "dict[str, EventHolder]" = {}

        self.registered: bool = False

        self.plugin_name: str | None = None

        self.asset_manager: AssetManager = AssetManager(self)
        self.asset_manager.load_assets()

        self.has_plugin_settings: bool = False
        self.first_setup: bool = True

        self.registered_pages: list[str] = []

    def get_plugin_id(self) -> str:
        """Read the plugin id from the manifest.

        Without an id in the manifest it uses the folder name.

        Returns:
            str: The plugin ID.
        """
        # Memoized per instance, so the instance frees the cache.
        cached = getattr(self, "_plugin_id_cache", None)
        if cached is not None:
            return cast(str, cached)
        manifest = self.get_manifest()
        self._plugin_id_cache = manifest.get("id") or self.get_plugin_id_from_folder_name()
        return cast(str, self._plugin_id_cache)

    def _resolve_settings_path(self) -> str:
        """Return the manifest-id settings path.
        Migrate folder-name settings so reinstalling a differently named plugin keeps its data."""
        # Decide from settings.json, not the directory alone; an incomplete id
        # directory must not hide valid folder-name settings.
        plugins_root = os.path.join(gl.DATA_PATH, "settings", "plugins")
        folder_name = self.get_plugin_id_from_folder_name()
        plugin_id = self.get_plugin_id()
        id_dir = os.path.join(plugins_root, plugin_id)
        id_settings_path = os.path.join(id_dir, "settings.json")

        if plugin_id != folder_name:
            legacy_settings_dir = os.path.join(plugins_root, folder_name)
            legacy_settings_path = os.path.join(legacy_settings_dir, "settings.json")

            if os.path.isfile(id_settings_path):
                # Prefer existing id-path settings, but preserve and report a
                # second folder-name copy.
                if os.path.isfile(legacy_settings_path):
                    log.warning(
                        f"Plugin {plugin_id}: settings exist under both {id_dir} "
                        f"(used) and {legacy_settings_dir} (ignored, left in place)"
                    )
            elif os.path.isfile(legacy_settings_path):
                # Quarantine exposes legacy settings to migration and can
                # restore older data, so warn before that fallback becomes active.
                try:
                    quarantined_files = sorted(
                        e for e in os.listdir(id_dir)
                        if e.startswith("settings.json.corrupt")
                    )
                except OSError:
                    quarantined_files = []
                if quarantined_files:
                    log.warning(
                        f"Plugin {plugin_id}: the id-path settings in {id_dir} were "
                        f"quarantined ({', '.join(quarantined_files)}) and the legacy "
                        f"folder-name settings in {legacy_settings_dir} are being migrated in "
                        f"-- the plugin will come back with those OLDER settings, not "
                        f"the quarantined ones"
                    )
                # The legacy folder-name path holds the only settings, so
                # migrate them to the id path and keep the plugin's data.
                try:
                    if not os.path.exists(id_dir):
                        # The fast path moves the whole directory, which keeps
                        # the sibling files beside settings.json.
                        os.makedirs(plugins_root, exist_ok=True)
                        os.rename(legacy_settings_dir, id_dir)
                    else:
                        # Complete an existing id directory with the legacy
                        # files, then remove the legacy directory if empty.
                        os.makedirs(id_dir, exist_ok=True)
                        for entry in os.listdir(legacy_settings_dir):
                            dest = os.path.join(id_dir, entry)
                            if not os.path.exists(dest):
                                os.rename(os.path.join(legacy_settings_dir, entry), dest)
                        # Ignore a remaining name collision or directory-removal
                        # failure; both leave the selected settings intact.
                        with contextlib.suppress(OSError):
                            os.rmdir(legacy_settings_dir)
                    log.info(
                        f"Plugin {plugin_id}: migrated settings from folder-name "
                        f"path {legacy_settings_dir} to id path {id_dir}"
                    )
                except OSError as e:
                    # Keep reading the settings where they are, instead of an
                    # empty start.
                    log.opt(exception=e).error(
                        f"Plugin {plugin_id}: could not migrate settings dir "
                        f"{legacy_settings_dir} -> {id_dir}; keeping the folder-name path"
                    )
                    return legacy_settings_path

        return id_settings_path

    def register(self, plugin_name: str | None = None, github_repo: str | None = None, plugin_version: str | None = None,
                 app_version: str | None = None) -> None:
        """Register a plugin with the given information.

        Args:
            plugin_name (str, optional): The name of the plugin. Defaults to None.
            github_repo (str, optional): The GitHub repository of the plugin. Defaults to None.
            plugin_version (str, optional): The version of the plugin. Defaults to None.
            app_version (str, optional): The version of Deckard. Defaults to None.

        Raises:
            ValueError: If the plugin name is not specified or if the plugin already exists.

        Returns:
            None
        """

        manifest = self.get_manifest()
        self.plugin_name = plugin_name or manifest.get("name") or None
        self.github_repo = github_repo or manifest.get("github") or None
        self.plugin_version = plugin_version or manifest.get("version") or None
        self.min_app_version = manifest.get("minimum-app-version")
        self.app_version = app_version or manifest.get("app-version")
        self.plugin_id = self.get_plugin_id()

        if self.plugin_name in ["", None]:
            log.error("Plugin: Please specify a plugin name")
            return
        if self.plugin_id in ["", None]:
            log.error(f"Plugin: {self.plugin_name}: Please specify a plugin id")
            return
        if self.github_repo in ["", None]:
            log.error(f"Plugin: {self.plugin_name}: Please specify a github repo")
            return
        if self.plugin_version in ["", None]:
            log.error(f"Plugin: {self.plugin_name}: Please specify a plugin version")
            return
        if self.app_version is None or self.app_version == "":
            log.error(f"Plugin: {self.plugin_name}: Please specify a app version")
            return


        for plugin_id in PluginBase.plugins.keys():
            plugin = PluginBase.plugins[plugin_id]["object"]
            if plugin.plugin_name == self.plugin_name:
                log.error(f"Plugin: {self.plugin_name}: Plugin already exists")
                return
            
        # Compatibility-check exceptions visibly disable the plugin; they do not unwind __init__.
        # This keeps it in disabled_plugins instead of absent from both registries.
        version_check_failed = False
        try:
            app_version_matches = self.is_app_version_matching()
        except Exception as e:
            log.opt(exception=e).error(
                f"Plugin {self.plugin_id}: could not check version compatibility "
                f"(app-version={self.app_version!r}, minimum-app-version={self.min_app_version!r}). Disabling plugin."
            )
            app_version_matches = False
            version_check_failed = True

        if app_version_matches:
            PluginBase.plugins[self.plugin_id] = {
                "object": self,
                "plugin_version": self.plugin_version,
                "minimum_app_version": self.min_app_version,
                "github": self.github_repo,
                "folder_path": os.path.dirname(inspect.getfile(self.__class__)),
                "file_name": os.path.basename(inspect.getfile(self.__class__))
            }
            self.registered = True

            settings = self.get_settings()
            self.first_setup = settings.get("first-setup", True)
        else:
            reason = "invalid-version" if version_check_failed else None

            if not version_check_failed:
                try:
                    min_app_version = self._get_parsed_base_version(self.min_app_version)
                    parsed_app_version = self._get_parsed_base_version(gl.app_version)
                    if min_app_version is not None and parsed_app_version is not None and min_app_version > parsed_app_version:
                        # The plugin is newer than this Deckard.
                        log.warning(
                            f"Plugin {self.plugin_id} is not compatible with this version of Deckard. "
                            f"Please update Deckard! Plugin requires app version {self.min_app_version} "
                            f"you are running version {gl.app_version}. Disabling plugin."
                        )
                        reason = "app-out-of-date"

                    elif version.parse(self.app_version).major != version.parse(gl.app_version).major:
                        # The plugin is older than this Deckard.
                        max_version = f"{version.parse(self.app_version).major}.x.x"
                        log.warning(
                            f"Plugin {self.plugin_id} is not compatible with this version of Deckard. "
                            f"Please update your assets! Plugin requires an app version between {self.min_app_version} and {max_version} "
                            f"you are running version {gl.app_version}. Disabling plugin."
                        )
                        reason = "plugin-out-of-date"
                except Exception as e:
                    # The and in is_app_version_matching() short-circuits, so
                    # a malformed minimum-app-version can appear here first.
                    log.opt(exception=e).error(
                        f"Plugin {self.plugin_id}: could not determine the disable reason from its version "
                        f"metadata (app-version={self.app_version!r}, minimum-app-version={self.min_app_version!r})."
                    )
                    reason = "invalid-version"

            PluginBase.disabled_plugins[self.plugin_id] = {
                "object": self,
                "plugin_version": self.plugin_version,
                "minimum_app_version": self.min_app_version,
                "github": self.github_repo,
                "folder_path": os.path.dirname(inspect.getfile(self.__class__)),
                "file_name": os.path.basename(inspect.getfile(self.__class__)),
                "reason": reason
            }

    def _get_parsed_base_version(self, version_str: str | None) -> "version.Version | None":
        """Parse a version string and return the base version.

        Args:
            version_str (str): The version string to parse.

        Returns:
            version.Version: The parsed base version.

        Raises:
            None.
        """
        if version_str is None:
            return None
        base_version = version.parse(version_str).base_version
        return version.parse(base_version)

    def get_plugin_id_from_folder_name(self) -> str:
        """Read the plugin id from the folder name of the subclass file.

        Returns:
            str: The plugin id from the folder name.
        """
        module = importlib.import_module(self.__module__)
        subclass_file = module.__file__
        if subclass_file is None:
            # Plugins must load from a file-backed folder; reject namespace or
            # built-in modules before os.path.abspath receives None.
            raise RuntimeError(f"Plugin module {self.__module__} has no file location")
        return os.path.basename(os.path.dirname(os.path.abspath(subclass_file)))
    
    def is_minimum_version_ok(self) -> bool:
        """Check that the app meets the minimum version of the plugin.

        Returns:
            bool: True when the app meets the minimum version.
        """
        if self.min_app_version is None:
            return True
        
        app_version = self._get_parsed_base_version(gl.app_version)
        min_app_version = self._get_parsed_base_version(self.min_app_version)
        if app_version is None or min_app_version is None:
            # The parser type permits None, but the constant and early return
            # make it unreachable here; preserve the no-pin result.
            return True

        return bool(app_version >= min_app_version)

    def are_major_versions_matching(self) -> bool:
        """Check that the major versions of the app and the plugin match.

        Returns:
            bool: True when the major versions match.
        """
        app_version = version.parse(gl.app_version)
        if self.app_version is None:
            # The loader disables a plugin without a stated app version
            # before this check runs.
            return False
        # Use the app version the plugin states, not its minimum app version.
        current_app_version = version.parse(self.app_version)

        return bool(app_version.major == current_app_version.major)

    def is_app_version_matching(self) -> bool:
        """Check that the app version fits this plugin.

        Returns:
            bool: True when the app version fits the plugin.
        """
        return self.are_major_versions_matching() and self.is_minimum_version_ok()

    def add_action_holder(self, action_holder: ActionHolder) -> None:
        """Add an action holder to the plugin.

        Args:
            action_holder (ActionHolder): The action holder to be added.

        Raises:
            ValueError: If action_holder is not an instance of ActionHolder.

        Returns:
            None
        """
        if not isinstance(action_holder, ActionHolder):
            raise ValueError("Please pass an ActionHolder")
        
        if not action_holder.get_is_compatible():
            return
        
        self.action_holders[action_holder.action_id] = action_holder

    def add_action_holders(self, action_holders: list[ActionHolder]) -> None:
        for action_holder in action_holders:
            self.add_action_holder(action_holder)

    def add_event_holder(self, event_holder: EventHolder) -> None:
        """Add an event holder to the plugin.

        Args:
            event_holder (EventHolder): The event holder

        Raises:
            ValueError: If the event holder is not an EventHolder

        Returns:
            None
        """
        if not isinstance(event_holder, EventHolder):
            raise ValueError("Please pass an SignalHolder")

        self.event_holders[event_holder.event_id] = event_holder

    def add_event_holders(self, event_holders: list[EventHolder]) -> None:
        for event_holder in event_holders:
            self.add_event_holder(event_holder)

    def add_action_holder_group(self, action_holder_group: ActionHolderGroup) -> None:
        self.action_holder_groups.add(action_holder_group)

    def add_action_holder_groups(self, action_holder_groups: list[ActionHolderGroup]) -> None:
        self.action_holder_groups.update(action_holder_groups)

    def connect_to_event(self, callback: Callable[..., Any], event_id: str | None = None, event_id_suffix: str | None = None) -> None:
        """Connect a callback to the event with this event id.

        Args:
            callback (callable): The callback the event calls
            event_id (str): The full id of the event. Pass event_id_suffix
                instead to address this plugin's own
                "<plugin_id>::<suffix>" events.

        Returns:
            None
        """
        full_id = event_id or f"{self.get_plugin_id()}::{event_id_suffix}"

        if full_id in self.event_holders:
            self.event_holders[full_id].add_listener(callback)
        else:
            log.warning(f"{full_id} does not exist in {self.plugin_name}")

    def connect_to_event_directly(self, plugin_id: str, event_id: str, callback: Callable[..., Any]) -> None:
        """Connect a callback to the plugin with this plugin id.

        Args:
            plugin_id (str): The id of the plugin
            event_id (str): The id of the event
            callback (callable): The callback the event calls

        Returns:
            None
        """
        plugin = self.get_plugin(plugin_id)
        if plugin is None:
            log.warning(f"{plugin_id} does not exist")
        else:
            plugin.connect_to_event(callback=callback, event_id=event_id)

    def disconnect_from_event(self, event_id: str | None = None, callback: Callable[..., Any] | None = None, event_id_suffix: str | None = None) -> None:
        """Disconnect a callback from the event with this event id.

        Args:
            event_id (str): The full id of the event. Pass event_id_suffix
                instead to address this plugin's own
                "<plugin_id>::<suffix>" events, as connect_to_event does.
            callback (callable): The callback to remove

        Returns:
            None
        """
        full_id = event_id or f"{self.get_plugin_id()}::{event_id_suffix}"

        if full_id in self.event_holders:
            if callback is not None:
                self.event_holders[full_id].remove_listener(callback)
        else:
            log.warning(f"{full_id} does not exist in {self.plugin_name}")

    def disconnect_from_event_directly(self, plugin_id: str, event_id: str, callback: Callable[..., Any]) -> None:
        """Disconnect a callback from the plugin with this plugin id.

        Args:
            plugin_id (str): The id of the plugin
            event_id (str): The full id of the event
            callback (callable): The callback to remove

        Returns:
            None
        """
        plugin = self.get_plugin(plugin_id)
        if plugin is None:
            log.warning(f"{plugin_id} does not exist")
        else:
            plugin.disconnect_from_event(event_id=event_id, callback=callback)

    # Guard lazy per-instance lock creation for rpyc or harness instances built
    # through __new__ without __init__.
    _settings_lock_creation_lock = threading.Lock()

    def _get_settings_lock(self) -> threading.Lock:
        lock = getattr(self, "_settings_lock", None)
        if lock is None:
            with PluginBase._settings_lock_creation_lock:
                lock = getattr(self, "_settings_lock", None)
                if lock is None:
                    lock = threading.Lock()
                    self._settings_lock = lock
        return cast("threading.Lock", lock)

    def get_settings(self) -> "dict[str, Any]":
        """Read the settings from the settings file.

        Returns:
            dict: The stored settings, or an empty dict without a file.
        """
        # The store owns layout, corrupt-file policy, and format migration;
        # this API owns the outer lock.
        with self._get_settings_lock():
            return PluginSettings(self.settings_path).read()

    def get_manifest(self) -> "dict[str, Any]":
        """Read the manifest file from the plugin's directory.

        Returns:
            dict: The manifest content, or an empty dict without a file.
        """
        manifest_path = os.path.join(self.PATH, "manifest.json")
        if os.path.exists(manifest_path):
            # Invalid manifests must not escape plugin __init__ and make the
            # plugin disappear without a load error.
            try:
                with open(manifest_path, "r") as f:
                    manifest = json.load(f)
            except ValueError as e:
                # Keep source JSON in place; dev plugins can point into Git.
                # The app never writes it; treat invalid data as missing and continue scanning.
                log.error(
                    f"Plugin manifest {manifest_path} contains invalid JSON: {e} -- treating "
                    f"it as empty and leaving it in place (the app never writes plugin "
                    f"source files)"
                )
                return {}
            except OSError as e:
                log.opt(exception=e).error(
                    f"Could not read plugin manifest {manifest_path} -- treating it as empty"
                )
                return {}
            if isinstance(manifest, dict):
                return manifest
            log.error(f"Plugin manifest {manifest_path} does not contain a JSON object -- treating it as empty")
        return {}

    def get_about(self) -> "dict[str, Any]":
        """Read the about file from the plugin's directory.

        A missing about.json, an undecodable one and one that holds no object
        each give an empty dict. An OSError from an unreadable file still
        propagates. An unreadable file is a system problem and not bad
        content, and this file comes from the plugin's source tree.

        Returns:
            dict: The about content, or an empty dict without a file.
        """

        about_path = os.path.join(self.PATH, "about.json")
        if os.path.exists(about_path):
            try:
                with open(about_path, "r") as f:
                    about = json.load(f)
            except ValueError as e:
                # Treat invalid plugin-source JSON as missing; do not quarantine
                # a file that the app never writes and a Git checkout can own.
                log.error(
                    f"Plugin about file {about_path} contains invalid JSON: {e} -- treating "
                    f"it as empty and leaving it in place (the app never writes plugin "
                    f"source files)"
                )
                return {}
            if isinstance(about, dict):
                return about
            # Reject non-object JSON before PluginAbout calls .get().
            log.error(
                f"Plugin about file {about_path} does not contain a JSON object "
                f"-- treating it as empty"
            )
        return {}
    
    def set_settings(self, settings: "dict[str, Any]") -> None:
        """Save the given settings to the settings file.

        Args:
            settings (dict): The settings to save.

        Returns:
            None
        """
        # Use the read-side lock; the store preserves the envelope and writes
        # the file atomically.
        with self._get_settings_lock():
            PluginSettings(self.settings_path).write(settings)


    def add_css_stylesheet(self, path: str) -> None:
        """Add a CSS stylesheet to the style context of the application.

        This marshals the work onto the GTK main loop, because a plugin calls
        it from __init__. __init__ runs on a store worker thread on the
        install path. On the main thread the marshal runs inline.

        Args:
            path (str): The path to the CSS file.

        Returns:
            None
        """
        def _add() -> None:
            css_provider = Gtk.CssProvider()
            css_provider.load_from_path(path)
            display = Gdk.Display.get_default()
            if display is None:
                return
            Gtk.StyleContext.add_provider_for_display(
                display,
                css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        from src.backend.main_loop import run_on_main
        run_on_main(_add)

    def register_page(self, path: str) -> None:
        """Register a page of this plugin for the UI.

        Args:
            path (str): The path of the page to register.

        Returns:
            None
        """
        if gl.page_manager is not None:
            gl.page_manager.register_page(path)
        self.registered_pages.append(path)

    def get_selector_icon(self) -> Gtk.Widget:
        """Return a Gtk.Image widget with the icon "view-paged".

        This marshals the work onto the GTK main loop. GTK4 works on the main
        thread alone, and a plugin override can reach this from another
        thread. On the main thread the marshal runs inline.

        Returns:
            Gtk.Widget: A Gtk.Image widget.
        """
        from src.backend.main_loop import run_on_main
        return cast("Gtk.Widget", run_on_main(lambda: Gtk.Image(icon_name="view-paged")))
    
    def on_uninstall(self) -> None:
        """Unregister the plugin pages and stop a running backend connection.

        The app calls this during the uninstall of the plugin.

        Returns:
            None
        """ 
        for page in self.registered_pages:
            if gl.page_manager is not None:
                gl.page_manager.unregister_page(page)
        try:
            if self.backend is not None:
                self.on_disconnect(self.backend_connection)
        except Exception as e:
            log.error(e)

    def get_plugin(self, plugin_id: str) -> "PluginBase | None":
        """Return the plugin with this plugin id.

        Args:
            plugin_id (str): The id of the plugin to return.

        Returns:
            PluginBase: The plugin object, or None.
        """
        if gl.plugin_manager is None:
            return None
        return gl.plugin_manager.get_plugin_by_id(plugin_id) or None

    # Asset Management

    def add_icon(self, key: str, path: str, size:float=1.0, halign:float=0.0, valign:float=0.0) -> None:
        self.asset_manager.icons.add_asset(key=key, asset=Icon(path=path, size=size, halign=halign, valign=valign))

    def add_color(self, key: str, color: tuple[int, int, int, int]) -> None:
        self.asset_manager.colors.add_asset(key=key, asset=Color(color=color))

    def get_asset_path(self, asset_name: str, subdirs: list[str] | None = None, asset_folder: str = "assets") -> str:
        """
        Helper method that returns paths to plugin assets.

        Args:
            asset_name (str): Name of the Asset File
            subdirs (list[str], optional): Subdirectories. Defaults to [].
            asset_folder (str, optional): Name of the folder where assets are stored. Defaults to "assets".

        Returns:
            str: The full path to the asset
        """

        if not subdirs:
            return os.path.join(self.PATH, asset_folder, asset_name)

        subdir = os.path.join(*subdirs)
        if subdir != "":
            return os.path.join(self.PATH, asset_folder, subdir, asset_name)
        return ""

    def get_settings_area(self) -> "Adw.PreferencesGroup | None":
        pass

    def start_server(self) -> None:
        """Start the rpyc server of the plugin.

        It starts a ThreadedServer, which accepts remote procedure calls for
        the plugin. With a running server it logs a warning and starts none.

        Returns:
            None
        """
        if self.server is not None:
            log.warning("Server already running, skipping...")
            return
        from src.backend.PluginManager.PluginManager import frontend_authenticator

        self.server = ThreadedServer(self, hostname="localhost", port=0, protocol_config={"allow_public_attrs": True},
                                     authenticator=frontend_authenticator)
        threading.Thread(target=self.server.start, name="server_start", daemon=True).start()

    @override
    def on_disconnect(self, conn: "Connection | None") -> None:
        """Handle the disconnection of the rpyc server.

        It releases the rpyc server, the backend connection and the backend
        process. It clears the references here, and the blocking close and
        terminate work runs on a worker thread. See
        _release_backend_resources. A call from the UI thread, such as the
        uninstall path through on_uninstall, therefore never stalls.

        Args:
            conn (Connection | None): The connection to disconnect. It is None
                when the plugin already dropped the connection.

        Returns:
            None
        """
        # Mark deactivation, unload, or disconnect as requested so the
        # registration watchdog does not report the resulting termination.
        self._backend_stop_requested = True
        self._release_backend_resources()

    def _release_backend_resources(self) -> None:
        """Detach and tear down the rpyc server, connection, and process.
        Concurrent callers are safe and idempotent, as in ActionCore."""
        # Detach first: rpyc close can wait; process termination can take 5 seconds.
        # Keep GTK and relaunch free; cancel holds that unregistered backends cannot release.
        self.backend_event_hold.cancel()

        if self.backend_connection is None and self.server is None and self.backend_process is None:
            return

        # Snapshot and detach the backend resources, then close them
        # off-thread.
        server, connection, process = self.server, self.backend_connection, self.backend_process
        self.server = None
        self.backend_connection = None
        self.backend_process = None
        self.backend = None

        if connection is not None and gl.plugin_manager is not None:
            with contextlib.suppress(ValueError):
                gl.plugin_manager.backends.remove(connection)
        if process is not None and gl.plugin_manager is not None:
            with contextlib.suppress(ValueError):
                gl.plugin_manager.backend_processes.remove(process)

        threading.Thread(
            target=self._teardown_backend_resources,
            args=(server, connection, process),
            name="plugin_backend_teardown",
            daemon=True,
        ).start()

    @staticmethod
    def _teardown_backend_resources(server: "ThreadedServer | None", connection: "Connection | None", process: "subprocess.Popen[bytes] | None") -> None:
        # Worker teardown tolerates each close or terminate failure so a hung
        # backend cannot stop the app.
        if connection is not None:
            try:
                connection.close()
            except Exception as e:
                log.error(f"Failed to close backend connection: {e}")
        if server is not None:
            try:
                server.close()
            except Exception as e:
                log.error(f"Failed to close backend server: {e}")
        if process is not None:
            from src.backend.PluginManager.PluginManager import terminate_backend_process
            terminate_backend_process(process)

    def launch_backend(self, backend_path: str, venv_path: str | None = None, open_in_terminal: bool = False) -> None:
        """Launch the backend process of the plugin.

        It starts the rpyc server, builds the command that runs the backend
        script, and runs it in a new subprocess. It can open the backend in a
        new terminal window.

        Args:
            backend_path (str): The path to the backend script.
            venv_path (str, optional): The path to the virtual environment
                whose interpreter runs the backend. Defaults to None, which
                selects this app's own interpreter.
            open_in_terminal (bool, optional): Open the backend in a new
                terminal window. Defaults to False.

        Raises:
            RuntimeError: When the rpyc server is not running after
                start_server(), so the backend has no port to register on.
            ValueError: When backend_path is None or absent, or when a given
                venv_path is absent. The validation stops a bad path here,
                before Popen receives it.

        Returns:
            None
        """
        from src.backend.PluginManager.PluginManager import (
            backend_guard_env,
            build_backend_launch_command,
            ensure_backend_venv,
            inject_backend_guard,
        )

        self.start_server()
        if self.server is None:
            # start_server() sets self.server. An override that does not set
            # it would launch a backend with no port to register on.
            raise RuntimeError("the rpyc server is not running, so the backend has no port to register on")
        port = self.server.port

        # Before the argv, which reads the venv's interpreter and refuses a
        # venv that a Python upgrade stranded.
        if venv_path is not None:
            ensure_backend_venv(venv_path, self.PATH, self.get_plugin_id_from_folder_name())

        command = build_backend_launch_command(backend_path, venv_path, port, open_in_terminal)

        # The guard binds the child server to loopback; the venv copy survives
        # terminal launch, while PYTHONPATH covers a venv-less child.
        if venv_path is not None:
            inject_backend_guard(venv_path)
        elif open_in_terminal:
            log.warning("Terminal backend launch without a venv: no loopback guard reaches the child")

        log.info(f"Launching backend: {command}")
        self._backend_stop_requested = False
        self._backend_launch_generation += 1
        self._backend_via_terminal = open_in_terminal
        # Clear readiness and arm the event hold after validation but before
        # spawn so both belong only to this launch.
        self._backend_ready.clear()
        self.backend_event_hold.arm()
        self.backend_process = subprocess.Popen(command, start_new_session=True, env=backend_guard_env())
        if gl.plugin_manager is not None:
            gl.plugin_manager.backend_processes.append(self.backend_process)

        self.wait_for_backend()
        if self.backend_connection is None:
            # Registration often exceeds the 0.3-second wait during boot;
            # continue watching so a late, failed, or timed-out start is visible.
            self._watch_backend_registration(
                self.backend_process, self._backend_launch_generation
            )

    def _watch_backend_registration(
        self,
        process: subprocess.Popen[bytes],
        launch_generation: int,
        timeout: float = 30.0,
    ) -> None:
        """Report registration latency, process exit, or timeout on a bounded daemon thread.
        Process lifecycle remains owned by launch, disconnect, and termination paths."""
        # Disarm silently after relaunch, a requested stop, or app shutdown.
        plugin_id = self.get_plugin_id_from_folder_name()

        def _watch() -> None:
            start = time.time()
            deadline = start + timeout
            while time.time() < deadline:
                if self._backend_launch_generation != launch_generation:
                    # A relaunch has its own watchdog; do not attribute its
                    # registration or the old process exit to this generation.
                    return
                if self._backend_stop_requested:
                    log.debug(f"Plugin {plugin_id}: backend stop requested before registration; watchdog disarmed")
                    return
                if not gl.threads_running:
                    return
                if self.backend_connection is not None:
                    log.info(f"Plugin {plugin_id}: backend registered after {time.time() - start:.1f}s")
                    return
                if process is not None and process.poll() is not None:
                    log.error(
                        f"Plugin {plugin_id}: backend process exited with code "
                        f"{process.returncode} before registering -- its actions will stay inert"
                    )
                    return
                time.sleep(0.25)
            log.error(
                f"Plugin {plugin_id}: backend did not register within {timeout:.0f}s -- "
                f"its actions will stay inert until it does"
            )

        threading.Thread(target=_watch, name=f"backend_watch_{plugin_id}", daemon=True).start()

    def wait_for_backend(self, tries: int = 3) -> None:
        """Wait for the backend to establish a connection.

        It blocks until register_backend signals the connection, or until the
        timeout expires.

        Args:
            tries (int, optional): A timeout budget in units of 0.1 seconds,
                so the default of 3 waits up to 0.3 seconds. The registration
                wakes this thread at once.

        Returns:
            None
        """
        self._backend_ready.wait(timeout=tries * 0.1)

    def register_backend(self, port: int) -> None:
        """Register the backend connection of the plugin.

        This is an internal method. Do not call it manually. It connects to the
        backend on the given port and adds the connection to the global plugin
        manager.

        Args:
            port (int): The port of the backend.

        Returns:
            None
        """
        from src.backend.PluginManager.PluginManager import terminate_refused_backend, verify_backend_port

        # Verify that the launched child owns a loopback port before exposing
        # this process through netref, then connect to the verified address.
        plugin_id = self.get_plugin_id_from_folder_name()
        try:
            host = verify_backend_port(port, self.backend_process, self._backend_via_terminal, plugin_id)
        except RuntimeError:
            # The backend is exposed or unverifiable. Terminate it, so a
            # LAN-reachable port does not outlive the refused registration.
            terminate_refused_backend(self.backend_process, plugin_id)
            raise
        self.backend_connection = rpyc.connect(host, port, config={"allow_public_attrs": True})
        self.backend = self.backend_connection.root

        if gl.plugin_manager is not None:
            gl.plugin_manager.backends.append(self.backend_connection)

        # Publish the connection before waking waiters; release held events
        # before the slow hook so events from it use the normal path.
        self._backend_ready.set()
        self.backend_event_hold.release()

        # Isolate the plugin hook so it cannot fail the backend's rpyc
        # registration call.
        try:
            self.on_backend_ready()
        except Exception as e:
            log.error(f"Plugin {self.get_plugin_id_from_folder_name()}: on_backend_ready failed: {e}")

    def on_backend_ready(self) -> None:
        """Synchronize backend-dependent state after registration, which can complete late.
        This runs on the rpyc service thread; do not access GTK from it."""
        pass

    def on_app_ready(self) -> None:
        """Start backend or long work once per plugin instance after startup.
        This runs off-main; do not access GTK. __init__ blocks, and on_ready needs a deck."""
        pass

    def ping(self) -> bool:
        """Check that the plugin answers.

        Returns:
            bool: Always True.
        """
        return True

    def request_dbus_permission(self, name: str, bus: str = "session", description: str | None = None) -> None:
        """Request a DBus permission for the plugin.

        It shows a dialog that requests the DBus permission for the given bus,
        name and description. Without a description it uses a default one.

        Args:
            name (str): The name of the bus.
            bus (str, optional): The bus type, "session" or "system".
                Defaults to "session".
            description (str, optional): Why the plugin needs the permission.
                Defaults to None.

        Raises:
            ValueError: When the plugin requests a permission before it
                registers.

        Returns:
            None
        """
        if description is None:
            description = gl.lm.get("permissions.request.plugin-blueprint")
            if self.plugin_name is None:
                raise ValueError("Register the plugin before requesting permissions")
            description = description.replace("{name}", self.plugin_name)
        gl.flatpak_permission_manager.show_dbus_permission_request_dialog(name, bus, description)

    def get_config_rows(self) -> list[Adw.PreferencesRow]:
        return []
