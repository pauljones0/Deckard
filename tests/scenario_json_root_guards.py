"""Reject non-object roots from migration, cache, and store JSON readers.
Quarantine invalid migration data instead of overwriting it."""

import json
import os

import fixtures  # noqa: F401  (isolated --data tempdir; import first)

import globals as gl

from src.backend.Migration.Migrator import Migrator
from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.StoreCache import StoreCache

NON_OBJECT_ROOTS = ["[]", '["a", "b"]', '"a string"', "42", "null"]


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def test_migration_flags_non_object_root() -> None:
    migrator = Migrator("0.0.1")
    for root in NON_OBJECT_ROOTS:
        _write(Migrator.SETTINGS_DIR, root)
        settings = migrator.get_settings()
        assert isinstance(settings, dict), (
            f"migration flags root {root!r} must read as a dict, got {type(settings).__name__}"
        )
        assert settings == {}, f"migration flags root {root!r} must read as empty, got {settings!r}"
        # The one caller that matters: a non-dict root reaching here raises
        # AttributeError on .get instead of reporting the migration as pending.
        assert migrator.get_need_migration() is True, (
            f"migration flags root {root!r} must leave the migration pending"
        )
        # The file held user data, so it must survive as a sidecar rather than
        # be overwritten by the next set_migrated.
        sidecars = [
            n for n in os.listdir(os.path.dirname(Migrator.SETTINGS_DIR))
            if n.startswith(os.path.basename(Migrator.SETTINGS_DIR) + ".corrupt")
        ]
        assert sidecars, f"migration flags root {root!r} must be quarantined, found no sidecar"
        for name in sidecars:
            os.remove(os.path.join(os.path.dirname(Migrator.SETTINGS_DIR), name))


def test_store_cache_index_requires_object() -> None:
    cache = StoreCache()
    for root in NON_OBJECT_ROOTS:
        _write(cache.files_json, root)
        files = cache.get_files()
        assert isinstance(files, dict), (
            f"cache index root {root!r} must read as a dict, got {type(files).__name__}"
        )
        assert files == {}, f"cache index root {root!r} must read as empty, got {files!r}"


class _StubBackend(StoreBackend):
    """A StoreBackend whose remote reads answer with a canned body, so the
    root-type guards can be driven with no network."""

    def __init__(self, body: str) -> None:
        self.body = body

    def get_remote_file(self, url: str, path: str, commit: "str | None" = None) -> str:
        return self.body


def test_store_manifest_non_object_root() -> None:
    """get_manifest must refuse a non-object root. A truthy one, a populated
    list, passes the caller's `if not manifest` test and then fails on .get."""
    for root in NON_OBJECT_ROOTS + ['["x"]']:
        manifest = _StubBackend(root).get_manifest("https://example.invalid/repo", None)
        assert manifest is None, f"manifest root {root!r} must read as None, got {manifest!r}"

    good = _StubBackend('{"id": "x"}').get_manifest("https://example.invalid/repo", None)
    assert good == {"id": "x"}, f"an object manifest must still read through, got {good!r}"


def test_store_attribution_non_object_root() -> None:
    """get_attribution must refuse a non-object root; the caller reads
    "generic" straight off the result."""
    for root in NON_OBJECT_ROOTS + ['["x"]']:
        attribution = _StubBackend(root).get_attribution("https://example.invalid/repo", None)
        assert attribution == {}, f"attribution root {root!r} must read as empty, got {attribution!r}"
        # The live call chains .get("generic") onto this.
        assert attribution.get("generic", {}) == {}

    good = _StubBackend('{"generic": {"a": 1}}').get_attribution("https://example.invalid/repo", None)
    assert good == {"generic": {"a": 1}}, f"an object attribution must still read through, got {good!r}"


def test_object_root_still_reads() -> None:
    """The guard must not swallow a well-formed index or flag file."""
    _write(Migrator.SETTINGS_DIR, json.dumps({"0.0.1": True}))
    assert Migrator("0.0.1").get_settings() == {"0.0.1": True}

    cache = StoreCache()
    _write(cache.files_json, json.dumps({"some/path": {"date": 1}}))
    assert cache.get_files() == {"some/path": {"date": 1}}


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_json_root_guards")
    assert gl.DATA_PATH, "fixtures must have bound an isolated data dir"
    test_migration_flags_non_object_root()
    test_store_cache_index_requires_object()
    test_store_manifest_non_object_root()
    test_store_attribution_non_object_root()
    test_object_root_still_reads()
    print("scenario_json_root_guards: PASS")


if __name__ == "__main__":
    main()
