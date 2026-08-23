"""
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

The plugin tab of the store, and the card it builds.

Both subclass the descriptor-driven classes in AssetPage, and both hold only
what a plugin does differently: a catalog that can carry an empty row, an
install button that reads a compatibility gate, and an uninstall that names an
id instead of a record.
"""
# Import gtk modules
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib

# Import python modules
from loguru import logger as log

# Import own modules
from src.backend.Store import asset_types
from src.windows.Store.AssetPage import StoreAssetPage, StoreAssetPreview
from src.windows.Store.StoreData import StoreAssetData


class PluginPage(StoreAssetPage):
    descriptor = asset_types.PLUGIN

    def skip_entry(self, asset: StoreAssetData) -> bool:
        # A catalog row that prepared to an empty record shows nothing, and
        # every read below it would raise on the loader thread.
        return not asset


class PluginPreview(StoreAssetPreview):
    """One plugin card.

    update() reinstalls through install_plugin, which deregisters the old
    version only after the download succeeds. A failed update therefore leaves
    the old version installed and registered, and no recovery reload runs.
    """

    # A plugin card takes no red border. The store already shows an
    # incompatible plugin in the incompatible section.
    shows_incompatible_border = False

    def _install_kwargs(self) -> "dict[str, object]":
        # Only a plugin carries an install script, so only a plugin install
        # prompts. The dialog is transient for the store window and answered
        # on the main loop; install() itself runs on a download worker.
        from src.windows.Store.install_consent import make_consent
        return {"ask_install_script": make_consent(self.store)}

    @staticmethod
    def get_install_state_for(asset_data: StoreAssetData) -> int:
        """0 is not installed, 1 is installed, and 2 is update available.

        An installed plugin whose pinned store version is incompatible reads
        as state 1. is_compatible is then False, because prepare_plugin pins
        the newest commit of another app major when no compatible one exists.
        That update would replace a working plugin with an incompatible build,
        which get_plugins_to_update also refuses on the auto-update path.

        The parameter widens to the whole catalog family, because it overrides
        the data-only gate. Every field it reads is one that all four classes
        carry.
        """
        if asset_data.local_sha is None:
            return 0
        if asset_data.local_sha == asset_data.commit_sha:
            return 1
        if asset_data.commit_sha is None:
            # The remote tip is unresolved, because get_last_commit returned
            # None for a branch-pinned plugin, after a 429 or an empty answer.
            # A None commit_sha differs from local_sha, which would show an
            # update-available badge whose install can only return 404. No
            # known target exists to update to.
            return 1
        if asset_data.is_compatible is False:
            return 1
        return 2

    def uninstall(self) -> None:
        backend = self.store.backend
        plugin_id = self.asset_data.asset_id
        if backend is None or plugin_id is None:
            # A record with no id uninstalls nothing, so the button must keep
            # saying installed rather than report a removal that never ran.
            log.error(f"Store backend unavailable; cannot uninstall {plugin_id}"
                      if backend is None else
                      "Cannot uninstall a store plugin whose record carries no id")
            return
        # uninstall_plugin takes the id, where the data-only uninstallers take
        # the record, so this one call does not go through the descriptor.
        backend.uninstall_plugin(plugin_id=plugin_id)
        GLib.idle_add(self.set_install_state, 0)


# check_required_version comes from StorePreview, which calls the one
# implementation in StoreData.is_min_app_version_satisfied.
