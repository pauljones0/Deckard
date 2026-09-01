"""Rebuild once when a backend venv interpreter cannot start, but keep usable cross-version venvs.
Use the install gate and argv steps; unattended launch runs them only with "always"."""

import os
import sys
import textwrap
import threading
import types

import fixtures  # noqa: F401  (isolated --data tempdir; import first)

import globals as gl  # noqa: E402

from src.backend.Store import install_script  # noqa: E402
from src.backend.PluginManager import PluginBase as plugin_base_module  # noqa: E402
from src.backend.PluginManager import PluginManager as plugin_manager_module  # noqa: E402
from src.backend.PluginManager.PluginManager import (  # noqa: E402
    attempt_backend_venv_repair,
    stale_venv_reason,
    venv_python_tag,
)

RUNNING_TAG = f"{sys.version_info.major}.{sys.version_info.minor}"
OLD_TAG = f"{sys.version_info.major}.{sys.version_info.minor - 1}"
PRESERVED_VENV_MARKER = "built-by-the-previous-python"


def _make_venv(venv_path: str, tag: str, marker: bool = False, stale: bool = False) -> None:
    """Build a venv-shaped tree with pyvenv.cfg, bin/python, and site-packages.
    With stale, bin/python points to an absent interpreter as after a system Python upgrade."""
    os.makedirs(os.path.join(venv_path, "bin"), exist_ok=True)
    os.makedirs(os.path.join(venv_path, "lib", f"python{tag}", "site-packages"), exist_ok=True)
    with open(os.path.join(venv_path, "pyvenv.cfg"), "w") as f:
        f.write(f"home = /usr/bin\nversion = {tag}.4\ninclude-system-site-packages = false\n")
    # A real venv's bin/python is a symlink to the interpreter it was built
    # against, which is what a Python upgrade breaks.
    link = os.path.join(venv_path, "bin", "python")
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(os.path.join(venv_path, "bin", "gone-python") if stale else sys.executable, link)
    if marker:
        with open(os.path.join(venv_path, PRESERVED_VENV_MARKER), "w") as f:
            f.write("this tree must survive a refused rebuild\n")


def _plugin(name: str, hook: bool = True, requirements: bool = False) -> str:
    plugin_dir = os.path.join(gl.DATA_PATH, "plugins", name)
    os.makedirs(os.path.join(plugin_dir, "backend"), exist_ok=True)
    if hook:
        with open(os.path.join(plugin_dir, "__install__.py"), "w") as f:
            f.write(textwrap.dedent("""
                # Stands in for a plugin's real install script, which builds
                # the backend venv. The scenario runs it through a stub.
            """))
    if requirements:
        with open(os.path.join(plugin_dir, "requirements.txt"), "w") as f:
            f.write("rpyc\n")
    return plugin_dir


def _set_policy(value: str) -> None:
    gl.settings_manager.get_app_settings().setdefault("store", {})["install-scripts"] = value


class _InstallStepStub:
    """Stands in for the gate's subprocess seam. It records the argv of every
    step and optionally builds the venv, as a real install script would."""

    def __init__(self, build: "tuple[str, str] | None" = None):
        self.commands: list[list[str]] = []
        self.build = build

    def __call__(self, cmd, timeout_s, env=None):
        self.commands.append(list(cmd))
        if self.build is not None:
            _make_venv(*self.build)
        return 0, False, ""


def _with_stub_gate(steps: _InstallStepStub, call) -> None:
    """Run a call with the gate's subprocess seam and bwrap probe stubbed.
    Disable bwrap for host-independent argv; scenario_install_script_gate covers confinement."""
    real_execute = install_script._execute
    real_bwrap = install_script._bwrap_works
    install_script._execute = steps
    install_script._bwrap_works = lambda: False
    try:
        call()
    finally:
        install_script._execute = real_execute
        install_script._bwrap_works = real_bwrap


def check_version_tag_reading() -> None:
    """Read the recorded version, with the site-packages name as fallback.
    The tag affects only the venv's log label."""
    base = os.path.join(gl.DATA_PATH, "tags")
    matching = os.path.join(base, "matching")
    _make_venv(matching, RUNNING_TAG)
    assert venv_python_tag(matching) == RUNNING_TAG

    # Newer venvs can use version_info.
    # Only config shows the old version when directories name the current one.
    info_only = os.path.join(base, "info")
    _make_venv(info_only, RUNNING_TAG)
    with open(os.path.join(info_only, "pyvenv.cfg"), "w") as f:
        f.write(f"version_info = {OLD_TAG}.4.final.0\n")
    assert venv_python_tag(info_only) == OLD_TAG, (
        "version_info was not read; the config must win over the directory name"
    )

    # No readable config, so the site-packages directory answers.
    no_config = os.path.join(base, "noconfig")
    _make_venv(no_config, OLD_TAG)
    os.unlink(os.path.join(no_config, "pyvenv.cfg"))
    assert venv_python_tag(no_config) == OLD_TAG, (
        "the site-packages fallback did not read the venv's Python version"
    )

    # An in-place upgrade leaves the older directory beside the one in use, so
    # the newest is the live one and the oldest names a dead tree.
    two_dirs = os.path.join(base, "twodirs")
    _make_venv(two_dirs, RUNNING_TAG)
    os.unlink(os.path.join(two_dirs, "pyvenv.cfg"))
    os.makedirs(os.path.join(two_dirs, "lib", f"python{OLD_TAG}", "site-packages"),
                exist_ok=True)
    assert venv_python_tag(two_dirs) == RUNNING_TAG, (
        f"two library directories must read as the newest, not the oldest: "
        f"{venv_python_tag(two_dirs)}"
    )

    # A config byte that is not UTF-8 must not raise out of the probe.
    bad_bytes = os.path.join(base, "badbytes")
    _make_venv(bad_bytes, RUNNING_TAG)
    with open(os.path.join(bad_bytes, "pyvenv.cfg"), "wb") as f:
        f.write(b"home = /usr/\xff\xfebin\nversion = " + RUNNING_TAG.encode() + b".4\n")
    assert venv_python_tag(bad_bytes) == RUNNING_TAG, (
        "a config byte that is not UTF-8 broke the version probe"
    )

    empty = os.path.join(base, "empty")
    os.makedirs(empty, exist_ok=True)
    assert venv_python_tag(empty) is None
    print("PASS: the venv's Python version reads from the config and from site-packages")


def check_stale_detection() -> None:
    """Treat a venv as stale only when its interpreter does not start.
    A usable older interpreter can be an intentional plugin pin and must remain intact."""
    base = os.path.join(gl.DATA_PATH, "stale")
    usable = os.path.join(base, "usable")
    _make_venv(usable, RUNNING_TAG)
    assert stale_venv_reason(usable) is None, (
        f"a venv with a working interpreter was called stale: {stale_venv_reason(usable)}"
    )

    other_version = os.path.join(base, "other_version")
    _make_venv(other_version, OLD_TAG)
    assert stale_venv_reason(other_version) is None, (
        "a venv recorded against another Python version, whose interpreter still "
        f"runs, must be left alone: {stale_venv_reason(other_version)}"
    )

    dangling = os.path.join(base, "dangling")
    _make_venv(dangling, RUNNING_TAG)
    os.unlink(os.path.join(dangling, "bin", "python"))
    os.symlink(os.path.join(dangling, "bin", "gone"), os.path.join(dangling, "bin", "python"))
    reason = stale_venv_reason(dangling)
    assert reason is not None, (
        "a dangling bin/python symlink was not detected; exists() must follow the link"
    )

    # A venv built with copies keeps a binary that outlives the library it
    # needs, so the file being there proves nothing and it has to be started.
    broken_binary = os.path.join(base, "broken_binary")
    _make_venv(broken_binary, RUNNING_TAG)
    interpreter = os.path.join(broken_binary, "bin", "python")
    os.unlink(interpreter)
    with open(interpreter, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(interpreter, 0o755)
    assert stale_venv_reason(broken_binary) is not None, (
        "an interpreter that is present but does not start was not detected"
    )

    assert stale_venv_reason(os.path.join(base, "absent")) is None, (
        "an absent venv is the launch command's verdict, not this one's"
    )
    print("PASS: only an interpreter that does not start makes a venv stale")


def check_rebuild_uses_gate_argv() -> None:
    """Run plugin install steps as argv lists and restore the loopback guard.
    A rebuilt venv must not launch an unguarded backend."""
    plugin_dir = _plugin("com_test_rebuild", requirements=True)
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, OLD_TAG, stale=True)
    _set_policy("always")

    steps = _InstallStepStub(build=(venv_path, RUNNING_TAG))
    _with_stub_gate(steps, lambda: attempt_backend_venv_repair(venv_path, plugin_dir, "com_test_rebuild"))

    hook = os.path.join(plugin_dir, "__install__.py")
    requirements = os.path.join(plugin_dir, "requirements.txt")
    assert steps.commands == [
        [sys.executable, hook],
        [sys.executable, "-m", "pip", "install", "-r", requirements],
    ], f"the rebuild did not run the install steps as argv lists: {steps.commands}"
    for command in steps.commands:
        assert all(isinstance(part, str) for part in command), "an argv item is not a string"

    assert stale_venv_reason(venv_path) is None, "the rebuild left the venv stale"
    assert not os.path.exists(f"{venv_path}.stale"), "the rebuild kept the stale tree"

    site_dir = os.path.join(venv_path, "lib", f"python{RUNNING_TAG}", "site-packages")
    pth = os.path.join(site_dir, "deckard_rpyc_guard.pth")
    assert os.path.isfile(pth), (
        "the rebuilt venv carries no loopback guard .pth; a backend launched "
        "from it would bind the wildcard address"
    )
    with open(pth) as f:
        assert f.read().strip() == "import deckard_rpyc_guard"
    assert os.path.isfile(os.path.join(site_dir, "deckard_rpyc_guard.py")), (
        "the guard module was not written beside its .pth"
    )
    print("PASS: the rebuild runs the install steps as argv lists and re-injects the guard")


def check_rebuild_runs_once_per_process() -> None:
    """Limit an unsuccessful rebuild to one attempt per process.
    Restore the stale tree so an incorrect verdict cannot destroy a working install."""
    plugin_dir = _plugin("com_test_once")
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, OLD_TAG, marker=True, stale=True)
    _set_policy("always")

    steps = _InstallStepStub()  # the steps build nothing, so the rebuild fails
    _with_stub_gate(steps, lambda: attempt_backend_venv_repair(venv_path, plugin_dir, "com_test_once"))
    assert len(steps.commands) == 1, f"expected one install step, got {steps.commands}"

    assert os.path.isfile(os.path.join(venv_path, PRESERVED_VENV_MARKER)), (
        "a failed rebuild destroyed the previous venv instead of putting it back"
    )
    assert not os.path.exists(f"{venv_path}.stale"), "the moved-aside tree was left behind"
    assert stale_venv_reason(venv_path) is not None, "the restored venv is not the stale one"

    _with_stub_gate(steps, lambda: attempt_backend_venv_repair(venv_path, plugin_dir, "com_test_once"))
    assert len(steps.commands) == 1, (
        f"the rebuild ran again for the same venv in one process: {steps.commands}"
    )
    print("PASS: a rebuild runs once per venv per process and restores what it moved aside")


def check_ask_policy_runs_nothing_unattended() -> None:
    """Run no install script at launch under the default "ask" policy.
    Unattended launch has no consent, including for plugins installed before consent records."""
    plugin_dir = _plugin("com_test_ask")
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, OLD_TAG, marker=True, stale=True)
    _set_policy("ask")
    assert not os.path.isfile(os.path.join(plugin_dir, install_script.SKIP_MARKER)), (
        "this leg needs a plugin with no recorded decision"
    )

    steps = _InstallStepStub(build=(venv_path, RUNNING_TAG))
    _with_stub_gate(steps, lambda: attempt_backend_venv_repair(venv_path, plugin_dir, "com_test_ask"))

    assert steps.commands == [], (
        f"the launch ran a plugin's install script with nobody asked: {steps.commands}"
    )
    assert os.path.isfile(os.path.join(venv_path, PRESERVED_VENV_MARKER)), (
        "the refused rebuild moved the venv aside anyway"
    )
    assert not os.path.exists(f"{venv_path}.stale"), "the refused rebuild left a stale tree"
    # A refusal here must record no decision: the user never answered, and a
    # marker would make a later store install skip the steps for good.
    assert not os.path.isfile(os.path.join(plugin_dir, install_script.SKIP_MARKER)), (
        "the launch recorded a decline the user never made"
    )
    _set_policy("always")
    print("PASS: the default policy runs no install script at a launch")


def check_rebuild_error_restores_venv() -> None:
    """Restore the plugin venv when an install step raises.
    An absent venv is not stale, so leaving it moved aside would prevent later repair and launch."""
    plugin_dir = _plugin("com_test_raise")
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, OLD_TAG, marker=True, stale=True)
    _set_policy("always")

    class _Raising(_InstallStepStub):
        def __call__(self, cmd, timeout_s, env=None):
            self.commands.append(list(cmd))
            raise OSError("no file descriptors left for the install step")

    steps = _Raising()
    raised = False
    try:
        _with_stub_gate(steps,
                        lambda: attempt_backend_venv_repair(venv_path, plugin_dir, "com_test_raise"))
    except OSError:
        raised = True
    assert raised, "the raise was swallowed, so the caller cannot see the failure"

    assert os.path.isdir(venv_path), "a raising rebuild left the plugin with no venv at all"
    assert os.path.isfile(os.path.join(venv_path, PRESERVED_VENV_MARKER)), (
        "a raising rebuild did not put the previous venv back"
    )
    assert not os.path.exists(f"{venv_path}.stale"), "the moved-aside tree was left behind"

    # An attempt that raised is no answer about this venv, so the next attempt
    # is allowed to try again.
    retry = _InstallStepStub(build=(venv_path, RUNNING_TAG))
    _with_stub_gate(retry, lambda: attempt_backend_venv_repair(venv_path, plugin_dir, "com_test_raise"))
    assert retry.commands, (
        "a rebuild that raised was booked as done, so nothing retries it"
    )
    print("PASS: a raising rebuild puts the venv back and stays retryable")


def check_rebuild_uses_short_timeout() -> None:
    """Give launch-time rebuilds less time than store installs.
    Rebuilds run on the serial warm-up thread, so one plugin must not block later hooks."""
    plugin_dir = _plugin("com_test_timeout")
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, OLD_TAG, stale=True)
    _set_policy("always")

    seen: list = []

    class _Timed(_InstallStepStub):
        def __call__(self, cmd, timeout_s, env=None):
            seen.append(timeout_s)
            return super().__call__(cmd, timeout_s, env)

    steps = _Timed(build=(venv_path, RUNNING_TAG))
    _with_stub_gate(steps,
                    lambda: attempt_backend_venv_repair(venv_path, plugin_dir, "com_test_timeout"))

    assert seen, "no install step ran"
    assert all(value == plugin_manager_module.BACKEND_VENV_REBUILD_TIMEOUT_S for value in seen), (
        f"the rebuild did not pass its own timeout to the gate: {seen}"
    )
    assert (plugin_manager_module.BACKEND_VENV_REBUILD_TIMEOUT_S
            < install_script.DEFAULT_TIMEOUT_S), (
        "the launch-time budget must be smaller than a store install's"
    )
    print("PASS: the launch-time rebuild runs on its own shorter budget")


def check_concurrent_launcher_waits_for_rebuild() -> None:
    """Make concurrent launchers of one venv wait for its rebuild.
    Seeing the tree moved aside looks non-stale and would launch an absent path."""
    plugin_dir = _plugin("com_test_concurrent")
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, OLD_TAG, stale=True)
    _set_policy("always")

    inside = threading.Event()
    may_finish = threading.Event()
    second_done = threading.Event()
    observed: list = []

    class _Blocking(_InstallStepStub):
        def __call__(self, cmd, timeout_s, env=None):
            inside.set()
            may_finish.wait(30)
            return super().__call__(cmd, timeout_s, env)

    steps = _Blocking(build=(venv_path, RUNNING_TAG))

    def first_launcher() -> None:
        _with_stub_gate(
            steps, lambda: attempt_backend_venv_repair(venv_path, plugin_dir, "com_test_concurrent"))

    def second_launcher() -> None:
        attempt_backend_venv_repair(venv_path, plugin_dir, "com_test_concurrent")
        observed.append(stale_venv_reason(venv_path))
        second_done.set()

    first = threading.Thread(target=first_launcher, name="first_launcher", daemon=True)
    first.start()
    assert inside.wait(30), "the first rebuild never reached its install step"

    second = threading.Thread(target=second_launcher, name="second_launcher", daemon=True)
    second.start()
    # The first rebuild has the tree moved aside right now, so the second must
    # still be waiting on the per-venv lock.
    assert not second_done.wait(0.5), (
        "the second launcher went through while the rebuild held the venv moved "
        "aside; it read an absent venv as nothing to repair"
    )

    may_finish.set()
    first.join(30)
    second.join(30)
    assert not first.is_alive() and not second.is_alive(), "a launcher never finished"
    assert observed == [None], (
        f"the second launcher did not find a repaired venv: {observed}"
    )
    assert os.path.isdir(venv_path), "the rebuild left no venv behind"
    print("PASS: a second launcher of the same venv waits for the rebuild")


def check_declined_steps_preserve_venv() -> None:
    """The install-script policy decides the rebuild, as it decides an install."""
    plugin_dir = _plugin("com_test_declined")
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, OLD_TAG, marker=True, stale=True)
    _set_policy("never")

    steps = _InstallStepStub(build=(venv_path, RUNNING_TAG))
    _with_stub_gate(steps,
                    lambda: attempt_backend_venv_repair(venv_path, plugin_dir, "com_test_declined"))

    assert steps.commands == [], (
        f"the rebuild ran install steps the policy refuses: {steps.commands}"
    )
    assert os.path.isfile(os.path.join(venv_path, PRESERVED_VENV_MARKER)), (
        "a refused rebuild moved the venv aside anyway"
    )
    assert not os.path.exists(f"{venv_path}.stale"), "a refused rebuild left a stale tree"
    _set_policy("always")
    print("PASS: the install-script policy decides whether a venv is rebuilt")


def check_missing_plugin_dir_records_attempt() -> None:
    """Report a venv with an unknown plugin directory only once.
    Without install steps there is nothing to retry, and each launch would only repeat the log."""
    venv_path = os.path.join(gl.DATA_PATH, "orphan", ".venv")
    _make_venv(venv_path, OLD_TAG, marker=True, stale=True)
    _set_policy("always")
    missing_dir = os.path.join(gl.DATA_PATH, "orphan", "no-such-plugin")

    steps = _InstallStepStub(build=(venv_path, RUNNING_TAG))
    for _ in range(2):
        _with_stub_gate(steps,
                        lambda: attempt_backend_venv_repair(venv_path, missing_dir, "com_test_orphan"))

    assert steps.commands == [], (
        f"install steps ran without a plugin directory: {steps.commands}"
    )
    assert os.path.isfile(os.path.join(venv_path, PRESERVED_VENV_MARKER)), "the venv was touched"
    key = os.path.realpath(venv_path)
    assert key in plugin_manager_module._venv_rebuild_attempts, (
        "a venv with no plugin directory was never booked, so every launch of "
        "that backend repeats the whole attempt"
    )
    print("PASS: a venv with no plugin directory is booked like any other attempt")


def check_usable_venv_is_untouched() -> None:
    """A venv the running Python can use runs no install steps at all."""
    plugin_dir = _plugin("com_test_usable")
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, RUNNING_TAG, marker=True)
    _set_policy("always")

    steps = _InstallStepStub()
    _with_stub_gate(steps, lambda: attempt_backend_venv_repair(venv_path, plugin_dir, "com_test_usable"))
    assert steps.commands == [], f"a usable venv was rebuilt: {steps.commands}"
    assert os.path.isfile(os.path.join(venv_path, PRESERVED_VENV_MARKER))
    print("PASS: a usable venv runs no install steps")


def check_venv_check_precedes_argv_build() -> None:
    """Check the venv before launch_backend builds its command.
    Command construction rejects a missing interpreter before a later check could repair it."""
    from src.backend.PluginManager.PluginBase import PluginBase

    plugin_dir = _plugin("com_test_launch_order")
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, RUNNING_TAG)
    os.unlink(os.path.join(venv_path, "bin", "python"))

    seen: list = []
    real_repair = plugin_manager_module.attempt_backend_venv_repair
    real_subprocess = plugin_base_module.subprocess

    def recording_repair(path, plugin_path, name):
        # Record without rebuilding; command refusal then proves that the check ran first.
        seen.append((path, plugin_path, name))

    class _LaunchStop(Exception):
        pass

    def _refuse_spawn(*args, **kwargs):
        raise _LaunchStop()

    plugin = PluginBase.__new__(PluginBase)
    plugin.server = types.SimpleNamespace(port=1)
    plugin.PATH = plugin_dir
    plugin._backend_event_hold = types.SimpleNamespace(arm=lambda: None)
    plugin._backend_launch_generation = 0
    plugin._backend_stop_requested = False
    plugin._backend_via_terminal = False
    plugin._backend_ready = types.SimpleNamespace(clear=lambda: None)

    plugin_manager_module.attempt_backend_venv_repair = recording_repair
    plugin_base_module.subprocess = types.SimpleNamespace(Popen=_refuse_spawn)
    try:
        plugin.launch_backend(fixtures.__file__, venv_path=venv_path)
    except (ValueError, _LaunchStop):
        pass
    finally:
        plugin_manager_module.attempt_backend_venv_repair = real_repair
        plugin_base_module.subprocess = real_subprocess

    assert seen == [(venv_path, plugin_dir, plugin.get_plugin_id_from_folder_name())], (
        f"launch_backend did not check the venv with the plugin's own directory "
        f"before it built the launch command: {seen}"
    )
    print("PASS: launch_backend checks the venv before it builds the launch command")


def main() -> None:
    # Below the per-scenario timeout of run_all.py, so a stall reports here
    # with a message instead of an opaque runner timeout.
    fixtures.start_watchdog(90, label="scenario_backend_venv_rebuild")
    # The gate reads the install-scripts policy from the app settings.
    fixtures.install_stub_globals()

    check_version_tag_reading()
    check_stale_detection()
    check_rebuild_uses_gate_argv()
    check_ask_policy_runs_nothing_unattended()
    check_rebuild_error_restores_venv()
    check_rebuild_uses_short_timeout()
    check_concurrent_launcher_waits_for_rebuild()
    check_rebuild_runs_once_per_process()
    check_declined_steps_preserve_venv()
    check_missing_plugin_dir_records_attempt()
    check_usable_venv_is_untouched()
    check_venv_check_precedes_argv_build()

    print("PASS: scenario_backend_venv_rebuild")


if __name__ == "__main__":
    main()
