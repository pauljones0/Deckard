"""The device handle is released, never bare-closed.

Every leg drives one production teardown path over a deck that models the
library's reader thread, and asserts that close() ran with the reader stopped
and the resume-from-suspend loop disarmed. A close that leaves either flag up
lets the library re-open the handle it just gave back, which on the quit path
hands the next process a busy device.
"""
import threading
import time

import fixtures  # must be first; isolates DATA_PATH before import globals
import globals as gl

from StreamDeck.Transport.Transport import TransportError

from faulty_fake_deck import FaultyFakeDeck

from src.backend.DeckManagement.DeckController import DeckController
from src.backend.DeckManagement.DeckManager import close_all_controllers


class ReaderDeck(FaultyFakeDeck):
    """FaultyFakeDeck plus the reader-thread contract of the library.

    A real StreamDeck runs a reader thread that polls run_read_thread and, on
    a transport error, re-opens the handle while reconnect_after_suspend is
    set. FakeDeck carries neither flag, so the stop path skips it and no
    scenario can see the ordering. This deck carries both flags, runs a thread
    that journals its own exit, and records the flags as close() saw them.

    flake_serial injects one TransportError into get_serial_number(), on the
    thread that built the controller: "first" fails the settings read that
    sits between open() and the clear probe, "after_writer" fails the first
    read once the media writer is up.
    """

    def __init__(self, *args, flake_serial=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_read_thread = False
        self.reconnect_after_suspend = True
        self.read_thread = None
        # (run_read_thread, reconnect_after_suspend) per close(), as close saw
        # them. The release must leave both False.
        self.flags_at_close: list[tuple] = []
        self._flake_serial = flake_serial
        self.serial_flakes = 0
        self._init_thread = None
        self._pre_open_idents: set = set()

    def open(self, *args, **kwargs):
        super().open(*args, **kwargs)
        # open() runs on the thread that builds the controller, and it runs
        # before the writer exists. Both facts anchor the injection below.
        self._init_thread = threading.current_thread()
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
        self.flags_at_close.append((self.run_read_thread, self.reconnect_after_suspend))
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
        return True

    def get_serial_number(self):
        if self._flake_due():
            # One shot. The constructor gives up on it, and the teardown that
            # follows reads the serial again.
            self._flake_serial = None
            self.serial_flakes += 1
            raise TransportError("ReaderDeck: injected serial read failure")
        return super().get_serial_number()


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


def assert_handle_released(deck: ReaderDeck, label: str) -> None:
    """The release contract: reader stopped, resume loop disarmed, then close.

    Ordering comes from the journal sequence, so a loaded host cannot make it
    brittle.
    """
    journal = deck.journal()
    closes = [e for e in journal if e[2] == "close"]
    assert len(closes) == 1, (
        f"{label}: expected exactly one close(), got {len(closes)}: {journal}")
    assert deck.flags_at_close == [(False, False)], (
        f"{label}: close() ran with (run_read_thread, reconnect_after_suspend) = "
        f"{deck.flags_at_close}. Both must be down before the handle closes, or "
        "the library's resume loop re-opens the device it just gave back."
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
    test_failed_clear_probe_releases_handle()
    test_failed_settings_read_releases_handle()
    test_failed_tail_releases_handle()
    print("ALL PASS: scenario_deck_release_handle")


if __name__ == "__main__":
    main()
