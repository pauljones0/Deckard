"""Keep corrupt or unreadable items unchanged and logged while good files migrate.
A failing migrator stays pending without aborting startup."""
import json
import os
import shutil

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

import globals as gl

from fixtures import start_watchdog
from src.backend.Migration.MigrationManager import MigrationManager
from src.backend.Migration.Migrators.Migrator_1_5_0 import Migrator_1_5_0
from src.backend.Migration.Migrators.Migrator_1_5_0_beta_5 import Migrator_1_5_0_beta_5

PAGES_DIR = os.path.join(gl.DATA_PATH, "pages")


def _reset() -> None:
    shutil.rmtree(PAGES_DIR, ignore_errors=True)
    os.makedirs(PAGES_DIR, exist_ok=True)
    migrations_json = os.path.join(gl.DATA_PATH, "settings", "migrations.json")
    if os.path.exists(migrations_json):
        os.remove(migrations_json)


def _write(name: str, text: str) -> str:
    path = os.path.join(PAGES_DIR, name)
    with open(path, "w") as f:
        f.write(text)
    return path


def main() -> int:
    start_watchdog(30, "migration_item_isolation")
    _reset()

    # A flat pre-beta.5 page that beta_5 must nest, a corrupt page, and a
    # second good page. The corrupt one sits between the two good ones.
    good_a = _write("aaa_good.json", json.dumps({"keys": {"0x0": {"labels": {}}}}))
    corrupt = _write("mmm_corrupt.json", '{"keys": {"0x0"')  # truncated
    good_b = _write("zzz_good.json", json.dumps({"keys": {"1x1": {"labels": {}}}}))

    manager = MigrationManager()
    manager.add_migrator(Migrator_1_5_0())
    manager.add_migrator(Migrator_1_5_0_beta_5())

    # Must not raise, whatever the corrupt file does.
    manager.run_migrators()

    failures: list[str] = []

    # Both good pages migrated: beta_5 nested their keys under states.0.
    for label, path in (("aaa_good", good_a), ("zzz_good", good_b)):
        with open(path) as f:
            page = json.load(f)
        key = next(iter(page["keys"]))
        if "states" not in page["keys"][key]:
            failures.append(f"{label} was not migrated (no states nesting): {page}")

    # The corrupt page is left exactly as written, not rewritten or deleted.
    if not os.path.exists(corrupt):
        failures.append("the corrupt page was deleted instead of left in place")
    else:
        with open(corrupt) as f:
            if f.read() != '{"keys": {"0x0"':
                failures.append("the corrupt page was altered")

    # A migrator that finished its good work recorded itself as migrated, so a
    # single bad file does not force the migration to repeat every launch.
    if Migrator_1_5_0_beta_5().get_need_migration():
        failures.append("beta_5 stayed pending though it completed its good items")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: good pages migrate around a corrupt one; startup survives; "
          "the bad file is preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
