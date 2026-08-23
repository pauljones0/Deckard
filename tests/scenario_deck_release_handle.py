"""The device handle is released, never bare-closed.

Every leg drives one production teardown path over a deck that models the
library's reader thread, and asserts that close() ran with the reader stopped
and the resume-from-suspend loop disarmed. A close that leaves either flag up
lets the library re-open the handle it just gave back, which on the quit path
hands the next process a busy device.

The flags alone do not settle it. The library re-opens from the except arm of
its read loop, where it reads neither flag, so one leg parks a reader inside
that arm and asserts the release keeps the handle closed anyway.
"""
import threading
import time

import fixtures  # must be first; isolates DATA_PATH before import globals
import globals as gl

from StreamDeck.Transport.Transport import TransportError

from faulty_fake_deck import FaultyFakeDeck

from src.backend.DeckManagement.DeckController import DeckController
from src.backend.DeckManagement.DeckManager import DeckManager, close_all_controllers


class ReaderDeck(FaultyFakeDeck):
    """FaultyFakeDeck plus the reader-thread contract of the library.

    A real StreamDeck runs a reader thread that polls run_read_thread and, on
    a transport error, re-opens the handle while reconnect_after_suspend is
    set. FakeDeck carries neither flag, so the stop path skips it and no
    scenario can see the ordering. This deck carries both flags, runs a thread
    that journals its own exit, and records the flags as close() saw them.

    flake_serial injects one failure into get_serial_number(), on the thread
    that built the controller, so the raise lands in the constructor and not
    in a worker that swallows it. "first" fails the settings read between
    open() and the clear probe, inside the bring-up guard. "after_guard"
    fails the next read, which sits past that guard and ahead of the tail
    guard. "after_writer" fails the first read once the media writer runs.
    flake_exc picks the exception, which decides which arm of the deck-open
    retry judges the failure.
    """

    def __init__(self, *args, flake_serial=None, flake_exc=TransportError, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_read_thread = False
        self.reconnect_after_suspend = True
        self.read_thread = None
        # (run_read_thread, reconnect_after_suspend, thread) per close(), as
        # close saw them. A release must leave both flags down.
        self.flags_at_close: list[tuple] = []
        self._flake_serial = flake_serial
        self._flake_exc = flake_exc
        self.serial_flakes = 0
        self._constructor_reads = 0
        self._init_thread = None
        self._pre_open_idents: set = set()

    def open(self, *args, **kwargs):
        super().open(*args, **kwargs)
        # Journal the open, so a leg can tell a reopen from the first open.
        self._record("open", "device", None)
        # open() runs on the thread that builds the controller, and it runs
        # before the writer exists. Both facts anchor the injection below.
        self._init_thread = threading.current_thread()
        self._constructor_reads = 0
        self._pre_open_idents = {t.ident for t in threading.enumerate()}
        if self.read_thread is not None and self.read_thread.is_alive():
            return
        self.reconnect_after_suspend = True
        self.run_read_thread = True
        self.read_thread = threading.Thread(
            target=self._read_loop, name=f"FakeReader-{id(self):x}", daemon=True)
        self.read_thread.start()

    def _read_loop(self) -> None:
        # The library reader polls and exits when the flag goes down. Journal
        # the exit, so a leg can order it against close().
        while self.run_read_thread:
            time.sleep(0.002)
        self._record("read_exit", "device", None)

    def close(self):
        self.flags_at_close.append((self.run_read_thread, self.reconnect_after_suspend,
                                    threading.current_thread().name))
        super().close()

    def _writer_running(self) -> bool:
        return any(t.name.startswith("MediaPlayerThread")
                   and t.ident not in self._pre_open_idents
                   for t in threading.enumerate())

    def _flake_due(self) -> bool:
        if self._flake_serial is None:
            return False
        # Only on the constructor's own thread, so the failure aborts the
        # construction instead of landing in a worker that swallows it.
        if threading.current_thread() is not self._init_thread:
            return False
        if self._flake_serial == "after_writer":
            return self._writer_running()
        if self._flake_serial == "after_guard":
            # The bring-up guard reads the serial once, for the rotation. The
            # next read on this thread is already past that guard.
            self._constructor_reads += 1
            return self._constructor_reads > 1
        return True

    def get_serial_number(self):
        if self._flake_due():
            # One shot. The constructor gives up on it, and the teardown that
            # follows reads the serial again.
            self._flake_serial = None
            self.serial_flakes += 1
            raise self._flake_exc("ReaderDeck: injected serial read failure")
        return super().get_serial_number()


class ResumeLoopDeck(ReaderDeck):
    """A deck whose reader parks inside the library's reopen loop.

    The library re-opens the device from the except arm of its read loop
    (StreamDeck.py:209-262), where it reads neither run_read_thread nor
    reconnect_after_suspend. This models that arm: on a fault the reader
    clears its own run flag, closes the handle, then calls open() until one
    returns, whatever the flags say. block_open makes those calls fail, so a
    leg can hold the reader in the loop while it releases the handle.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fault = threading.Event()
        self.in_resume_loop = threading.Event()
        self.block_open = threading.Event()
        self.resume_attempts = 0

    def _read_loop(self) -> None:
        while self.run_read_thread:
            if self.fault.is_set():
                self.run_read_thread = False
                self.close()
                self.in_resume_loop.set()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    self.resume_attempts += 1
                    try:
                        # Looked up per call, so a shadow installed on this
                        # instance takes effect from here on.
                        self.open()
                        break
                    except Exception:
                        time.sleep(0.01)
                break
            time.sleep(0.002)
        self._record("read_exit", "device", None)

    def open(self, *args, **kwargs):
        if self.block_open.is_set():
            raise TransportError("ResumeLoopDeck: the device is not ready")
        return super().open(*args, **kwargs)


def make_controller(serial: str, **deck_kwargs):
    """A real DeckController over a ReaderDeck, the integration tier."""
    fixtures.seed_page("Main")
    deck = ReaderDeck(serial_number=serial, deck_type="Fake Deck", **deck_kwargs)
    controller = DeckController(gl.deck_manager, deck)
    gl.deck_manager.deck_controller.append(controller)
    return controller, deck


def build_and_expect_failure(serial: str, fail_op=None, **deck_kwargs) -> ReaderDeck:
    """Build a controller that must fail, and hand back its deck."""
    fixtures.seed_page("Main")
    deck = ReaderDeck(serial_number=serial, deck_type="Fake Deck", **deck_kwargs)
    if fail_op is not None:
        deck.fail_next(fail_op)
    try:
        controller = DeckController(gl.deck_manager, deck)
    except Exception:
        return deck
    fixtures.teardown(controller)
    raise AssertionError(
        f"{serial}: the constructor was supposed to fail, so this leg covers nothing")


def app_closes(deck: ReaderDeck) -> list:
    """Flag records for the closes the app performed.

    A close from the modeled reader is the library closing on its own behalf
    inside its transport-error arm, and it carries the flags the library
    leaves. Only a close the app made states the release contract.
    """
    return [flags for flags in deck.flags_at_close
            if not flags[2].startswith("FakeReader-")]


def assert_handle_released(deck: ReaderDeck, label: str) -> None:
    """The release contract: reader stopped, resume loop disarmed, then close.

    A close is idempotent, and a path can legitimately run it twice: the
    constructor's guard releases, and the deck-open retry releases the same
    handle again. So this counts no closes. It asserts that at least one
    happened and that every one the app made found both flags down. Ordering
    comes from the journal sequence, so a loaded host cannot make it brittle.
    """
    journal = deck.journal()
    closes = [e for e in journal if e[2] == "close"]
    assert closes, f"{label}: the handle was never closed: {journal}"
    releases = app_closes(deck)
    assert releases, f"{label}: no close came from the app: {deck.flags_at_close}"
    for flags in releases:
        assert flags[0] is False and flags[1] is False, (
            f"{label}: close() ran with (run_read_thread, reconnect_after_suspend) = "
            f"{flags[:2]} on {flags[2]}. Both must be down before the handle closes, "
            "or the library's reader takes back the device it just gave up."
        )
    exits = [e for e in journal if e[2] == "read_exit"]
    assert exits, f"{label}: the reader thread never exited: {journal}"
    assert exits[0][1] < closes[0][1], (
        f"{label}: the reader exited at seq {exits[0][1]}, after close() at "
        f"{closes[0][1]}. The release joins the reader first."
    )
    assert not deck.read_thread.is_alive(), (
        f"{label}: the reader thread outlived the release")
    assert not deck.is_open(), f"{label}: the handle is still open after the release"


def test_quit_path_releases_handle() -> None:
    """close_all_controllers drives the writer's terminal ClearAndClose."""
    controller, deck = make_controller("release-quit")
    assert fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=10), (
        "the boot paint never landed, so this leg would measure a bare deck")
    deck.clear_journal()

    close_all_controllers([controller])
    try:
        assert fixtures.wait_until(lambda: not controller.media_player.is_alive(), timeout=10), (
            "the media writer must exit within the bounded join")
        assert_handle_released(deck, "quit path")
    finally:
        # Stop the non-daemon tick thread even when the leg fails, so the
        # failure reports as a traceback and not as a scenario timeout.
        fixtures.teardown(controller)
    print("PASS: the quit path stops the reader before it closes the handle")


def test_direct_close_arm_releases_handle() -> None:
    """A controller with no writer thread is released by close_all_controllers
    itself, and not by a control message no thread would drain."""
    controller, deck = make_controller("release-direct")
    assert fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=10)

    controller.media_player.stop(timeout=2.0)
    assert fixtures.wait_until(lambda: not controller.media_player.is_alive(), timeout=10)
    deck.clear_journal()
    controller.media_player = None

    close_all_controllers([controller])
    try:
        assert_handle_released(deck, "direct close arm")
    finally:
        fixtures.teardown(controller)
    print("PASS: the direct close arm stops the reader before it closes the handle")


def test_failed_clear_probe_releases_handle() -> None:
    """The liveness probe fails, so the constructor gives the handle back."""
    deck = build_and_expect_failure("release-clear-probe", fail_op="set_key_image")
    assert_handle_released(deck, "failed clear probe")
    print("PASS: a failed clear probe releases the handle")


def test_failed_settings_read_releases_handle() -> None:
    """The settings read sits between open() and the clear probe.

    A failure there leaves an open handle with a live reader unless the guard
    covers the whole window.
    """
    deck = build_and_expect_failure("release-settings-read", flake_serial="first")
    assert deck.serial_flakes == 1, (
        f"expected one injected serial failure, got {deck.serial_flakes}")
    assert_handle_released(deck, "failed settings read")
    print("PASS: a failure between open() and the clear probe releases the handle")


def test_failed_tail_releases_handle() -> None:
    """A failure in the guarded tail runs the failed-init teardown, which
    releases the handle once the writer has stopped."""
    deck = build_and_expect_failure("release-tail", flake_serial="after_writer")
    assert deck.serial_flakes == 1, (
        f"expected one injected serial failure, got {deck.serial_flakes}")
    assert_handle_released(deck, "failed tail")
    print("PASS: the failed-init teardown releases the handle")


def test_generic_retry_arm_releases_handle() -> None:
    """A raise past the bring-up guard and ahead of the tail guard leaves the
    constructor with the handle still open.

    Nothing in the constructor covers that window, so the arm of the deck-open
    retry that judges a non-transport failure owns the release.
    """
    fixtures.seed_page("Main")
    deck = ReaderDeck(serial_number="release-generic-arm", deck_type="Fake Deck",
                      flake_serial="after_guard", flake_exc=RuntimeError)
    controller = DeckManager._init_deck_controller_with_retry(
        gl.deck_manager, deck, attempts=1, retry_delay=0.05)

    assert controller is None, "a non-transport failure must not register the deck"
    assert deck.serial_flakes == 1, (
        f"expected one injected serial failure, got {deck.serial_flakes}")
    assert_handle_released(deck, "generic retry arm")
    print("PASS: the give-up arm of the retry releases the handle")


def test_release_beats_the_resume_loop() -> None:
    """A reader already inside the reopen loop reads neither flag.

    The release has to leave the handle unable to take an open() at all, or
    the reader re-opens the device after the release returned.
    """
    fixtures.seed_page("Main")
    deck = ResumeLoopDeck(serial_number="release-resume-loop", deck_type="Fake Deck")
    controller = DeckController(gl.deck_manager, deck)
    gl.deck_manager.deck_controller.append(controller)
    try:
        assert fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=10), (
            "the boot paint never landed")

        # Park the reader inside the reopen loop, with its reopens failing.
        deck.block_open.set()
        deck.fault.set()
        assert deck.in_resume_loop.wait(timeout=10), "the reader never entered the reopen loop"

        released_at = deck.current_seq()
        controller._release_handle()
        # Let the loop's next attempt through. Without a released handle that
        # refuses open(), this is where the device comes back.
        deck.block_open.clear()

        assert fixtures.wait_until(lambda: not deck.read_thread.is_alive(), timeout=10), (
            "the reader stayed in the reopen loop after the release")
        assert deck.resume_attempts >= 1, (
            "the reader never tried a reopen, so this leg proves nothing")
        reopens = [e for e in deck.journal() if e[2] == "open" and e[1] > released_at]
        assert not reopens, (
            f"the handle was re-opened after the release: {reopens}. A released "
            "handle must answer every later open() with nothing.")
        assert not deck.is_open(), "the handle came back open after the release"
        releases = app_closes(deck)
        assert releases and releases[-1][0] is False and releases[-1][1] is False, (
            f"the release closed with flags {releases[-1][:2] if releases else None}")
    finally:
        fixtures.teardown(controller)
    print("PASS: a release the reader cannot undo, even from inside the reopen loop")


def test_out_of_range_rotation_comes_up_unrotated() -> None:
    """A persisted rotation now reaches the wrapper's own validation.

    An unusable value must leave the deck unrotated and running, not raising
    on every key write.
    """
    serial = "release-rotation"
    settings = gl.settings_manager.get_deck_settings(serial)
    settings["rotation"] = 45
    gl.settings_manager.save_deck_settings(serial, settings)

    controller, deck = make_controller(serial)
    try:
        assert controller.deck.get_rotation() == 0, (
            f"a persisted rotation of 45 must come up as 0, got "
            f"{controller.deck.get_rotation()}")
        assert fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=10), (
            "a deck with an unusable persisted rotation must still paint")
    finally:
        fixtures.teardown(controller)
    print("PASS: an unusable persisted rotation comes up as no rotation")


def main() -> None:
    # A release that waits on a reader it cannot stop parks here, and must
    # fail loud rather than sit until the per-scenario timeout of run_all.py.
    fixtures.start_watchdog(60, label="scenario_deck_release_handle")

    # One ordinary controller first. It installs the integration globals and
    # warms every lazily started global thread, so a leg measures its own deck.
    warm = fixtures.make_headless_controller(serial="release-warm")
    fixtures.wait_until(lambda: warm.active_page is not None, timeout=10)
    fixtures.teardown(warm)

    test_quit_path_releases_handle()
    test_direct_close_arm_releases_handle()
    test_release_beats_the_resume_loop()
    test_failed_clear_probe_releases_handle()
    test_failed_settings_read_releases_handle()
    test_failed_tail_releases_handle()
    test_generic_retry_arm_releases_handle()
    test_out_of_range_rotation_comes_up_unrotated()
    print("ALL PASS: scenario_deck_release_handle")


if __name__ == "__main__":
    main()
