"""A backend venv that a Python upgrade stranded is rebuilt once.

A plugin backend runs on its own venv's interpreter, and that venv is built
for one Python minor version. A system upgrade to the next one leaves the
venv behind, and the backend never starts again. The launch detects that,
rebuilds the venv through the install-script gate a store install uses, and
tries it once per venv per process.

The install steps run as real argv lists here, captured at the gate's
subprocess seam, so the shape of every command the rebuild issues is pinned.
"""

import os
import sys
import textwrap
import types

import fixtures  # noqa: F401  (isolated --data tempdir; import first)

import globals as gl  # noqa: E402

from src.backend.Store import install_script  # noqa: E402
from src.backend.PluginManager import PluginBase as plugin_base_module  # noqa: E402
from src.backend.PluginManager import PluginManager as plugin_manager_module  # noqa: E402
from src.backend.PluginManager.PluginManager import (  # noqa: E402
    ensure_backend_venv,
    stale_venv_reason,
    venv_python_tag,
)

RUNNING_TAG = f"{sys.version_info.major}.{sys.version_info.minor}"
OLD_TAG = f"{sys.version_info.major}.{sys.version_info.minor - 1}"
MARKER = "built-by-the-previous-python"


def _make_venv(venv_path: str, tag: str, marker: bool = False) -> None:
    """A venv-shaped tree: pyvenv.cfg, bin/python and site-packages."""
    os.makedirs(os.path.join(venv_path, "bin"), exist_ok=True)
    os.makedirs(os.path.join(venv_path, "lib", f"python{tag}", "site-packages"), exist_ok=True)
    with open(os.path.join(venv_path, "pyvenv.cfg"), "w") as f:
        f.write(f"home = /usr/bin\nversion = {tag}.4\ninclude-system-site-packages = false\n")
    # A real venv's bin/python is a symlink to the interpreter it was built
    # against, which is what a Python upgrade breaks.
    link = os.path.join(venv_path, "bin", "python")
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(sys.executable, link)
    if marker:
        with open(os.path.join(venv_path, MARKER), "w") as f:
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


class _Steps:
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


def _with_stub_gate(steps: _Steps, call) -> None:
    """Run call with the gate's subprocess seam and bwrap probe stubbed.

    bwrap is stubbed off so the recorded argv is the command itself, not a
    confinement prefix whose shape depends on the host. scenario_install_
    script_gate covers the confined tier.
    """
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
    """The recorded version, and the site-packages name as the fallback."""
    base = os.path.join(gl.DATA_PATH, "tags")
    matching = os.path.join(base, "matching")
    _make_venv(matching, RUNNING_TAG)
    assert venv_python_tag(matching) == RUNNING_TAG

    # version_info instead of version, which newer venvs also write. The
    # directories say the running version here, so only a config read gives
    # the old one.
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

    empty = os.path.join(base, "empty")
    os.makedirs(empty, exist_ok=True)
    assert venv_python_tag(empty) is None
    print("PASS: the venv's Python version reads from the config and from site-packages")


def check_stale_detection() -> None:
    """A usable venv is left alone; an upgrade and a lost interpreter are not."""
    base = os.path.join(gl.DATA_PATH, "stale")
    usable = os.path.join(base, "usable")
    _make_venv(usable, RUNNING_TAG)
    assert stale_venv_reason(usable) is None, (
        f"a venv built for the running Python was called stale: {stale_venv_reason(usable)}"
    )

    upgraded = os.path.join(base, "upgraded")
    _make_venv(upgraded, OLD_TAG)
    reason = stale_venv_reason(upgraded)
    assert reason is not None and OLD_TAG in reason and RUNNING_TAG in reason, (
        f"a venv built for an older Python was not detected: {reason}"
    )

    dangling = os.path.join(base, "dangling")
    _make_venv(dangling, RUNNING_TAG)
    os.unlink(os.path.join(dangling, "bin", "python"))
    os.symlink(os.path.join(dangling, "bin", "gone"), os.path.join(dangling, "bin", "python"))
    assert stale_venv_reason(dangling) is not None, (
        "a dangling bin/python symlink was not detected; exists() must follow the link"
    )

    assert stale_venv_reason(os.path.join(base, "absent")) is None, (
        "an absent venv is the launch command's verdict, not this one's"
    )
    print("PASS: a stranded venv is detected and a usable one is left alone")


def check_rebuild_runs_the_gate_with_argv_lists() -> None:
    """The rebuild runs the plugin's install steps as argv lists.

    It also puts the loopback guard back into the venv the steps created, so
    a rebuilt venv never launches a backend unguarded.
    """
    plugin_dir = _plugin("com_test_rebuild", requirements=True)
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, OLD_TAG)
    _set_policy("always")

    steps = _Steps(build=(venv_path, RUNNING_TAG))
    _with_stub_gate(steps, lambda: ensure_backend_venv(venv_path, plugin_dir, "com_test_rebuild"))

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
    """A rebuild that cannot succeed costs one attempt, not one per launch.

    The stale tree also comes back, so a wrong verdict never destroys a
    working install.
    """
    plugin_dir = _plugin("com_test_once")
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, OLD_TAG, marker=True)
    _set_policy("always")

    steps = _Steps()  # the steps build nothing, so the rebuild fails
    _with_stub_gate(steps, lambda: ensure_backend_venv(venv_path, plugin_dir, "com_test_once"))
    assert len(steps.commands) == 1, f"expected one install step, got {steps.commands}"

    assert os.path.isfile(os.path.join(venv_path, MARKER)), (
        "a failed rebuild destroyed the previous venv instead of putting it back"
    )
    assert not os.path.exists(f"{venv_path}.stale"), "the moved-aside tree was left behind"
    assert stale_venv_reason(venv_path) is not None, "the restored venv is not the stale one"

    _with_stub_gate(steps, lambda: ensure_backend_venv(venv_path, plugin_dir, "com_test_once"))
    assert len(steps.commands) == 1, (
        f"the rebuild ran again for the same venv in one process: {steps.commands}"
    )
    print("PASS: a rebuild runs once per venv per process and restores what it moved aside")


def check_declined_install_steps_leave_the_venv() -> None:
    """The install-script policy decides the rebuild, as it decides an install."""
    plugin_dir = _plugin("com_test_declined")
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, OLD_TAG, marker=True)
    _set_policy("never")

    steps = _Steps(build=(venv_path, RUNNING_TAG))
    _with_stub_gate(steps,
                    lambda: ensure_backend_venv(venv_path, plugin_dir, "com_test_declined"))

    assert steps.commands == [], (
        f"the rebuild ran install steps the policy refuses: {steps.commands}"
    )
    assert os.path.isfile(os.path.join(venv_path, MARKER)), (
        "a refused rebuild moved the venv aside anyway"
    )
    assert not os.path.exists(f"{venv_path}.stale"), "a refused rebuild left a stale tree"
    _set_policy("always")
    print("PASS: the install-script policy decides whether a venv is rebuilt")


def check_usable_venv_is_untouched() -> None:
    """A venv the running Python can use runs no install steps at all."""
    plugin_dir = _plugin("com_test_usable")
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, RUNNING_TAG, marker=True)
    _set_policy("always")

    steps = _Steps()
    _with_stub_gate(steps, lambda: ensure_backend_venv(venv_path, plugin_dir, "com_test_usable"))
    assert steps.commands == [], f"a usable venv was rebuilt: {steps.commands}"
    assert os.path.isfile(os.path.join(venv_path, MARKER))
    print("PASS: a usable venv runs no install steps")


def check_launch_checks_the_venv_before_the_argv() -> None:
    """launch_backend asks about the venv before it builds the command.

    build_backend_launch_command refuses a venv with no interpreter, so a
    check that ran after it would never see a stranded venv.
    """
    from src.backend.PluginManager.PluginBase import PluginBase

    plugin_dir = _plugin("com_test_launch_order")
    venv_path = os.path.join(plugin_dir, "backend", ".venv")
    _make_venv(venv_path, RUNNING_TAG)
    os.unlink(os.path.join(venv_path, "bin", "python"))

    seen: list = []
    real_ensure = plugin_manager_module.ensure_backend_venv
    real_subprocess = plugin_base_module.subprocess

    def recording_ensure(path, plugin_path, name):
        # It records and rebuilds nothing, so the launch command below still
        # refuses the venv. That refusal is what proves the order: a check
        # that ran after it would never be reached.
        seen.append((path, plugin_path, name))

    class _LaunchStop(Exception):
        pass

    def _refuse_spawn(*args, **kwargs):
        raise _LaunchStop()

    plugin = PluginBase.__new__(PluginBase)
    plugin.server = types.SimpleNamespace(port=1)
    plugin.PATH = plugin_dir
    plugin._backend_event_hold = types.SimpleNamespace(arm=lambda: None)
    plugin._backend_launch_gen = 0
    plugin._backend_stop_requested = False
    plugin._backend_via_terminal = False
    plugin._backend_ready = types.SimpleNamespace(clear=lambda: None)

    plugin_manager_module.ensure_backend_venv = recording_ensure
    plugin_base_module.subprocess = types.SimpleNamespace(Popen=_refuse_spawn)
    try:
        plugin.launch_backend(fixtures.__file__, venv_path=venv_path)
    except (ValueError, _LaunchStop):
        pass
    finally:
        plugin_manager_module.ensure_backend_venv = real_ensure
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
    check_rebuild_runs_the_gate_with_argv_lists()
    check_rebuild_runs_once_per_process()
    check_declined_install_steps_leave_the_venv()
    check_usable_venv_is_untouched()
    check_launch_checks_the_venv_before_the_argv()

    print("PASS: scenario_backend_venv_rebuild")


if __name__ == "__main__":
    main()
