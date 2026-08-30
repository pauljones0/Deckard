"""Reopen a connected deck after its input reader dies with its handle open or closed.
Run attempt-count policy checks at shipped constants and restore changed time bounds."""
import threading
import time

import fixtures  # must be first; isolates DATA_PATH before import globals
import globals as gl

from loguru import logger as log

from StreamDeck.Transport.Transport import TransportError

from faulty_fake_deck import FaultyFakeDeck

from src.backend.DeckManagement import reader_supervisor
from src.backend.DeckManagement.DeckController import DeckController
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.fair_lock import FairLock
from src.backend.DeckManagement.reader_supervisor import DeckReaderWatchdog


class _FakeTransport:
    """Provide the transport mutex needed to verify that a reopen keeps its FIFO lock."""

    def __init__(self) -> None:
        self.mutex = threading.Lock()


class SupervisedDeck(FaultyFakeDeck):
    """Model handle-open, handle-closed, and live resume-loop reader states.
    Journal release order and provide blocked-open, one-query bus-drop, and short-life policies."""

    def __init__(self, *args, reader_life_s=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.device = _FakeTransport()
        self.run_read_thread = False
        self.reconnect_after_suspend = True
        self.read_thread = None
        # (run_read_thread, reconnect_after_suspend, thread) per close(), as
        # close saw them. A release must leave both flags down.
        self.flags_at_close: list[tuple] = []
        self.block_open = threading.Event()
        self.drop_bus_on_open_failure = False
        self.in_resume_loop = threading.Event()
        self.resume_attempts = 0
        self.reader_starts = 0
        self.reader_life_s = reader_life_s
        self._bus_dropped = False
        self._die = None
        # Count all write attempts because failures do not reach the operation journal.
        self.write_attempts = 0

    def _do_write(self, op: str, slot, data) -> None:
        self.write_attempts += 1
        super()._do_write(op, slot, data)

    # Reader deaths
    def die_open(self) -> None:
        self._die = "open"

    def die_closed(self) -> None:
        self._die = "closed"

    def park_in_resume_loop(self) -> None:
        self._die = "resume"

    def open(self, *args, **kwargs):
        if self.block_open.is_set():
            if self.drop_bus_on_open_failure:
                self._bus_dropped = True
            raise TransportError("SupervisedDeck: the device is not ready")
        super().open(*args, **kwargs)
        # Journal the open, so a leg can tell a reopen from the first open.
        self._record("open", "device", None)
        if self.read_thread is not None and self.read_thread.is_alive():
            return
        # A fresh reader starts healthy. The library builds a new thread here
        # too, so whatever killed the last one is not carried over.
        self._die = None
        self.reconnect_after_suspend = True
        self.run_read_thread = True
        self.reader_starts += 1
        self.read_thread = threading.Thread(
            target=self._read_loop, name=f"FakeReader-{id(self):x}", daemon=True)
        self.read_thread.start()

    def connected(self):
        if self._bus_dropped:
            # One answer per drop, so the next sweep finds the device back.
            self._bus_dropped = False
            return False
        return super().connected()

    def close(self):
        self.flags_at_close.append((self.run_read_thread, self.reconnect_after_suspend,
                                    threading.current_thread().name))
        super().close()

    def _read_loop(self) -> None:
        end_of_life = (None if self.reader_life_s is None
                       else time.monotonic() + self.reader_life_s)
        while self.run_read_thread:
            if end_of_life is not None and time.monotonic() >= end_of_life:
                # A reader that does not survive its hold window.
                self._record("read_died_open", "device", None)
                return
            die = self._die
            if die == "open":
                self._record("read_died_open", "device", None)
                return
            if die == "closed":
                self.run_read_thread = False
                self.close()
                self._record("read_died_closed", "device", None)
                return
            if die == "resume":
                self.run_read_thread = False
                self.close()
                self.in_resume_loop.set()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    self.resume_attempts += 1
                    try:
                        # Clear this reader handle before open() so the reopen starts
                        # a new reader instead of finding the current thread alive.
                        self.read_thread = None
                        self.open()
                        break
                    except Exception:
                        self.read_thread = threading.current_thread()
                        time.sleep(0.02)
                self._record("resume_loop_exit", "device", None)
                return
            time.sleep(0.002)


def make_controller(serial: str, **deck_kwargs):
    """A real DeckController over a SupervisedDeck, the integration tier."""
    fixtures.seed_page("Main")
    deck = SupervisedDeck(serial_number=serial, deck_type="Fake Deck", **deck_kwargs)
    controller = DeckController(gl.deck_manager, deck)
    gl.deck_manager.deck_controller.append(controller)
    return controller, deck


def app_closes(deck: SupervisedDeck) -> list:
    """Return close flags recorded outside the modeled reader thread."""
    return [flags for flags in deck.flags_at_close
            if not flags[2].startswith("FakeReader-")]


def kill_the_reader(deck: SupervisedDeck, mode: str, label: str):
    """End and return the reader; open keeps its handle and closed releases it."""
    if mode == "open":
        deck.die_open()
    else:
        deck.die_closed()
    return wait_for_a_dead_reader(deck, label)


def wait_for_a_dead_reader(deck: SupervisedDeck, label: str):
    """Wait until the modeled reader thread has exited, and hand it back."""
    reader = deck.read_thread
    assert reader is not None, f"{label}: the deck never started a reader"
    assert fixtures.wait_until(lambda: not reader.is_alive(), timeout=10), (
        f"{label}: the modeled reader never exited, so this leg covers nothing")
    return reader


def boot_paint(deck: SupervisedDeck, label: str) -> None:
    assert fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=10), (
        f"{label}: the boot paint never landed, so this leg would measure a bare deck")


def sweep_until(watchdog, supervisor, predicate, rounds: int = 40) -> None:
    """Sweep, wait out the attempt it may start, and repeat until predicate
    holds or the rounds run out."""
    for _ in range(rounds):
        if predicate():
            return
        watchdog.sweep()
        fixtures.wait_until(lambda: not supervisor.attempt_in_flight(), timeout=30)
        # A reopen that succeeds leaves a reader the modeled deck may end a
        # moment later, and the next round needs to see that.
        time.sleep(0.05)


def test_dead_reader_is_reopened() -> None:
    """The watchdog detects, and the media thread reopens through the seam."""
    controller, deck = make_controller("reader-reopen")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    try:
        boot_paint(deck, "reopen")
        # Wait for a protocol paint because the constructor's direct clear records
        # no present state and would make later state checks vacuous.
        assert fixtures.wait_until(
            lambda: any(k.present_state.last_presented_hash is not None
                        for k in controller.inputs.get(Input.Key, [])), timeout=10), (
            "no key recorded a presented image, so this leg would measure nothing")
        keys = controller.inputs.get(Input.Key, [])
        # The input list is not indexed by key index, so name the key the
        # modeled press lands on.
        key0 = next(k for k in keys if k.index == 0)
        mutex_before = deck.device.mutex
        assert isinstance(mutex_before, FairLock), (
            "the fair transport lock was never installed, so this leg cannot say "
            "whether a reopen keeps it")

        # A gesture in flight when the reader dies never sees its release.
        deck.fire_key_event(0, True)
        assert fixtures.wait_until(lambda: key0.down_start_time is not None, timeout=10), (
            "the modeled key press started no gesture, so this leg would measure nothing")

        first_reader = kill_the_reader(deck, "open", "reopen")
        assert deck.is_open(), "this leg models the death that leaves the handle open"
        assert deck.connected(), "the device must still be on the bus for this leg"
        deck.clear_journal()
        deck.flags_at_close.clear()
        # Hold the repaint in its rate-limit window to inspect the reopened
        # present state before the repaint clears it again.
        controller._last_full_repaint_ts = time.time()

        watchdog.sweep()
        supervisor = watchdog.supervisor_for(controller)
        assert fixtures.wait_until(lambda: supervisor.reopens == 1, timeout=30), (
            f"the deck was never reopened: attempts={supervisor.attempts_started}, "
            f"given_up={supervisor.given_up}")

        assert deck.read_thread is not first_reader and deck.read_thread.is_alive(), (
            "the reopen left no live reader, so the deck still takes no input")
        assert deck.is_open(), "the handle is closed after a successful reopen"

        releases = app_closes(deck)
        assert releases, (
            "the reopen closed nothing. A reopen without a close is not a "
            "close-before-retry, and the library's reader can take the handle back.")
        run_flag, resume_flag, thread_name = releases[-1]
        assert run_flag is False and resume_flag is False, (
            f"the reopen closed with (run_read_thread, reconnect_after_suspend) = "
            f"{(run_flag, resume_flag)}. Both go down before the close, or the "
            f"library re-opens the handle the release just gave back.")
        assert thread_name.startswith("MediaPlayerThread"), (
            f"the release ran on {thread_name}. Only the sole device writer may "
            f"close and reopen a handle.")

        journal = deck.journal()
        closes = [e for e in journal if e[2] == "close"]
        opens = [e for e in journal if e[2] == "open"]
        assert closes and opens, f"no close-then-open pair in the journal: {journal}"
        assert closes[0][1] < opens[0][1], (
            f"the reopen at seq {opens[0][1]} preceded the release at {closes[0][1]}")

        assert deck.device.mutex is mutex_before, (
            "the reopen replaced the transport mutex. The FIFO lock is installed once, "
            "before the first open, and a deck that loses it starves its HID read poll "
            "under a write burst.")
        assert key0.down_start_time is None, (
            "the reopen left a gesture in flight. Its first callback then dispatches a "
            "hold stop or an up into the snapshot taken before the outage.")

        assert all(k.present_state.last_presented_hash is None
                   and k.present_state.last_enqueued_hash is None for k in keys), (
            "the reopen left the keys naming what the device showed before it went "
            "deaf. A paint offered before the repaint fires matches those hashes and "
            "is skipped, so the deck keeps the stale imagery.")
        assert controller._full_repaint_pending, (
            "the reopen armed no full repaint, so a static page never repaints")
        assert supervisor.consecutive_attempts == 1, (
            "the successful reopen cleared the attempt count before its reader held. "
            "A deck that reopens and dies again forever would then never be given up.")

        # Unchanged content writes only if the reopen reset present-state hashes.
        reopen_seq = opens[0][1]
        assert fixtures.wait_until(
            lambda: any(e[2] == "set_key_image" and e[1] > reopen_seq
                        for e in deck.journal()), timeout=30), (
            "nothing repainted after the reopen. The device keeps whatever it "
            "showed when it went deaf unless the present state is reset, a full "
            "repaint is scheduled and the writer takes device writes again.")
    finally:
        fixtures.teardown(controller)
    print("PASS: a reader that died under a live device is reopened on the media thread")


def test_a_reader_that_closed_the_handle_is_reopened() -> None:
    """The other death: the resume loop ran out and left the handle closed."""
    controller, deck = make_controller("reader-reopen-closed")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    try:
        boot_paint(deck, "reopen-closed")
        kill_the_reader(deck, "closed", "reopen-closed")
        assert not deck.is_open(), "this leg models the death that closes the handle"
        assert deck.connected(), "the device must still be on the bus for this leg"

        watchdog.sweep()
        supervisor = watchdog.supervisor_for(controller)
        assert fixtures.wait_until(lambda: supervisor.reopens == 1, timeout=30), (
            f"a deck with a closed handle and a dead reader was not reopened: "
            f"attempts={supervisor.attempts_started}, given_up={supervisor.given_up}")
        assert deck.is_open() and deck.read_thread.is_alive(), (
            "the reopen left the deck without an open handle and a live reader")
    finally:
        fixtures.teardown(controller)
    print("PASS: a reader that gave the handle back is reopened too")


def test_a_reader_in_the_resume_loop_is_left_alone() -> None:
    """Leave a live reader in the library reopen arm alone.
    Concurrent recovery would close and open the same handle without a stop flag."""
    controller, deck = make_controller("reader-resume")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    try:
        boot_paint(deck, "resume")
        deck.block_open.set()
        deck.park_in_resume_loop()
        assert deck.in_resume_loop.wait(timeout=10), (
            "the modeled reader never entered the reopen loop")
        parked = deck.read_thread
        deck.flags_at_close.clear()

        for _ in range(5):
            watchdog.sweep()
            time.sleep(0.05)

        supervisor = watchdog.supervisor_for(controller)
        assert supervisor.attempts_started == 0, (
            f"the supervisor started {supervisor.attempts_started} attempt(s) against a "
            f"reader that is alive inside the library's own resume loop")
        assert parked.is_alive(), (
            "the modeled resume loop exited early, so this leg proves nothing")
        assert not app_closes(deck), (
            f"the app closed the handle under a live reader: {deck.flags_at_close}")

        # The library's own recovery still wins, with nothing done to it.
        deck.block_open.clear()
        assert fixtures.wait_until(
            lambda: (deck.read_thread is not None and deck.read_thread is not parked
                     and deck.read_thread.is_alive()), timeout=10), (
            "the modeled resume loop never got its reader back")
        assert deck.resume_attempts >= 1, (
            "the modeled reader never tried a reopen, so this leg proves nothing")
        assert supervisor.attempts_started == 0, (
            "the supervisor attacked the reader while the library recovered it")

        # Reject open_handle() while the reader runs because library open()
        # joins that reader without a bound or a stop mechanism.
        deck.block_open.set()
        deck.in_resume_loop.clear()
        deck.park_in_resume_loop()
        assert deck.in_resume_loop.wait(timeout=10), (
            "the modeled reader never re-entered the reopen loop")
        assert controller.deck.open_handle() is False, (
            "the wrapper opened a handle whose reader thread is still running")
        deck.block_open.clear()  # let the modeled reader out again
    finally:
        fixtures.teardown(controller)
    print("PASS: a reader inside the library's resume loop is not treated as dead")


def test_a_closing_or_quitting_app_reopens_nothing() -> None:
    """Do not reopen a handle owned by teardown or quit.
    Check closing during the sweep and recheck quit on the media thread."""
    controller, deck = make_controller("reader-teardown")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    try:
        boot_paint(deck, "teardown")
        kill_the_reader(deck, "open", "teardown")
        deck.flags_at_close.clear()

        controller._closing = True
        watchdog.sweep()
        supervisor = watchdog.supervisor_for(controller)
        assert supervisor.attempts_started == 0, (
            "a controller in teardown was handed a reopen, which races the teardown "
            "for the handle")
        controller._closing = False

        gl.threads_running = False
        try:
            watchdog.sweep()
            assert fixtures.wait_until(
                lambda: not supervisor.attempt_in_flight(), timeout=30), (
                "the attempt never finished")
            assert supervisor.reopens == 0, "the deck was reopened during a quit"
            assert not app_closes(deck), (
                f"a quitting app released the handle for a reopen: {deck.flags_at_close}")
            assert deck.is_open(), "the handle was closed during a quit"
        finally:
            gl.threads_running = True

        # The same deck reopens once neither guard holds, so the two asserts
        # above measure the guards and not a deck that could never come back.
        watchdog.sweep()
        assert fixtures.wait_until(lambda: supervisor.reopens == 1, timeout=30), (
            f"the deck never reopened after the guards cleared: "
            f"attempts={supervisor.attempts_started}, given_up={supervisor.given_up}")
    finally:
        fixtures.teardown(controller)
    print("PASS: neither a closing controller nor a quitting app is reopened")


def test_a_teardown_that_starts_mid_attempt_wins() -> None:
    """Let close() win after a reopen attempt enters its retry loop.
    Recheck teardown under the device lock immediately before open()."""
    # Shorten and restore the deadline to bound the retry loop around teardown.
    shipped_deadline = reader_supervisor.REOPEN_DEADLINE_S
    shipped_gap = reader_supervisor.REOPEN_RETRY_GAP_S
    reader_supervisor.REOPEN_DEADLINE_S = 5.0
    reader_supervisor.REOPEN_RETRY_GAP_S = 0.05
    controller, deck = make_controller("reader-mid-teardown")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    try:
        boot_paint(deck, "mid-teardown")
        deck.block_open.set()  # the attempt stays in its retry loop
        kill_the_reader(deck, "open", "mid-teardown")

        watchdog.sweep()
        supervisor = watchdog.supervisor_for(controller)
        # A closed handle proves the in-flight attempt reached its retry loop;
        # the marker alone can precede execution by the writer.
        assert fixtures.wait_until(lambda: not deck.is_open(), timeout=10), (
            "the attempt never reached its retry loop, so this leg would interrupt "
            "nothing")
        assert supervisor.attempt_in_flight(), (
            "the attempt ended before the teardown could land inside it")
        deck.clear_journal()
        # The device answers again from here on, so nothing but the teardown
        # check keeps the attempt from taking the handle back.
        deck.block_open.clear()

        # Hold the device lock after the retry-loop check and set closing before
        # open(), which isolates the final under-lock teardown check.
        with controller.deck._lock:
            time.sleep(0.2)
            controller._closing = True

        started = time.monotonic()
        assert fixtures.wait_until(
            lambda: not supervisor.attempt_in_flight(), timeout=10), (
            "the attempt never ended")
        assert time.monotonic() - started < reader_supervisor.REOPEN_DEADLINE_S, (
            "the attempt ran to its deadline instead of ending at the teardown")
        assert supervisor.reopens == 0, "a controller in teardown was reopened"
        assert not [e for e in deck.journal() if e[2] == "open"], (
            f"the handle was opened after the teardown began: {deck.journal()}. That "
            f"lifts the release shadow the teardown installed and hands the next "
            f"process a busy device.")
        assert not deck.is_open(), "the teardown's handle came back open"
    finally:
        controller._closing = False
        reader_supervisor.REOPEN_DEADLINE_S = shipped_deadline
        reader_supervisor.REOPEN_RETRY_GAP_S = shipped_gap
        fixtures.teardown(controller)
    print("PASS: a teardown that starts mid-attempt keeps the handle")


def test_the_attempt_cap_gives_a_flapping_deck_up() -> None:
    """Cap a flapping device at the shipped attempt count.
    Model it as present during the sweep and absent during the attempt."""
    controller, deck = make_controller("reader-flapping")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    records: list[str] = []
    sink_id = log.add(lambda msg: records.append(str(msg)), level="WARNING")
    escalated: list = []
    reader_supervisor.set_give_up_escalation(escalated.append)
    shipped_log_gap = reader_supervisor.GIVE_UP_LOG_GAP_S
    try:
        boot_paint(deck, "flapping")
        deck.block_open.set()
        deck.drop_bus_on_open_failure = True
        kill_the_reader(deck, "open", "flapping")

        supervisor = watchdog.supervisor_for(controller)
        sweep_until(watchdog, supervisor, lambda: supervisor.given_up)

        assert supervisor.given_up, (
            f"a deck whose handle never opens again was retried "
            f"{supervisor.attempts_started} times without a give-up")
        assert supervisor.attempts_started == reader_supervisor.MAX_CONSECUTIVE_ATTEMPTS, (
            f"the cap allowed {supervisor.attempts_started} attempts, not "
            f"{reader_supervisor.MAX_CONSECUTIVE_ATTEMPTS}")
        assert supervisor.reopens == 0, "a blocked device reported a successful reopen"
        assert escalated == [controller], (
            f"the give-up escalation hook received {escalated}, not the controller "
            f"once. A later recovery step layers on that hook.")

        # A given-up deck is left alone: no attempts, and no device writes.
        assert controller.media_player.device_writes_suspended, (
            "the writer still writes to a deck with no handle. Every write raises and "
            "arms another full repaint, which composites the deck and logs an error row "
            "every two seconds for the rest of the session.")
        for _ in range(10):
            watchdog.sweep()
        assert supervisor.attempts_started == reader_supervisor.MAX_CONSECUTIVE_ATTEMPTS, (
            "a given-up deck was retried again")
        # Keep a producer active because direct producers can storm a closed handle.
        # Full repaints check the deck first and are not the write source here.
        quiet_from = len(deck.journal())
        attempts_from = deck.write_attempts
        native = fixtures.make_native_image()
        for _ in range(5):
            controller.media_player.add_image_task(
                0, native, page=controller.active_page)
            time.sleep(0.1)
        # An absence needs a window, and this one covers the writer's two-second
        # pending-repaint retry with room to spare.
        time.sleep(2.5)
        assert deck.write_attempts == attempts_from, (
            f"the writer attempted {deck.write_attempts - attempts_from} device writes "
            f"against a given-up deck. Each one raises, arms another full repaint and "
            f"logs, which composites the deck and storms the log every two seconds for "
            f"the rest of the session.")
        assert len(deck.journal()) == quiet_from, (
            f"a given-up deck received {len(deck.journal()) - quiet_from} device "
            f"operations: {deck.journal()[quiet_from:]}")
        assert not controller._full_repaint_pending, (
            "the repaint kept re-arming itself against a deck with no handle, which is "
            "the two-second composite-and-fail loop this suspension exists to stop")

        give_ups = [r for r in records if "left alone" in r]
        assert len(give_ups) == 1, (
            f"the give-up was logged {len(give_ups)} times; it belongs in the log once")
        still_down = [r for r in records if "still down" in r]
        assert not still_down, (
            f"the still-down line repeated {len(still_down)} times inside its rate-limit "
            f"window, which storms the log once per sweep for the rest of the session")

        # The rate limit defers the line; it does not drop it forever.
        reader_supervisor.GIVE_UP_LOG_GAP_S = 0.0
        watchdog.sweep()
        assert [r for r in records if "still down" in r], (
            "a deck that stays down never says so again, whatever the rate limit")
    finally:
        reader_supervisor.GIVE_UP_LOG_GAP_S = shipped_log_gap
        reader_supervisor.set_give_up_escalation(None)
        log.remove(sink_id)
        fixtures.teardown(controller)
    print("PASS: the attempt cap stops a flapping device and leaves it alone")


def test_a_reopen_that_never_holds_is_capped() -> None:
    """Cap repeated successful reopens whose readers never hold.
    Run at shipped constants so transient success cannot reset the policy."""
    controller, deck = make_controller("reader-never-holds", reader_life_s=0.15)
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    try:
        boot_paint(deck, "never-holds")
        wait_for_a_dead_reader(deck, "never-holds")

        supervisor = watchdog.supervisor_for(controller)
        sweep_until(watchdog, supervisor, lambda: supervisor.given_up)

        assert supervisor.given_up, (
            f"a deck that loses its reader again after every reopen was reopened "
            f"{supervisor.reopens} times and never given up")
        assert supervisor.attempts_started == reader_supervisor.MAX_CONSECUTIVE_ATTEMPTS, (
            f"the cap allowed {supervisor.attempts_started} attempts, not "
            f"{reader_supervisor.MAX_CONSECUTIVE_ATTEMPTS}")
        assert supervisor.holds == 0, (
            "a reader that lived a fraction of the hold window counted as held")
        assert supervisor.reopens >= 1, (
            "no reopen ever succeeded, so this leg measures the same thing as the "
            "blocked-device leg instead of the flap")
    finally:
        fixtures.teardown(controller)
    print("PASS: reopens that never hold still reach the cap")


def test_a_message_the_writer_refuses_counts_no_attempt() -> None:
    """Do not count a reopen message that the writer refuses.
    A refused message must not set the give-up count or in-flight marker."""
    controller, deck = make_controller("reader-refused")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    try:
        boot_paint(deck, "refused")
        kill_the_reader(deck, "open", "refused")
        supervisor = watchdog.supervisor_for(controller)

        class _RefusingWriter:
            """Model the interval after a terminal message but before loop exit."""
            running = True

            def submit_control(self, msg) -> bool:
                return False

        real_writer = controller.media_player
        controller.media_player = _RefusingWriter()
        try:
            assert supervisor.request_reopen() is False, (
                "a message the writer refused was reported as submitted")
        finally:
            controller.media_player = real_writer

        assert supervisor.attempts_started == 0, (
            "an attempt was counted for a message nothing will ever run")
        assert supervisor.consecutive_attempts == 0, (
            "the refused message moved the deck toward the give-up latch")
        assert not supervisor.attempt_in_flight(), (
            "the refused message left the in-flight marker set, which refuses every "
            "later attempt for the life of this deck")

        # The real writer still takes one.
        watchdog.sweep()
        assert fixtures.wait_until(lambda: supervisor.reopens == 1, timeout=30), (
            "the supervisor never recovered from the refused message")
    finally:
        fixtures.teardown(controller)
    print("PASS: a control message the writer refuses counts no attempt")


def test_a_reopen_that_holds_clears_the_count() -> None:
    """A deck that comes back and stays gets its attempt count back."""
    # The hold window is what this leg measures, so it is the one constant it
    # shrinks, and it goes back at the end.
    shipped_hold = reader_supervisor.HOLD_WINDOW_S
    reader_supervisor.HOLD_WINDOW_S = 0.3
    controller, deck = make_controller("reader-holds")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    try:
        boot_paint(deck, "holds")
        kill_the_reader(deck, "open", "holds")

        watchdog.sweep()
        supervisor = watchdog.supervisor_for(controller)
        assert fixtures.wait_until(lambda: supervisor.reopens == 1, timeout=30), (
            "the deck was never reopened, so there is no hold to settle")
        assert supervisor.consecutive_attempts == 1, (
            "the attempt count cleared before the hold window elapsed")

        settled = False
        for _ in range(60):
            watchdog.sweep()
            if supervisor.consecutive_attempts == 0:
                settled = True
                break
            time.sleep(0.05)
        assert settled, (
            f"the reopened reader stayed alive past the hold window and the attempt "
            f"count never cleared: {supervisor.consecutive_attempts}")
        assert supervisor.holds == 1, (
            f"the hold was settled {supervisor.holds} times, not once")
        assert not supervisor.given_up, "a recovered deck was given up"
    finally:
        reader_supervisor.HOLD_WINDOW_S = shipped_hold
        fixtures.teardown(controller)
    print("PASS: a reopen that holds clears the attempt count")


def test_a_synchronous_reopen_keeps_its_hold() -> None:
    """Keep the hold armed by a reopen that completes inside submit_control.
    Clear the previous hold before submission so synchronous completion survives."""
    controller, deck = make_controller("reader-sync-hold")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    try:
        boot_paint(deck, "sync")
        kill_the_reader(deck, "open", "sync")
        supervisor = watchdog.supervisor_for(controller)

        # A writer that drains the reopen inline, the worst case for the race.
        real_writer = controller.media_player

        class _SynchronousWriter:
            running = True

            def submit_control(self, msg) -> bool:
                # Run the attempt before returning, as a media thread that
                # drained the queue immediately would.
                msg.supervisor.run_attempt(stopping=lambda: False)
                return True

        controller.media_player = _SynchronousWriter()
        try:
            assert supervisor.request_reopen() is True, "the synchronous reopen was not submitted"
        finally:
            controller.media_player = real_writer

        # The reopen armed a hold. request_reopen must not have cleared it: the
        # deadline is still set, so the reader can settle its count.
        assert supervisor.reopens == 1, "the synchronous reopen did not run"
        assert supervisor.has_pending_hold(), (
            "request_reopen cleared the hold the synchronous reopen just armed, "
            "so the reader can never settle its recovery count")
    finally:
        fixtures.teardown(controller)
    print("PASS: a synchronous reopen keeps the hold it armed")


def main() -> None:
    # A reopen that waits on a handle it cannot take parks here, and must fail
    # loud rather than sit until the per-scenario timeout of run_all.py.
    fixtures.start_watchdog(60, label="scenario_reader_reconnect")

    # One ordinary controller first. It installs the integration globals and
    # warms every lazily started global thread, so a leg measures its own deck.
    warm = fixtures.make_headless_controller(serial="reader-warm")
    fixtures.wait_until(lambda: warm.active_page is not None, timeout=10)
    fixtures.teardown(warm)

    test_dead_reader_is_reopened()
    test_a_reader_that_closed_the_handle_is_reopened()
    test_a_reader_in_the_resume_loop_is_left_alone()
    test_a_closing_or_quitting_app_reopens_nothing()
    test_a_teardown_that_starts_mid_attempt_wins()
    test_the_attempt_cap_gives_a_flapping_deck_up()
    test_a_reopen_that_never_holds_is_capped()
    test_a_message_the_writer_refuses_counts_no_attempt()
    test_a_reopen_that_holds_clears_the_count()
    test_a_synchronous_reopen_keeps_its_hold()
    print("ALL PASS: scenario_reader_reconnect")


if __name__ == "__main__":
    main()
