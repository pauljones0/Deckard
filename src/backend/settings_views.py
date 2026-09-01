"""Implement sparse schema views plus deck, app, and plugin settings adapters.
settings_store re-exports them after defining specs and get(); callers use that entry point."""
from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from typing import cast, Any

from loguru import logger as log

from src.backend.settings_store import (
    APP,
    APP_DEFAULTS,
    APP_FONT_DEFAULTS,
    DECK,
    DECK_DEFAULTS,
    DECK_NAME_MAX_LENGTH,
    PLUGIN,
    PLUGIN_FILE_VERSION,
    get,
)


#: Device-product fallback for missing name, model, and serial; it is not a
#: sentence and intentionally stays outside localization.
UNNAMED_DECK = "Stream Deck"


def _copied(value: Any) -> Any:
    """Deep-copy containers and return scalars unchanged.
    Copy schema containers so one mutation cannot change later default reads."""
    return copy.deepcopy(value) if isinstance(value, (dict, list)) else value


class SchemaView:
    """Wrap one settings read: absent keys use copied defaults, and unknown writes raise.
    Stored values copy unless shared=True throughout; defaults copy; reads do not persist them."""

    def __init__(self, data: dict[str, Any], schema: Mapping[str, Any], shared: bool = False):
        self.data: dict[str, Any] = data
        self.schema: Mapping[str, Any] = schema
        #: Hand stored values back by reference rather than as copies.
        self.shared: bool = shared

    def get(self, name: str, key: str | None = None) -> Any:
        """Return a top-level or section setting from storage or its schema default.
        Unknown names and keys raise instead of becoming None for the rest of the run."""
        if key is None:
            default = self._top_level(name)
            return self._stored_value(self.data[name]) if name in self.data else _copied(default)
        defaults = self._section_defaults(name)
        if key not in defaults:
            raise KeyError(f"{name}.{key} is not in this schema")
        stored = self._stored_section(name)
        return self._stored_value(stored[key]) if key in stored else _copied(defaults[key])

    def section(self, name: str) -> dict[str, Any]:
        """Return a copied section with absent schema keys filled and unknown stored keys retained.
        The result aliases neither source; saving it would persist filled defaults."""
        merged = {k: _copied(v) for k, v in self._section_defaults(name).items()}
        merged.update(copy.deepcopy(dict(self._stored_section(name))))
        return merged

    def set_section_value(self, name: str, key: str, value: Any) -> None:
        """Store one setting inside section name. This writes nothing else,
        so every key still absent keeps following the schema."""
        if key not in self._section_defaults(name):
            raise KeyError(f"{name}.{key} is not in this schema")
        section = self.data.get(name)
        if not isinstance(section, dict):
            # The section is absent, or a hand edit left a scalar there. No
            # section exists to add to, and the caller chose this value.
            section = {}
            self.data[name] = section
        section[key] = value

    def set_top_level_value(self, name: str, value: Any) -> None:
        """Store one top-level setting, which the file holds as a bare
        value."""
        self._top_level(name)
        self.data[name] = value

    def _stored_value(self, value: Any) -> Any:
        """Return a stored value by reference for shared views and by copy otherwise."""
        return value if self.shared else _copied(value)

    def _section_defaults(self, name: str) -> Mapping[str, Any]:
        try:
            section = self.schema[name]
        except KeyError:
            raise KeyError(f"{name} is not in this schema") from None
        if not isinstance(section, Mapping):
            raise KeyError(f"{name} is a top-level setting in this schema, not a section")
        return section

    def _top_level(self, name: str) -> Any:
        try:
            default = self.schema[name]
        except KeyError:
            raise KeyError(f"{name} is not in this schema") from None
        if isinstance(default, Mapping):
            raise KeyError(f"{name} is a section in this schema: name a key inside it")
        return default

    def _stored_section(self, name: str) -> Mapping[str, Any]:
        stored = self.data.get(name)
        # A hand-edited or half-written file can leave a scalar where a
        # section belongs. Answer from the schema then.
        return stored if isinstance(stored, Mapping) else {}


class DeckSettings(SchemaView):
    """Copy one deck's DECK_DEFAULTS-backed settings; only serial-backed views can save.
    Each store read and value handout is private, so edits remain caller-owned until save."""

    def __init__(self, data: dict[str, Any], serial: str | None = None):
        super().__init__(data, DECK_DEFAULTS)
        self.serial: str | None = serial

    def display_name(self, model_name: str | None = None) -> str:
        """Return stripped chosen name, model, serial, or UNNAMED_DECK, in that order.
        Limit only the chosen name to DECK_NAME_MAX_LENGTH; preserve model and serial."""
        stored = self.get("name")
        chosen = stored.strip()[:DECK_NAME_MAX_LENGTH] if isinstance(stored, str) else ""
        if chosen:
            return cast(str, chosen)
        if isinstance(model_name, str) and model_name.strip():
            return model_name.strip()
        if isinstance(self.serial, str) and self.serial.strip():
            return self.serial.strip()
        # Every source was empty. A deck with no name, no model and no serial
        # is a broken read rather than a state to render blank.
        return UNNAMED_DECK

    def save(self) -> None:
        """Atomically persist this serial-backed view and invalidate its deck cache."""
        if self.serial is None:
            raise ValueError(
                "this deck-settings view wraps a dict, not a deck: it has no file to save to"
            )
        get().write(DECK, self.data, self.serial)


class AppSettings(SchemaView):
    """Alias an APP_DEFAULTS-backed mapping so stored-container edits can persist on save.
    Missing defaults copy; the caller chooses a shared mapping or private snapshot."""

    def __init__(self, data: dict[str, Any]):
        super().__init__(data, APP_DEFAULTS, shared=True)

    def save(self) -> None:
        """Persist the whole wrapped dict atomically, and drop the shared
        copy, so the next reader loads what this write left."""
        get().write(APP, self.data)

    @property
    def hold_time(self) -> float:
        return cast(float, self.get("general", "hold-time"))

    @hold_time.setter
    def hold_time(self, value: float) -> None:
        self.set_section_value("general", "hold-time", value)

    @property
    def rolling_labels(self) -> bool:
        return cast(bool, self.get("general", "rolling-labels"))

    @rolling_labels.setter
    def rolling_labels(self, value: bool) -> None:
        self.set_section_value("general", "rolling-labels", value)

    @property
    def shrink_on_press(self) -> bool:
        return cast(bool, self.get("general", "shrink-on-press"))

    @shrink_on_press.setter
    def shrink_on_press(self, value: bool) -> None:
        self.set_section_value("general", "shrink-on-press", value)

    @property
    def app_launches(self) -> int:
        return cast(int, self.get("general", "app-launches"))

    @app_launches.setter
    def app_launches(self, value: int) -> None:
        self.set_section_value("general", "app-launches", value)

    @property
    def show_donate_window(self) -> bool:
        return cast(bool, self.get("general", "show-donate-window"))

    @show_donate_window.setter
    def show_donate_window(self, value: bool) -> None:
        self.set_section_value("general", "show-donate-window", value)

    @property
    def default_font(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.get("general", "default-font"))

    @default_font.setter
    def default_font(self, value: dict[str, Any]) -> None:
        self.set_section_value("general", "default-font", value)

    def font_default(self, key: str) -> Any:
        """A general.default-font subkey. A falsy stored value falls back to
        the default too."""
        default = APP_FONT_DEFAULTS[key]
        if callable(default):
            default = default()
        return self.default_font.get(key) or default

    @property
    def tray_icon(self) -> bool:
        return cast(bool, self.get("ui", "tray-icon"))

    @tray_icon.setter
    def tray_icon(self, value: bool) -> None:
        self.set_section_value("ui", "tray-icon", value)

    @property
    def allow_white_mode(self) -> bool:
        return cast(bool, self.get("ui", "allow-white-mode"))

    @allow_white_mode.setter
    def allow_white_mode(self, value: bool) -> None:
        self.set_section_value("ui", "allow-white-mode", value)

    @property
    def show_notifications(self) -> bool:
        return cast(bool, self.get("ui", "show-notifications"))

    @show_notifications.setter
    def show_notifications(self, value: bool) -> None:
        self.set_section_value("ui", "show-notifications", value)

    @property
    def auto_open_action_config(self) -> bool:
        return cast(bool, self.get("ui", "auto-open-action-config"))

    @auto_open_action_config.setter
    def auto_open_action_config(self, value: bool) -> None:
        self.set_section_value("ui", "auto-open-action-config", value)

    @property
    def emulate_at_double_click(self) -> bool:
        return cast(bool, self.get("key-grid", "emulate-at-double-click"))

    @emulate_at_double_click.setter
    def emulate_at_double_click(self, value: bool) -> None:
        self.set_section_value("key-grid", "emulate-at-double-click", value)

    @property
    def enable_fps_warnings(self) -> bool:
        return cast(bool, self.get("warnings", "enable-fps-warnings"))

    @enable_fps_warnings.setter
    def enable_fps_warnings(self, value: bool) -> None:
        self.set_section_value("warnings", "enable-fps-warnings", value)

    @property
    def keep_running(self) -> bool | None:
        return cast(bool | None, self.get("system", "keep-running"))

    @keep_running.setter
    def keep_running(self, value: bool | None) -> None:
        self.set_section_value("system", "keep-running", value)

    @property
    def autostart(self) -> bool:
        return cast(bool, self.get("system", "autostart"))

    @autostart.setter
    def autostart(self, value: bool) -> None:
        self.set_section_value("system", "autostart", value)

    @property
    def lock_on_lock_screen(self) -> bool:
        return cast(bool, self.get("system", "lock-on-lock-screen"))

    @lock_on_lock_screen.setter
    def lock_on_lock_screen(self, value: bool) -> None:
        self.set_section_value("system", "lock-on-lock-screen", value)

    @property
    def n_cached_pages(self) -> int:
        return cast(int, self.get("performance", "n-cached-pages"))

    @n_cached_pages.setter
    def n_cached_pages(self, value: int) -> None:
        self.set_section_value("performance", "n-cached-pages", value)

    @property
    def cache_videos(self) -> bool:
        return cast(bool, self.get("performance", "cache-videos"))

    @cache_videos.setter
    def cache_videos(self, value: bool) -> None:
        self.set_section_value("performance", "cache-videos", value)

    @property
    def animation_pause_mode(self) -> str:
        return cast(str, self.get("performance", "animation-pause-mode"))

    @animation_pause_mode.setter
    def animation_pause_mode(self, value: str) -> None:
        self.set_section_value("performance", "animation-pause-mode", value)

    @property
    def animation_idle_minutes(self) -> int:
        return cast(int, self.get("performance", "animation-idle-minutes"))

    @animation_idle_minutes.setter
    def animation_idle_minutes(self, value: int) -> None:
        self.set_section_value("performance", "animation-idle-minutes", value)

    @property
    def auto_update(self) -> bool:
        return cast(bool, self.get("store", "auto-update"))

    @auto_update.setter
    def auto_update(self, value: bool) -> None:
        self.set_section_value("store", "auto-update", value)

    @property
    def install_script_policy(self) -> str:
        # Accept only "ask", "always", or "never" for downloaded install code;
        # invalid data falls back to the safe "ask" policy.
        value = self.get("store", "install-scripts")
        return cast(str, value if value in ("ask", "always", "never") else "ask")

    @install_script_policy.setter
    def install_script_policy(self, value: str) -> None:
        self.set_section_value("store", "install-scripts", value)

    @property
    def responsibility_notes_accepted(self) -> bool:
        return cast(bool, self.get("store", "responsibility-notes-agreed"))

    @responsibility_notes_accepted.setter
    def responsibility_notes_accepted(self, value: bool) -> None:
        self.set_section_value("store", "responsibility-notes-agreed", value)

    @property
    def enable_custom_stores(self) -> bool:
        return cast(bool, self.get("store", "enable-custom-stores"))

    @enable_custom_stores.setter
    def enable_custom_stores(self, value: bool) -> None:
        self.set_section_value("store", "enable-custom-stores", value)

    @property
    def enable_custom_plugins(self) -> bool:
        return cast(bool, self.get("store", "enable-custom-plugins"))

    @enable_custom_plugins.setter
    def enable_custom_plugins(self, value: bool) -> None:
        self.set_section_value("store", "enable-custom-plugins", value)

    @property
    def custom_stores(self) -> list[Any]:
        return cast(list[Any], self.get("store", "custom-stores"))

    @custom_stores.setter
    def custom_stores(self, value: list[Any]) -> None:
        self.set_section_value("store", "custom-stores", value)

    @property
    def custom_plugins(self) -> list[Any]:
        return cast(list[Any], self.get("store", "custom-plugins"))

    @custom_plugins.setter
    def custom_plugins(self, value: list[Any]) -> None:
        self.set_section_value("store", "custom-plugins", value)

    @property
    def n_fake_decks(self) -> int:
        return cast(int, self.get("dev", "n-fake-decks"))

    @n_fake_decks.setter
    def n_fake_decks(self, value: int) -> None:
        self.set_section_value("dev", "n-fake-decks", value)

    @property
    def n_remote_decks(self) -> int:
        return cast(int, self.get("dev", "n-remote-decks"))

    @n_remote_decks.setter
    def n_remote_decks(self, value: int) -> None:
        self.set_section_value("dev", "n-remote-decks", value)


class PluginSettings:
    """Retain assets, quarantine decode failures, preserve source files, and read OSError empty.
    No lock here; only PluginBase get/set hold an outer per-plugin lock around store work."""

    def __init__(self, settings_path: str) -> None:
        self.path: str = settings_path

    def document(self) -> dict[str, Any] | None:
        """Return the object, or None for absent, quarantined, unreadable, or non-object content.
        An empty object remains a real document that migration can rewrite."""
        try:
            content, corrupt = get().read_with_corruption_status(PLUGIN, self.path)
        except OSError as e:
            log.opt(exception=e).error(
                f"Could not read plugin settings file {self.path} -- treating it as "
                f"empty; whatever is saved next replaces it"
            )
            return None
        if corrupt:
            return None
        if not content and not os.path.isfile(self.path):
            # Check after reading so an absent or concurrently quarantined file
            # returns None instead of an empty document that migration rewrites.
            return None
        if not isinstance(content, dict):
            log.error(
                f"Plugin settings file {self.path} does not contain a JSON object "
                f"-- treating it as empty"
            )
            return None
        return content

    def save_document(self, document: dict[str, Any]) -> None:
        """Replace the file with document, atomically."""
        get().write(PLUGIN, document, self.path)

    def read(self) -> dict[str, Any]:
        """The plugin's settings, unwrapped, migrating a pre-envelope file once."""
        document = self.document()
        if document is None:
            return {}
        if document.get("file-version") == PLUGIN_FILE_VERSION:
            return cast(dict[str, Any], document.get("settings", {}))
        # Pre-envelope files contain settings at the root; return them even if
        # wrapping fails so migration does not remove settings for this run.
        try:
            self.save_document({"file-version": PLUGIN_FILE_VERSION, "settings": document})
        except OSError as e:
            log.opt(exception=e).error(
                f"Could not migrate plugin settings file {self.path} to the current format"
            )
        return document

    def write(self, settings: Any) -> None:
        """Store settings as the plugin's own keys, atomically."""
        document = self.document()
        if document is None or document.get("file-version") != PLUGIN_FILE_VERSION:
            # Start a new envelope for missing or legacy content, replacing old
            # root settings; valid envelopes retain app-owned siblings such as assets.
            document = {"file-version": PLUGIN_FILE_VERSION}
        document["settings"] = settings
        self.save_document(document)
