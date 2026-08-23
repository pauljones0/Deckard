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
import shutil
import signal
import subprocess
import sys
import threading

from loguru import logger as log


# A stuck install script parked an install thread forever before the
# gate existed. Venv creation over a slow network is the honest upper
# bound, so the default stays generous.
DEFAULT_TIMEOUT_S = 600

# An invalid bus address, not an absent one: gio and flatpak-spawn fall
# back to $XDG_RUNTIME_DIR/bus when the variable is missing, and an
# invalid address makes the connection fail instead. This is the tier
# that stops a script from reconfiguring the host through
# "flatpak-spawn --host" on every install type.
_POISONED_BUS = "disabled:"


class Outcome(enum.Enum):
    NO_STEPS = "no steps"
    RAN = "ran"
    FAILED = "failed"
    TIMEOUT = "timeout"


def run_install_steps(plugin_dir: str, display_name: str,
                      timeout_s: float = DEFAULT_TIMEOUT_S,
                      use_bwrap: "bool | None" = None) -> Outcome:
    """Run a freshly installed plugin's install script and requirements
    step, each when present. Returns the worst outcome; a later step
    still runs after an earlier one failed, matching what the two
    previously independent calls did.

    The script runs confined: a poisoned session-bus address always, and
    under bwrap where it operates (probed once per process), which makes
    the filesystem read-only outside the plugin dir and the pip cache and
    hides every session socket. The requirements step gets the poisoned
    environment only: its purpose is a write into this app's own
    interpreter, which no read-only root can allow. use_bwrap None means
    the probe decides; scenarios pass an explicit value."""
    hook = os.path.join(plugin_dir, "__install__.py")
    requirements = os.path.join(plugin_dir, "requirements.txt")
    steps: list[list[str]] = []
    if os.path.isfile(hook):
        # The interpreter that runs this process, so a venv the script
        # creates keeps its dependencies. A list and no shell: an f-string
        # command breaks on a space in the data path, and lets a crafted
        # path component inject shell syntax.
        cmd = [sys.executable, hook]
        if use_bwrap is True or (use_bwrap is None and _bwrap_works()):
            cmd = _bwrap_prefix(plugin_dir) + cmd
        steps.append(cmd)
    if os.path.isfile(requirements):
        steps.append([sys.executable, "-m", "pip", "install", "-r", requirements])
    if not steps:
        return Outcome.NO_STEPS

    env = dict(os.environ)
    env["DBUS_SESSION_BUS_ADDRESS"] = _POISONED_BUS
    worst = Outcome.RAN
    for cmd in steps:
        rc, timed_out = _execute(cmd, timeout_s, env)
        if timed_out:
            log.error(f"Install step of {display_name} killed after {timeout_s:.0f}s: {cmd[-1]}")
            worst = Outcome.TIMEOUT
        elif rc != 0:
            log.error(f"Install step of {display_name} exited {rc}: {cmd[-1]}")
            if worst is Outcome.RAN:
                worst = Outcome.FAILED

    _reinject_backend_guards(plugin_dir)
    return worst


def _execute(cmd: list[str], timeout_s: float, env: "dict[str, str] | None" = None) -> tuple[int, bool]:
    """Run one install step in its own session. Returns (returncode,
    timed_out). On timeout the whole process group dies, because pip and
    venv creation fork children that a lone child kill leaves running."""
    process = subprocess.Popen(cmd, start_new_session=True, env=env)
    try:
        return process.wait(timeout=timeout_s), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait()
        return process.returncode, True


_bwrap_usable: "bool | None" = None
_bwrap_probe_lock = threading.Lock()


def _bwrap_works() -> bool:
    """Whether bwrap confines here, probed once per process with the real
    argument shape. bwrap is absent on some hosts, and inside a flatpak
    sandbox the nested namespaces it needs are commonly refused, so the
    gate tiers down to the poisoned environment and says so once."""
    global _bwrap_usable
    with _bwrap_probe_lock:
        if _bwrap_usable is not None:
            return _bwrap_usable
        if shutil.which("bwrap") is None:
            log.info("bwrap not found; install scripts run with the poisoned environment only")
            _bwrap_usable = False
            return False
        probe = _bwrap_prefix(os.getcwd()) + ["/bin/true"]
        try:
            result = subprocess.run(probe, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning(f"bwrap probe failed ({e}); install scripts run with the poisoned environment only")
            _bwrap_usable = False
            return False
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            log.warning(f"bwrap does not operate here ({stderr!r}); install scripts run with the poisoned environment only")
            _bwrap_usable = False
            return False
        _bwrap_usable = True
        return True


def _bwrap_prefix(plugin_dir: str) -> list[str]:
    """The confinement for one install script: the filesystem read-only,
    the plugin dir and the pip cache writable, a private /tmp, and a
    tmpfs over the runtime dir so no session socket exists, the bus
    included. The network stays shared, because the script's legitimate
    job is a venv install over pip."""
    prefix = [
        "bwrap", "--die-with-parent", "--unshare-pid",
        "--ro-bind", "/", "/",
        "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--bind", plugin_dir, plugin_dir,
    ]
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        prefix += ["--tmpfs", runtime_dir]
    cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "pip")
    try:
        os.makedirs(cache, exist_ok=True)
        prefix += ["--bind", cache, cache]
    except OSError:
        # pip warns about an unwritable cache and continues without it.
        pass
    return prefix + ["--"]


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
