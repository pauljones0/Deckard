"""Check idempotent loopback-guard injection through .pth and PYTHONPATH."""
import os
import subprocess
import sys

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl

import types

# PluginManager pulls PluginBase, whose module import reads these registries.
gl.plugin_manager = types.SimpleNamespace(backends=[], backend_processes=[])

from src.backend.PluginManager.PluginManager import backend_guard_env, inject_backend_guard  # noqa: E402
from src.backend.PluginManager.backend_guard import deckard_rpyc_guard  # noqa: E402

GUARD_SOURCE = os.path.abspath(deckard_rpyc_guard.__file__)


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def check_injection_into_skeleton() -> None:
    venv_path = os.path.join(gl.DATA_PATH, "skeleton venv")
    site_dir = os.path.join(venv_path, "lib", "python3.13", "site-packages")
    os.makedirs(site_dir)

    inject_backend_guard(venv_path)
    module_path = os.path.join(site_dir, "deckard_rpyc_guard.py")
    pth_path = os.path.join(site_dir, "deckard_rpyc_guard.pth")
    assert _read(module_path) == _read(GUARD_SOURCE)
    assert _read(pth_path) == b"import deckard_rpyc_guard\n"
    print("PASS: injection writes the guard module and the .pth line")

    # A second run rewrites nothing: a matching copy keeps its mtime.
    before = (os.stat(module_path).st_mtime_ns, os.stat(pth_path).st_mtime_ns)
    inject_backend_guard(venv_path)
    after = (os.stat(module_path).st_mtime_ns, os.stat(pth_path).st_mtime_ns)
    assert before == after, "a matching guard copy was rewritten"
    print("PASS: a second injection is a no-op")

    # A stale copy, such as one from an older app version, is refreshed.
    with open(module_path, "wb") as f:
        f.write(b"# stale guard from an older install\n")
    inject_backend_guard(venv_path)
    assert _read(module_path) == _read(GUARD_SOURCE)
    print("PASS: a stale guard copy is refreshed")

    # A venv without site-packages logs and returns; the launch path above it
    # must not die on this.
    inject_backend_guard(os.path.join(gl.DATA_PATH, "not a venv"))
    print("PASS: a venv without site-packages does not raise")


def check_pth_vector_in_real_venv() -> None:
    """Check startup import through .pth in a real venv built without pip."""
    venv_path = os.path.join(gl.DATA_PATH, "real-venv")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv_path],
                   check=True, timeout=60)
    inject_backend_guard(venv_path)

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    output = subprocess.run(
        [os.path.join(venv_path, "bin", "python"), "-c",
         "import sys; print('deckard_rpyc_guard' in sys.modules)"],
        env=env, capture_output=True, text=True, timeout=30, check=True)
    assert output.stdout.strip() == "True", (output.stdout, output.stderr)
    print("PASS: the .pth line arms the guard in a real venv with no environment help")


def check_guard_env() -> None:
    guard_dir = os.path.dirname(GUARD_SOURCE)

    os.environ.pop("PYTHONPATH", None)
    env = backend_guard_env()
    assert env["PYTHONPATH"] == guard_dir, env["PYTHONPATH"]

    os.environ["PYTHONPATH"] = "/existing/entry"
    try:
        env = backend_guard_env()
        assert env["PYTHONPATH"] == guard_dir + os.pathsep + "/existing/entry", env["PYTHONPATH"]
    finally:
        os.environ.pop("PYTHONPATH", None)
    print("PASS: backend_guard_env prepends the guard dir and keeps existing entries")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_guard_injection")
    check_injection_into_skeleton()
    check_pth_vector_in_real_venv()
    check_guard_env()
    print("PASS: scenario_guard_injection")


if __name__ == "__main__":
    main()
