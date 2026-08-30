"""Authenticate callers and use only a loopback port owned by the launched backend.
Pin its listening socket through /proc/<pid>/fd to reject lure ports."""
import socket
import subprocess
import sys
import types

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl

# PluginManager pulls PluginBase, whose module import reads these registries.
gl.plugin_manager = types.SimpleNamespace(backends=[], backend_processes=[])

from src.backend.PluginManager.ActionCore import ActionCore  # noqa: E402
from src.backend.PluginManager.PluginManager import verify_backend_port  # noqa: E402

# Listen on the selected address, report the port, and wait for stdin to close.
CHILD_SRC = """\
import socket, sys
s = socket.socket()
s.bind((sys.argv[1], 0))
s.listen()
print(s.getsockname()[1], flush=True)
sys.stdin.read()
"""


def _spawn_listener(bind_ip: str) -> tuple[subprocess.Popen, int]:
    # start_new_session leads a process group, so terminate_backend_process's
    # killpg reaches this listener the way it reaches a real backend.
    process = subprocess.Popen([sys.executable, "-c", CHILD_SRC, bind_ip],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                               start_new_session=True)
    port = int(process.stdout.readline())
    return process, port


def _make_action() -> ActionCore:
    action = ActionCore.__new__(ActionCore)
    action.action_id = "enforce-test-action"
    action.backend_connection = None
    action.backend = None
    action.server = None
    action.backend_process = None
    action._backend_via_terminal = False
    return action


def check_register_backend_terminates_exposed_child() -> None:
    """Terminate a backend that registers from a wildcard bind.
    Wait because refusal leaves the listener alive and termination runs off-thread."""
    action = _make_action()
    wild_child, wild_port = _spawn_listener("0.0.0.0")
    action.backend_process = wild_child
    try:
        try:
            action.register_backend(port=wild_port)
        except RuntimeError:
            pass
        else:
            raise AssertionError("an exposed backend registration was accepted")
        assert action.backend_connection is None, "register_backend connected to an exposed backend"
        assert fixtures.wait_until(lambda: wild_child.poll() is not None, timeout=15.0), (
            "the exposed backend process was not terminated"
        )
        print("PASS: register_backend refuses and terminates a wildcard-bound backend")
    finally:
        if wild_child.poll() is None:
            wild_child.kill()
        wild_child.stdin.close()


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_register_backend_pin")

    child, child_port = _spawn_listener("127.0.0.1")
    own_listener = socket.socket()
    own_listener.bind(("127.0.0.1", 0))
    own_listener.listen()
    own_port = own_listener.getsockname()[1]

    try:
        # The port the child owns passes and yields its loopback address.
        assert verify_backend_port(child_port, child, False, "pin-test") == "127.0.0.1"
        print("PASS: the launched child's own port passes the pin and returns its loopback address")

        # A listener the child does not own is a lure and refuses.
        try:
            verify_backend_port(own_port, child, False, "pin-test")
        except RuntimeError as e:
            assert "pid" in str(e), e
        else:
            raise AssertionError("a port the child does not own was accepted")
        print("PASS: a port owned by another process is refused")

        # No launched process, nothing to pin against: refuse.
        try:
            verify_backend_port(own_port, None, False, "pin-test")
        except RuntimeError:
            pass
        else:
            raise AssertionError("a registration without a launched process was accepted")
        print("PASS: a registration without a launched process is refused")

        # A dead port refuses: nothing listens there.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        try:
            verify_backend_port(dead_port, child, False, "pin-test")
        except RuntimeError:
            pass
        else:
            raise AssertionError("a port with no listener was accepted")
        print("PASS: a port with no listener is refused")

        # The terminal path cannot pin a pid (the terminal service reparents
        # the command), so a same-uid loopback listener suffices there.
        assert verify_backend_port(own_port, None, True, "pin-test") == "127.0.0.1"
        print("PASS: the terminal path accepts any same-uid loopback listener")

        # Refuse an owned wildcard listener because it is reachable from the LAN.
        wild_child, wild_port = _spawn_listener("0.0.0.0")
        try:
            try:
                verify_backend_port(wild_port, wild_child, False, "pin-test")
            except RuntimeError as e:
                assert "non-loopback" in str(e), e
            else:
                raise AssertionError("a wildcard-only backend was accepted")
        finally:
            wild_child.stdin.close()
            wild_child.wait(timeout=10)
        print("PASS: a wildcard-only backend is refused")
    finally:
        own_listener.close()
        child.stdin.close()
        child.wait(timeout=10)

    check_register_backend_terminates_exposed_child()

    print("PASS: scenario_register_backend_pin")


if __name__ == "__main__":
    main()
