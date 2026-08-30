from dataclasses import dataclass, field
from typing import TypeVar, override
from PIL import Image

from loguru import logger as log
from packaging import version
from packaging.version import InvalidVersion


def is_min_app_version_satisfied(minimum_app_version: str | None) -> bool:
    """Whether the running base version meets or equals an asset minimum."""
    import globals as gl  # deferred, to keep this leaf module cycle-free

    if minimum_app_version is None:
        return True
    try:
        # Compare parsed base versions so prerelease badges match the install gate
        minimum = version.parse(version.parse(minimum_app_version).base_version)
        running = version.parse(version.parse(gl.app_version).base_version)
        return bool(minimum <= running)
    except (InvalidVersion, TypeError):
        # Malformed or non-string catalog versions stay compatible and do not abort UI
        log.warning(
            f"Unparseable minimum app version {minimum_app_version!r}; assuming compatible"
        )
        return True


@dataclass
class StoreData:
    github: str | None = None # Link to the github repository
    # Preserve None versus empty translations because LocaleManager distinguishes them
    descriptions: dict[str, str] | None = field(default_factory=dict) # All the translations for the description
    short_descriptions: dict[str, str] | None = field(default_factory=dict) # All the translations for the short descriptions
    description: str | None = None # Translated Description of the Content
    short_description: str | None = None # Translated short Description of the Content
    author: str | None = None # Author of the Content
    official: bool | None = None # If the Content is Officially Made or not
    commit_sha: str | None = None # SHA of the github commit that gets used
    local_sha: str | None = None # The local SHA that verifies whether a plugin is installed
    minimum_app_version: str | None = None # Minimum app version that is required to use the Content
    app_version: str | None = None # The Current app version the Plugin is made for
    repository_name: str | None = None # Name of the Repository
    tags: list[str] | None = field(default_factory=list) # If the asset has a compatible version
    is_compatible: bool | None = None

    @property
    def asset_id(self) -> str | None:
        # Each concrete class reads its own id field; see the naming note
        # below the ImageData and LicenceData definitions.
        raise NotImplementedError
    branch: str | None = None # Repo branch to install from; None = the repo default
    verified: bool = False

# The concrete catalog dataclass one fetch pass builds; StoreBackend's
# process_store_data narrows to it through its isinstance filter.
StoreDataT = TypeVar("StoreDataT", bound=StoreData)


@dataclass
class ImageData:
    thumbnail: str | None = None # Path to the Thumbnail used in the Store
    image: Image.Image | None = None # The Image that gets displayed in the Store

@dataclass
class LicenceData:
    copyright: str | None = None
    original_url: str | None = None
    license: str | None = None # The actual licence
    license_descriptions: dict[str, str] | None = field(default_factory=dict) # Translations for the Licence Description

# Shared read-only properties normalize each class's id, name, and version fields
# Catalog asset_id is a manifest id, unlike installed-directory and update-match ids

@dataclass
class PluginData(StoreData, ImageData, LicenceData):
    plugin_name: str | None = None # Name of the Plugin
    plugin_version: str | None = None # Version of the Plugin
    plugin_id: str | None = None # Plugin ID in the com.author.name format

    @property
    @override
    def asset_id(self) -> str | None:
        return self.plugin_id

    @property
    def asset_name(self) -> str | None:
        return self.plugin_name

    @property
    def asset_version(self) -> str | None:
        return self.plugin_version

@dataclass
class IconData(StoreData, ImageData, LicenceData):
    icon_name: str | None = None # Name of the icon
    icon_version: str | None = None # Version of the icons
    icon_id: str | None = None # Icon ID in the com.author.name format

    @property
    @override
    def asset_id(self) -> str | None:
        return self.icon_id

    @property
    def asset_name(self) -> str | None:
        return self.icon_name

    @property
    def asset_version(self) -> str | None:
        return self.icon_version

@dataclass
class WallpaperData(StoreData, ImageData, LicenceData):
    wallpaper_name: str | None = None # Name of the wallpaper
    wallpaper_version: str | None = None # Version of the wallpaper
    wallpaper_id: str | None = None # Icon ID in the com.author.name format

    @property
    @override
    def asset_id(self) -> str | None:
        return self.wallpaper_id

    @property
    def asset_name(self) -> str | None:
        return self.wallpaper_name

    @property
    def asset_version(self) -> str | None:
        return self.wallpaper_version

@dataclass
class SDPlusBarWallpaperData(StoreData, ImageData, LicenceData):
    name: str | None = None # Name of the SD+ Bar wallpaper
    version: str | None = None # Version of the SD+ Bar wallpaper
    id: str | None = None # SD+ Bar wallpaper ID in the com.author.name format

    @property
    @override
    def asset_id(self) -> str | None:
        return self.id

    @property
    def asset_name(self) -> str | None:
        return self.name

    @property
    def asset_version(self) -> str | None:
        return self.version


# Concrete assets combine image, licence, and normalized name/version fields
type StoreAssetData = PluginData | IconData | WallpaperData | SDPlusBarWallpaperData
