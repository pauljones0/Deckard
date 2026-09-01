"""Launch plugin and action backends with argv lists and a usable interpreter.
Preserve spaces and quotes, select venv Python or sys.executable, and validate paths."""
import os
import sys
import threading
import time
import types

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl

# Stub launch and registration registries before importing ActionCore.
# A real PluginManager loads the plugin ecosystem.
gl.plugin_manager = types.SimpleNamespace(backends=[], backend_processes=[])

from src.backend.PluginManager.ActionCore import ActionCore  # noqa: E402
from src.backend.PluginManager.PluginManager import (  # noqa: E402
    build_backend_launch_command,
    frontend_authenticator,
)


# A path that a shell would mangle in three different ways at once.
SHELL_METACHAR_DIR = "backend dir with spaces & 'quotes'"

STUB_BACKEND = '''\
"""Minimal mirror of streamcontroller_plugin_tools.BackendBase: parse --port,
connect back to the frontend, serve on our own port, register."""
import argparse
import threading

import rpyc
from rpyc.utils.server import ThreadedServer


class StubBackend(rpyc.Service):
    bind_ip = None

    def get_marker(self) -> str:
        return "stub-backend-alive"

    def get_bind_ip(self) -> str:
        return type(self).bind_ip


def main() -> None:
    parser = argparse.ArgumentParser(prog="stub backend")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    frontend_connection = rpyc.connect("localhost", args.port,
                                       config={"allow_public_attrs": True})
    frontend = frontend_connection.root

    # ThreadedServer binds in __init__, so .port is valid before start(). No
    # hostname, so an unguarded child binds the wildcard address; the injected
    # guard rewrites this one to loopback, which get_bind_ip() reports back.
    server = ThreadedServer(StubBackend(), port=0,
                            protocol_config={"allow_public_attrs": True})
    StubBackend.bind_ip = server.listener.getsockname()[0]
    threading.Thread(target=server.start, name="stub_backend_server",
                     daemon=False).start()

    frontend.register_backend(port=server.port)


if __name__ == "__main__":
    main()
'''


def _write_stub_backend() -> str:
    directory = os.path.join(gl.DATA_PATH, SHELL_METACHAR_DIR)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "backend stub.py")
    with open(path, "w") as f:
        f.write(STUB_BACKEND)
    return path


def _make_venv(name: str) -> str:
    """Build a venv skeleton with a resolvable bin/python.
    A dangling interpreter must produce a descriptive ValueError, not Popen's FileNotFoundError."""
    venv_path = os.path.join(gl.DATA_PATH, name)
    os.makedirs(os.path.join(venv_path, "bin"), exist_ok=True)
    interpreter = os.path.join(venv_path, "bin", "python")
    if not os.path.exists(interpreter):
        os.symlink(sys.executable, interpreter)
    return venv_path


def check_argv_shape(backend_path: str) -> None:
    venv_path = _make_venv("venv with spaces")

    # Without a venv the command uses this interpreter, not the python3 on PATH.
    command = build_backend_launch_command(backend_path, None, 4242)
    assert command == [sys.executable, backend_path, "--port=4242"], command
    print("PASS: venv-less launch uses sys.executable and a 3-item argv")

    # With a venv the command uses that venv's interpreter.
    command = build_backend_launch_command(backend_path, venv_path, 4242)
    assert command == [os.path.join(venv_path, "bin", "python"), backend_path, "--port=4242"], command
    print("PASS: venv launch uses {venv}/bin/python")

    # Spaces and quotes stay inside one argv item. A shell string would turn
    # each of these into several words.
    assert " " in backend_path and "'" in backend_path, "test path lost its metacharacters"
    for item in command:
        assert isinstance(item, str), f"argv items must be strings: {item!r}"
    assert command.count(backend_path) == 1, (
        f"backend path is not a single intact argv item: {command}"
    )
    print("PASS: spaced/quoted paths survive as single argv items")

    # In the terminal debug form the paths ride in as bash positional
    # parameters, so the script text stays constant with nothing to interpolate.
    os.environ.pop("DECKARD_TERMINAL", None)
    command = build_backend_launch_command(backend_path, venv_path, 4242, open_in_terminal=True)
    assert command == [
        "gnome-terminal", "--", "bash", "-c", '"$1" "$2" --port="$3"; exec $SHELL',
        "deckard-backend", os.path.join(venv_path, "bin", "python"), backend_path, "4242",
    ], command
    script = command[4]
    assert backend_path not in script and venv_path not in script, (
        f"user paths were interpolated into the bash script: {script!r}"
    )
    print("PASS: terminal form passes paths as bash positional parameters")

    # DECKARD_TERMINAL is a complete prefix: gnome-terminal uses --;
    # konsole/alacritty/xterm use -e; kitty uses positional, so no separator serves all.
    for spec, expected_prefix in (
        ("kitty", ["kitty"]),
        ("konsole -e", ["konsole", "-e"]),
        ("alacritty -e", ["alacritty", "-e"]),
        ("'my terminal' --", ["my terminal", "--"]),
    ):
        os.environ["DECKARD_TERMINAL"] = spec
        command = build_backend_launch_command(backend_path, None, 4242, open_in_terminal=True)
        assert command[:len(expected_prefix)] == expected_prefix, (spec, command)
        assert command[len(expected_prefix)] == "bash", (spec, command)
        assert command[-3:] == [sys.executable, backend_path, "4242"], (spec, command)
    os.environ.pop("DECKARD_TERMINAL", None)
    print("PASS: DECKARD_TERMINAL carries the terminal's own exec flag")

    # A blank setting must not strip the terminal off the argv, which would run
    # bash headless and pass the whole thing to the wrong argv[0].
    os.environ["DECKARD_TERMINAL"] = "   "
    command = build_backend_launch_command(backend_path, None, 4242, open_in_terminal=True)
    assert command[:2] == ["gnome-terminal", "--"], command
    os.environ.pop("DECKARD_TERMINAL", None)
    print("PASS: a blank DECKARD_TERMINAL falls back to the default")


def check_path_validation(backend_path: str) -> None:
    """Verify the shared PluginBase and ActionCore path-validation contract."""
    missing = os.path.join(gl.DATA_PATH, "definitely", "not", "here.py")

    for bad in (None, missing):
        try:
            build_backend_launch_command(bad, None, 4242)
        except ValueError:
            pass
        except TypeError as e:
            raise AssertionError(f"backend_path={bad!r} reached os.path.exists: {e}")
        else:
            raise AssertionError(f"backend_path={bad!r} did not raise -- would Popen garbage")

    try:
        build_backend_launch_command(backend_path, missing, 4242)
    except ValueError:
        pass
    else:
        raise AssertionError("a missing venv_path did not raise")

    # A Python upgrade can leave a dangling bin/python.
    # Raise ValueError naming it instead of exposing Popen's FileNotFoundError.
    broken_venv = os.path.join(gl.DATA_PATH, "broken venv")
    os.makedirs(os.path.join(broken_venv, "bin"), exist_ok=True)
    dangling = os.path.join(broken_venv, "bin", "python")
    if not os.path.lexists(dangling):
        os.symlink(os.path.join(gl.DATA_PATH, "gone", "python3.13"), dangling)
    try:
        build_backend_launch_command(backend_path, broken_venv, 4242)
    except ValueError as e:
        assert "interpreter" in str(e), f"unhelpful message for a broken venv: {e}"
    except OSError as e:
        raise AssertionError(f"a dangling venv interpreter escaped as OSError: {e}")
    else:
        raise AssertionError("a venv with no usable interpreter did not raise")

    print("PASS: ValueError for None/missing backend_path, missing venv and dangling interpreter")


def _make_action() -> ActionCore:
    """Build an ActionCore with only backend-launch state.
    Bypass __init__ because its deck controller, page, and plugin base are outside this contract."""
    action = ActionCore.__new__(ActionCore)
    action.action_id = "argv-test-action"
    action.backend_connection = None
    action.backend = None
    action.server = None
    action.backend_process = None
    action._backend_via_terminal = False
    action._backend_ready = threading.Event()
    return action


def check_end_to_end_spaced_path(backend_path: str) -> None:
    """Launch and register a backend under a spaced directory name.
    A shell string would split the path into words."""
    action = _make_action()
    try:
        action.launch_backend(backend_path)

        assert action.backend_process is not None, "no backend process was spawned"
        assert fixtures.wait_until(
            lambda: action.backend_connection is not None, timeout=30.0
        ), (
            f"the backend under {SHELL_METACHAR_DIR!r} never registered (process "
            f"returncode={action.backend_process.poll()!r}) -- a shell-built "
            f"command line splits the spaced path into separate words"
        )
        assert action.backend.get_marker() == "stub-backend-alive", (
            "registered, but the backend proxy does not answer"
        )
        print("PASS: a backend under a spaced/quoted path launches and registers")

        # Verify live wiring for the frontend authenticator, the PYTHONPATH-injected loopback guard,
        # and register_backend's child-port ownership check.
        assert action.server.authenticator is frontend_authenticator, (
            "start_server did not install the frontend authenticator"
        )
        assert action.backend.get_bind_ip() == "127.0.0.1", (
            f"the launched child bound {action.backend.get_bind_ip()!r}, not loopback -- "
            f"the injected guard did not arm in the real Popen environment"
        )
        print("PASS: the live launch installs the authenticator and the guard binds the child to loopback")
    finally:
        # Keep the handle before resource release clears it and sends SIGTERM on a daemon thread.
        # Otherwise the stdout-inheriting stub can outlive us and block run_all.py's pipe.
        process = action.backend_process
        action.on_disconnect(None)
        # Guarded, because launch_backend may have raised and left no process.
        # An AttributeError here would replace the real failure.
        if process is not None:
            assert fixtures.wait_until(lambda: process.poll() is not None, timeout=15.0), (
                "the stub backend was never terminated"
            )


def check_wait_for_backend_event() -> None:
    """Wait on the event set by register_backend.
    Mid-wait registration wakes immediately, while tries remains a tries * 0.1 s timeout budget."""
    action = _make_action()

    # An already registered backend returns at once, not on a tick boundary.
    action._backend_ready.set()
    start = time.monotonic()
    action.wait_for_backend()
    elapsed = time.monotonic() - start
    assert elapsed < 0.05, f"wait_for_backend slept {elapsed:.3f}s despite a ready backend"
    print(f"PASS: wait_for_backend returns in {elapsed*1000:.1f}ms when the backend is ready")

    # A backend that never registers stays bounded by tries * 0.1 s.
    action._backend_ready.clear()
    start = time.monotonic()
    action.wait_for_backend()
    elapsed = time.monotonic() - start
    assert 0.2 < elapsed < 1.5, f"default wait was {elapsed:.3f}s, expected ~0.3s"
    print(f"PASS: wait_for_backend times out after {elapsed:.2f}s (tries=3 -> 0.3s)")

    # A registration that arrives mid-wait wakes the Event early.
    action._backend_ready.clear()
    threading.Timer(0.1, action._backend_ready.set).start()
    start = time.monotonic()
    action.wait_for_backend(tries=50)  # 5s budget
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"wait_for_backend did not wake on the Event: {elapsed:.3f}s"
    print(f"PASS: a mid-wait registration wakes wait_for_backend in {elapsed*1000:.0f}ms")


def check_register_backend_verifies_port() -> None:
    """Verify that register_backend rejects a port unowned by the launched backend.
    With no launched process every port is unowned, so skipped live wiring would connect."""
    action = _make_action()
    try:
        action.register_backend(port=0)
    except RuntimeError:
        pass
    else:
        raise AssertionError("register_backend connected without verifying the port")
    assert action.backend_connection is None, "register_backend connected despite the refusal"
    print("PASS: register_backend verifies the port before connecting")


def main() -> None:
    # Below the per-scenario timeout of run_all.py, so a stall is reported here
    # with a message rather than as an opaque runner timeout.
    fixtures.start_watchdog(75, label="scenario_backend_launch_argv")

    backend_path = _write_stub_backend()

    check_argv_shape(backend_path)
    check_path_validation(backend_path)
    check_end_to_end_spaced_path(backend_path)
    check_register_backend_verifies_port()
    check_wait_for_backend_event()

    print("PASS: scenario_backend_launch_argv")


if __name__ == "__main__":
    main()
