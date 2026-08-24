"""A deck whose input reader dies under a live device is reopened.

The library's reader thread is what makes a deck an input device. It dies in
two ways that leave the device connected: an exception the library's except arm
does not catch, which leaves the handle open, and an exhausted resume loop,
which leaves it closed. Both leave the deck deaf to every press, and both pass
the USB liveness checks, because the device is present.

Each leg drives the production watchdog over a deck that models the reader
thread, and asserts what the supervisor does with it: reopen through the
release seam on the media thread, leave a reader inside the library's own
resume loop alone, give a flapping deck up after the attempt cap, and touch
nothing during a teardown or a quit.
"""
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
from src.backend.DeckManagement.reader_supervisor import DeckReaderWatchdog


class SupervisedDeck(FaultyFakeDeck):
    """FaultyFakeDeck plus the reader-thread contract of the library.

    open() starts a reader thread the way StreamDeck.open() does through
    _setup_reader, and journals the open, so a leg can order a reopen against
    the release that must precede it. The thread ends when run_read_thread goes
    down, which is how the release seam stops it, and close() records the flags
    as it saw them.

    Three reader states are modeled, all with the device still connected:

    die_open() ends the thread with the handle still open and run_read_thread
    still up. That is any exception which is not a TransportError, such as one
    raised by an input callback, because the library's read loop catches only
    the transport kind.

    die_closed() closes the handle first and then ends the thread, which is
    what an exhausted resume loop leaves behind.

    park_in_resume_loop() is the state the supervisor must not touch: the
    thread stays alive inside the reopen arm, which reads neither flag, and
    re-opens the handle itself.

    block_open makes every open() raise, which models a device that answers
    nothing. It parks a reader in the resume loop and it fails a reopen.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_read_thread = False
        self.reconnect_after_suspend = True
        self.read_thread = None
        # (run_read_thread, reconnect_after_suspend, thread) per close(), as
        # close saw them. A release must leave both flags down.
        self.flags_at_close: list[tuple] = []
        self.block_open = threading.Event()
        self.in_resume_loop = threading.Event()
        self.resume_attempts = 0
        self.reader_starts = 0
        self._die = None

    # Reader deaths
    def die_open(self) -> None:
        self._die = "open"

    def die_closed(self) -> None:
        self._die = "closed"

    def park_in_resume_loop(self) -> None:
        self._die = "resume"

    def open(self, *args, **kwargs):
        if self.block_open.is_set():
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

    def close(self):
        self.flags_at_close.append((self.run_read_thread, self.reconnect_after_suspend,
                                    threading.current_thread().name))
        super().close()

    def _read_loop(self) -> None:
        while self.run_read_thread:
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
                        # Drop the handle on this thread first, the way the
                        # library's _setup_reader replaces it, so a successful
                        # reopen starts a fresh reader instead of finding this
                        # one alive and returning.
                        self.read_thread = None
                        self.open()
                        break
                    except Exception:
                        self.read_thread = threading.current_thread()
                        time.sleep(0.02)
                self._record("resume_loop_exit", "device", None)
                return
            time.sleep(0.002)


def make_controller(serial: str):
    """A real DeckController over a SupervisedDeck, the integration tier."""
    fixtures.seed_page("Main")
    deck = SupervisedDeck(serial_number=serial, deck_type="Fake Deck")
    controller = DeckController(gl.deck_manager, deck)
    gl.deck_manager.deck_controller.append(controller)
    return controller, deck


def app_closes(deck: SupervisedDeck) -> list:
    """Flag records for the closes the app performed.

    A close from the modeled reader is the library closing on its own behalf,
    and only a close the app made says anything about the supervisor.
    """
    return [flags for flags in deck.flags_at_close
            if not flags[2].startswith("FakeReader-")]


def kill_the_reader(deck: SupervisedDeck, mode: str, label: str):
    """End the modeled reader the named way, and hand the dead thread back.

    "open" leaves the handle open, "closed" gives it back first. Both leave
    the device connected.
    """
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


def test_dead_reader_is_reopened() -> None:
    """The watchdog detects, and the media thread reopens through the seam."""
    controller, deck = make_controller("reader-reopen")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    try:
        boot_paint(deck, "reopen")
        # The boot journal entry above comes from the constructor's direct
        # clear, which carries no present state. Wait for a paint that records
        # one, or the present-state assertions below hold on a deck that never
        # painted through the present protocol.
        assert fixtures.wait_until(
            lambda: any(k.present_state.last_presented_hash is not None
                        for k in controller.inputs.get(Input.Key, [])), timeout=10), (
            "no key recorded a presented image, so this leg would measure nothing")
        keys = controller.inputs.get(Input.Key, [])

        first_reader = kill_the_reader(deck, "open", "reopen")
        assert deck.is_open(), "this leg models the death that leaves the handle open"
        assert deck.connected(), "the device must still be on the bus for this leg"
        deck.clear_journal()
        deck.flags_at_close.clear()
        # Hold the scheduled repaint inside its rate-limit window, so the
        # present state can be read after the reopen and before the repaint
        # clears it again on its way through.
        controller._last_full_repaint_ts = time.time()

        watchdog.sweep()
        supervisor = watchdog.supervisor_for(controller)
        assert fixtures.wait_until(lambda: supervisor.reopens == 1, timeout=20), (
            f"the deck was never reopened: attempts={supervisor.attempts_started}, "
            f"given_up={supervisor.given_up}")

        assert deck.read_thread is not first_reader and deck.read_thread.is_alive(), (
            "the reopen left no live reader, so the deck still takes no input")
        assert deck.is_open(), "the handle is closed after a successful reopen"

        assert all(k.present_state.last_presented_hash is None
                   and k.present_state.last_enqueued_hash is None for k in keys), (
            "the reopen left the keys naming what the device showed before it went "
            "deaf. A paint offered before the repaint fires matches those hashes and "
            "is skipped, so the deck keeps the stale imagery.")
        assert controller._full_repaint_pending, (
            "the reopen armed no full repaint, so a static page never repaints")

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

        # The page content did not change across the reopen, so a repaint that
        # kept its present-state hashes would match them and write nothing.
        # Writes after the reopen are what proves the reset ran.
        reopen_seq = opens[0][1]
        assert fixtures.wait_until(
            lambda: any(e[2] == "set_key_image" and e[1] > reopen_seq
                        for e in deck.journal()), timeout=20), (
            "nothing repainted after the reopen. The device keeps whatever it "
            "showed when it went deaf unless the present state is reset and a "
            "full repaint is scheduled.")
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
        assert fixtures.wait_until(lambda: supervisor.reopens == 1, timeout=20), (
            f"a deck with a closed handle and a dead reader was not reopened: "
            f"attempts={supervisor.attempts_started}, given_up={supervisor.given_up}")
        assert deck.is_open() and deck.read_thread.is_alive(), (
            "the reopen left the deck without an open handle and a live reader")
    finally:
        fixtures.teardown(controller)
    print("PASS: a reader that gave the handle back is reopened too")


def test_a_reader_in_the_resume_loop_is_left_alone() -> None:
    """A reader inside the library's reopen arm is alive, so it is not dead.

    Both would otherwise close and open the same handle at once, and the
    library's arm reads no flag that would stop it.
    """
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
    finally:
        fixtures.teardown(controller)
    print("PASS: a reader inside the library's resume loop is not treated as dead")


def test_the_attempt_cap_gives_a_flapping_deck_up() -> None:
    """A device that never comes back costs a bounded number of attempts."""
    # Shrink the per-attempt timings, so a failing attempt costs milliseconds.
    # The cap counts attempts inside a window, and both keep their shipped
    # values here.
    reader_supervisor.REOPEN_DEADLINE_S = 0.2
    reader_supervisor.REOPEN_RETRY_GAP_S = 0.02
    controller, deck = make_controller("reader-flapping")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    records: list[str] = []
    sink_id = log.add(lambda msg: records.append(str(msg)), level="WARNING")
    try:
        boot_paint(deck, "flapping")
        deck.block_open.set()  # every reopen fails
        kill_the_reader(deck, "open", "flapping")

        supervisor = watchdog.supervisor_for(controller)
        for _ in range(reader_supervisor.MAX_ATTEMPTS_IN_WINDOW * 3):
            watchdog.sweep()
            fixtures.wait_until(lambda: not supervisor.attempt_in_flight(), timeout=20)
            if supervisor.given_up:
                break

        assert supervisor.given_up, (
            f"a deck whose handle never opens again was retried "
            f"{supervisor.attempts_started} times without a give-up")
        assert supervisor.attempts_started == reader_supervisor.MAX_ATTEMPTS_IN_WINDOW, (
            f"the cap allowed {supervisor.attempts_started} attempts, not "
            f"{reader_supervisor.MAX_ATTEMPTS_IN_WINDOW}")
        assert supervisor.reopens == 0, "a blocked device reported a successful reopen"

        # It stays down, and it says so at most once per rate-limit window.
        for _ in range(10):
            watchdog.sweep()
        assert supervisor.attempts_started == reader_supervisor.MAX_ATTEMPTS_IN_WINDOW, (
            "a given-up deck was retried again")
        give_ups = [r for r in records if "Giving the" in r]
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
        log.remove(sink_id)
        fixtures.teardown(controller)
    print("PASS: the attempt cap stops a flapping device and says so once")


def test_a_closing_or_quitting_app_reopens_nothing() -> None:
    """Teardown and quit own the handle, and the supervisor stands off.

    The two guards sit on different threads. The sweep skips a controller that
    is closing, and the attempt itself re-checks on the media thread, because
    the watchdog decided up to one sweep earlier.
    """
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
            assert fixtures.wait_until(lambda: not supervisor.attempt_in_flight(), timeout=20), (
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
        assert fixtures.wait_until(lambda: supervisor.reopens == 1, timeout=20), (
            f"the deck never reopened after the guards cleared: "
            f"attempts={supervisor.attempts_started}, given_up={supervisor.given_up}")
    finally:
        fixtures.teardown(controller)
    print("PASS: neither a closing controller nor a quitting app is reopened")


def main() -> None:
    # A reopen that waits on a handle it cannot take parks here, and must fail
    # loud rather than sit until the per-scenario timeout of run_all.py.
    fixtures.start_watchdog(180, label="scenario_reader_reconnect")

    # One ordinary controller first. It installs the integration globals and
    # warms every lazily started global thread, so a leg measures its own deck.
    warm = fixtures.make_headless_controller(serial="reader-warm")
    fixtures.wait_until(lambda: warm.active_page is not None, timeout=10)
    fixtures.teardown(warm)

    test_dead_reader_is_reopened()
    test_a_reader_that_closed_the_handle_is_reopened()
    test_a_reader_in_the_resume_loop_is_left_alone()
    test_a_closing_or_quitting_app_reopens_nothing()
    test_the_attempt_cap_gives_a_flapping_deck_up()
    print("ALL PASS: scenario_reader_reconnect")


if __name__ == "__main__":
    main()
