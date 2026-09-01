"""Keep backend and DBus page renames inside pages and refuse overwrites.
Leave source and destination unchanged when validation fails."""
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

    # Backend move_page containment
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

    # Existing destination refusal
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

    # Normal in-tree rename
    src_c = fixtures.seed_page("RenameSrcC")
    dst_c = os.path.join(pages_dir, "RenameDstC.json")
    gl.page_manager.move_page(src_c, dst_c)
    if os.path.exists(src_c) or not os.path.exists(dst_c):
        failures.append("move_page: a normal in-tree rename did not land")

    # DBus RenamePage handler
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
