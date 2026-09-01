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
from src.backend.Migration.Migrator import Migrator
from src.backend.atomic_json import atomic_write_json
import json
import os
from typing import override

from loguru import logger as log

import globals as gl

class Migrator_1_5_0_beta_5(Migrator):
    def __init__(self) -> None:
        super().__init__("1.5.0-beta.5")
        
    @override
    def migrate(self) -> None:
        self.migrate_pages()
        self.migrate_plugin_settings()

        self.set_migrated(True)

    def migrate_pages(self) -> None:
        pages_dir = os.path.join(gl.DATA_PATH, "pages")
        if not os.path.exists(pages_dir):
            return
        
        for page_path in os.listdir(pages_dir):
            if not page_path.endswith(".json"):
                continue
            page_path = os.path.join(pages_dir, page_path)
            # Leave a corrupt or unreadable page unchanged so the remaining pages still migrate.
            try:
                with open(page_path, "r") as f:
                    page = json.load(f)

                for key in page.get("keys", {}):
                    if "states" in page["keys"][key]:
                        continue

                    key_dict = page["keys"][key].copy()
                    page["keys"][key].clear()

                    page["keys"][key]["states"] = {}
                    page["keys"][key]["states"]["0"] = key_dict

                    page["keys"][key]["states"]["0"].setdefault("image-control-action", 0)
                    page["keys"][key]["states"]["0"].setdefault("label-control-actions", [0, 0, 0])

                atomic_write_json(page_path, page)
            except (OSError, ValueError) as e:
                log.warning(f"Skipping page during migration, left unchanged: {page_path}: {e}")

    def migrate_plugin_settings(self) -> None:
        if not os.path.exists(gl.PLUGIN_DIR):
            return
        for plugin_dir_name in os.listdir(gl.PLUGIN_DIR):
            old_settings_path = os.path.join(gl.PLUGIN_DIR, plugin_dir_name, "settings.json")
            if not os.path.exists(old_settings_path):
                continue
            try:
                with open(old_settings_path, "r") as f:
                    settings = json.load(f)
            except (OSError, ValueError) as e:
                # Leave an unreadable old file in place for retry and continue with other plugins.
                log.warning(f"Skipping plugin settings during migration, left unchanged: {old_settings_path}: {e}")
                continue

            new_settings_path = os.path.join(gl.DATA_PATH, "settings", "plugins", plugin_dir_name, "settings.json")
            # Write the new copy before removal; an existing destination contains current settings.
            # Never overwrite it with the stale pre-beta.5 copy.
            if not os.path.exists(new_settings_path):
                # The atomic write creates parents and publishes a complete file after fsync.
                # A failure propagates before the old file can be removed.
                atomic_write_json(new_settings_path, settings)

            os.remove(old_settings_path)
