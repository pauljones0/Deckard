"""The gate in front of a plugin's install steps.

run_install_steps is the one executor of __install__.py and the
requirements pip step. This drives a real hook subprocess through it and
pins the timeout's process-group kill, the loopback-guard re-injection
into a venv the hook created, and the pip step's routing through the
gate's _execute seam so nothing here ever really runs pip.
"""

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401  (import order; the gate reads settings later)

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
    install_script._execute = lambda cmd, timeout_s: (seen.append(cmd), (0, False))[1]
    try:
        assert run_install_steps(plugin_dir, "Reqs") is Outcome.RAN
    finally:
        install_script._execute = real_execute
    assert len(seen) == 1 and seen[0][-1] == req and "pip" in seen[0], (
        f"the pip step must route through _execute, got {seen!r}"
    )


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_install_script_gate")
    test_no_steps()
    test_hook_runs_and_guard_lands_in_new_venv()
    test_failing_hook_reports_failed()
    test_timeout_kills_the_process_group()
    test_pip_step_routes_through_execute()
    print("scenario_install_script_gate: OK")


if __name__ == "__main__":
    main()
