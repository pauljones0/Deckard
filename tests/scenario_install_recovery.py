"""Recover store installs interrupted between atomic renames.
Covers normal swap, rollback, missing destinations, and stale leftovers."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import os  # noqa: E402
import tempfile  # noqa: E402
from unittest import mock  # noqa: E402

from fixtures import start_watchdog  # noqa: E402
from src.backend.Store import install_recovery  # noqa: E402

NEW = install_recovery.SWAP_NEW_SUFFIX
OLD = install_recovery.SWAP_OLD_SUFFIX


def _tree(path: str, marker: str) -> None:
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "which"), "w") as f:
        f.write(marker)


def _marker(path: str) -> str | None:
    try:
        with open(os.path.join(path, "which")) as f:
            return f.read()
    except OSError:
        return None


def main() -> int:
    start_watchdog(30, "install_recovery")
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as parent:
        dest = os.path.join(parent, "asset")

        # Normal swap over an existing install
        _tree(dest, "old")
        staging = os.path.join(parent, "staging1")
        _tree(staging, "new")
        install_recovery.swap_into_place(staging, dest)
        if _marker(dest) != "new":
            failures.append("a normal swap did not install the new tree")
        if os.path.exists(os.path.join(parent, f".asset{OLD}")) or \
           os.path.exists(os.path.join(parent, f".asset{NEW}")):
            failures.append("a normal swap left a dot leftover behind")

        # Rollback when the final rename fails
        _tree(dest, "current")
        staging = os.path.join(parent, "staging2")
        _tree(staging, "incoming")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst, *a, **k):
            # Fail the second os.replace (new -> destination), after the old
            # install has been moved aside.
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated crash before the destination rename")
            return real_replace(src, dst, *a, **k)

        raised = False
        with mock.patch("os.replace", side_effect=flaky_replace):
            try:
                install_recovery.swap_into_place(staging, dest)
            except OSError:
                raised = True
        if not raised:
            failures.append("a failed swap did not report the failure")
        if _marker(dest) != "current":
            failures.append("a failed swap did not roll the old install back")

        # Restore a parked old tree when the destination is absent.
        if os.path.lexists(dest):
            install_recovery._remove_leftover(dest)
        _tree(os.path.join(parent, f".asset{OLD}"), "previous")
        install_recovery.recover_interrupted_installs([parent])
        if _marker(dest) != "previous":
            failures.append("recovery did not restore the previous install")
        if os.path.exists(os.path.join(parent, f".asset{OLD}")):
            failures.append("recovery left the old leftover behind")

        # Complete an interrupted first install from its new tree.
        install_recovery._remove_leftover(dest)
        _tree(os.path.join(parent, f".asset{NEW}"), "fresh")
        install_recovery.recover_interrupted_installs([parent])
        if _marker(dest) != "fresh":
            failures.append("recovery did not complete a staged first install")

        # Remove a stale old tree after a completed swap.
        _tree(dest, "live")
        _tree(os.path.join(parent, f".asset{OLD}"), "stale")
        install_recovery.recover_interrupted_installs([parent])
        if _marker(dest) != "live":
            failures.append("recovery disturbed a live install")
        if os.path.exists(os.path.join(parent, f".asset{OLD}")):
            failures.append("recovery did not sweep a stale old leftover")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: the install swap rolls back on failure and recovery repairs "
          "every crash leftover")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
