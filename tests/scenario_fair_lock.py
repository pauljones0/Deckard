"""Check FIFO transport-lock ordering, starvation bounds, and installation."""
import threading
import time

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from src.backend.DeckManagement.fair_lock import FairLock

WATCHDOG_SECONDS = 60


def _wait_for_queue_depth(lock: FairLock, depth: int) -> None:
    """Wait for tickets drawn under the condition, the exact queue edge.

    Polling worker state would race the ticket draw.
    """
    assert fixtures.wait_until(lambda: lock._next_ticket >= depth, timeout=10.0), (
        f"only {lock._next_ticket} of {depth} tickets were drawn"
    )


def check_fifo_service_order() -> None:
    lock = FairLock()
    served = []

    lock.acquire()  # Held, so every worker below is forced to queue.

    workers = []
    for index in range(8):
        def _worker(index=index):
            with lock:
                served.append(index)

        t = threading.Thread(target=_worker, name=f"fifo-{index}", daemon=True)
        t.start()
        workers.append(t)
        # Wait for each ticket before starting the next thread to fix arrival order.
        _wait_for_queue_depth(lock, index + 2)

    lock.release()
    for t in workers:
        t.join(timeout=10.0)
        assert not t.is_alive(), "a queued waiter never got served"

    assert served == list(range(8)), f"served out of arrival order: {served}"
    assert not lock.locked(), "lock still owned after every waiter released"
    print("PASS: service order equals arrival order")


class _TicketDrawProbe:
    """Capture the watched thread's first wait after its ticket draw."""

    def __init__(self, cond, sample):
        self._cond = cond
        self._sample = sample
        self.watch = None      # thread whose ticket draw to catch
        self.snapshot = None   # (sample, monotonic) taken at that draw

    def __enter__(self):
        return self._cond.__enter__()

    def __exit__(self, *exc_info):
        return self._cond.__exit__(*exc_info)

    def notify_all(self):
        return self._cond.notify_all()

    def wait(self, timeout=None):
        # Do not re-baseline the sample when the acquire loop rechecks.
        if self.snapshot is None and threading.current_thread() is self.watch:
            self.snapshot = (self._sample(), time.monotonic())
        return self._cond.wait(timeout)


def check_hot_loop_cannot_starve_waiter() -> None:
    """Check that a hot acquire loop overtakes one queued waiter at most once."""
    HOLD_S = 0.0005
    RUN_S = 2.0

    lock = FairLock()
    acquisitions = 0
    stop = threading.Event()

    probe = _TicketDrawProbe(lock._cond, lambda: acquisitions)
    probe.watch = threading.current_thread()
    lock._cond = probe

    def _hot():
        nonlocal acquisitions
        while not stop.is_set():
            with lock:
                acquisitions += 1
                end = time.monotonic() + HOLD_S
                while time.monotonic() < end:
                    pass

    hot = threading.Thread(target=_hot, name="fair-lock-hot", daemon=True)
    hot.start()

    worst_latency = 0.0
    worst_call_latency = 0.0
    worst_overtakes = 0
    samples = 0
    queued_samples = 0
    deadline = time.monotonic() + RUN_S
    while time.monotonic() < deadline:
        probe.snapshot = None
        called_at = time.monotonic()
        with lock:
            served_at = time.monotonic()
            served = acquisitions
            drawn = probe.snapshot
        samples += 1
        worst_call_latency = max(worst_call_latency, served_at - called_at)
        if drawn is not None:
            # Measure overtaking only when the caller queued behind the hot loop.
            queued_samples += 1
            worst_overtakes = max(worst_overtakes, served - drawn[0])
            worst_latency = max(worst_latency, served_at - drawn[1])
        time.sleep(0.05)  # The library's 20Hz read poll cadence.

    stop.set()
    hot.join(timeout=10.0)

    # Require actual queueing before applying the starvation bounds.
    assert queued_samples > 5, (
        f"the contention this check needs never happened: only "
        f"{queued_samples} of {samples} samples queued behind the hot loop, "
        f"so nothing here measured an ordering guarantee at all"
    )
    assert samples > 10, f"poller only got {samples} samples in {RUN_S}s"
    assert acquisitions > 100, (
        f"hot loop only managed {acquisitions} acquisitions -- FairLock "
        f"throughput collapsed"
    )
    # Only the acquisition already in flight when ticket T was drawn may land first.
    assert worst_overtakes <= 1, (
        f"hot loop overtook the queued waiter {worst_overtakes} times -- "
        f"ordering is not FIFO"
    )
    # The read poll needs one slot per 50 ms window.
    assert worst_latency < 0.05, (
        f"worst queued wait {worst_latency * 1000:.1f}ms exceeds one poll window"
    )
    # Include the scheduler-governed condition wait in a two-window bound.
    assert worst_call_latency < 0.10, (
        f"worst end-to-end acquire {worst_call_latency * 1000:.1f}ms exceeds "
        f"two poll windows -- a waiter is being starved before it can even "
        f"draw a ticket"
    )
    print(
        f"PASS: hot loop ({acquisitions} acquisitions) never starved the "
        f"poller ({queued_samples} queued samples, worst {worst_overtakes} "
        f"overtakes, {worst_latency * 1000:.2f}ms queued / "
        f"{worst_call_latency * 1000:.2f}ms end-to-end)"
    )


def check_exception_path_releases() -> None:
    lock = FairLock()

    class _Boom(Exception):
        pass

    try:
        with lock:
            raise _Boom()
    except _Boom:
        pass
    else:
        raise AssertionError("__exit__ swallowed the exception")

    assert not lock.locked(), "lock still owned after an exception in the body"
    assert lock.acquire(timeout=1.0), "lock unusable after an exception in the body"
    lock.release()
    print("PASS: context manager releases on the exception path")


def check_lock_protocol_semantics() -> None:
    lock = FairLock()

    assert lock.acquire(blocking=False), "non-blocking acquire failed on a free lock"
    assert lock.locked()
    assert not lock.acquire(blocking=False), "non-blocking acquire succeeded while owned"

    t0 = time.monotonic()
    assert not lock.acquire(timeout=0.05), "timed acquire succeeded while owned"
    assert time.monotonic() - t0 < 5.0, "timed acquire ignored its timeout"

    # The abandoned ticket must not stall the queue behind it.
    lock.release()
    assert not lock.locked(), "abandoned ticket left the lock owned"
    assert lock.acquire(timeout=1.0), "abandoned ticket wedged the queue"
    lock.release()

    try:
        lock.release()
    except RuntimeError:
        pass
    else:
        raise AssertionError("releasing an unowned FairLock must raise")

    print("PASS: non-blocking, timeout and release-unlocked semantics hold")


class _StubTransport:
    def __init__(self, mutex=None):
        if mutex is not None:
            self.mutex = mutex


class _StubDeck:
    def __init__(self, device=None):
        self.device = device


def check_install_happens_before_open() -> None:
    """Check that DeckController installs the lock before open starts its reader."""
    import globals as gl
    from faulty_fake_deck import FaultyFakeDeck

    # A deck with no transport at all, such as every FakeDeck, must construct.
    plain = fixtures.make_headless_controller(serial="fairlock-noop")
    fixtures.teardown(plain)

    class _ProbeDeck(FaultyFakeDeck):
        """A FakeDeck carrying a transport shaped like the real one."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.device = _StubTransport(threading.Lock())
            self.mutex_type_at_open = None

        def open(self, *args, **kwargs):
            self.mutex_type_at_open = type(self.device.mutex)
            return super().open(*args, **kwargs)

    from src.backend.DeckManagement.DeckController import DeckController

    probe = _ProbeDeck(serial_number="fairlock-probe", deck_type="Fake Deck")
    controller = DeckController(gl.deck_manager, probe)
    gl.deck_manager.deck_controller.append(controller)
    try:
        assert probe.mutex_type_at_open is FairLock, (
            f"the transport mutex was {probe.mutex_type_at_open} when open() "
            f"started the reader thread -- the install is missing or too late"
        )
        assert isinstance(probe.device.mutex, FairLock), "the swap did not stick"
    finally:
        fixtures.teardown(controller)

    print("PASS: DeckController installs the FIFO lock before opening the deck")


def check_install_guards() -> None:
    from src.backend.DeckManagement.DeckController import _install_fair_transport_lock
    from faulty_fake_deck import FaultyFakeDeck

    stock = threading.Lock()
    deck = _StubDeck(_StubTransport(stock))
    assert _install_fair_transport_lock(deck), "stock transport mutex was not swapped"
    installed = deck.device.mutex
    assert isinstance(installed, FairLock), f"mutex is {type(installed).__name__}"

    # A second pass must not replace the installed lock.
    assert _install_fair_transport_lock(deck)
    assert deck.device.mutex is installed, "install replaced an existing FairLock"

    # No transport device, such as FakeDeck, RemoteDeck or anything non-HID.
    assert not _install_fair_transport_lock(_StubDeck(None)), (
        "install claimed success on a deck with no transport"
    )
    assert not _install_fair_transport_lock(
        FaultyFakeDeck(serial_number="fairlock-fake", deck_type="Fake Deck")
    ), "install claimed success on a FakeDeck"

    # Library drift, a transport whose mutex attribute is gone.
    assert not _install_fair_transport_lock(_StubDeck(_StubTransport())), (
        "install claimed success on a transport with no mutex"
    )

    # A held mutex must never be swapped out from under its holder.
    held = threading.Lock()
    held.acquire()
    held_deck = _StubDeck(_StubTransport(held))
    assert not _install_fair_transport_lock(held_deck), "install swapped a held mutex"
    assert held_deck.device.mutex is held, "held mutex was replaced anyway"
    held.release()

    print("PASS: install guard swaps a stock mutex and no-ops on every other shape")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_fair_lock")

    check_fifo_service_order()
    check_hot_loop_cannot_starve_waiter()
    check_exception_path_releases()
    check_lock_protocol_semantics()
    check_install_happens_before_open()
    check_install_guards()

    print("PASS: scenario_fair_lock")


if __name__ == "__main__":
    main()
