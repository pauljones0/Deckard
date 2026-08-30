"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import json
import shutil
import tempfile
import globals as gl
import os
from packaging import version
from loguru import logger as log

from src.backend.atomic_json import atomic_write_json, prune_corrupt_sidecars, quarantine_corrupt_file
from typing import cast, Any

class Migrator:
    SETTINGS_DIR = os.path.join(gl.DATA_PATH, "settings", "migrations.json")
    def __init__(self, app_version: str):
        self.app_version = app_version
        self.parsed_app_version = version.parse(app_version)

    def get_need_migration(self) -> bool:
        app_version = version.parse(gl.app_version)
        migrator_version = self.parsed_app_version
        if app_version < migrator_version:
            return False

        settings = self.get_settings()
        return not settings.get(self.app_version, False)
    
    def set_migrated(self, migrated: bool) -> None:
        settings = self.get_settings()
        settings[self.app_version] = migrated
        self.set_settings(settings)

    def get_settings(self) -> dict[str, Any]:
        """SettingsManager does not exist yet when a caller reaches this."""
        if not os.path.exists(self.SETTINGS_DIR):
            return {}
        try:
            with open(self.SETTINGS_DIR, "r") as f:
                root = json.load(f)
            # Only an object can hold migration keys; treat any other JSON root as corrupt.
            # Raise into recovery so set_migrated does not overwrite the original file.
            if not isinstance(root, dict):
                raise ValueError(f"root is a JSON {type(root).__name__}, not an object")
            return cast(dict[str, Any], root)
        except ValueError as e:
            # ValueError covers decode errors, invalid JSON, and the non-object check above.
            # Quarantine the file and retry all idempotent, backup-protected migrations.
            moved, dest = quarantine_corrupt_file(self.SETTINGS_DIR)
            if moved:
                log.error(
                    f"Could not read {self.SETTINGS_DIR} ({e}) -- preserved at "
                    f"{dest}, treating all migrations as pending"
                )
                # Bound this file's sidecars; atomic_json is available before SettingsManager.
                for pruned in prune_corrupt_sidecars(self.SETTINGS_DIR, protect=dest):
                    log.info(f"Pruned old quarantined copy {pruned}")
            else:
                log.error(
                    f"Could not read {self.SETTINGS_DIR} ({e}) -- it was NOT moved aside "
                    f"here (rename failed, or another reader quarantined it first), "
                    f"treating all migrations as pending"
                )
            return {}
        except OSError as e:
            # An unreadable file can be healthy, so do not quarantine it after EACCES or EIO.
            # Leave it in place and report all migrations as pending.
            log.error(
                f"Could not read {self.SETTINGS_DIR} ({e}) -- leaving it in place "
                f"(unreadable, not corrupt), treating all migrations as pending"
            )
            return {}
        
    def set_settings(self, settings: dict[str, Any]) -> None:
        """SettingsManager does not exist yet when a caller reaches this."""
        atomic_write_json(self.SETTINGS_DIR, settings)

    def migrate(self) -> None:
        """Apply this migrator's changes. Every concrete migrator defines it."""
        raise NotImplementedError

    def create_backup(self) -> None:
        # Back up pages and plugin settings because migrators can rewrite or delete both trees.
        # A fresh install has neither tree and needs no backup.
        pages_path = os.path.join(gl.DATA_PATH, "pages")
        plugin_settings_path = os.path.join(gl.DATA_PATH, "settings", "plugins")
        sources = [p for p in (pages_path, plugin_settings_path) if os.path.exists(p)]
        if not sources:
            return

        backup_path = os.path.join(gl.DATA_PATH, "backups")
        os.makedirs(backup_path, exist_ok=True)

        # Use the migrator version because chained migrators share gl.app_version.
        # The unique name prevents one migration backup from overwriting another.
        safe_version = self.app_version.replace(os.sep, "_")
        with tempfile.TemporaryDirectory() as staging:
            for src in sources:
                shutil.copytree(src, os.path.join(staging, os.path.basename(src)))

            log.info(f"Creating backup to {backup_path}")
            path = shutil.make_archive(
                base_name=os.path.join(backup_path, f"before_{safe_version}_migration"),
                format="zip",
                root_dir=staging,
            )
        log.success(f"Saved backup to {path}")
