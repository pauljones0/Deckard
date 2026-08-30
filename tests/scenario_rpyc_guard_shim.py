"""Rebind hostname-less rpyc servers to loopback through the injected guard.
Keep explicit bind arguments and add peer-UID authentication."""
import json
import os
import subprocess
import sys

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl

from src.backend.PluginManager.backend_guard import deckard_rpyc_guard

# Probes the three construction cases and reports facts, not verdicts, so
# the assertions live in one place below.
CHILD_SRC = """\
import json, socket, sys
import rpyc
from rpyc.utils.server import ThreadedServer


class Probe(rpyc.Service):
    pass


def run_authenticator(auth):
    # Drive the authenticator against a live same-uid loopback pair and
    # report what it returns, so a probe checks behavior and not a name.
    if auth is None:
        return {"present": False}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    client = socket.socket()
    client.connect(listener.getsockname())
    accepted, _ = listener.accept()
    try:
        result = auth(accepted)
        return {"present": True, "credentials": result[1]}
    finally:
        accepted.close()
        client.close()
        listener.close()


out = {"guard_imported": "deckard_rpyc_guard" in sys.modules}

s1 = ThreadedServer(Probe(), port=0, protocol_config={"allow_public_attrs": True})
out["default_bind"] = s1.listener.getsockname()[0]
out["default_auth"] = getattr(s1.authenticator, "__name__", None)
s1.close()

s2 = ThreadedServer(Probe(), hostname="0.0.0.0", port=0)
out["explicit_bind"] = s2.listener.getsockname()[0]
out["explicit_auth"] = getattr(s2.authenticator, "__name__", None)
s2.close()

s4 = ThreadedServer(Probe(), port=0, ipv6=True)
out["ipv6_bind"] = s4.listener.getsockname()[0]
out["ipv6_auth"] = getattr(s4.authenticator, "__name__", None)
s4.close()


def plugin_auth(sock):
    return sock, "plugin-credentials"


s3 = ThreadedServer(Probe(), port=0, authenticator=plugin_auth)
out["composed_auth"] = getattr(s3.authenticator, "__name__", None)
out["composed_result"] = run_authenticator(s3.authenticator)
s3.close()

print(json.dumps(out))
"""


def _run_probe(with_guard: bool) -> dict:
    script = os.path.join(gl.DATA_PATH, "guard_probe.py")
    with open(script, "w") as f:
        f.write(CHILD_SRC)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if with_guard:
        env["PYTHONPATH"] = os.path.dirname(os.path.abspath(deckard_rpyc_guard.__file__))
    output = subprocess.run([sys.executable, script], env=env, capture_output=True,
                            text=True, timeout=30, check=True)
    return json.loads(output.stdout)


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_rpyc_guard_shim")

    # The control run pins what the guard protects against: without it the
    # default construction binds the wildcard address, unauthenticated.
    control = _run_probe(with_guard=False)
    assert control["guard_imported"] is False, control
    assert control["default_bind"] == "0.0.0.0", control
    assert control["default_auth"] is None, control
    print("PASS: without the guard a hostname-less server binds 0.0.0.0")

    guarded = _run_probe(with_guard=True)
    assert guarded["guard_imported"] is True, "sitecustomize did not arm the guard"
    assert guarded["default_bind"] == "127.0.0.1", guarded
    assert guarded["default_auth"] == "loopback_uid_authenticator", guarded
    print("PASS: with the guard the same construction binds 127.0.0.1 with the peer-uid authenticator")

    # Keep explicit hostnames but add peer-UID authentication because an
    # explicit wildcard remains reachable from the LAN.
    assert guarded["explicit_bind"] == "0.0.0.0", guarded
    assert guarded["explicit_auth"] == "loopback_uid_authenticator", guarded
    print("PASS: an explicit hostname keeps its bind but still gets the peer-uid authenticator")

    # ipv6=True must not fall back to a wildcard bind: the rewrite picks the
    # loopback address of the matching family.
    assert guarded["ipv6_bind"] in ("::1", "0:0:0:0:0:0:0:1"), guarded
    assert guarded["ipv6_auth"] == "loopback_uid_authenticator", guarded
    print("PASS: an ipv6 server binds ::1 with the peer-uid authenticator, not a wildcard fallback")

    assert guarded["composed_auth"] == "composed_authenticator", guarded
    assert guarded["composed_result"] == {"present": True, "credentials": "plugin-credentials"}, guarded
    print("PASS: a plugin's own authenticator runs after the peer-uid check and its result is returned")

    print("PASS: scenario_rpyc_guard_shim")


if __name__ == "__main__":
    main()
