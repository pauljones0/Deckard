"""A deferred page write that fails transiently is retried, not dropped.

The flush seam used to retire a pending edit whether or not the write landed,
so a transient filesystem error lost the edit unless the user typed again. Now
a transient failure keeps the edit pending and re-arms a retry, so a later
flush or the quit flush persists it, while a permanent serialization error is
retired rather than retried forever. This drives the seam with a manual
scheduler and a fake page source, and injects the write outcomes.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

from typing import Any  # noqa: E402

from src.backend.PageManagement import page_flush  # noqa: E402
from src.backend.PageManagement.page_flush import PageFlush  # noqa: E402
from fixtures import start_watchdog  # noqa: E402


class ManualScheduler:
    """Fires nothing on its own. The test decides when a timer runs."""

    def __init__(self):
        self.armed: list = []

    def schedule(self, delay_s, callback):
        handle = object()
        self.armed.append((handle, callback))
        return handle

    def cancel(self, handle):
        self.armed = [(h, c) for (h, c) in self.armed if h is not handle]

    def fire_all(self):
        due = self.armed
        self.armed = []
        for _handle, callback in due:
            callback()


class FakeSource:
    """A minimal PageContent the seam can flush."""

    def __init__(self, path: str):
        self.json_path = path
        self.backups = 0

    def get_without_action_objects(self) -> dict[str, Any]:
        return {"keys": {}}

    def move_key_to_end(self, dictionary: dict[str, Any], key: str) -> None:
        pass

    def make_backup(self, json_path: str) -> None:
        self.backups += 1


def main() -> int:
    start_watchdog(30, "page_flush_retry")

    path = "/tmp/deckard-scenario-retry/page.json"

    # A programmable stand-in for the atomic write. Each call pops the next
    # outcome: an exception type to raise, or None to succeed and record.
    outcomes: list = []
    writes: list[str] = []

    def fake_atomic_write_json(file_path, data, indent=4):
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        writes.append(file_path)

    real_write = page_flush.atomic_write_json
    page_flush.atomic_write_json = fake_atomic_write_json

    clock = [0.0]
    failures: list[str] = []
    try:
        # --- Transient failure retains and retries ------------------------
        sched = ManualScheduler()
        flush = PageFlush(scheduler=sched, clock=lambda: clock[0])
        src = FakeSource(path)
        flush.mark_dirty(src)

        # First write fails transiently; the edit must stay pending.
        outcomes[:] = [OSError("disk full")]
        flush.flush_path(path)
        if flush.pending_source(path) is not src:
            failures.append("a transient write failure dropped the pending edit")
        if writes:
            failures.append("a failed write was recorded as written")

        # The seam re-armed a retry. Fire it with a good outcome; it persists.
        outcomes[:] = [None]
        sched.fire_all()
        if flush.pending_source(path) is not None:
            failures.append("a successful retry did not retire the edit")
        if writes != [path]:
            failures.append(f"the retry did not write the page: {writes}")

        # --- Quit flush persists a still-pending edit ---------------------
        sched2 = ManualScheduler()
        flush2 = PageFlush(scheduler=sched2, clock=lambda: clock[0])
        src2 = FakeSource(path)
        flush2.mark_dirty(src2)
        outcomes[:] = [OSError("temporarily unavailable")]
        flush2.flush_path(path)  # transient fail, retained
        writes.clear()
        outcomes[:] = [None]
        flush2.flush_all()  # the quit path retries and succeeds
        if writes != [path]:
            failures.append(f"quit flush did not persist the retained edit: {writes}")
        if flush2.pending_source(path) is not None:
            failures.append("quit flush left the edit pending after a good write")

        # --- Permanent serialization failure is retired -------------------
        sched3 = ManualScheduler()
        flush3 = PageFlush(scheduler=sched3, clock=lambda: clock[0])
        src3 = FakeSource(path)
        flush3.mark_dirty(src3)
        outcomes[:] = [TypeError("not JSON serializable")]
        flush3.flush_path(path)
        if flush3.pending_source(path) is not None:
            failures.append("a permanent serialization failure was not retired")
    finally:
        page_flush.atomic_write_json = real_write

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: a transient write failure retries and persists; a permanent "
          "one is retired")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
