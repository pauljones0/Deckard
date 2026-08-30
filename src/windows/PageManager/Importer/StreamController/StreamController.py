import os
import json

from src.backend import services
from src.backend.PageManagement import page_flush
from src.backend.atomic_json import atomic_write_json, require_containment

from loguru import logger as log

import globals as gl

from gi.repository import GLib
from typing import Any

class StreamControllerImporter:
    def __init__(self, json_export_path: str):
        self.json_export_path = json_export_path

    
    def save_json(self, json_path: str, data: dict[str, Any], _retries: int = 3) -> None:
        atomic_write_json(json_path, data)

        loaded = None
        try:
            with open(json_path) as f:
                loaded = json.load(f)
        except Exception as e:
            pass

        if loaded != data:
            if _retries > 0:
                log.error(f"Failed to save {json_path}, trying again ({_retries} retries left)")
                self.save_json(json_path, data, _retries=_retries - 1)
            else:
                log.error(f"Failed to save {json_path} after all retries, giving up")
            
    def perform_import(self) -> None:
        with open(self.json_export_path) as f:
            self.export = json.load(f)

        pages_dir = os.path.join(gl.DATA_PATH, "pages")
        for page_name in self.export:
            page = self.export[page_name]
            page_path = os.path.join(pages_dir, f"{page_name}.json")
            if ".json.json" in page_path:
                page_path = page_path.replace(".json.json", ".json")

            # Export keys are untrusted page names; skip paths outside pages_dir.
            try:
                require_containment(pages_dir, page_path)
            except ValueError:
                log.error(f"Skipped a page whose name points outside the pages directory: {page_name!r}")
                continue

            # Drop pending writes for this page before replacing it, or a stale
            # write can land after the import and restore discarded data.
            page_flush.get().discard_path(page_path)

            self.save_json(page_path, page)

            page_manager = services.require_page_manager()
            page_manager.refresh_document(page_path)
            page_manager.reload_pages_with_path(page_path)

            log.success(f"Imported page {page_name}")

        log.success("Imported all pages from StreamController")

        # Whole-page imports bypass setting hooks, so refresh the window watcher
        # after importing possible auto-change rules.
        if gl.page_manager is not None:
            gl.page_manager.refresh_window_watch_state()

        main_win = services.main_window()
        if main_win is not None:
            sidebar = main_win.get_sidebar()
            if sidebar is not None:
                GLib.idle_add(sidebar.page_selector.update)
        page_manager_window = gl.page_manager_window
        if page_manager_window is not None:
            page_selector = page_manager_window.get_page_selector()
            if page_selector is not None:
                GLib.idle_add(page_selector.load_pages)
        log.success("Updated ui")
