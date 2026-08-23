"""The gate in front of a plugin's install steps.

run_install_steps is the one executor of __install__.py and the
requirements pip step. This drives a real hook subprocess through it and
pins the timeout's process-group kill, the loopback-guard re-injection
into a venv the hook created, and the pip step's routing through the
gate's _execute seam so nothing here ever really runs pip.
"""

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401  (import order; the gate reads settings later)

import json
import os
import textwrap
import time

from src.backend.Store import install_script
from src.backend.Store.install_script import Outcome, run_install_steps


def _plugin_dir(name: str) -> str:
    path = os.path.join(gl.DATA_PATH, "plugins", name)
    os.makedirs(path, exist_ok=True)
    return path


def _write_hook(plugin_dir: str, body: str) -> None:
    with open(os.path.join(plugin_dir, "__install__.py"), "w") as f:
        f.write(textwrap.dedent(body))


def test_no_steps() -> None:
    plugin_dir = _plugin_dir("com_test_Empty")
    assert run_install_steps(plugin_dir, "Empty") is Outcome.NO_STEPS


def test_hook_runs_and_guard_lands_in_new_venv() -> None:
    """The hook really executes, and a venv it creates carries the
    loopback guard before any backend ever launches from it."""
    plugin_dir = _plugin_dir("com_test_Runs")
    _write_hook(plugin_dir, """
        import os
        base = os.path.dirname(os.path.abspath(__file__))
        open(os.path.join(base, "hook-ran"), "w").write("yes")
        site = os.path.join(base, ".venv", "lib", "python3.13", "site-packages")
        os.makedirs(site)
        open(os.path.join(base, ".venv", "pyvenv.cfg"), "w").write("home = /usr\\n")
    """)
    assert run_install_steps(plugin_dir, "Runs") is Outcome.RAN
    assert os.path.isfile(os.path.join(plugin_dir, "hook-ran")), "the hook must execute"
    site = os.path.join(plugin_dir, ".venv", "lib", "python3.13", "site-packages")
    assert os.path.isfile(os.path.join(site, "deckard_rpyc_guard.pth")), (
        "a venv the hook created must carry the loopback guard"
    )
    assert os.path.isfile(os.path.join(site, "deckard_rpyc_guard.py"))


def test_failing_hook_reports_failed() -> None:
    plugin_dir = _plugin_dir("com_test_Fails")
    _write_hook(plugin_dir, "raise SystemExit(3)\n")
    assert run_install_steps(plugin_dir, "Fails") is Outcome.FAILED


def test_timeout_kills_the_process_group() -> None:
    """A hung hook, children included, dies at the deadline instead of
    parking the install thread forever."""
    plugin_dir = _plugin_dir("com_test_Hangs")
    _write_hook(plugin_dir, """
        import os, subprocess, sys, time
        base = os.path.dirname(os.path.abspath(__file__))
        child = subprocess.Popen([sys.executable, "-c",
            "import time, sys; time.sleep(30); open(sys.argv[1], 'w').write('leaked')",
            os.path.join(base, "child-survived")])
        open(os.path.join(base, "child-pid"), "w").write(str(child.pid))
        time.sleep(30)
    """)
    started = time.monotonic()
    assert run_install_steps(plugin_dir, "Hangs", timeout_s=1.5) is Outcome.TIMEOUT
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"the kill must fire at the deadline, took {elapsed:.1f}s"
    # The grandchild shares the session, so the group kill took it too.
    with open(os.path.join(plugin_dir, "child-pid")) as f:
        child_pid = int(f.read())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            # The pid died and another user's process reused it.
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the hook's child survived the group kill")
    assert not os.path.isfile(os.path.join(plugin_dir, "child-survived"))


def test_pip_step_routes_through_execute() -> None:
    """The requirements step never runs pip here: it goes through the
    _execute seam, and the gate hands it the file the tree carries."""
    plugin_dir = _plugin_dir("com_test_Reqs")
    req = os.path.join(plugin_dir, "requirements.txt")
    with open(req, "w") as f:
        f.write("example-package==1.0\n")
    seen: list = []
    real_execute = install_script._execute
    install_script._execute = lambda cmd, timeout_s, env=None: (seen.append((cmd, env)), (0, False))[1]
    try:
        assert run_install_steps(plugin_dir, "Reqs") is Outcome.RAN
    finally:
        install_script._execute = real_execute
    (cmd, env), = seen
    assert cmd[-1] == req and "pip" in cmd, f"the pip step must route through _execute, got {cmd!r}"
    assert env is not None and env.get("DBUS_SESSION_BUS_ADDRESS") == "disabled:", (
        "the pip step must run with the poisoned bus address too"
    )


def test_hook_env_is_poisoned() -> None:
    """The script's session-bus address is invalid, not merely absent:
    an absent variable falls back to the runtime-dir bus socket."""
    plugin_dir = _plugin_dir("com_test_Env")
    _write_hook(plugin_dir, """
        import json, os
        base = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base, "env-seen"), "w") as f:
            json.dump({"bus": os.environ.get("DBUS_SESSION_BUS_ADDRESS")}, f)
    """)
    assert run_install_steps(plugin_dir, "Env", use_bwrap=False) is Outcome.RAN
    with open(os.path.join(plugin_dir, "env-seen")) as f:
        seen = json.load(f)
    assert seen["bus"] == "disabled:", f"bus address must be poisoned, got {seen!r}"


def test_bwrap_confines_the_hook() -> None:
    """Where bwrap operates: the script writes inside the plugin dir,
    cannot write outside it, and sees no session bus socket."""
    if not install_script._bwrap_works():
        print("  (bwrap unavailable here; confinement arm not exercised)")
        return
    plugin_dir = _plugin_dir("com_test_Bwrap")
    escape = os.path.join(os.path.expanduser("~"), "install-gate-escape-probe")
    _write_hook(plugin_dir, f"""
        import json, os
        base = os.path.dirname(os.path.abspath(__file__))
        report = {{}}
        try:
            open({escape!r}, "w").write("escaped")
            report["escape"] = "wrote"
        except OSError as e:
            report["escape"] = f"denied: {{e.__class__.__name__}}"
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        report["bus_socket"] = bool(runtime_dir) and os.path.exists(os.path.join(runtime_dir, "bus"))
        with open(os.path.join(base, "confinement-report"), "w") as f:
            json.dump(report, f)
    """)
    try:
        assert run_install_steps(plugin_dir, "Bwrap", use_bwrap=True) is Outcome.RAN
        with open(os.path.join(plugin_dir, "confinement-report")) as f:
            report = json.load(f)
        assert report["escape"].startswith("denied"), (
            f"a write outside the plugin dir must fail, got {report!r}"
        )
        assert report["bus_socket"] is False, "no session bus socket may be visible"
        assert not os.path.exists(escape), "the escape file must not exist on the host"
    finally:
        if os.path.exists(escape):
            os.remove(escape)


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_install_script_gate")
    test_no_steps()
    test_hook_runs_and_guard_lands_in_new_venv()
    test_failing_hook_reports_failed()
    test_timeout_kills_the_process_group()
    test_pip_step_routes_through_execute()
    test_hook_env_is_poisoned()
    test_bwrap_confines_the_hook()
    print("scenario_install_script_gate: OK")


if __name__ == "__main__":
    main()
