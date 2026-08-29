#!/usr/bin/env python3
# hw: no-deck-ok
"""The harness's own self-test, as a gated entry.

Runs hw_verify's parser/evaluator self-test and checks the orchestrator's
script contract mechanics (the no-deck marker scan). It needs no deck, so it
is the entry a deckless machine or CI can run to prove the pipeline:
discovery, the process-group runner, and the summary all execute for real.
"""
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

    print("PASS: harness selftest (hw_verify evaluators + orchestrator contract)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
