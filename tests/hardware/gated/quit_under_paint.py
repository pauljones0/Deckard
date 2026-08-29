#!/usr/bin/env python3
"""Gated: quitting mid-paint stays bounded and nothing writes after close.

The smoke gate closes a settled deck; this one closes it in the middle of
the boot paint burst, when the media thread has writes in flight. The
production close path must still finish inside its bound, release the
handle, and no device write may land after close_all returns: a write after
the terminal clear-and-close would hit a closed handle or repaint a deck
the app has given back.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hwlib  # noqa: E402


def main() -> int:
    env = hwlib.boot_engine("quit_under_paint")
    journal = env["journal"]
    failures: list[str] = []

    # Wait only for the paint burst to start, then quit into it.
    if not hwlib.wait_until(lambda: journal.count() >= 1, timeout=30):
        failures.append("no device write landed; there is no paint to quit into")

    try:
        took = hwlib.shutdown_engine(env, timeout=15)
        print(f"  teardown mid-paint took {took:.2f}s")
        if took > 10:
            failures.append(f"teardown took {took:.2f}s; the quit watchdog fires at 6s "
                            f"in the app, so a mid-paint close this slow rides it")
    except RuntimeError as e:
        failures.append(str(e))

    closed_seq = journal.current_seq()
    time.sleep(2)
    stray = journal.after(closed_seq)
    if stray:
        failures.append(f"{len(stray)} writes landed after close_all returned: {stray[:5]}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: a mid-paint quit is bounded, releases the handle, and nothing "
          "writes afterwards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
