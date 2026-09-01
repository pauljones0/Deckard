"""Verify page saves snapshot concurrent mutations without changing live data."""

# Saves for one json_path serialize across Page objects, which two controllers
# showing one page hold separately.
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import threading
import time

from fixtures import FaultyFakeDeck, seed_page, start_watchdog

from src.backend.PageManagement import page_flush
from src.backend.PageManagement.Page import Page


class StubController:
    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None

    def serial_number(self) -> str:
        return self.deck.get_serial_number()


def make_action(sentinel) -> dict:
    return {"id": "com_example::Thing", "settings": {"a": 1}, "object": sentinel}


def main() -> int:
    start_watchdog(60, "page_save_mutation")
    fixtures._install_integration_globals()

    path = seed_page("SaveMutation")
    page = Page(json_path=path, deck_controller=StubController("save-mut-1"))

    # A large live dict widens the serialization window. Every action carries
    # a live, non-serializable "object", as it does at runtime.
    sentinel = object()
    page.dict.setdefault("keys", {})
    for i in range(1500):
        page.dict["keys"][f"{i}x0"] = {
            "states": {"0": {"actions": [make_action(sentinel)]}}
        }

    # A. Save under concurrent mutation.
    stop = threading.Event()

    def mutator():
        # Use batches so dict size differs from its iteration-start size for
        # most of each GIL slice.
        i = 0
        while not stop.is_set():
            batch = [f"mut-{i}-{j}x9" for j in range(25)]
            for key in batch:
                page.dict["keys"][key] = {"states": {"0": {"actions": [make_action(sentinel)]}}}
            for key in batch:
                del page.dict["keys"][key]
            i += 1

    t = threading.Thread(target=mutator, daemon=True)
    t.start()
    try:
        # Twenty rounds repeatedly expose a snapshot that walks the live dict
        # while keeping scenario time bounded.
        for i in range(20):
            try:
                # Flush after each save because serialization and its possible
                # RuntimeError occur in the flush.
                page.save()
                page_flush.get().flush_path(path)
            except RuntimeError as e:
                print(f"FAIL: save() raised under concurrent mutation on iteration {i}: {e}")
                return 1
    finally:
        stop.set()
        t.join(timeout=5)

    with open(path) as f:
        saved = json.load(f)  # a raise here means the file is not valid JSON
    if "keys" not in saved:
        print("FAIL: saved page lost its keys section")
        return 1

    # B. The live dict keeps its "object" entries.
    live_action = page.dict["keys"]["0x0"]["states"]["0"]["actions"][0]
    if "object" not in live_action:
        print("FAIL: save() stripped 'object' from the LIVE action dict (mutated original)")
        return 1
    if "object" in saved["keys"]["0x0"]["states"]["0"]["actions"][0]:
        print("FAIL: 'object' leaked into the serialized page")
        return 1

    # C. Same-path saves serialize across Page objects.
    shared_path = seed_page("SaveShared")
    page_a = Page(json_path=shared_path, deck_controller=StubController("save-shared-a"))
    page_b = Page(json_path=shared_path, deck_controller=StubController("save-shared-b"))

    events = []
    ev_lock = threading.Lock()
    in_critical = threading.Event()

    def instrument_snapshot(page_obj, name):
        # Hook the snapshot that every flush takes under the save lock; backup
        # creation is only once per file and cannot observe both sections.
        orig = page_obj.snapshot_for_save

        def probe():
            with ev_lock:
                events.append((name, "enter", time.monotonic()))
            in_critical.set()
            time.sleep(0.15)  # hold the critical section open
            snapshot = orig()
            with ev_lock:
                events.append((name, "exit", time.monotonic()))
            return snapshot

        page_obj.snapshot_for_save = probe

    instrument_snapshot(page_a, "a")
    instrument_snapshot(page_b, "b")

    def saver(page_obj, wait_for_first_save):
        # Mark the second save after the first enters its critical section;
        # otherwise one pending record coalesces both saves into one write.
        if wait_for_first_save and not in_critical.wait(timeout=10):
            raise AssertionError("the first save never reached its critical section")
        page_obj.save()
        page_flush.get().flush_path(shared_path)

    threads = [threading.Thread(target=saver, args=(p, wait))
               for p, wait in ((page_a, False), (page_b, True))]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)
        if th.is_alive():
            print("FAIL: concurrent same-path save hung")
            return 1

    # Critical sections may not overlap. Between one page's enter and exit
    # there must be no other page's enter.
    spans = {}
    for name, kind, ts in events:
        spans.setdefault(name, {})[kind] = ts
    page_a_span = spans.get("a", {})
    page_b_span = spans.get("b", {})
    if not all(k in page_a_span and k in page_b_span for k in ("enter", "exit")):
        print(f"FAIL: instrumentation incomplete: {events}")
        return 1
    overlap = (
        page_a_span["enter"] < page_b_span["exit"]
        and page_b_span["enter"] < page_a_span["exit"]
    )
    if overlap:
        print("FAIL: same-path saves from two Page objects ran concurrently "
              f"(a={page_a_span}, b={page_b_span})")
        return 1

    with open(shared_path) as f:
        json.load(f)

    print("PASS: save survives concurrent mutation, never mutates the live dict, "
          "and same-path saves serialize across Page objects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
