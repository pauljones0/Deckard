"""The one gate in front of a plugin's install steps.

A downloaded plugin tree can carry two executable install steps: an
"__install__.py" script, and a requirements.txt that installs into this
app's own interpreter. Both are arbitrary code the moment they run.

The gate has two halves. decide_install_scripts answers whether the steps
may run, from the policy and, under "ask", a consent prompt; a caller
resolves it before it destroys a working install, so a decline never
leaves a half-updated plugin behind. run_install_steps then runs or skips
the steps, owns the timeout that kills the whole process group of a hung
step, confines each step, and re-injects the loopback guard into every
venv a script created, so a rebuilt or fresh venv never launches
unguarded. The future venv rebuild path calls run_install_steps too.

Confinement has two tiers, and only one of them confines.

Under bwrap, where it operates, a step runs on a read-only filesystem
with only the writable paths it needs, no session socket, and its own pid
namespace: that is the real boundary.

Without bwrap there is no boundary, only a discouragement. The gate
poisons the session-bus and display variables in the child environment
and nothing else, so the step still runs as the user, with the user's
whole filesystem readable and writable, with the network open, and with
no limit on the processes it starts. Even the environment is
best-effort: a determined script reads the real values back from the
parent's /proc, and a step that daemonizes outlives the timeout that
kills its process group. On such a host the consent prompt is the whole
boundary. What the user agreed to there is arbitrary code with their own
privileges, and the tier decides how loudly that is true, not whether it
is.
"""

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

# Written into a plugin dir when its install steps were skipped, so the
# auto-update path can tell a deliberately unbuilt plugin from one that
# never got the chance, and not re-run a script the user declined. A
# reinstall's staged swap replaces the whole tree, so it clears itself.
SKIP_MARKER = "install-scripts-skipped"

# A stuck install script parked an install thread forever before the
# gate existed. Venv creation over a slow network is the honest upper
# bound, so the default stays generous.
DEFAULT_TIMEOUT_S = 600
# Grace between the polite stop and the hard kill of a timed-out step.
_TERM_GRACE_S = 5

# Environment keys that would let a confined step reach the desktop
# session. The bus address is set invalid rather than removed, because an
# absent one falls back to $XDG_RUNTIME_DIR/bus; the display keys are
# dropped so a script cannot open the X or Wayland socket that the
# read-only root still exposes. This is best-effort without bwrap: a
# child reads the real values from the parent's /proc there.
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
    """Whether a plugin's install steps may run, decided before the caller
    downloads or swaps anything.

    "never" refuses. "always" allows. "ask" asks the consent callable when
    one is given, which is the store window's prompt. With no callable, the
    auto-update, onboarding and headless paths, "ask" allows, so a plugin
    the user already installed keeps building itself, unless a prior run
    was declined: a SKIP_MARKER under an existing install means the user
    said no, and an unattended re-run must respect that rather than run the
    script behind their back."""
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
    """Run or skip a plugin's install script and requirements step, each
    when present. run is the decision decide_install_scripts returned.
    Returns the worst outcome; a later step still runs after an earlier one
    failed, matching the two previously independent calls.

    Each step is confined as far as the host allows, which on a host
    without a working bwrap is not at all (see the module docstring). The
    requirements step installs into this app's own interpreter, so its
    write target, the interpreter prefix, is bound writable rather than
    left read-only; every other confinement still applies."""
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
        # The interpreter that runs this process, so a venv the script
        # creates keeps its dependencies. A list and no shell: an f-string
        # command breaks on a space in the data path, and lets a crafted
        # path component inject shell syntax.
        cmd = [sys.executable, hook]
        if confine:
            cmd = _bwrap_prefix(plugin_dir, []) + cmd
        steps.append(cmd)
    if has_requirements:
        cmd = [sys.executable, "-m", "pip", "install", "-r", requirements]
        if confine:
            # pip writes into this interpreter's prefix, so that one tree
            # is writable; the rest of the sandbox is not.
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
    """Run one install step in its own session. Returns (returncode,
    timed_out, stderr). On timeout the whole process group is asked to
    stop and then killed, because pip and venv creation fork children a
    lone-child kill leaves running. start_new_session makes the child the
    group leader, so process.pid is the pgid to signal; without it the
    signal would reach this app's own group."""
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
        return cast(str, manager.app().install_scripts)
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
    """The confinement for one install step: the filesystem read-only, a
    private /tmp, a tmpfs over the runtime dir so no session socket exists,
    and its own pid namespace so a timeout tears down every descendant. The
    plugin dir, the pip cache and each writable_extra path are bound
    writable. The network stays shared, because the step's legitimate job
    is a package install over pip."""
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
            # A path that cannot be created is left read-only; pip warns
            # about an unwritable cache and continues.
            continue
        prefix += ["--bind", path, path]
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
