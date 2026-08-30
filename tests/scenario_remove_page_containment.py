"""Keep backend and DBus page removal inside the pages directory.
Refuse parent traversal while normal in-tree removal continues to work."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import os

import globals as gl
from fixtures import start_watchdog


SECRET = {"secret": True}


def _write_secret(path: str) -> None:
    with open(path, "w") as f:
        json.dump(SECRET, f)


def main() -> int:
    start_watchdog(60, "remove_page_containment")
    fixtures._install_integration_globals()

    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    failures: list[str] = []

    # A real .json one level above the pages directory, still inside DATA_PATH,
    # that a traversal name resolves to. It must survive every refused removal.
    backend_secret = os.path.join(gl.DATA_PATH, "evil_backend.json")
    dbus_secret = os.path.join(gl.DATA_PATH, "evil_dbus.json")

    # Backend remove_page
    _write_secret(backend_secret)
    traversal = os.path.join(pages_dir, "..", "evil_backend.json")
    try:
        gl.page_manager.remove_page(traversal)
    except ValueError:
        pass
    else:
        failures.append("remove_page: a traversal path was not refused")
    if not os.path.exists(backend_secret):
        failures.append(
            "remove_page: a traversal path deleted a file outside the pages "
            "directory")

    # A normal in-tree page must still delete.
    good = fixtures.seed_page("GoodRemove")
    gl.page_manager.remove_page(good)
    if os.path.exists(good):
        failures.append("remove_page: a normal in-tree page was not removed")

    # DBus RemovePage handler
    from src.api import DeckardAPI
    api = DeckardAPI()

    _write_secret(dbus_secret)
    # The handler resolves "../evil_dbus" to an existing file above pages,
    # which remove_page must refuse.
    api.RemovePage("../evil_dbus")
    if not os.path.exists(dbus_secret):
        failures.append(
            "RemovePage: a traversal name deleted a file outside the pages "
            "directory")

    # A normal in-tree page must still delete through the DBus handler.
    good_dbus = fixtures.seed_page("GoodRemoveDBus")
    api.RemovePage("GoodRemoveDBus")
    if os.path.exists(good_dbus):
        failures.append("RemovePage: a normal in-tree page was not removed")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print("PASS: page removal stays inside the pages directory and normal "
          "removals still land")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
