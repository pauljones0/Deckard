"""Gate plugin install steps by policy and consent before download.
Cover timeout, environment, loopback guard, and optional bwrap confinement."""

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401  (import order; the gate reads settings later)

import json
import os
import textwrap
import time

from src.backend.Store import install_script
from src.backend.Store.install_script import Outcome, decide_install_scripts, run_install_steps


def _plugin_dir(name: str) -> str:
    path = os.path.join(gl.DATA_PATH, "plugins", name)
    os.makedirs(path, exist_ok=True)
    return path


def _write_hook(plugin_dir: str, body: str) -> None:
    with open(os.path.join(plugin_dir, "__install__.py"), "w") as f:
        f.write(textwrap.dedent(body))


def _set_policy(value: str) -> None:
    gl.settings_manager.get_app_settings().setdefault("store", {})["install-scripts"] = value


def test_no_steps() -> None:
    plugin_dir = _plugin_dir("com_test_Empty")
    assert run_install_steps(plugin_dir, "Empty", run=True) is Outcome.NO_STEPS


def test_decide_policy_and_consent() -> None:
    """The decision is policy first, then the consent callable under ask,
    then a sticky prior decline for unattended runs."""
    plugin_dir = _plugin_dir("com_test_Decide")

    _set_policy("never")
    assert decide_install_scripts(None, "D", None) is False
    assert decide_install_scripts(None, "D", lambda n: True) is False, "never overrides consent"

    _set_policy("always")
    assert decide_install_scripts(None, "D", None) is True
    assert decide_install_scripts(None, "D", lambda n: False) is True, "always overrides consent"

    _set_policy("ask")
    seen: list = []
    assert decide_install_scripts(None, "D", lambda n: seen.append(n) or True) is True
    assert seen == ["D"], "the consent callable must receive the display name"
    assert decide_install_scripts(None, "D", lambda n: False) is False
    assert decide_install_scripts(None, "D", None) is True, "ask with no callable runs"

    # A prior decline is sticky for an unattended re-run (auto-update).
    with open(os.path.join(plugin_dir, install_script.SKIP_MARKER), "w") as f:
        f.write("declined\n")
    assert decide_install_scripts(plugin_dir, "D", None) is False, "a prior decline must be respected"
    assert decide_install_scripts(plugin_dir, "D", lambda n: True) is True, "an explicit ask overrides the marker"

    _set_policy("garbage")
    assert decide_install_scripts(None, "D", lambda n: False) is False, "garbage policy reads as ask"
    _set_policy("ask")


def test_skip_marker_without_execution() -> None:
    plugin_dir = _plugin_dir("com_test_Skip")
    _write_hook(plugin_dir, """
        import os
        base = os.path.dirname(os.path.abspath(__file__))
        open(os.path.join(base, "hook-ran"), "w").write("yes")
    """)
    assert run_install_steps(plugin_dir, "Skip", run=False) is Outcome.SKIPPED
    assert not os.path.isfile(os.path.join(plugin_dir, "hook-ran")), "a skip must not run the hook"
    assert os.path.isfile(os.path.join(plugin_dir, install_script.SKIP_MARKER)), "a skip must leave a marker"


def test_hook_guard_in_new_venv() -> None:
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
    assert run_install_steps(plugin_dir, "Runs", run=True) is Outcome.RAN
    assert os.path.isfile(os.path.join(plugin_dir, "hook-ran")), "the hook must execute"
    site = os.path.join(plugin_dir, ".venv", "lib", "python3.13", "site-packages")
    assert os.path.isfile(os.path.join(site, "deckard_rpyc_guard.pth")), (
        "a venv the hook created must carry the loopback guard"
    )
    assert os.path.isfile(os.path.join(site, "deckard_rpyc_guard.py"))


def test_failing_hook_reports_failed() -> None:
    plugin_dir = _plugin_dir("com_test_Fails")
    _write_hook(plugin_dir, "raise SystemExit(3)\n")
    assert run_install_steps(plugin_dir, "Fails", run=True) is Outcome.FAILED


def test_timeout_process_group_kill() -> None:
    """Kill a timed-out unconfined hook and its process-group children.
    bwrap uses its PID namespace instead, so this test disables it."""
    plugin_dir = _plugin_dir("com_test_Hangs")
    _write_hook(plugin_dir, """
        import os, subprocess, sys, time
        base = os.path.dirname(os.path.abspath(__file__))
        # A child in the SAME session, so the group kill reaches it.
        child = subprocess.Popen([sys.executable, "-c",
            "import time, sys; time.sleep(30); open(sys.argv[1], 'w').write('leaked')",
            os.path.join(base, "child-survived")])
        open(os.path.join(base, "child-pid"), "w").write(str(child.pid))
        time.sleep(30)
    """)
    started = time.monotonic()
    assert run_install_steps(plugin_dir, "Hangs", run=True, timeout_s=1.0, use_bwrap=False) is Outcome.TIMEOUT
    elapsed = time.monotonic() - started
    assert elapsed < 12, f"the kill must fire near the deadline, took {elapsed:.1f}s"
    with open(os.path.join(plugin_dir, "child-pid")) as f:
        child_pid = int(f.read())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            break  # the pid died and another user's process reused it
        time.sleep(0.05)
    else:
        raise AssertionError("the hook's group child survived the kill")
    assert not os.path.isfile(os.path.join(plugin_dir, "child-survived"))


def test_env_is_poisoned_and_stripped() -> None:
    """Poison the session bus address and remove display environment keys."""
    plugin_dir = _plugin_dir("com_test_Env")
    os.environ["DISPLAY"] = ":0"
    os.environ["XAUTHORITY"] = "/home/x/.Xauthority"
    _write_hook(plugin_dir, """
        import json, os
        base = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base, "env-seen"), "w") as f:
            json.dump({
                "bus": os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
                "display": os.environ.get("DISPLAY"),
                "xauth": os.environ.get("XAUTHORITY"),
            }, f)
    """)
    assert run_install_steps(plugin_dir, "Env", run=True, use_bwrap=False) is Outcome.RAN
    with open(os.path.join(plugin_dir, "env-seen")) as f:
        seen = json.load(f)
    assert seen["bus"] == "disabled:", f"bus address must be poisoned, got {seen!r}"
    assert seen["display"] is None and seen["xauth"] is None, f"display keys must be stripped, got {seen!r}"


def test_confined_pip_environment() -> None:
    """Route requirements through bwrap with a writable interpreter prefix."""
    plugin_dir = _plugin_dir("com_test_Reqs")
    req = os.path.join(plugin_dir, "requirements.txt")
    with open(req, "w") as f:
        f.write("example-package==1.0\n")
    seen: list = []
    real_execute = install_script._execute

    def capture(cmd, timeout_s, env=None):
        seen.append((cmd, env))
        return 0, False, ""

    install_script._execute = capture
    try:
        assert run_install_steps(plugin_dir, "Reqs", run=True, use_bwrap=True) is Outcome.RAN
    finally:
        install_script._execute = real_execute
    (cmd, env), = seen
    assert cmd[0] == "bwrap" and "--" in cmd, f"the pip step must be confined, got {cmd!r}"
    import sys
    assert cmd.count(sys.prefix) >= 1, "the interpreter prefix must be bound writable for pip"
    assert req in cmd and "pip" in cmd, f"the pip file must reach pip, got {cmd!r}"
    assert env is not None and env.get("DBUS_SESSION_BUS_ADDRESS") == "disabled:"


def test_bwrap_production_confinement() -> None:
    """Confine hooks through automatic bwrap detection when bwrap works.
    The sandbox must hide the session bus and deny writes outside the plugin."""
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
        # use_bwrap=None routes through _bwrap_works, the production path.
        assert run_install_steps(plugin_dir, "Bwrap", run=True, use_bwrap=None) is Outcome.RAN
        with open(os.path.join(plugin_dir, "confinement-report")) as f:
            report = json.load(f)
        assert report["escape"].startswith("denied"), f"a write outside the plugin dir must fail, got {report!r}"
        assert report["bus_socket"] is False, "no session bus socket may be visible"
        assert not os.path.exists(escape), "the escape file must not exist on the host"
    finally:
        if os.path.exists(escape):
            os.remove(escape)


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_install_script_gate")
    fixtures.install_stub_globals()
    test_no_steps()
    test_decide_policy_and_consent()
    test_skip_marker_without_execution()
    test_hook_guard_in_new_venv()
    test_failing_hook_reports_failed()
    test_timeout_process_group_kill()
    test_env_is_poisoned_and_stripped()
    test_confined_pip_environment()
    test_bwrap_production_confinement()
    print("scenario_install_script_gate: OK")


if __name__ == "__main__":
    main()
