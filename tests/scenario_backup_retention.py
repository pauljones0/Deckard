"""Backup retention keeps exactly MAX_BACKUPS, not one fewer.

remove_old_backups returned at a count of MAX_BACKUPS and deleted from index
MAX_BACKUPS-1, so a directory that reached the cap was pruned to one below it.
It now keeps the newest MAX_BACKUPS and deletes only the rest.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import os  # noqa: E402

import globals as gl  # noqa: E402
from fixtures import start_watchdog  # noqa: E402


def _seed_backups(backup_dir: str, count: int) -> None:
    os.makedirs(backup_dir, exist_ok=True)
    for entry in os.listdir(backup_dir):
        os.remove(os.path.join(backup_dir, entry))
    # Timestamped names, so the newest-first sort has a stable order.
    for i in range(count):
        name = f"backup_202601{i + 1:02d}T000000.zip"
        with open(os.path.join(backup_dir, name), "w") as f:
            f.write("x")


def main() -> int:
    start_watchdog(30, "backup_retention")
    fixtures._install_integration_globals()

    pm = gl.page_manager
    backup_dir = os.path.join(pm.PAGE_PATH, "backups")
    cap = pm.MAX_BACKUPS

    failures: list[str] = []
    # Below, at, and above the cap. At and below must keep everything; above
    # must prune down to exactly the cap.
    for count, expected in ((cap - 1, cap - 1), (cap, cap), (cap + 3, cap)):
        _seed_backups(backup_dir, count)
        pm.remove_old_backups()
        survivors = [e for e in os.listdir(backup_dir) if e.endswith(".zip")]
        if len(survivors) != expected:
            failures.append(
                f"with {count} backups, expected {expected} to survive, got "
                f"{len(survivors)}: {sorted(survivors)}")
        # The survivors must be the newest ones.
        if count > cap:
            newest = {f"backup_202601{i + 1:02d}T000000.zip"
                      for i in range(count - cap, count)}
            if set(survivors) != newest:
                failures.append(
                    f"the wrong backups survived: kept {sorted(survivors)}, "
                    f"expected the newest {sorted(newest)}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print(f"PASS: retention keeps exactly MAX_BACKUPS ({cap}), newest first")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
