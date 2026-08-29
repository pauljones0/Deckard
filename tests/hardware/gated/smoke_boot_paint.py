#!/usr/bin/env python3
"""Gated smoke: the engine boots on the real deck, paints every key, and
settles.

The foundational hardware gate. It proves what no fake deck can: real
transport writes land for every key of the synthetic page, the media loop
goes quiet afterwards instead of repainting a static page, and a clean
close through the production quit path releases the handle inside its bound.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hwlib  # noqa: E402


def main() -> int:
    env = hwlib.boot_engine("smoke_boot_paint")
    journal = env["journal"]
    failures: list[str] = []

    # Every key of the synthetic page reaches the device.
    began = time.monotonic()
    painted = hwlib.wait_until(
        lambda: all(journal.last_op_for(slot) is not None for slot in env["key_slots"]),
        timeout=30)
    boot_paint_s = time.monotonic() - began
    if not painted:
        missing = [s for s in env["key_slots"] if journal.last_op_for(s) is None]
        failures.append(f"boot paint incomplete after 30s; unpainted: {missing}")
    else:
        print(f"  boot paint complete: {env['key_count']} keys in {boot_paint_s:.2f}s")

    # A static page settles: no write for a full second inside the window.
    if not hwlib.wait_quiet(journal, quiet_for=1.0, timeout=30):
        failures.append("the media loop never went quiet on a static page")
    quiet_seq = journal.current_seq()

    # It stays quiet: a static page must not repaint on its own.
    time.sleep(3)
    stray = journal.after(quiet_seq)
    if stray:
        failures.append(f"{len(stray)} writes landed on a quiet static page: {stray[:5]}")

    # Clean close through the production path, inside the bound, handle
    # released.
    try:
        took = hwlib.shutdown_engine(env, timeout=15)
        print(f"  teardown took {took:.2f}s")
    except RuntimeError as e:
        failures.append(str(e))

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: boot paint, quiet settle, and bounded release on the real deck")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
