#!/usr/bin/env python3
"""Gated: a dead input reader is reopened on the real deck and the deck
recovers.

The real-transport half of the headless reader-reconnect scenario. The
library's reader thread is stopped through the same flags the release path
writes, the reader supervisor's sweep must reopen the real handle from the
media thread, the deck must fully repaint after the reopen, and the reopened
reader must hold long enough to settle the recovery count. One handle cycle,
no USB reset: the contract's one-deck-cycle rule is exactly this script's
budget.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hwlib  # noqa: E402


def main() -> int:
    env = hwlib.boot_engine("reader_recovery")
    controller = env["controller"]
    journal = env["journal"]
    failures: list[str] = []

    from src.backend.DeckManagement import reader_supervisor

    # Shrink the hold window so the settle leg finishes inside the gate.
    shipped_hold = reader_supervisor.HOLD_WINDOW_S
    reader_supervisor.HOLD_WINDOW_S = 1.0
    watchdog = reader_supervisor.DeckReaderWatchdog(env["gl"].deck_manager)
    try:
        if not hwlib.wait_until(
                lambda: all(journal.last_op_for(s) is not None for s in env["key_slots"]),
                timeout=30):
            failures.append("boot paint incomplete; recovery would prove nothing")

        raw = controller.deck.deck
        reader = getattr(raw, "read_thread", None)
        if reader is None or not reader.is_alive():
            failures.append("the real deck has no live reader thread to kill")
        else:
            # Stop the reader the way the release path does: both flags down,
            # then wait for the thread to exit. The handle stays open, which
            # is the shape a died-mid-read reader leaves.
            if hasattr(raw, "reconnect_after_suspend"):
                raw.reconnect_after_suspend = False
            raw.run_read_thread = False
            if not hwlib.wait_until(lambda: not reader.is_alive(), timeout=10):
                failures.append("the reader thread did not exit after its flags went down")

        if not failures:
            before_seq = journal.current_seq()
            # The supervisor detects the dead reader and reopens on the media
            # thread. Sweep until it reports the reopen.
            supervisor = watchdog.supervisor_for(controller)
            deadline = time.monotonic() + 30
            while supervisor.reopens == 0 and time.monotonic() < deadline:
                watchdog.sweep()
                time.sleep(0.2)
            if supervisor.reopens != 1:
                failures.append(f"the supervisor never reopened the reader "
                                f"(reopens={supervisor.reopens})")
            # A fresh reader thread is alive on the reopened handle.
            elif not hwlib.wait_until(
                    lambda: getattr(raw, "read_thread", None) is not None
                    and raw.read_thread.is_alive(), timeout=10):
                failures.append("no live reader thread after the reopen")

            # The reopen schedules a full repaint: every key lands again.
            if not failures and not hwlib.wait_until(
                    lambda: all(
                        (e := journal.last_op_for(s)) is not None and e[1] > before_seq
                        for s in env["key_slots"]),
                    timeout=30):
                failures.append("the deck did not fully repaint after the reopen")

            # The reopened reader holds, so the recovery count settles.
            if not failures:
                deadline = time.monotonic() + 15
                while supervisor.consecutive_attempts and time.monotonic() < deadline:
                    watchdog.sweep()
                    time.sleep(0.2)
                if supervisor.consecutive_attempts != 0:
                    failures.append(
                        f"the hold never settled (consecutive_attempts="
                        f"{supervisor.consecutive_attempts}); a later transient "
                        f"failure would push toward give-up")
                if supervisor.given_up:
                    failures.append("a recovered deck was given up")
    finally:
        reader_supervisor.HOLD_WINDOW_S = shipped_hold
        try:
            took = hwlib.shutdown_engine(env, timeout=15)
            print(f"  teardown took {took:.2f}s")
        except RuntimeError as e:
            failures.append(str(e))

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: a dead reader is reopened, the deck repaints, and the hold settles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
