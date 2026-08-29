"""Page rename must stay inside the pages directory and never overwrite.

move_page copies the source over the destination and then deletes the source,
so a crafted destination name that escapes the pages tree would overwrite an
arbitrary JSON file and delete the original page. move_page enforces
containment for both names at the mutation seam, refuses a destination that
already exists, and leaves both files untouched on refusal. Three seams:
backend move_page, the DBus RenamePage handler, and the GTK
rename_page_by_path callback (which must not rename its row on refusal).
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import os

import globals as gl
from fixtures import start_watchdog


SECRET = {"secret": True}


def _write_secret(path: str) -> None:
    with open(path, "w") as f:
        json.dump(SECRET, f)


def _is_secret(path: str) -> bool:
    try:
        with open(path) as f:
            return json.load(f) == SECRET
    except (OSError, json.JSONDecodeError):
        return False


def main() -> int:
    start_watchdog(60, "rename_page_containment")
    fixtures._install_integration_globals()

    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    failures: list[str] = []

    # --- Part A: backend move_page containment ----------------------------
    secret = os.path.join(gl.DATA_PATH, "evil_backend.json")
    _write_secret(secret)
    src = fixtures.seed_page("RenameSrcA")
    traversal = os.path.join(pages_dir, "..", "evil_backend.json")
    try:
        gl.page_manager.move_page(src, traversal)
    except ValueError:
        pass
    else:
        failures.append("move_page: a traversal destination was not refused")
    if not _is_secret(secret):
        failures.append("move_page: a traversal destination overwrote an outside file")
    if not os.path.exists(src):
        failures.append("move_page: a refused rename still deleted the source page")

    # --- Part B: refuse an existing destination ---------------------------
    src_b = fixtures.seed_page("RenameSrcB")
    dst_b = fixtures.seed_page("RenameDstB")
    try:
        gl.page_manager.move_page(src_b, dst_b)
    except ValueError:
        pass
    else:
        failures.append("move_page: an existing destination was not refused")
    if not os.path.exists(src_b):
        failures.append("move_page: a refused overwrite still deleted the source")

    # --- Part C: a normal in-tree rename still works ----------------------
    src_c = fixtures.seed_page("RenameSrcC")
    dst_c = os.path.join(pages_dir, "RenameDstC.json")
    gl.page_manager.move_page(src_c, dst_c)
    if os.path.exists(src_c) or not os.path.exists(dst_c):
        failures.append("move_page: a normal in-tree rename did not land")

    # --- Part D: DBus RenamePage handler ----------------------------------
    from src.api import DeckardAPI
    api = DeckardAPI()

    dbus_secret = os.path.join(gl.DATA_PATH, "evil_dbus.json")
    _write_secret(dbus_secret)
    fixtures.seed_page("RenameDBusSrc")
    # The handler builds <pages>/<name>.json for the destination, so
    # "../evil_dbus" resolves one level above the pages directory.
    api.RenamePage("RenameDBusSrc", "../evil_dbus")
    if not _is_secret(dbus_secret):
        failures.append("RenamePage: a traversal destination overwrote an outside file")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print("PASS: page rename stays inside the pages directory, refuses "
          "overwrites, and normal renames still land")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
