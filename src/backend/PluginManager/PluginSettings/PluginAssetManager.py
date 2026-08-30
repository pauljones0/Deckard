"""
Author: G4PLS
Year: 2024
"""

from typing import Any, TYPE_CHECKING

from .Manager import Manager
from .Asset import Color, Icon
from src.backend.settings_store import PluginSettings

if TYPE_CHECKING:
    # PluginBase imports this module at module scope, so keep this type-only.
    from src.backend.PluginManager.PluginBase import PluginBase


class AssetManager:
    """Manage plugin icon and colour overrides in the app-owned assets section.
    The settings-store surface quarantines corrupt shared settings files."""

    def __init__(self, plugin_base: "PluginBase"):
        self.plugin_base = plugin_base
        self.colors = Manager(Color, "colors")
        self.icons = Manager(Icon, "icons")

    def _settings_file(self) -> PluginSettings:
        """Create settings access from the plugin's current path on each call."""
        return PluginSettings(self.plugin_base.settings_path)

    def load_assets(self) -> "dict[str, Any] | None":
        # This can be the only read for an unregistered plugin, so corrupt files
        # must be quarantined here without stopping plugin loading.
        content = self._settings_file().document()
        if content is None:
            return {}

        assets = content.get("assets", {})
        self.icons.load_json(assets)
        self.colors.load_json(assets)
        return None

    def save_assets(self) -> None:
        assets = {}
        assets[self.colors.get_save_key()] = self.colors.get_override_json()
        assets[self.icons.get_save_key()] = self.icons.get_override_json()

        # Preserve non-asset settings before the wholesale write; an unreadable
        # file becomes an empty document without raising in UI callers.
        settings = self._settings_file()
        content = settings.document() or {}
        content["assets"] = assets
        settings.save_document(content)
