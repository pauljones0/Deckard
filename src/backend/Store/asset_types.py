"""One row per store asset class, so the backend carries one implementation
of work that four types share.

The four asset classes, which are plugins, icon packs, wallpapers and SD+ bar
wallpapers, differ in a handful of names. Those names are the catalog file
that lists them, the directory they install into, and the dataclass that
describes them. They also include the id, name and version field names on
that dataclass. A descriptor names those differences as data, so the prepare,
install and update pipelines exist once and look the right names up here.

Two constraints shape the field types, and both come from how the store is
tested and how the data directory moves under it.

Every cross-method reference the backend makes is a method name, resolved with
getattr(self, name) at call time, and never a bound callable captured here.
The store's tests stub an instance attribute, such as sb.get_all_icons = fake
or sb.install_icon = fake. A descriptor that held a function reference runs the
original past the stub and defeats every one of those tests.

The store window's preview class is a name for the same reason, resolved
against the module that defines the page class when the page builds a row.
The off-main construction test replaces that module global with a recording
stub, and a class captured here at import time runs the real widget past it.
A class object here would also drag the GTK preview module into this import,
which the last paragraph rules out.

An install directory is the name of a backend method, and no path. The backend
resolves gl.DATA_PATH and gl.PLUGIN_DIR when a caller invokes the method, and
the test harness re-points the data directory per process. A path frozen at
import time here points at the wrong tree.

This module reads no globals, and touches neither GTK nor json at import time.
It imports the dataclasses it names and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.windows.Store.StoreData import (
    IconData,
    PluginData,
    SDPlusBarWallpaperData,
    StoreData,
    WallpaperData,
)


@dataclass(frozen=True)
class AssetTypeDescriptor:
    """The names that distinguish one store asset class from the others."""

    # The human label of the class, in the backend log lines and in the store
    # window's install-failure notification. The notification title capitalises
    # the first character and leaves the rest, so "SD+ bar wallpaper" keeps its
    # spelling where str.capitalize would flatten it to "Sd+ bar wallpaper".
    display_name: str
    data_cls: type[StoreData]    # the dataclass a prepared entry becomes
    catalog_file: str            # the store's catalog filename for this class
    base_dir_attr: str           # backend method NAME giving the install dir
    id_field: str                # the data_cls field holding the asset id
    name_field: str              # the data_cls field holding the display name
    version_field: str           # the data_cls field holding the version
    get_all_attr: str            # backend method name that lists every entry
    get_to_update_attr: str      # backend method name that lists the outdated
    update_all_attr: str         # backend method name that reinstalls those
    install_attr: str            # backend method name that installs one entry
    uninstall_attr: str          # backend method name that removes one entry
    get_custom_attr: str | None  # backend method name for user-added entries
    badge_key_prefix: str        # locale key prefix of the preview badges
    search_placeholder_key: str  # locale key of the section search placeholder
    preview_cls_name: str        # preview class name, read from the page module
    is_plugin: bool              # gates the plugin-only install/prepare paths


PLUGIN = AssetTypeDescriptor(
    display_name="plugin",
    data_cls=PluginData,
    catalog_file="Plugins.json",
    base_dir_attr="plugins_dir",
    id_field="plugin_id",
    name_field="plugin_name",
    version_field="plugin_version",
    get_all_attr="get_all_plugins",
    get_to_update_attr="get_plugins_to_update",
    update_all_attr="update_all_plugins",
    install_attr="install_plugin",
    # uninstall_plugin takes the plugin id, where the data-only uninstallers
    # take the record. PluginPreview.uninstall calls it, and the shared
    # preview's record-passing uninstall does not run for this class.
    uninstall_attr="uninstall_plugin",
    get_custom_attr="get_custom_plugins",
    badge_key_prefix="store.badges.plugin",
    search_placeholder_key="store.plugins.search-placeholder",
    preview_cls_name="PluginPreview",
    is_plugin=True,
)

ICON = AssetTypeDescriptor(
    display_name="icon pack",
    data_cls=IconData,
    catalog_file="Icons.json",
    base_dir_attr="icons_dir",
    id_field="icon_id",
    name_field="icon_name",
    version_field="icon_version",
    get_all_attr="get_all_icons",
    get_to_update_attr="get_icons_to_update",
    update_all_attr="update_all_icons",
    install_attr="install_icon",
    uninstall_attr="uninstall_icon",
    get_custom_attr=None,
    badge_key_prefix="store.badges.icon",
    search_placeholder_key="store.icons.search-placeholder",
    preview_cls_name="StoreAssetPreview",
    is_plugin=False,
)

WALLPAPER = AssetTypeDescriptor(
    display_name="wallpaper",
    data_cls=WallpaperData,
    catalog_file="Wallpapers.json",
    base_dir_attr="wallpapers_dir",
    id_field="wallpaper_id",
    name_field="wallpaper_name",
    version_field="wallpaper_version",
    get_all_attr="get_all_wallpapers",
    get_to_update_attr="get_wallpapers_to_update",
    update_all_attr="update_all_wallpapers",
    install_attr="install_wallpaper",
    uninstall_attr="uninstall_wallpaper",
    get_custom_attr=None,
    badge_key_prefix="store.badges.wallpaper",
    search_placeholder_key="store.wallpapers.search-placeholder",
    preview_cls_name="StoreAssetPreview",
    is_plugin=False,
)

SD_PLUS_BAR = AssetTypeDescriptor(
    display_name="SD+ bar wallpaper",
    data_cls=SDPlusBarWallpaperData,
    catalog_file="SDPlusBarWallpapers.json",
    base_dir_attr="sd_plus_bar_wallpapers_dir",
    id_field="id",
    name_field="name",
    version_field="version",
    get_all_attr="get_all_sd_plus_bar_wallpapers",
    get_to_update_attr="get_sd_plus_bar_wallpapers_to_update",
    update_all_attr="update_all_sd_plus_bar_wallpapers",
    install_attr="install_sd_plus_bar_wallpaper",
    uninstall_attr="uninstall_sd_plus_bar_wallpaper",
    get_custom_attr=None,
    badge_key_prefix="store.badges.sd_plus_bar_wallpaper",
    # The wallpaper key, and no key of its own. The SD+ bar tab has shown the
    # wallpaper search placeholder since it was written, and a key of its own
    # would need a new string in every locale file.
    search_placeholder_key="store.wallpapers.search-placeholder",
    preview_cls_name="StoreAssetPreview",
    is_plugin=False,
)

ASSET_TYPES: tuple[AssetTypeDescriptor, ...] = (PLUGIN, ICON, WALLPAPER, SD_PLUS_BAR)
