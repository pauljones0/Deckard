"""The one gate in front of a plugin's install steps.

A downloaded plugin tree can carry two executable install steps: an
"__install__.py" script, and a requirements.txt that installs into this
app's own interpreter. Both are arbitrary code the moment they run, so
every caller funnels through run_install_steps, and the future venv
rebuild path must too. The gate owns the timeout, kills the whole
process group of a hung step, and re-injects the loopback guard into
every venv a script created, so a rebuilt or fresh venv never launches
unguarded.
"""

import enum
import os
import signal
import subprocess
import sys

from loguru import logger as log


# A stuck install script parked an install thread forever before the
# gate existed. Venv creation over a slow network is the honest upper
# bound, so the default stays generous.
DEFAULT_TIMEOUT_S = 600


class Outcome(enum.Enum):
    NO_STEPS = "no steps"
    RAN = "ran"
    FAILED = "failed"
    TIMEOUT = "timeout"


def run_install_steps(plugin_dir: str, display_name: str,
                      timeout_s: float = DEFAULT_TIMEOUT_S) -> Outcome:
    """Run a freshly installed plugin's install script and requirements
    step, each when present. Returns the worst outcome; a later step
    still runs after an earlier one failed, matching what the two
    previously independent calls did."""
    hook = os.path.join(plugin_dir, "__install__.py")
    requirements = os.path.join(plugin_dir, "requirements.txt")
    steps: list[list[str]] = []
    if os.path.isfile(hook):
        # The interpreter that runs this process, so a venv the script
        # creates keeps its dependencies. A list and no shell: an f-string
        # command breaks on a space in the data path, and lets a crafted
        # path component inject shell syntax.
        steps.append([sys.executable, hook])
    if os.path.isfile(requirements):
        steps.append([sys.executable, "-m", "pip", "install", "-r", requirements])
    if not steps:
        return Outcome.NO_STEPS

    worst = Outcome.RAN
    for cmd in steps:
        rc, timed_out = _execute(cmd, timeout_s)
        if timed_out:
            log.error(f"Install step of {display_name} killed after {timeout_s:.0f}s: {cmd[-1]}")
            worst = Outcome.TIMEOUT
        elif rc != 0:
            log.error(f"Install step of {display_name} exited {rc}: {cmd[-1]}")
            if worst is Outcome.RAN:
                worst = Outcome.FAILED

    _reinject_backend_guards(plugin_dir)
    return worst


def _execute(cmd: list[str], timeout_s: float) -> tuple[int, bool]:
    """Run one install step in its own session. Returns (returncode,
    timed_out). On timeout the whole process group dies, because pip and
    venv creation fork children that a lone child kill leaves running."""
    process = subprocess.Popen(cmd, start_new_session=True)
    try:
        return process.wait(timeout=timeout_s), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait()
        return process.returncode, True


def _reinject_backend_guards(plugin_dir: str) -> None:
    """Put the loopback guard into every venv under the plugin dir. The
    backend launch injects again and stays the authority; this covers the
    window between an install script creating a venv and that launch."""
    from src.backend.PluginManager.PluginManager import inject_backend_guard

    base_depth = plugin_dir.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(plugin_dir):
        if "pyvenv.cfg" in files:
            inject_backend_guard(root)
            dirs.clear()
            continue
        # Venvs sit at the top of a plugin tree (.venv, backend/.venv).
        # The depth cap keeps the walk out of large asset trees.
        if root.count(os.sep) - base_depth >= 3:
            dirs.clear()
