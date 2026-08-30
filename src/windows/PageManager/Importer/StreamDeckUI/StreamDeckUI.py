from functools import lru_cache
import os
from collections.abc import Iterable
import json

from src.backend import services
from src.backend.PageManagement import page_flush
from src.backend import settings_store
from src.backend.atomic_json import atomic_write_json
from src.windows.PageManager.Importer.StreamDeckUI.helper import font_family_from_path, hex_to_rgba255
from src.windows.PageManager.Importer.StreamDeckUI.code_conv import parse_keys_as_keycodes

from loguru import logger as log

import globals as gl

from gi.repository import GLib
from typing import Any

class StreamDeckUIImporter:
    def __init__(self, json_export_path: str):
        self.json_export_path = json_export_path

    @lru_cache(maxsize=None)
    def index_to_page_coords(self, index: int, deck_serial: str) -> str:
        # Find deck
        rows, cols = 3, 5
        deck_manager = gl.app.deck_manager if gl.app is not None else None
        for deck_controller in (deck_manager.deck_controller if deck_manager is not None else []):
            if deck_controller.serial_number() == deck_serial:
                rows, cols = deck_controller.deck.key_layout()
                break
        y = index // cols
        x = index % cols
        return f"{x}x{y}"
    
    def save_json(self, json_path: str, data: dict[str, Any], _retries: int = 3) -> None:
        # This write bypasses setting hooks; StreamDeck-UI has no auto-change rules.
        # Any importer that emits them must refresh window watch state after its loop.
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
            
    def allocate_page_paths(self, deck: str, page_names: Iterable[str]) -> dict[str, str]:
        """Allocate collision-free paths before resolving ChangePage links.

        Numeric suffixes preserve existing pages and imported cross-references.
        """
        pages_dir = os.path.join(gl.DATA_PATH, "pages")
        os.makedirs(pages_dir, exist_ok=True)

        page_paths: dict[str, str] = {}
        allocated: set[str] = set()
        for page_name in page_names:
            base = f"ui_{deck}_{int(page_name) + 1}"
            candidate = os.path.join(pages_dir, f"{base}.json")
            suffix = 2
            while os.path.exists(candidate) or candidate in allocated:
                candidate = os.path.join(pages_dir, f"{base}_{suffix}.json")
                suffix += 1
            allocated.add(candidate)
            page_paths[page_name] = candidate

        return page_paths

    def get_state_map(self, available_states: list[str]) -> dict[str, str]:
        state_numbers = [int(state) for state in available_states]
        state_numbers.sort()

        state_map = {}
        for i, original_number in enumerate(state_numbers):
            state_map[str(i)] = str(original_number)

        return state_map

    def perform_import(self) -> None:
        with open(self.json_export_path) as f:
            self.export = json.load(f)


        for deck in self.export.get("state", {}):
            # Export keys are untrusted deck serials; accept only one plain path
            # component to keep deck and page writes inside the settings tree.
            if os.sep in deck or (os.altsep and os.altsep in deck) or deck in (os.curdir, os.pardir):
                log.error(f"Skipped a deck whose serial points outside the settings directory: {deck!r}")
                continue

            # Merge through the settings store to preserve rotation and key layout
            # and keep its deck-settings cache current.
            with settings_store.get().edit(settings_store.DECK, deck) as preferences:
                preferences.setdefault("brightness", {})["value"] = self.export["state"][deck].get("brightness", 75)
                screensaver = preferences.setdefault("screensaver", {})
                screensaver["enable"] = True
                screensaver["time-delay"] = self.export["state"][deck].get("display_timeout", 5*60)//60
                screensaver["brightness"] = self.export["state"][deck].get("brightness_dimmed", 0)

            # Allocate final paths first to preserve existing pages and imported
            # ChangePage references when collisions need suffixes.
            page_paths = self.allocate_page_paths(deck, self.export["state"][deck].get("buttons", {}).keys())

            for page_name in self.export["state"][deck].get("buttons", {}):
                ## Keys
                page: dict[str, Any] = {}
                page["keys"] = {}

                for button in self.export["state"][deck]["buttons"][page_name]:
                    coords = self.index_to_page_coords(int(button), deck)
                    page["keys"][coords] = {}

                    button_data = self.export["state"][deck]["buttons"][page_name][button]

                    # Support an explicit "states" dictionary and flat button properties.
                    if "states" in button_data and button_data["states"]:
                        states = button_data["states"]
                    else:
                        states = {"0": button_data}

                    state_map = self.get_state_map(available_states=list(states.keys()))
                    for page_state, export_state in state_map.items():
                        state_data = states[export_state]

                        page["keys"][coords].setdefault("states", {})
                        page["keys"][coords]["states"].setdefault(page_state, {})

                        ## Text
                        font_color_hex = state_data.get("font_color")
                        if font_color_hex in [None, ""]:
                            font_color_hex = "#FFFFFFFF"
                        page["keys"][coords]["states"][page_state]["labels"] = {}
                        page["keys"][coords]["states"][page_state]["labels"]["bottom"] = {
                            "text": state_data.get("text", None),
                            "color": hex_to_rgba255(font_color_hex),
                            # Page and LabelManager read only the hyphenated keys.
                            "font-size": None,
                            "font-family": font_family_from_path(state_data.get("font"))
                        }

                        page["keys"][coords]["states"][page_state]["background"] = {}
                        color_hex = state_data.get("background_color")
                        if color_hex not in [None, ""]:
                            page["keys"][coords]["states"][page_state]["background"]["color"] = hex_to_rgba255(color_hex)

                        ## Icon
                        page["keys"][coords]["states"][page_state]["media"] = {}
                        export_icon = state_data.get("icon")
                        if export_icon not in [None, ""]:
                            if os.path.exists(export_icon):
                                asset_id = gl.asset_manager_backend.add(asset_path=export_icon)
                                # Both a refused add and a missing asset resolve to None.
                                asset = (gl.asset_manager_backend.get_by_id(asset_id)
                                         if asset_id is not None else None)
                                if asset is not None:
                                    page["keys"][coords]["states"][page_state]["media"]["path"] = asset["internal-path"]
                                else:
                                    # Skip corrupt or unreadable icons without dropping the page.
                                    log.warning(f"Could not import icon {export_icon}, skipping")
                            else:
                                log.warning(f"Icon {export_icon} not found, skipping")

                        ## Actions
                        page["keys"][coords]["states"][page_state]["actions"] = []

                        # Switch page
                        export_switch_page = state_data.get("switch_page")
                        if str(export_switch_page) != str(int(page_name)+1) and export_switch_page not in [0, "0", None, ""]:
                            if export_switch_page not in [None, ""]:
                                # Convert the 1-based target through the path map so
                                # collision suffixes remain consistent.
                                page_path = None
                                try:
                                    page_path = page_paths.get(str(int(export_switch_page) - 1))
                                except (TypeError, ValueError):
                                    page_path = None
                                if page_path is None:
                                    # Use the fallback filename when the export has no target page.
                                    page_path = os.path.join(gl.DATA_PATH, "pages", f"ui_{deck}_{export_switch_page}.json")
                                action: dict[str, Any] = {
                                    "id": "com_core447_DeckPlugin::ChangePage",
                                    "settings": {
                                        "selected_page": page_path,
                                        "deck_number": None
                                    }
                                }
                                page["keys"][coords]["states"][page_state]["actions"].append(action)

                        # Hotkey
                        if state_data.get("keys") not in [None, ""]:
                            # Keep the sentinel empty after a parse failure so no action
                            # is added; preserve a successful empty parse result.
                            parsed: list[int] | str = ""
                            try:
                                parsed = parse_keys_as_keycodes(state_data["keys"])[0]
                            except Exception as e:
                                log.error(f"Failed to parse keys: {state_data['keys']}. Error: {e}")

                            if parsed not in [None, ""]:
                                action = {
                                    "id": "com_core447_OSPlugin::Hotkey",
                                    "settings": {
                                        "keys": []
                                    }
                                }
                                for key in parsed:
                                    action["settings"]["keys"].append([key, 1]) # Press

                                for key in parsed:
                                    action["settings"]["keys"].append([key, 0]) # Release

                                page["keys"][coords]["states"][page_state]["actions"].append(action)

                        # Write text
                        export_write = state_data.get("write")
                        if export_write not in [None, ""]:
                            action = {
                                "id": "com_core447_OSPlugin::WriteText",
                                "settings": {
                                    "text": export_write
                                }
                            }
                            page["keys"][coords]["states"][page_state]["actions"].append(action)

                        # Command
                        export_command = state_data.get("command")
                        if export_command not in [None, ""]:
                            action = {
                                "id": "com_core447_OSPlugin::RunCommand",
                                "settings": {
                                    "command": export_command
                                }
                            }
                            page["keys"][coords]["states"][page_state]["actions"].append(action)

                        # Brightness
                        export_brightness_change = state_data.get("brightness_change")
                        if export_brightness_change not in [None, "", 0]:
                            if export_brightness_change > 0:
                                action = {
                                    "id": "com_core447_DeckPlugin::IncreaseBrightness",
                                    "settings": {}
                                }
                            else:
                                action = {
                                    "id": "com_core447_DeckPlugin::DecreaseBrightness",
                                    "settings": {}
                                }
                            page["keys"][coords]["states"][page_state]["actions"].append(action)


                page_path = page_paths[page_name]
                # Drop pending writes at the replacement boundary, or a stale
                # write can land after the import and undo it.
                page_flush.get().discard_path(page_path)
                self.save_json(page_path, page)

                page_manager = services.require_page_manager()
                page_manager.refresh_document(page_path)
                page_manager.reload_pages_with_path(page_path)
                log.success(f"Imported page {page_name} as page {os.path.basename(page_path)} on deck {deck}")

            log.success(f"Imported all pages of deck {deck}")

        log.success("Imported all pages from StreamDeck UI")

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
