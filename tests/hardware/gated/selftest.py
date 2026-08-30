#!/usr/bin/env python3
# hw: no-deck-ok
"""Run deck-free self-tests for hardware parsing and orchestration.
The gated entry exercises discovery, process-group execution, and summary output."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.dirname(HERE)
sys.path.insert(0, HARNESS)


def main() -> int:
    proc = subprocess.run(
        [sys.executable, os.path.join(HARNESS, "hw_verify.py"), "--selftest"],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or "selftest OK" not in proc.stdout:
        print(f"FAIL: hw_verify --selftest rc={proc.returncode}\n{proc.stdout[-2000:]}"
              f"{proc.stderr[-2000:]}")
        return 1

    import orchestrator
    if not orchestrator.no_deck_ok(os.path.abspath(__file__)):
        print("FAIL: the no-deck marker scan does not recognize this script")
        return 1

    # hwlib's deck-free mechanics: the journal, quiet detection, and the
    # synthetic scratch builder.
    import json

    import hwlib

    j = hwlib.Journal()
    j.record("set_key_image", "key:0", b"pixels")
    j.record("set_key_image", "key:1", b"pixels")
    if j.count() != 2 or j.last_op_for("key:0") is None or j.after(1)[0][3] != "key:1":
        print("FAIL: journal mechanics are wrong")
        return 1
    if not hwlib.wait_quiet(j, quiet_for=0.1, timeout=2):
        print("FAIL: wait_quiet never settles on a quiet journal")
        return 1

    scratch = hwlib.build_scratch("selftest")
    page = os.path.join(scratch, "pages", "HWGate.json")
    with open(page) as f:
        keys = json.load(f)["keys"]
    if len(keys) != 32 or not all(
            os.path.isfile(k["states"]["0"]["media"]["path"]) for k in keys.values()):
        print("FAIL: the synthetic scratch page or its icons are incomplete")
        return 1
    hwlib.default_page_for(scratch, "SELFTEST")
    with open(os.path.join(scratch, "settings", "pages.json")) as f:
        if json.load(f)["default-pages"]["SELFTEST"] != page:
            print("FAIL: default_page_for did not point at the synthetic page")
            return 1

    # Verify boot refusal against a live instance; skip when none exists
    import hw_verify
    if hw_verify.dbus_owner_pid() is not None:
        try:
            hwlib.boot_engine("selftest_guard")
        except RuntimeError as e:
            if "orchestrator.py" not in str(e):
                print(f"FAIL: the boot refusal has the wrong shape: {e}")
                return 1
        else:
            print("FAIL: boot_engine did not refuse while the app instance runs")
            return 1
        print("  boot guard verified against the live instance")
    else:
        print("  boot guard leg skipped: no running instance to refuse against")

    print("PASS: harness selftest (hw_verify evaluators, orchestrator contract, "
          "hwlib mechanics)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
