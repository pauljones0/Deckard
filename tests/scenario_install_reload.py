"""Invalidate finder caches before loading dependencies installed in-process.
Recreate same-mtime staleness by restoring the primed directory timestamp."""
import fixtures  # must be first; isolates DATA_PATH before import globals

import importlib
import os
import sys
import tempfile

import globals as gl

from src.backend.Store import install_reload


def prime_stale_dir(name: str) -> str:
    """A sys.path directory whose finder cache predates the module in it."""
    tmp = tempfile.mkdtemp(prefix="reload-", dir=gl.DATA_PATH)
    # Replace any earlier primed dir rather than stack entries.
    sys.path[:] = [p for p in sys.path if "/reload-" not in p]
    sys.path.insert(0, tmp)
    try:
        importlib.import_module(name)
        raise AssertionError(f"{name} must not exist yet")
    except ModuleNotFoundError:
        pass
    stat = os.stat(tmp)
    with open(os.path.join(tmp, f"{name}.py"), "w") as f:
        f.write("value = 1\n")
    # Pin the directory mtime to the primed scan, the same-tick state a pip
    # install can leave. The finder then trusts its stale listing.
    os.utime(tmp, (stat.st_atime, stat.st_mtime))
    return tmp


def check_stale_cache_hides_the_module() -> None:
    prime_stale_dir("reload_dep_a")
    try:
        importlib.import_module("reload_dep_a")
        raise AssertionError(
            "the stale finder cache served the fresh module -- this check "
            "cannot manufacture the precondition on this filesystem")
    except ModuleNotFoundError:
        pass
    importlib.invalidate_caches()
    importlib.import_module("reload_dep_a")
    print("PASS: a stale finder cache hides a fresh module until the caches drop")


class RecordingManager:
    def __init__(self, dep_name: str, error: "str | None" = None) -> None:
        self.dep_name = dep_name
        self.error = error
        self.calls: list[str] = []
        self.imported = False

    def load_plugins(self) -> None:
        self.calls.append("load")
        importlib.import_module(self.dep_name)
        self.imported = True

    def init_plugins(self) -> None:
        self.calls.append("init")

    def generate_action_index(self) -> None:
        self.calls.append("index")

    def load_error_of(self, folder: str) -> "str | None":
        self.calls.append(f"error:{folder}")
        return self.error


class RecordingNotify:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, body: str, title: str = "") -> None:
        self.errors.append(body)


def check_reload_sees_the_fresh_dependency() -> None:
    prime_stale_dir("reload_dep_b")
    manager = RecordingManager("reload_dep_b")
    old_manager = gl.plugin_manager
    gl.plugin_manager = manager
    try:
        error = install_reload.reload_after_install("some_plugin")
    finally:
        gl.plugin_manager = old_manager
    assert error is None, f"a clean reload must report no error: {error}"
    assert manager.imported, "the reload must import through the fresh caches"
    assert manager.calls[:3] == ["load", "init", "index"], manager.calls
    print("PASS: the install reload imports a dependency installed mid-process")


def check_load_failure_reaches_the_user() -> None:
    manager = RecordingManager("reload_dep_b", error="import failed: no module")
    notify = RecordingNotify()
    old_manager, old_notify = gl.plugin_manager, gl.notify
    gl.plugin_manager = manager
    gl.notify = notify
    try:
        error = install_reload.reload_after_install("broken_plugin")
    finally:
        gl.plugin_manager, gl.notify = old_manager, old_notify
    assert error == "import failed: no module", error
    assert len(notify.errors) == 1 and "broken_plugin" in notify.errors[0], (
        f"the load failure must reach the user: {notify.errors}")
    print("PASS: a plugin that fails to load after install tells the user")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_install_reload")
    # Run the platform-dependent stale-cache precondition after behavior checks.
    check_reload_sees_the_fresh_dependency()
    check_load_failure_reaches_the_user()
    check_stale_cache_hides_the_module()
    print("PASS: scenario_install_reload")


if __name__ == "__main__":
    main()
