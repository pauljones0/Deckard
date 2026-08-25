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
    """Send SIGTERM to the process group of a launched backend.

    The backend leads its own session. With escalate, this waits, sends SIGKILL
    to a process that stays, and reaps it. Pass escalate=False at app quit,
    where os._exit reaps the whole tree."""
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.terminate()
        except Exception:
            pass
    if not escalate:
        return
    try:
        process.wait(timeout=3)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except Exception:
                pass
        try:
            process.wait(timeout=2)
        except Exception:
            pass


def build_backend_launch_command(backend_path: str, venv_path: str | None, port: int,
                                 open_in_terminal: bool = False) -> list[str]:
    """Build the argv that launches a plugin or action backend.

    ActionCore.launch_backend and PluginBase.launch_backend share this, so the
    path validation covers both and the two cannot drift.

    Raises:
        ValueError: When a given venv_path is absent or has no usable
            interpreter, or when backend_path is None or absent.
    """
    # It returns an argv list and never a shell string. The shell splits a
    # backend or venv path that holds a space into separate words, and it
    # executes the metacharacters in that path.
    if venv_path is not None:
        if not os.path.exists(venv_path):
            raise ValueError(f"Venv path does not exist: {venv_path}")
    # One gate covers both a None path and an absent path. A gate on None
    # alone lets os.path.exists raise TypeError, and it passes an absent path
    # to Popen.
    if backend_path is None or not os.path.exists(backend_path):
        raise ValueError(f"Backend path does not exist: {backend_path}")

    if venv_path is not None:
        # The interpreter is the venv's own python. A python3 from PATH is the
        # system python on a native install, which carries no rpyc, so the
        # backend dies at import. A run of {venv}/bin/python resolves the
        # imports as a source of {venv}/bin/activate does, because venv.create()
        # builds a plugin venv without the system site packages. The activation
        # also exports VIRTUAL_ENV and prepends {venv}/bin to PATH, which this
        # omits, and only a backend that runs a console script of its own venv
        # notices that.
        interpreter = os.path.join(venv_path, "bin", "python")
        # bin/python is a symlink to the interpreter the venv was built
        # against, and a python upgrade under a native install leaves that
        # symlink dangling. exists() follows the link, so this catches it. The
        # check here gives the caller the documented ValueError with a useful
        # message, instead of a bare FileNotFoundError out of Popen.
        if not os.path.exists(interpreter):
            raise ValueError(f"Venv has no usable interpreter: {interpreter}")
    else:
        interpreter = sys.executable

    if not open_in_terminal:
        return [interpreter, backend_path, f"--port={port}"]

    # A debug affordance runs the backend in a terminal that stays open after
    # the backend exits, through exec $SHELL, so its output survives a crash.
    # The paths arrive as bash positional parameters, so bash interpolates
    # nothing in them.
    #
    # DECKARD_TERMINAL holds the whole terminal command prefix and not the
    # binary alone, because no flag for "run this command" works everywhere.
    # gnome-terminal and its family take --, konsole, alacritty, xterm and
    # xfce4-terminal take -e, and kitty takes the command as a bare positional.
    # A split of the whole variable expresses each of those, such as
    # "konsole -e", "alacritty -e" and "kitty". A hardcoded double dash after
    # the binary works for one family, and for the rest the terminal prints its
    # usage and exits, so the backend never registers.
    terminal = shlex.split(os.environ.get("DECKARD_TERMINAL", "")) or ["gnome-terminal", "--"]
    return [*terminal, "bash", "-c", '"$1" "$2" --port="$3"; exec $SHELL',
            "deckard-backend", interpreter, backend_path, str(port)]


def frontend_authenticator(sock: "socket.socket") -> "tuple[socket.socket, None]":
    """rpyc authenticator for the frontend servers of plugins and actions.

    Loopback TCP carries no peer credentials, so without this any local
    process could connect and call register_backend. This accepts only a
    loopback peer that the socket table attributes to the app's own UID.
    rpyc runs it on the accepted socket before the protocol starts, so the
    unmodified backend child passes with no cooperation.
    """
    reason = deckard_rpyc_guard.refusal_reason(sock)
    if reason is not None:
        log.error(f"Refused a connection to a plugin frontend server: {reason}")
        raise AuthenticationError(reason)
    return sock, None


def verify_backend_port(port: int, process: subprocess.Popen[bytes] | None,
                        via_terminal: bool, owner: str) -> str:
    """Give the loopback address to register the launched backend on.

    The frontend authenticator gates who may call register_backend; this
    gates the argument. A connect to an unverified port hands the rpyc
    netref surface, and through it this process, to whoever listens there.
    The caller connects to the returned address, and not to a name that
    could resolve to a squatter on the same port in another family.

    Raises:
        RuntimeError: On refusal. register_backend runs on an rpyc service
            thread, so the exception travels back into the registering child
            and its startup fails visibly.
    """
    rows = deckard_rpyc_guard.listen_rows_of_port(port)
    if via_terminal:
        # A terminal launch hands the command to the terminal service over
        # D-Bus (gnome-terminal-server), so the backend is not a child of the
        # Popen handle. The owner UID of the listener is the check that
        # remains on this debug path.
        matched = [row for row in rows if row.uid == os.getuid()]
        detail = "no listener on that port belongs to this user"
    elif process is None:
        matched = []
        detail = "no backend process was launched"
    else:
        matched = [row for row in rows
                   if deckard_rpyc_guard.pid_owns_inode(process.pid, row.inode)]
        detail = f"no listener on that port belongs to the launched backend (pid {process.pid})"
    if not matched:
        message = f"{owner}: refused backend registration on port {port}: {detail}"
        log.error(message)
        raise RuntimeError(message)

    # The backend must listen on loopback. A wildcard-only listener is
    # LAN-reachable, so the loopback guard did not take effect there; refuse
    # rather than connect the app to an exposed backend.
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
    """Terminate a backend whose registration verify_backend_port refused.

    A refused backend is exposed or unverifiable, and refusing the connection
    leaves it listening. Killing the child closes that port. This runs off
    the caller's thread, because register_backend runs on an rpyc service
    thread and terminate_backend_process can wait several seconds. The
    frontend server and connection then tear down through on_disconnect when
    the child's own connection drops.
    """
    if process is None:
        return
    log.warning(f"{owner}: terminating the refused backend process (pid {process.pid})")
    threading.Thread(target=terminate_backend_process, args=(process,),
                     name="terminate_refused_backend", daemon=True).start()


def inject_backend_guard(venv_path: str) -> None:
    """Copy the loopback guard into a plugin venv before a backend launch.

    A .pth line imports the guard at every interpreter start in that venv,
    so the injection survives the terminal debug path, which loses the
    environment. The write is idempotent, and it refreshes a stale copy
    after an app upgrade. A failure only logs: the app-side gates hold
    without the guard, and a launch must not die on a read-only venv.
    """
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
    # Write to a unique temp file and rename, so a concurrent interpreter
    # start in the venv never imports a half-written guard, and two launches
    # into the same venv do not race one shared temp name.
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".deckard_guard_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def backend_guard_env() -> dict[str, str]:
    """Environment for a backend launch: the guard directory on PYTHONPATH.

    sitecustomize.py next to the guard imports it in a child that runs on
    the app's own interpreter. No plugin venv exists there to carry the .pth
    file. The app's own site-packages stays untouched, so the app process
    itself never imports the hook.
    """
    env = dict(os.environ)
    guard_dir = os.path.dirname(os.path.abspath(deckard_rpyc_guard.__file__))
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = guard_dir if not existing else guard_dir + os.pathsep + existing
    return env


# A backend runs on its own venv's interpreter, and that venv is built for one
# Python minor version. A system upgrade to the next one strands it: bin/python
# is a symlink to an interpreter that is gone, and lib/python<old>/site-packages
# is a directory the new interpreter never puts on sys.path. The rebuild below
# runs at most once per venv per process, whatever the outcome, so a rebuild
# that cannot succeed costs one attempt and not one per launch.
_rebuilt_venvs: set[str] = set()
_rebuild_lock = threading.Lock()


def venv_python_tag(venv_path: str) -> str | None:
    """The major.minor a venv was built against, or None when nothing says.

    pyvenv.cfg records it, and the site-packages directory name carries the
    same answer for a venv whose config a plugin wrote by hand.
    """
    try:
        with open(os.path.join(venv_path, "pyvenv.cfg")) as f:
            for line in f:
                key, _, value = line.partition("=")
                if key.strip() in ("version", "version_info"):
                    parts = value.strip().split(".")
                    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                        return f"{parts[0]}.{parts[1]}"
    except OSError:
        pass
    for site_dir in sorted(glob.glob(os.path.join(venv_path, "lib", "python*", "site-packages"))):
        tag = os.path.basename(os.path.dirname(site_dir)).removeprefix("python")
        if tag.count(".") == 1:
            return tag
    return None


def stale_venv_reason(venv_path: str) -> str | None:
    """Why a backend cannot start from this venv, or None when it can.

    An absent venv is no verdict here: build_backend_launch_command reports
    that one, with the path the caller passed.
    """
    if not os.path.isdir(venv_path):
        return None
    # exists() follows the link, so a dangling bin/python reads as absent.
    if not os.path.exists(os.path.join(venv_path, "bin", "python")):
        return "its interpreter is gone"
    tag = venv_python_tag(venv_path)
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    if tag is not None and tag != running:
        return f"it was built for Python {tag} and this app runs on {running}"
    return None


def ensure_backend_venv(venv_path: str, plugin_dir: str, display_name: str) -> None:
    """Rebuild a backend venv that the running Python cannot use.

    The rebuild is the plugin's own install steps, run through the same
    consent gate a store install uses, so one policy decides both, a plugin
    whose install steps the user declined stays declined, and the gate's
    confinement, timeout and loopback-guard injection apply to the venv it
    creates. The gate needs no consent callable here: a backend launch owns no
    window and must not block on a dialog.

    The stale tree moves aside first and comes back when the steps produce no
    usable venv, so a wrong verdict cannot destroy a working install.
    """
    reason = stale_venv_reason(venv_path)
    if reason is None:
        return
    if not plugin_dir or not os.path.isdir(plugin_dir):
        log.error(f"{display_name}: the backend venv is unusable because {reason}, and "
                  f"its plugin directory is unknown, so it cannot be rebuilt")
        return
    key = os.path.realpath(venv_path)
    with _rebuild_lock:
        if key in _rebuilt_venvs:
            return
        _rebuilt_venvs.add(key)

    from src.backend.Store import install_script

    log.warning(f"{display_name}: the backend venv at {venv_path} is unusable because "
                f"{reason}; rebuilding it from the plugin's install steps")
    if not install_script.decide_install_scripts(plugin_dir, display_name, None):
        log.warning(f"{display_name}: the install steps may not run, so the backend venv "
                    f"stays as it is and the backend will not start")
        return

    stash = f"{venv_path}.stale"
    try:
        shutil.rmtree(stash, ignore_errors=True)
        os.rename(venv_path, stash)
    except OSError as e:
        log.error(f"{display_name}: could not move the stale backend venv aside: {e}")
        return

    outcome = install_script.run_install_steps(plugin_dir, display_name, run=True)
    if os.path.isdir(venv_path) and stale_venv_reason(venv_path) is None:
        shutil.rmtree(stash, ignore_errors=True)
        log.info(f"{display_name}: rebuilt the backend venv ({outcome.value})")
        return

    log.error(f"{display_name}: the install steps left no usable backend venv "
              f"({outcome.value}); putting the previous one back")
    shutil.rmtree(venv_path, ignore_errors=True)
    try:
        os.rename(stash, venv_path)
    except OSError as e:
        log.error(f"{display_name}: could not restore the previous backend venv: {e}")


class PluginManager:
    action_index: dict[str, ActionHolder] = {}
    def __init__(self) -> None:
        self.initialized_plugin_classes = list[type[PluginBase]]()
        self.backends: list[Connection] = []
        # The subprocess.Popen handles of the launched backends. The teardown
        # terminates each one.
        self.backend_processes: list[subprocess.Popen[bytes]] = []
        # The first warm_up_plugins() call, from App.on_activate, sets this.
        # After that, load_plugins() runs the warm-up again, so a plugin
        # installed later gets its on_app_ready too. A store install calls
        # load_plugins long after the activation. The fired marker per plugin
        # keeps every hook to one call.
        self._app_ready: bool = False
        # The plugins that failed to load, keyed by their folder name under
        # PLUGIN_DIR, each with a short reason for a reader. The full traceback
        # goes to the logs. The UI shows these in the startup toast and in the
        # empty state of the Add Action dialog, so a broken plugin never fails
        # in silence. An entry is pruned when its folder disappears, or when
        # the plugin registers later.
        #
        # Two threads reach this dict. A store install runs load_plugins() and
        # init_plugins() again on a background thread, from
        # StoreBackend.install_plugin, and that rebuilds and writes the dict.
        # The GTK main thread reads it through get_load_health() for the
        # Add-Action empty state. _load_errors_lock keeps the rebuild atomic
        # against those reads. A plain dict is GIL-safe today, and the prune
        # inside a rebuild could expose a half-built dict on a later Python.
        self.load_errors: dict[str, str] = {}
        self._load_errors_lock = threading.Lock()

    def terminate_all_backends(self) -> None:
        """Terminate every launched backend child process. Called at app quit."""
        for process in list(self.backend_processes):
            terminate_backend_process(process, escalate=False)
        self.backend_processes.clear()

    def warm_up_plugins(self) -> None:
        """Initialize the plugin backends early, without a block on the caller.

        It calls the on_app_ready() hook of every registered plugin that has
        not fired one yet. The calls run on one background daemon thread, one
        plugin at a time, each isolated from the exceptions of the rest.
        """
        # This is the supported point for an early backend launch. Background
        # mode with -b opens no config UI, and without an enumerable deck at
        # startup no page load fires an action on_ready, so a lazily launched
        # backend would stay down until some user interaction forces it. A
        # backend launch spawns a subprocess, so this must never run on the GTK
        # main thread or block it.
        self._app_ready = True
        threading.Thread(
            target=self._warm_up_plugins,
            name="plugin_warm_up",
            daemon=True,
        ).start()

    def _warm_up_plugins(self) -> None:
        for plugin_id, plugin in list(PluginBase.plugins.items()):
            # Skip a malformed entry without an object; a scenario pins this
            # resilience.
            plugin_base = plugin.get("object")
            if plugin_base is None:
                continue
            # One call per plugin instance. The startup warm-up and the
            # warm-ups of a later load, after a store install, share this
            # dict.
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
                # A dot makes the import below impossible. The import string
                # plugins.<folder>.main reads every dot as a package boundary,
                # so a timestamped backup directory raises ModuleNotFoundError
                # and adds a false toast entry at every startup. This is no
                # plugin failure, so it warns without a traceback and stays out
                # of load_errors. The test covers a dot alone and not
                # isidentifier(), because importlib imports a name that starts
                # with a dash or a digit, and such a name can be a real
                # plugin.
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

        # Build an object for every class that inherits from PluginBase.
        self.init_plugins()

        # A plugin installed after startup must get its on_app_ready, like a
        # plugin loaded at startup. A store install runs load_plugins again,
        # and the warm-up of on_activate ran long before. This does nothing for
        # a plugin that is warm already.
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
        # The plugin load calls this, which on the boot path runs before the
        # app exists. The queue answers whether this thread delivers now, or
        # the drain in App.on_activate does. See src/backend/startup_queue.py.
        if startup_queue.get().when_app_ready(call):
            call()

    def show_load_errors_notification(self) -> None:
        """Show the plugin load failures to the user.

        Any thread can call this at any point during startup, because gl.notify
        defers the message during startup and marshals it to the main
        thread."""
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
        """Map a PluginBase subclass back to its folder name under PLUGIN_DIR.

        The module plugins.<folder>.main gives <folder>, which load_errors
        keys by."""
        module = getattr(subclass, "__module__", "") or ""
        parts = module.split(".")
        if len(parts) >= 2 and parts[0] == "plugins":
            return parts[1]
        return module or str(subclass)

    @staticmethod
    def _is_plugin_disabled(plugin_base: PluginBase) -> bool:
        return any(entry.get("object") is plugin_base for entry in PluginBase.disabled_plugins.values())

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
                # register() stopped over an invalid manifest or a duplicate
                # name, and it disabled no plugin. Without this record the
                # plugin vanishes with no trace for the user.
                log.error(
                    f"Plugin {subclass} (folder: {folder}) initialized but never registered successfully "
                    f"-- its actions will not be available. See the errors above for the reason."
                )
                with self._load_errors_lock:
                    self.load_errors[folder] = "did not register (invalid or incomplete manifest?)"

    def generate_action_index(self) -> None:
        """Rebuild the action index and publish it in one assignment.

        A store install rebuilds this on its worker thread while a page load
        resolves action ids on another. A clear() and a refill leave the index
        empty, and then half filled, in between. A read in that window finds
        no holder for an action that is installed, and the page keeps a
        NoActionHolderFound placeholder for it until something reloads the
        page, so one background install turns live actions into permanent
        placeholders. The rebuild therefore fills a fresh dict and publishes
        it with a single reference assignment: a reader holds either the whole
        previous index or the whole new one, and never a partial one.

        The slot is the class attribute, which is where the previous in-place
        rebuild wrote and where every reader of a PluginManager still finds
        it. An assignment through self would shadow it with an instance
        attribute and leave a class-level reader on an index that never
        changes again.
        """
        index: dict[str, ActionHolder] = {}
        for plugin in self.get_plugins().values():
            plugin_base = plugin["object"]
            index.update(plugin_base.action_holders)
        PluginManager.action_index = index

    def get_plugins(self, include_disabled: bool = False) -> dict[str, Any]:
        # A copy. An in-place update of PluginBase.plugins, a class attribute,
        # merges the disabled plugins into the enabled registry for good.
        # get_plugin_by_id() defaults to include_disabled=True and runs for
        # every action a page load resolves, so the first call would leak every
        # disabled plugin into the action index and the action chooser.
        plugins = dict(PluginBase.plugins)

        if include_disabled:
            plugins.update(PluginBase.disabled_plugins)

        return plugins
    
    def get_action_holder_from_id(self, action_id: str) -> ActionHolder | None:
        """Example string: dev_core447_MediaPlugin::Pause"""
        try:
            return self.action_index[action_id]
        except KeyError:
            log.warning(f"Requested action {action_id} not found, skipping...")
            return None
            
    def get_plugin_by_id(self, plugin_id:str, include_disabled: bool = True) -> PluginBase | None:
        return cast("PluginBase | None", self.get_plugins(include_disabled).get(plugin_id, {}).get("object", None))
            
    def remove_plugin_from_list(self, plugin_base: PluginBase) -> None:
        # A plugin can live in either registry. A version gate puts a plugin
        # in disabled_plugins alone, and get_plugin_by_id hands it out too,
        # because include_disabled defaults to True. A del on
        # PluginBase.plugins raises KeyError for such a plugin and aborts
        # uninstall_plugin in the middle, which keeps the registry entry and
        # skips the sys.modules purge. An update of a disabled plugin then
        # keeps serving the old code from the module cache.
        PluginBase.plugins.pop(plugin_base.plugin_id, None)
        PluginBase.disabled_plugins.pop(plugin_base.plugin_id, None)

    def get_plugin_id_from_action_id(self, action_id: str | None) -> str | None:
        if action_id is None:
            return None

        return action_id.split("::")[0]
    
    def get_load_health(self) -> tuple[int, int]:
        """Return the count of failed plugins and the count of disabled ones.

        A version gate disables a plugin. The UI reads this on the GTK main
        thread and explains an empty action list with it, instead of a blank
        page. The lock snapshots load_errors against a concurrent store-install
        reload, which rebuilds it on a background thread."""
        with self._load_errors_lock:
            n_failed = len(self.load_errors)
        return n_failed, len(PluginBase.disabled_plugins)

    def get_is_plugin_out_of_date(self, plugin_id: str) -> bool:
        plugin = PluginBase.disabled_plugins.get(plugin_id)
        if plugin is None:
            # The plugin is not installed.
            return False
        
        reason = PluginBase.disabled_plugins[plugin_id].get("reason")
        return reason == "plugin-out-of-date"