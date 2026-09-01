"""Consent, confinement, timeout, and guard injection for plugin install steps.
bwrap is the boundary; without it, steps keep user files/network and daemons can outlive timeout."""

import contextlib
import enum
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import cast

from loguru import logger as log

import globals as gl

# Mark skipped steps so unattended updates do not run them; a staged reinstall clears the marker.
SKIP_MARKER = "install-scripts-skipped"

# Allow slow network-backed environment creation but bound a stuck step.
DEFAULT_TIMEOUT_S = 600
# Grace between the polite stop and the hard kill of a timed-out step.
_TERM_GRACE_S = 5

# Use an invalid bus address to prevent fallback, and remove display endpoints.
# Without bwrap, a child can recover the original environment from its parent.
_POISONED_BUS = "disabled:"
_STRIPPED_ENV_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SYSTEM_BUS_ADDRESS")


class Outcome(enum.Enum):
    NO_STEPS = "no steps"
    RAN = "ran"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


def decide_install_scripts(existing_dir: "str | None", display_name: str,
                          consent: "Callable[[str], bool] | None") -> bool:
    """Decide before download whether plugin install steps may run.
    Under ask, unattended paths allow steps unless a marker records skipped steps."""
    policy = _policy()
    if policy == "never":
        return False
    if policy == "always":
        return True
    if consent is not None:
        return consent(display_name)
    if existing_dir is not None and os.path.isfile(os.path.join(existing_dir, SKIP_MARKER)):
        log.info(f"Not re-running the previously declined install steps of {display_name}")
        return False
    return True


def run_install_steps(plugin_dir: str, display_name: str, run: bool,
                      timeout_s: float = DEFAULT_TIMEOUT_S,
                      use_bwrap: "bool | None" = None) -> Outcome:
    """Run or skip present hook and requirements steps, then return the worst outcome.
    Continue after failure; confined requirements add the interpreter prefix as writable."""
    hook = os.path.join(plugin_dir, "__install__.py")
    requirements = os.path.join(plugin_dir, "requirements.txt")
    has_hook = os.path.isfile(hook)
    has_requirements = os.path.isfile(requirements)
    if not has_hook and not has_requirements:
        return Outcome.NO_STEPS
    if not run:
        return _skip(plugin_dir, display_name)

    confine = use_bwrap is True or (use_bwrap is None and _bwrap_works())
    steps: list[list[str]] = []
    if has_hook:
        # Use this interpreter and argv tokens to preserve paths without shell injection.
        cmd = [sys.executable, hook]
        if confine:
            cmd = _bwrap_prefix(plugin_dir, []) + cmd
        steps.append(cmd)
    if has_requirements:
        cmd = [sys.executable, "-m", "pip", "install", "-r", requirements]
        if confine:
            # Permit pip to write this interpreter prefix inside the read-only sandbox.
            cmd = _bwrap_prefix(plugin_dir, [sys.prefix]) + cmd
        steps.append(cmd)

    env = dict(os.environ)
    env["DBUS_SESSION_BUS_ADDRESS"] = _POISONED_BUS
    for key in _STRIPPED_ENV_KEYS:
        env.pop(key, None)

    worst = Outcome.RAN
    for cmd in steps:
        rc, timed_out, stderr = _execute(cmd, timeout_s, env)
        label = "__install__.py" if cmd[-1] == hook else "requirements.txt"
        if timed_out:
            log.error(f"Install step {label} of {display_name} killed after {timeout_s:.0f}s")
            worst = Outcome.TIMEOUT
        elif rc != 0:
            tail = stderr.strip().splitlines()[-8:]
            log.error(f"Install step {label} of {display_name} exited {rc}: {' / '.join(tail)}")
            if worst is Outcome.RAN:
                worst = Outcome.FAILED

    _reinject_backend_guards(plugin_dir)
    return worst


def _execute(cmd: list[str], timeout_s: float, env: "dict[str, str] | None" = None) -> "tuple[int, bool, str]":
    """Run a step in a new session and return its code, timeout state, and stderr.
    On timeout, stop and then kill its complete process group."""
    process = subprocess.Popen(cmd, start_new_session=True, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _, stderr = process.communicate(timeout=timeout_s)
        return cast(int, process.returncode), False, stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        _kill_group(process.pid, signal.SIGTERM)
        try:
            _, stderr = process.communicate(timeout=_TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            _kill_group(process.pid, signal.SIGKILL)
            _, stderr = process.communicate()
        return cast(int, process.returncode), True, (stderr or b"").decode(errors="replace")


def _kill_group(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def _policy() -> str:
    manager = getattr(gl, "settings_manager", None)
    if manager is None:
        return "ask"
    try:
        return cast(str, manager.app().install_script_policy)
    except Exception as e:
        log.warning(f"Could not read the install-scripts policy ({e}); defaulting to ask")
        return "ask"


def _skip(plugin_dir: str, display_name: str) -> Outcome:
    log.info(f"Skipping the install steps of {display_name}")
    try:
        with open(os.path.join(plugin_dir, SKIP_MARKER), "w") as f:
            f.write("the install scripts were not run\n")
    except OSError as e:
        log.warning(f"Could not write the skip marker for {display_name}: {e}")
    return Outcome.SKIPPED


_bwrap_usable: "bool | None" = None
_bwrap_probe_lock = threading.Lock()


def _bwrap_works() -> bool:
    """Probe bwrap once with the real argument shape.
    Fall back to the poisoned environment when absent or denied nested namespaces."""
    global _bwrap_usable
    with _bwrap_probe_lock:
        if _bwrap_usable is not None:
            return _bwrap_usable
        if shutil.which("bwrap") is None:
            log.info("bwrap not found; install scripts run with the poisoned environment only")
            _bwrap_usable = False
            return False
        probe = _bwrap_prefix(os.getcwd(), []) + ["/bin/true"]
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


def _bwrap_prefix(plugin_dir: str, writable_extra: list[str]) -> list[str]:
    """Build bwrap confinement with a read-only root, private runtime, and pid namespace.
    Keep network access and bind only plugin, pip cache, and requested paths writable."""
    prefix: list[str] = [
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
    for path in [cache, *writable_extra]:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            # Leave an unavailable path read-only; pip can continue without its cache.
            continue
        prefix += ["--bind", path, path]
    return prefix + ["--"]


def _reinject_backend_guards(plugin_dir: str) -> None:
    """Inject the loopback guard into plugin venvs; backend launch repeats it as authority."""
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
