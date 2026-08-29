"""release_handle and open_handle cannot interleave to close a fresh handle.

release_handle once installed the shadow, stopped the reader, and closed the
device with only the close under the wrapper lock. A reopen could slip between
the reader stop and the close: it passed open_handle's reader-alive check
(the stop had just joined the reader), opened the device, and then the close
in release_handle closed the handle it had just opened. release_handle now
runs the whole transition under the lock, so the two serialize. This drives
the race with a device whose close blocks on a gate, and proves a concurrent
open cannot proceed while a release holds the lock.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import threading  # noqa: E402
import time  # noqa: E402

from fixtures import start_watchdog  # noqa: E402
from src.backend.DeckManagement.BetterDeck import BetterDeck  # noqa: E402


class GatedDevice:
    """A device whose close() blocks on a gate, so a leg can hold a release
    mid-transition and try to open concurrently. It has no reader thread, so
    the stop is just the flag writes."""

    def __init__(self):
        self.ops: list[str] = []
        self.open_count = 0
        self.close_count = 0
        self.run_read_thread = False
        self.reconnect_after_suspend = True
        self.read_thread = None
        self.close_gate = threading.Event()
        self.close_gate.set()  # open by default; a leg clears it to hold close

    def open(self, *args, **kwargs) -> None:
        self.ops.append("open")
        self.open_count += 1

    def close(self) -> None:
        self.close_gate.wait(timeout=10)
        self.ops.append("close")
        self.close_count += 1


def main() -> int:
    start_watchdog(30, "deck_release_reopen_race")
    failures: list[str] = []

    dev = GatedDevice()
    bd = BetterDeck(dev)
    # Start from an open handle, as a live deck has.
    bd.open_handle()
    if dev.open_count != 1:
        failures.append("the initial open did not run")

    # Hold the close inside release_handle, so the release is mid-transition
    # with the lock held.
    dev.close_gate.clear()
    release_done = threading.Event()

    def do_release():
        bd.release_handle()
        release_done.set()

    releaser = threading.Thread(target=do_release, name="releaser", daemon=True)
    releaser.start()

    # Let the releaser reach the blocked close while holding the lock.
    time.sleep(0.2)

    # A concurrent open must not proceed while the release holds the lock.
    open_result: list[bool] = []

    def do_open():
        open_result.append(bd.open_handle())

    opener = threading.Thread(target=do_open, name="opener", daemon=True)
    opener.start()
    opener.join(timeout=0.5)

    if not opener.is_alive():
        failures.append(
            "open_handle completed while a release held the lock -- the two "
            "interleaved instead of serializing")
    opens_during_release = dev.open_count
    if opens_during_release != 1:
        failures.append(
            f"the device was opened during a held release (open_count="
            f"{opens_during_release}); the release could then close a fresh handle")

    # Let the release finish; the opener then proceeds after it.
    dev.close_gate.set()
    assert release_done.wait(timeout=5), "the release never completed"
    opener.join(timeout=5)

    # The open ran after the release, and its handle stands: nothing closes it.
    if open_result != [True]:
        failures.append(f"the post-release open did not succeed: {open_result}")
    # Final op order: the open is last, so the handle the opener took is open.
    if dev.ops[-1] != "open":
        failures.append(f"a release closed the handle the reopen took: ops={dev.ops}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: release_handle and open_handle serialize; no reopen is closed "
          "by a release")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
