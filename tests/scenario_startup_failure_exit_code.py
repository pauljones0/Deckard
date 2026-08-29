"""An unexpected startup failure must exit nonzero, not 0.

main() used to run under a bare log.catch that logged the failure and
returned, so the process exited 0 and a supervisor read a crash as success.
The entry point now logs once and exits nonzero. This runs the real main.py
as __main__ in a subprocess, injects a failure into main()'s first startup
call, and asserts the exit code is 1.

The child imports the toolkit that main.py pulls in at module load. Where the
environment cannot load it (no display driver, a mismatched interpreter) the
child cannot run, and this scenario skips, as the other GTK-touching
scenarios do.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402

from fixtures import start_watchdog  # noqa: E402

_REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MAIN = os.path.join(_REPO_ROOT, "main.py")

# The exit code a probe child returns when the heavy imports load. Distinct
# from 0 and 1 so the driver can tell "environment cannot run this" apart from
# the real assertions.
IMPORTS_OK = 42

# A child that only tries the module-level imports main.py performs. If it
# cannot, this scenario skips instead of failing on an environment limit.
_PROBE_CHILD = (
    "import cv2\n"
    "from src.app import App\n"
    "from src.backend.DeckManagement.DeckManager import DeckManager\n"
    f"raise SystemExit({IMPORTS_OK})\n"
)

# The driver: pre-break main()'s first startup call, then run main.py as
# __main__ so its top-level error handler decides the exit code.
_DRIVER_CHILD = (
    "import runpy\n"
    "import src.backend.log_hooks as lh\n"
    "def _boom():\n"
    "    raise RuntimeError('injected startup failure')\n"
    "lh.install_exception_hooks = _boom\n"
    f"runpy.run_path({MAIN!r}, run_name='__main__')\n"
)


def _run_child(source: str, data_dir: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    # A short-lived launch that never opens the real app: it fails in main()'s
    # first line. Give it its own data dir so it touches nothing shared.
    return subprocess.run(
        [sys.executable, "-c", source, "--data", data_dir],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)


def main() -> int:
    start_watchdog(90, "startup_failure_exit_code")

    with tempfile.TemporaryDirectory() as data_dir:
        probe = _run_child(_PROBE_CHILD, data_dir)
        if probe.returncode != IMPORTS_OK:
            print("SKIP: the environment cannot load main.py's imports "
                  f"(probe rc={probe.returncode}); startup exit code untested here")
            return 0

        driver = _run_child(_DRIVER_CHILD, data_dir)

    if driver.returncode == 0:
        print("FAIL: an injected startup failure still exited 0")
        return 1
    if driver.returncode != 1:
        print(f"FAIL: expected exit code 1, got {driver.returncode}\n"
              f"stderr tail:\n{driver.stderr[-800:]}")
        return 1

    print("PASS: an unexpected startup failure exits nonzero (1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
