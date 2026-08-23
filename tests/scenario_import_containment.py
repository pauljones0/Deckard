"""Page imports must stay inside the pages directory.

Each importer builds a target filename from the untrusted content of an
export file. A crafted page name or deck serial that carries a parent
reference must not push a write outside the pages tree, while a normal name
must still import. Three seams are covered: the StreamController importer, the
single-page add_page seam behind the menu and the external API, and the
StreamDeck-UI importer's deck serial.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import os
import types

import globals as gl
from fixtures import start_watchdog


ESCAPE_MARKER = {"escaped": True}


def _no_escape(failures: list[str], label: str) -> None:
    """Confirm no importer wrote the marker outside the pages directory."""
    for rel in ("sc_escape.json", "add_escape.json",
                os.path.join("outside", "sdui_escape_1.json"),
                os.path.join("settings", "decks", "..", "sdui_escape.json")):
        stray = os.path.join(gl.DATA_PATH, rel)
        if os.path.exists(stray):
            failures.append(f"{label}: a write escaped to {stray}")
    outside_dir = os.path.join(gl.DATA_PATH, "outside")
    if os.path.isdir(outside_dir):
        failures.append(f"{label}: an out-of-tree directory was created at {outside_dir}")


def main() -> int:
    start_watchdog(60, "import_containment")
    fixtures._install_integration_globals()
    # The StreamDeck-UI importer reads gl.app.deck_manager.deck_controller.
    gl.app = types.SimpleNamespace(deck_manager=gl.deck_manager)

    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    failures: list[str] = []

    # --- Part A: StreamController importer -------------------------------
    # One traversal key that resolves one level above the pages directory,
    # and one plain key that must still import.
    sc_export = {
        "../sc_escape": ESCAPE_MARKER,
        "GoodSCPage": {"keys": {}, "dials": {}, "touchscreens": {}},
    }
    sc_export_path = os.path.join(gl.DATA_PATH, "sc_export.json")
    with open(sc_export_path, "w") as f:
        json.dump(sc_export, f)

    from src.windows.PageManager.Importer.StreamController.StreamController import (
        StreamControllerImporter,
    )
    StreamControllerImporter(sc_export_path).perform_import()

    good_sc = os.path.join(pages_dir, "GoodSCPage.json")
    if not os.path.isfile(good_sc):
        failures.append("StreamController: the plain page did not import")
    else:
        with open(good_sc) as f:
            if json.load(f) != {"keys": {}, "dials": {}, "touchscreens": {}}:
                failures.append("StreamController: imported page content is wrong")
    _no_escape(failures, "StreamController")

    # --- Part B: add_page seam (menu import, external API, dialogs) ------
    try:
        gl.page_manager.add_page("../add_escape", ESCAPE_MARKER)
    except ValueError:
        pass
    else:
        failures.append("add_page: a traversal name was not refused")
    _no_escape(failures, "add_page")

    good_add = gl.page_manager.add_page("GoodAddPage", {"keys": {}})
    if not os.path.isfile(good_add):
        failures.append("add_page: the plain page was not created")

    # --- Part C: StreamDeck-UI importer deck serial ---------------------
    # One traversal deck serial and one plain serial. The plain deck imports
    # one page; the traversal deck must write nothing, settings or page.
    sdui_export = {
        "state": {
            "../../outside/sdui_escape": {
                "brightness": 50,
                "buttons": {"0": {"0": {"text": "evil"}}},
            },
            "GOODDECK": {
                "brightness": 50,
                "buttons": {"0": {"0": {"text": "ok"}}},
            },
        },
    }
    sdui_export_path = os.path.join(gl.DATA_PATH, "sdui_export.json")
    with open(sdui_export_path, "w") as f:
        json.dump(sdui_export, f)

    from src.windows.PageManager.Importer.StreamDeckUI.StreamDeckUI import (
        StreamDeckUIImporter,
    )
    StreamDeckUIImporter(sdui_export_path).perform_import()

    good_deck_pages = [
        p for p in os.listdir(pages_dir)
        if p.startswith("ui_GOODDECK_") and p.endswith(".json")
    ]
    if not good_deck_pages:
        failures.append("StreamDeck-UI: the plain deck did not import a page")
    stray_settings = os.path.join(gl.DATA_PATH, "settings", "decks", "..",
                                  "..", "outside", "sdui_escape.json")
    if os.path.exists(stray_settings):
        failures.append(f"StreamDeck-UI: deck settings escaped to {stray_settings}")
    _no_escape(failures, "StreamDeck-UI")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print("PASS: page imports stay inside the pages directory and normal "
          "imports still land")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
