import contextlib
import glob
import os
import signal
import importlib
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from loguru import logger as log
import threading

from rpyc.core.protocol import Connection
from rpyc.utils.authenticators import AuthenticationError

from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.backend_guard import deckard_rpyc_guard

import globals as gl
from src.backend import startup_queue
from typing import cast, Any


def terminate_backend_process(process: "subprocess.Popen[bytes] | None", escalate: bool = True) -> None:
    """Send SIGTERM to the launched backend's process group.
    With escalation, wait three seconds, send SIGKILL, wait two more, and reap; app quit relies on os._exit."""
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        with contextlib.suppress(Exception):
            process.terminate()
    if not escalate:
        return
    try:
        process.wait(timeout=3)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            with contextlib.suppress(Exception):
                process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=2)


def build_backend_launch_command(backend_path: str, venv_path: str | None, port: int,
                                 open_in_terminal: bool = False) -> list[str]:
    """Build argv for a plugin or action backend launch.
    Raise ValueError when the backend is absent or a provided venv has no usable interpreter."""
    # Return argv to preserve spaces and prevent shell metacharacter execution.
    if venv_path is not None:
        if not os.path.exists(venv_path):
            raise ValueError(f"Venv path does not exist: {venv_path}")
    # Validate None and absent paths together to avoid TypeError and invalid
    # Popen argv.
    if backend_path is None or not os.path.exists(backend_path):
        raise ValueError(f"Backend path does not exist: {backend_path}")

    if venv_path is not None:
        # Use the venv interpreter because native system Python lacks plugin
        # dependencies; unlike activation, this does not set VIRTUAL_ENV or PATH.
        interpreter = os.path.join(venv_path, "bin", "python")
        # exists() follows a dangling post-upgrade symlink, which gives callers
        # a useful ValueError instead of Popen's FileNotFoundError.
        if not os.path.exists(interpreter):
            raise ValueError(f"Venv has no usable interpreter: {interpreter}")
    else:
        interpreter = sys.executable

    if not open_in_terminal:
        return [interpreter, backend_path, f"--port={port}"]

    # Keep the terminal open after crashes; positional arguments prevent path interpolation.
    # DECKARD_TERMINAL includes the prefix: GNOME family uses `--`, Konsole, Alacritty, XTerm, and Xfce use `-e`, and Kitty uses none.
    terminal = shlex.split(os.environ.get("DECKARD_TERMINAL", "")) or ["gnome-terminal", "--"]
    return [*terminal, "bash", "-c", '"$1" "$2" --port="$3"; exec $SHELL',
            "deckard-backend", interpreter, backend_path, str(port)]


def frontend_authenticator(sock: "socket.socket") -> "tuple[socket.socket, None]":
    """Authenticate plugin and action frontend clients through socket tables because loopback TCP has no peer credentials.
    rpyc accepts only same-UID loopback peers before protocol startup, so children need no cooperation."""
    reason = deckard_rpyc_guard.refusal_reason(sock)
    if reason is not None:
        log.error(f"Refused a connection to a plugin frontend server: {reason}")
        raise AuthenticationError(reason)
    return sock, None


def verify_backend_port(port: int, process: subprocess.Popen[bytes] | None,
                        via_terminal: bool, owner: str) -> str:
    """Return a verified loopback address for a launched backend.
    Raise RuntimeError for unowned, unverifiable, or non-loopback listeners so rpyc never connects to an untrusted port."""
    rows = deckard_rpyc_guard.listen_rows_of_port(port)
    if via_terminal:
        # Terminal services detach process ancestry, so verify the listener's
        # owner UID instead.
        matched = [row for row in rows if row.uid == os.getuid()]
        detail = "no listener on that port belongs to this user"
    elif process is None:
        matched: list[deckard_rpyc_guard.TcpRow] = []
        detail = "no backend process was launched"
    else:
        matched = [row for row in rows
                   if deckard_rpyc_guard.pid_owns_inode(process.pid, row.inode)]
        detail = f"no listener on that port belongs to the launched backend (pid {process.pid})"
    if not matched:
        message = f"{owner}: refused backend registration on port {port}: {detail}"
        log.error(message)
        raise RuntimeError(message)

    # Refuse wildcard-only listeners because they expose the backend to the LAN.
    loopback = [row for row in matched if deckard_rpyc_guard.is_loopback(row.local_ip)]
    if not loopback:
        addresses = ", ".join(sorted({row.local_ip for row in matched}))
        message = (
            f"{owner}: refused backend registration on port {port}: the backend "
            f"listens only on a non-loopback address ({addresses}); the loopback "
            f"guard did not take effect in that process"
        )
        log.error(message)
        raise RuntimeError(message)
    # rpyc's client resolves and connects AF_INET first, so prefer an IPv4
    # loopback row when one exists.
    for row in loopback:
        if row.local_ip.startswith("127."):
            return row.local_ip
    return loopback[0].local_ip


def terminate_refused_backend(process: subprocess.Popen[bytes] | None, owner: str) -> None:
    """Terminate a refused backend asynchronously so its exposed or unverifiable listener closes.
    The rpyc service thread must not wait; child disconnect then tears down the frontend connection."""
    if process is None:
        return
    log.warning(f"{owner}: terminating the refused backend process (pid {process.pid})")
    threading.Thread(target=terminate_backend_process, args=(process,),
                     name="terminate_refused_backend", daemon=True).start()


def inject_backend_guard(venv_path: str) -> None:
    """Copy the current loopback guard and persistent .pth hook into a plugin venv before launch.
    Writes are idempotent; failures only log because app-side gates remain active and read-only venvs must still launch."""
    try:
        source = os.path.abspath(deckard_rpyc_guard.__file__)
        with open(source, "rb") as f:
            payload = f.read()
        site_dirs = glob.glob(os.path.join(venv_path, "lib", "python*", "site-packages"))
        if not site_dirs:
            log.error(f"Backend guard not injected: no site-packages under {venv_path}")
            return
        for site_dir in site_dirs:
            _write_if_differs(os.path.join(site_dir, "deckard_rpyc_guard.py"), payload)
            _write_if_differs(os.path.join(site_dir, "deckard_rpyc_guard.pth"),
                              b"import deckard_rpyc_guard\n")
    except Exception as e:
        log.error(f"Backend guard injection into {venv_path} failed: {e}")


def _write_if_differs(path: str, payload: bytes) -> None:
    try:
        with open(path, "rb") as f:
            if f.read() == payload:
                return
    except OSError:
        pass
    # Atomic unique temp names prevent half-written imports and concurrent
    # launch races.
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".deckard_guard_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def backend_guard_env() -> dict[str, str]:
    """Add the guard directory to PYTHONPATH for app-interpreter children without plugin venvs.
    sitecustomize arms the child without modifying the app's site-packages."""
    env = dict(os.environ)
    guard_dir = os.path.dirname(os.path.abspath(deckard_rpyc_guard.__file__))
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = guard_dir if not existing else guard_dir + os.pathsep + existing
    return env


# Rebuild venvs stranded by removed interpreters at most once per process.
_rebuilt_venvs: set[str] = set()
# Guards _rebuilt_venvs and the per-venv lock table alone. Short critical
# sections, never held across a rebuild.
_rebuild_registry_lock = threading.Lock()
# Serialize each venv rebuild so concurrent plugin and action readiness cannot
# launch against a moved or partial tree.
_rebuild_locks: dict[str, threading.Lock] = {}

# Limit inline rebuilds because one warm-up thread serves all plugins serially;
# repairs beyond 120 seconds require an interactive store reinstall.
BACKEND_VENV_REBUILD_TIMEOUT_S = 120.0

_INTERPRETER_PROBE_TIMEOUT_S = 20.0


def venv_python_tag(venv_path: str) -> str | None:
    """Return the venv's recorded major.minor for logs, or None.
    Usability depends only on stale_venv_reason's interpreter probe."""
    try:
        # errors="replace" because a hand-written config can hold a byte that
        # is not UTF-8, and a decode error here must not read as "no config".
        with open(os.path.join(venv_path, "pyvenv.cfg"), errors="replace") as f:
            for line in f:
                key, _, value = line.partition("=")
                if key.strip() in ("version", "version_info"):
                    parts = value.strip().split(".")
                    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                        return f"{parts[0]}.{parts[1]}"
    except OSError:
        pass
    tags: list[tuple[int, int]] = []
    for site_dir in glob.glob(os.path.join(venv_path, "lib", "python*", "site-packages")):
        tag = os.path.basename(os.path.dirname(site_dir)).removeprefix("python")
        parts = tag.split(".")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            tags.append((int(parts[0]), int(parts[1])))
    if not tags:
        return None
    # The newest, because a venv upgraded in place keeps the older directory
    # beside the one it uses, and the lowest would name the dead one.
    major, minor = max(tags)
    return f"{major}.{minor}"


def _interpreter_runs(interpreter: str) -> bool:
    """Return whether the interpreter starts.
    Execute it because copied binaries can outlive the standard library they need."""
    try:
        result = subprocess.run([interpreter, "-c", ""], capture_output=True,
                                timeout=_INTERPRETER_PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug(f"Backend venv interpreter {interpreter} did not run: {e}")
        return False
    return result.returncode == 0


def stale_venv_reason(venv_path: str) -> str | None:
    """Return why an existing backend venv cannot launch, or None.
    Ignore absent venvs and version mismatches with runnable interpreters; launch validation handles absence."""
    if not os.path.isdir(venv_path):
        return None
    interpreter = os.path.join(venv_path, "bin", "python")
    # exists() follows the link, so a dangling bin/python reads as absent.
    if not os.path.exists(interpreter):
        tag = venv_python_tag(venv_path)
        built_for = f" (it was built for Python {tag})" if tag else ""
        return f"its interpreter is gone{built_for}"
    if not _interpreter_runs(interpreter):
        return "its interpreter no longer starts"
    return None


def _refuse_unattended(display_name: str) -> bool:
    """Refuse consent for unattended backend launches with no dialog-capable window.
    The "always" policy can override this answer, but the default "ask" policy cannot."""
    log.info(f"{display_name}: install steps need a decision that a backend launch "
             f"cannot ask for")
    return False


def _rebuild_lock_for(key: str) -> threading.Lock:
    with _rebuild_registry_lock:
        lock = _rebuild_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _rebuild_locks[key] = lock
        return lock


def ensure_backend_venv(venv_path: str, plugin_dir: str, display_name: str) -> None:
    """Rebuild a stale backend venv once per process and serially per venv through the confined install gate.
    Only "always" permits unattended steps; apply launch timeout and guard injection, and restore the stale tree on every failure."""
    key = os.path.realpath(venv_path)
    # Lock before probing because another rebuild temporarily moves the venv;
    # waiters then see the repaired tree instead of treating absence as healthy.
    with _rebuild_lock_for(key):
        reason = stale_venv_reason(venv_path)
        if reason is None:
            return
        with _rebuild_registry_lock:
            if key in _rebuilt_venvs:
                return
        _rebuild_backend_venv(venv_path, plugin_dir, display_name, reason)
        # Booked only after the attempt returned. An attempt that raised is
        # not an answer about this venv, so the next process tries again.
        with _rebuild_registry_lock:
            _rebuilt_venvs.add(key)


def _rebuild_backend_venv(venv_path: str, plugin_dir: str, display_name: str,
                          reason: str) -> None:
    """One rebuild attempt. ensure_backend_venv owns the locking and the
    once-per-process book-keeping."""
    from src.backend.Store import install_script

    if not plugin_dir or not os.path.isdir(plugin_dir):
        log.error(f"{display_name}: the backend venv is unusable because {reason}, and "
                  f"its plugin directory is unknown, so it cannot be rebuilt")
        return

    log.warning(f"{display_name}: the backend venv at {venv_path} is unusable because "
                f"{reason}")
    if not install_script.decide_install_scripts(plugin_dir, display_name, _refuse_unattended):
        log.error(f"{display_name}: its backend will not start, and rebuilding it runs the "
                  f"plugin's install scripts, which needs a decision this launch cannot ask "
                  f"for. Reinstall the plugin to rebuild it.")
        _report_rebuild_needs_consent(display_name)
        return

    stash = f"{venv_path}.stale"
    try:
        shutil.rmtree(stash, ignore_errors=True)
        os.rename(venv_path, stash)
    except OSError as e:
        log.error(f"{display_name}: could not move the stale backend venv aside: {e}")
        return

    try:
        outcome = install_script.run_install_steps(
            plugin_dir, display_name, run=True, timeout_s=BACKEND_VENV_REBUILD_TIMEOUT_S)
    except BaseException:
        # Restore the previous venv if install machinery raises; absent venvs
        # are not stale, so later launches would not repair one.
        log.opt(exception=True).error(f"{display_name}: the backend venv rebuild failed")
        _restore_stashed_venv(stash, venv_path, display_name)
        raise

    if os.path.isdir(venv_path) and stale_venv_reason(venv_path) is None:
        shutil.rmtree(stash, ignore_errors=True)
        log.info(f"{display_name}: rebuilt the backend venv ({outcome.value})")
        return

    log.error(f"{display_name}: the install steps left no usable backend venv "
              f"({outcome.value}); putting the previous one back")
    _restore_stashed_venv(stash, venv_path, display_name)


def _restore_stashed_venv(stash: str, venv_path: str, display_name: str) -> None:
    """Put a moved-aside venv back where the launch looks for it."""
    if not os.path.isdir(stash):
        return
    shutil.rmtree(venv_path, ignore_errors=True)
    try:
        os.rename(stash, venv_path)
    except OSError as e:
        log.error(f"{display_name}: could not restore the previous backend venv "
                  f"from {stash}: {e}")


def _report_rebuild_needs_consent(display_name: str) -> None:
    """Tell the user that a plugin needs a rebuild only they can allow."""
    notify = getattr(gl, "notify", None)
    if notify is None:
        return
    try:
        notify.error(
            f"{display_name} needs its backend rebuilt, which runs the plugin's install "
            f"scripts. Reinstall the plugin to do that.",
            title="Plugins")
    except Exception as e:
        log.warning(f"Could not report the backend venv of {display_name}: {e}")


class PluginManager:
    action_index: dict[str, ActionHolder] = {}
    def __init__(self) -> None:
        self.initialized_plugin_classes = list[type[PluginBase]]()
        self.backends: list[Connection] = []
        self.backend_processes: list[subprocess.Popen[bytes]] = []
        # Enable warm-up on activation and after later loads; each plugin's
        # fired marker limits on_app_ready to one call.
        self._app_ready: bool = False
        # Map plugin folders to user-facing failures; prune on removal or registration.
        # Full tracebacks stay in logs, and the lock makes worker updates atomic for GTK reads.
        self.load_errors: dict[str, str] = {}
        self._load_errors_lock = threading.Lock()

    def terminate_all_backends(self) -> None:
        """Terminate every launched backend child process. Called at app quit."""
        for process in list(self.backend_processes):
            terminate_backend_process(process, escalate=False)
        self.backend_processes.clear()

    def warm_up_plugins(self) -> None:
        """Start one daemon thread that calls each unfired plugin on_app_ready in order.
        Return without waiting, and isolate each plugin's exception."""
        # Warm-up starts backends in background mode or without decks before interaction.
        # It must run off the GTK main thread because backend launch can block.
        self._app_ready = True
        threading.Thread(
            target=self._warm_up_plugins,
            name="plugin_warm_up",
            daemon=True,
        ).start()

    def _warm_up_plugins(self) -> None:
        for plugin_id, plugin in list(PluginBase.plugins.items()):
            # Skip malformed entries that contain no plugin object.
            plugin_base = plugin.get("object")
            if plugin_base is None:
                continue
            # Each plugin instance fires once across startup and post-install warm-ups.
            if getattr(plugin_base, "_on_app_ready_fired", False):
                continue
            plugin_base._on_app_ready_fired = True
            try:
                plugin_base.on_app_ready()
            except Exception as e:
                log.error(f"Plugin {plugin_id}: on_app_ready failed: {e}")

    def load_plugins(self, show_notification: bool = False) -> None:
        os.makedirs(gl.PLUGIN_DIR, exist_ok=True)
        try:
            folders = os.listdir(gl.PLUGIN_DIR)
        except OSError as e:
            log.opt(exception=e).error(
                f"Could not read the plugin directory {gl.PLUGIN_DIR} -- no plugins will be loaded"
            )
            folders = []

        # Drop the stale errors of the plugins an uninstall removed.
        with self._load_errors_lock:
            self.load_errors = {folder: error for folder, error in self.load_errors.items() if folder in folders}

        for folder in folders:
            if folder.startswith(".") or not os.path.isdir(os.path.join(gl.PLUGIN_DIR, folder)):
                # A stray file and a hidden directory are no plugin.
                continue
            if "." in folder:
                # Skip dotted folders because import treats dots as package boundaries; warn
                # without load errors, but permit leading dashes or digits that importlib accepts.
                log.warning(
                    f"Skipping plugin directory '{folder}': dots in the name make "
                    f"it unimportable as a Python module -- rename it to load it "
                    f"as a plugin, or ignore this if it is a backup"
                )
                continue
            import_string = f"plugins.{folder}.main"
            if import_string not in sys.modules.keys():
                try:
                    importlib.import_module(import_string)
                except Exception as e:
                    log.opt(exception=e).error(f"Error importing plugin {folder}: {e}")
                    with self._load_errors_lock:
                        self.load_errors[folder] = f"import failed: {e}"

        self.init_plugins()

        # Warm later-installed plugins only after app readiness; fired markers
        # make this a no-op for plugins that are already warm.
        if self._app_ready:
            self.warm_up_plugins()

        if show_notification:
            self.show_n_disabled_plugins_notification()
            self.show_load_errors_notification()

    def show_n_disabled_plugins_notification(self) -> None:
        n_deactivated_plugins = len(PluginBase.disabled_plugins)
        if n_deactivated_plugins == 0:
            return
        
        body = f"{n_deactivated_plugins} plugins have been disabled because they are no longer compatible with the current app version"
        if n_deactivated_plugins == 1:
            body = f"{n_deactivated_plugins} plugin has been disabled because it is no longer compatible with the current app version"
        
        def call() -> None:
            # Read the app at call time, not at definition time: the queue
            # below may hold this until App.on_activate drains it.
            app = gl.app
            if app is None:
                return
            app.send_notification(
                "dialog-information-symbolic",
                "Plugins",
                body,
                button=("Update All", "app.update-all-assets", None)
            )
        # Queue boot-time notification until the app exists; otherwise deliver
        # it on this thread.
        if startup_queue.get().when_app_ready(call):
            call()

    def show_load_errors_notification(self) -> None:
        """Show plugin load failures from any thread.
        gl.notify defers startup messages and marshals them to the GTK main thread."""
        with self._load_errors_lock:
            n_failed = len(self.load_errors)
        if n_failed == 0:
            return

        if n_failed == 1:
            body = "1 plugin failed to load -- check the logs for details"
        else:
            body = f"{n_failed} plugins failed to load -- check the logs for details"

        gl.notify.error(body, title="Plugins")

    @staticmethod
    def _plugin_folder_of(subclass: "type[PluginBase]") -> str:
        """Return the PluginBase subclass folder under PLUGIN_DIR.
        Modules use plugins.<folder>.main, matching load_errors keys."""
        module = cast(str, getattr(subclass, "__module__", "") or "")
        parts = module.split(".")
        if len(parts) >= 2 and parts[0] == "plugins":
            return parts[1]
        return module or str(subclass)

    @staticmethod
    def _is_plugin_disabled(plugin_base: PluginBase) -> bool:
        return any(entry.get("object") is plugin_base for entry in PluginBase.disabled_plugins.values())

    def load_error_of(self, folder: str) -> "str | None":
        """The recorded load failure of one plugin folder, or None."""
        with self._load_errors_lock:
            return self.load_errors.get(folder)

    def init_plugins(self) -> None:
        subclasses = PluginBase.__subclasses__()
        for subclass in subclasses:
            if subclass in self.initialized_plugin_classes:
                log.info(f"Skipping {subclass} because it's already initialized")
                continue
            folder = self._plugin_folder_of(subclass)
            try:
                obj = subclass()
            except Exception as e:
                log.opt(exception=e).error(f"Error initializing plugin {subclass} (folder: {folder}): {e}. Skipping...")
                with self._load_errors_lock:
                    self.load_errors[folder] = f"crashed during initialization: {e}"
                continue
            self.initialized_plugin_classes.append(subclass)

            if getattr(obj, "registered", False):
                # A failure recorded earlier for this folder is stale.
                with self._load_errors_lock:
                    self.load_errors.pop(folder, None)
            elif not self._is_plugin_disabled(obj):
                # Record enabled classes that fail registration so invalid or
                # duplicate manifests remain visible to the user.
                log.error(
                    f"Plugin {subclass} (folder: {folder}) initialized but never registered successfully "
                    f"-- its actions will not be available. See the errors above for the reason."
                )
                with self._load_errors_lock:
                    self.load_errors[folder] = "did not register (invalid or incomplete manifest?)"

    def generate_action_index(self) -> None:
        """Atomically replace the class action index so concurrent page loads see the old or complete new mapping.
        A fresh dict avoids persistent placeholders; assignment through self would shadow class-level readers."""
        index: dict[str, ActionHolder] = {}
        for plugin in self.get_plugins().values():
            plugin_base = plugin["object"]
            index.update(plugin_base.action_holders)
        PluginManager.action_index = index

    def get_plugins(self, include_disabled: bool = False) -> dict[str, Any]:
        # Copy before optionally merging disabled plugins, or class-level state
        # would leak disabled actions into page resolution and the chooser.
        plugins = dict(PluginBase.plugins)

        if include_disabled:
            plugins.update(PluginBase.disabled_plugins)

        return plugins
    
    def get_action_holder_from_id(self, action_id: str) -> ActionHolder | None:
        try:
            return self.action_index[action_id]
        except KeyError:
            log.warning(f"Requested action {action_id} not found, skipping...")
            return None
            
    def get_plugin_by_id(self, plugin_id:str, include_disabled: bool = True) -> PluginBase | None:
        return cast("PluginBase | None", self.get_plugins(include_disabled).get(plugin_id, {}).get("object", None))
            
    def remove_plugin_from_list(self, plugin_base: PluginBase) -> None:
        # Remove from both registries because version-gated plugins exist only
        # in disabled; interrupted uninstall would serve cached old code after updates.
        PluginBase.plugins.pop(plugin_base.plugin_id, None)
        PluginBase.disabled_plugins.pop(plugin_base.plugin_id, None)

    def get_plugin_id_from_action_id(self, action_id: str | None) -> str | None:
        if action_id is None:
            return None

        return action_id.split("::")[0]
    
    def get_load_health(self) -> tuple[int, int]:
        """Return failed and disabled plugin counts for the empty-action UI.
        Lock load errors against worker-thread reloads."""
        with self._load_errors_lock:
            n_failed = len(self.load_errors)
        return n_failed, len(PluginBase.disabled_plugins)

    def get_is_plugin_out_of_date(self, plugin_id: str) -> bool:
        plugin = PluginBase.disabled_plugins.get(plugin_id)
        if plugin is None:
            return False
        
        reason = PluginBase.disabled_plugins[plugin_id].get("reason")
        return reason == "plugin-out-of-date"
