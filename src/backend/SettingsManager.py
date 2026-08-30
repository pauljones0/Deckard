"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
from src.backend import settings_store

# Re-export app schemas and views from the legacy facade used by existing callers.
from src.backend.settings_store import (  # noqa: F401
    APP_DEFAULTS as DEFAULTS,
    APP_FONT_DEFAULTS as FONT_DEFAULTS,
    AppSettings as AppSettings,
)
from typing import cast, Any


class SettingsManager:
    def __init__(self) -> None:
        self.font_defaults: dict[str, Any] = {}
        self.load_font_defaults()

    @staticmethod
    def load_settings_from_file(file_path: str) -> dict[str, Any]:
        data, _corrupt = SettingsManager.load_settings_reporting_corruption(file_path)
        return data

    @staticmethod
    def load_settings_reporting_corruption(file_path: str) -> tuple[dict[str, Any], bool]:
        """Load through the store and return data plus this read's corruption flag.
        Only an existing unparseable file is corrupt; missing or empty objects are not, and recovery must not depend on quarantine success."""
        return settings_store.get().load_file(file_path)

    @staticmethod
    def save_settings_to_file(file_path: str, settings: dict[str, Any]) -> None:
        # The store atomically replaces this file and invalidates only its resolved
        # cache entry; this facade owns no separate cache.
        settings_store.get().save_file(file_path, settings)

    def get_deck_settings(self, deck_serial_number: str) -> dict[str, Any]:
        """
        Retrieves the deck settings for a given deck serial number.
        The cached result is deep-copied per call and invalidated on save.

        Args:
            deck_serial_number (str): The serial number of the deck.

        Returns:
            dict: The deck settings loaded from the file.
        """
        return cast(dict[str, Any], settings_store.get().read(settings_store.DECK, deck_serial_number))

    def save_deck_settings(self, deck_serial_number: str, settings: dict[str, Any]) -> None:
        """
        Saves the settings for a deck.

        The atomic write invalidates only this deck's cached copy.

        Args:
            deck_serial_number (str): The serial number of the deck.
            settings (dict): The settings to save.

        Returns:
            None
        """
        settings_store.get().write(settings_store.DECK, settings, deck_serial_number)

    def deck(self, deck_serial_number: str) -> settings_store.DeckSettings:
        """Return one-read deck settings with defaults and checked writes.
        Build one view per load path and destructure it instead of reading per key."""
        return settings_store.DeckSettings(
            self.get_deck_settings(deck_serial_number), deck_serial_number
        )

    def deck_view(self, settings: dict[str, Any]) -> settings_store.DeckSettings:
        """Wrap deck settings the caller already read without another file access.
        This preserves callers that return empty settings after their deck is gone."""
        return settings_store.DeckSettings(settings)

    def get_app_settings(self) -> dict[str, Any]:
        """Return the current shared app-settings dictionary for visible pre-save edits.
        The store shares one object per cache generation; a write invalidates it so later readers load disk."""
        return cast(dict[str, Any], settings_store.get().read(settings_store.APP))

    def app(self) -> AppSettings:
        """Typed view onto the shared app-settings dict."""
        return AppSettings(self.get_app_settings())

    def app_snapshot(self) -> AppSettings:
        """Return a private disk snapshot for editors that save several changes together.
        Such editors must not expose unfinished changes through the shared settings dictionary."""
        return AppSettings(settings_store.get().read_fresh(settings_store.APP))

    def save_app_settings(self, settings: dict[str, Any]) -> None:
        settings_store.get().write(settings_store.APP, settings)

    def get_static_settings(self) -> dict[str, Any]:
        """
        Returns always the same settings, no matter what the data path is set to.

        STATIC reads the fixed data-path override file; globals.py alone reads it earlier during bootstrap.
        """
        return cast(dict[str, Any], settings_store.get().read(settings_store.STATIC))

    def save_static_settings(self, settings: dict[str, Any]) -> None:
        settings_store.get().write(settings_store.STATIC, settings)

    def load_font_defaults(self) -> None:
        self.font_defaults = self.app().default_font

    def save_font_defaults(self) -> None:
        # Merge only default-font so hold-time, rolling-labels, app-launches,
        # and show-donate-window remain in the general section.
        app = self.app()
        app.default_font = self.font_defaults
        app.save()
