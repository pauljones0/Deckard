"""Data descriptors for shared plugin, icon, wallpaper, and SD+ bar workflows.
Method, path, and preview references stay as names for late resolution without GTK or globals."""
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

    # Preserve label spelling when only the first character is capitalized for a title.
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
    # None when removal takes another key and the preview owns the uninstall call.
    uninstall_attr: str | None
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
    # Plugin removal takes an id, so its preview owns the uninstall call.
    uninstall_attr=None,
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
    # Reuse the wallpaper search translation for the SD+ bar tab.
    search_placeholder_key="store.wallpapers.search-placeholder",
    preview_cls_name="StoreAssetPreview",
    is_plugin=False,
)

ASSET_TYPES: tuple[AssetTypeDescriptor, ...] = (PLUGIN, ICON, WALLPAPER, SD_PLUS_BAR)
