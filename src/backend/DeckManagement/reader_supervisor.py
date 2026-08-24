"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

Supervision of the library's input reader thread, one per deck.

That thread is what makes a deck an input device. It polls the HID transport
and calls the key, dial and touchscreen callbacks. It dies in two ways, and
neither one removes the deck or logs a verdict of its own.

A read-side transport error takes the reader into the library's resume loop,
where it closes the handle and re-opens it for up to ten seconds. A loop that
runs out of attempts leaves the thread exited and the handle closed while the
device is still on the bus. An exception that is not a transport error, such
as one raised by an input callback, is caught by nothing in that loop, so the
thread dies with the handle still open and the deck still painting. Either way
the deck ignores every press until the app restarts: the USB liveness checks
test presence, and the device is present.

The supervision is split across two threads on purpose.

The watchdog thread only detects. It sweeps the registered controllers and
never touches a device. Its predicate is cheap for a healthy deck, so the
sweep costs one attribute read and one is_alive() per deck.

The media thread performs. It is the sole device writer, so it is the only
thread that may close and re-open a handle, and the only one that can afford
to wait on the device lock a wedged write holds. The watchdog submits a
ReopenDeckMsg, and the writer runs the attempt in its control drain.
"""
import threading
import time
from collections import deque

from loguru import logger as log

from src.backend.DeckManagement.deck_controller.media_writer import ReopenDeckMsg

import globals as gl

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.backend.DeckManagement.BetterDeck import BetterDeck
    from src.backend.DeckManagement.DeckManager import DeckManager
    from src.backend.DeckManagement.deck_controller.controller import DeckController


# How long the watchdog waits between two sweeps.
WATCHDOG_INTERVAL_S = 2.0
# Wall clock one attempt may spend re-opening the handle. It matches the
# library's own resume-loop timeout, because it recovers from the same device
# states. The media thread paints nothing while an attempt runs, so this is
# also the longest a reopen can hold up the writer.
REOPEN_DEADLINE_S = 10.0
# Gap between two open() tries inside one attempt.
REOPEN_RETRY_GAP_S = 0.25
# Attempt cap. MAX_ATTEMPTS_IN_WINDOW attempts inside ATTEMPT_WINDOW_S seconds
# means the device is flapping, not recovering, and the supervisor gives it up
# for good. A reopen is a device close and open plus a full repaint, so an
# unbounded retry of a device that refuses to stay open costs the writer its
# whole duty cycle. A deck given up here comes back on a replug, which builds
# a fresh controller and a fresh supervisor.
ATTEMPT_WINDOW_S = 30.0
MAX_ATTEMPTS_IN_WINDOW = 5
# Rate limit on the log line that a given-up deck stays down. The predicate
# below stays true for as long as the deck lives, so an unlimited line here
# would repeat once per sweep for the rest of the session.
GIVE_UP_LOG_GAP_S = 60.0


def reader_is_dead(deck: "BetterDeck") -> bool:
    """Whether the library's reader thread for this deck has exited.

    The predicate is exactly this: the handle carries a reader thread, and
    that thread is not alive.

    A reader inside the library's resume loop is not dead. It sits in the
    except arm of its read loop, re-opening the handle for up to ten seconds,
    and its thread stays alive the whole time. The supervisor must not fight
    that loop, because both would close and open the same handle at once, so
    the predicate fires only once the thread has left the loop and exited. A
    resume that succeeds starts a fresh reader thread and never reaches this.

    A FakeDeck and a RemoteDeck carry no read_thread attribute, so neither is
    ever supervised.
    """
    read_thread = getattr(deck.deck, "read_thread", None)
    if read_thread is None:
        return False
    return not read_thread.is_alive()


def _still_connected(deck: "BetterDeck") -> bool:
    """Whether the device is still on the bus. A transport that raises here
    counts as gone, and the disconnect sweep owns that deck."""
    try:
        return deck.connected()
    except Exception:
        return False


class DeckReaderSupervisor:
    """One deck's reopen state: the attempt window, the in-flight marker and
    the give-up latch.

    The watchdog thread decides whether to submit an attempt and the media
    thread runs it, so both touch these fields. One lock covers them, every
    critical section is a few statements long, and no device call happens
    under it.
    """

    def __init__(self, controller: "DeckController"):
        self.controller = controller
        self._lock = threading.Lock()
        # Monotonic start time of each attempt inside the current window.
        self._attempts: "deque[float]" = deque()
        self._in_flight = False
        # Latched. The deck stays down until a replug builds a new controller.
        self.given_up = False
        self._last_give_up_log = 0.0
        # Counters for the scenarios and for a field log read.
        self.attempts_started = 0
        self.reopens = 0

    def attempt_in_flight(self) -> bool:
        """Whether an attempt is queued on the writer or running there. The
        watchdog submits no second attempt while it is."""
        with self._lock:
            return self._in_flight

    def request_reopen(self) -> bool:
        """Submit one reopen attempt to the media thread, and report whether
        it was submitted. Watchdog thread only.

        It refuses while an attempt is in flight, after the give-up latch, and
        when the deck has no running writer to perform the attempt.
        """
        media_player = getattr(self.controller, "media_player", None)
        if media_player is None or not media_player.running:
            # Nothing would drain the message. A controller in this state is
            # being torn down or failed to build, and neither is this
            # supervisor's business.
            return False
        now = time.monotonic()
        with self._lock:
            if self.given_up:
                self._log_given_up(now)
                return False
            if self._in_flight:
                return False
            while self._attempts and now - self._attempts[0] > ATTEMPT_WINDOW_S:
                self._attempts.popleft()
            if len(self._attempts) >= MAX_ATTEMPTS_IN_WINDOW:
                self.given_up = True
                self._last_give_up_log = now
                log.error(
                    f"Deck {self._serial()}: {len(self._attempts)} reopen attempts within "
                    f"{ATTEMPT_WINDOW_S:g}s did not bring the input reader back. Giving the "
                    f"deck up: it keeps its imagery but stays deaf to input. Replug it to "
                    f"recover."
                )
                return False
            self._attempts.append(now)
            self.attempts_started += 1
            self._in_flight = True
        # Outside the lock. submit_control only appends and wakes, and the
        # writer must never wait on this lock to finish an attempt.
        media_player.submit_control(ReopenDeckMsg(supervisor=self))
        return True

    def run_attempt(self, stopping: "Callable[[], bool]") -> bool:
        """Release the handle and open it again. Media thread only.

        stopping() reports that the writer is stopping, which cuts the retry
        loop short so a quit does not wait out the reopen deadline.

        This is on the media thread because the media thread is the sole
        device writer. _release_handle() raises nothing, but it closes under
        the wrapper's device lock, so a thread that cannot afford to wait for
        a write in flight must not call it. The writer owns those writes.
        """
        try:
            return self._reopen(stopping)
        except Exception:
            log.opt(exception=True).error(
                f"Deck {self._serial()}: the reader reopen attempt failed")
            return False
        finally:
            with self._lock:
                self._in_flight = False

    def _reopen(self, stopping: "Callable[[], bool]") -> bool:
        controller = self.controller
        deck = getattr(controller, "deck", None)
        if deck is None:
            return False
        # Re-check on this thread. The watchdog decided up to one sweep ago,
        # and a quit, an unplug teardown or the library's own recovery can
        # land in between. A reopen during teardown hands the next process a
        # busy device, and one over a live reader would join a thread that is
        # not stoppable from outside.
        if stopping() or not gl.threads_running or getattr(controller, "_closing", False):
            return False
        if not reader_is_dead(deck):
            self._note_success()
            return True
        if not _still_connected(deck):
            return False

        log.warning(
            f"Deck {self._serial()}: the input reader thread is gone while the device is "
            f"still connected, so the deck takes no input. Reopening the handle.")
        # Close before retry, through the release seam: the reader stops, the
        # handle takes no re-open from anywhere else, and only then does it
        # close. In the failure mode where the reader died with the handle
        # still open, this close is what makes the open below meaningful.
        controller._release_handle()

        deadline = time.monotonic() + REOPEN_DEADLINE_S
        while True:
            if stopping() or not gl.threads_running:
                return False
            try:
                # Through the seam, which lifts the release shadow the line
                # above installed. A bare open() on a released handle is
                # silently ignored. The transport's FIFO lock lives on the
                # Device instance, which the reopen reuses, so nothing has to
                # reinstall it.
                deck.open_handle()
                break
            except Exception as e:
                if not _still_connected(deck):
                    log.warning(
                        f"Deck {self._serial()}: the device left the bus during the reopen. "
                        f"The disconnect sweep owns it now.")
                    return False
                if time.monotonic() >= deadline:
                    log.warning(
                        f"Deck {self._serial()}: the handle did not open within "
                        f"{REOPEN_DEADLINE_S:g}s: {e}")
                    return False
                time.sleep(REOPEN_RETRY_GAP_S)

        if reader_is_dead(deck):
            log.warning(
                f"Deck {self._serial()}: the handle re-opened but the reader thread did not "
                f"start, so the deck still takes no input.")
            return False

        # The device lost its handle and took a new one, so no present state
        # describes what it shows any more. The repaint clears them itself
        # when it fires, but its rate limit can defer that by two seconds, and
        # a paint offered by any producer in between would be judged against
        # hashes that name what the deck showed before it went deaf, and
        # skipped as a repeat.
        controller._reset_dedup_hashes()
        controller._schedule_full_repaint()
        self._note_success()
        self.reopens += 1
        log.info(f"Deck {self._serial()}: the input reader is back and the deck repaints.")
        return True

    def _note_success(self) -> None:
        """Forget the attempt window after a deck comes back. A deck that dies
        once an hour must not accumulate its way into the give-up latch."""
        with self._lock:
            self._attempts.clear()

    def _log_given_up(self, now: float) -> None:
        """Say once per GIVE_UP_LOG_GAP_S that the deck is still down. Called
        under the lock."""
        if now - self._last_give_up_log < GIVE_UP_LOG_GAP_S:
            return
        self._last_give_up_log = now
        log.warning(
            f"Deck {self._serial()}: the input reader is still down and this deck was "
            f"given up. Replug it to recover.")

    def _serial(self) -> str:
        return getattr(self.controller, "_serial_number", None) or "unknown"


class DeckReaderWatchdog(threading.Thread):
    """The detection half: one thread that sweeps every registered controller.

    One thread serves the whole process, not one per deck, and it makes no
    device call of its own. reader_is_dead() answers from an attribute and a
    thread state, and only a deck that fails it pays for connected(), whose
    hardware implementation enumerates every HID device on the system. A
    healthy deck therefore costs two reads per sweep.

    The loop stops on app quit, which clears gl.threads_running before it
    closes any deck. That ordering is what keeps a sweep from submitting a
    reopen into a controller the quit path is closing, and the attempt itself
    re-checks the same flag on the media thread.
    """

    def __init__(self, deck_manager: "DeckManager"):
        super().__init__(name="DeckReaderWatchdog", daemon=True)
        self._deck_manager = deck_manager
        self._stop_event = threading.Event()
        # One supervisor per registered controller, keyed by the controller
        # itself, which hashes by identity. Watchdog thread only, so it takes
        # no lock, and each sweep drops the entries of controllers that left
        # the register, which is what keeps a closed deck from being held here.
        self._supervisors: "dict[DeckController, DeckReaderSupervisor]" = {}

    def run(self) -> None:
        while gl.threads_running and not self._stop_event.is_set():
            self._stop_event.wait(WATCHDOG_INTERVAL_S)
            if self._stop_event.is_set() or not gl.threads_running:
                return
            try:
                self.sweep()
            except Exception:
                # A sweep that raised must not take the watchdog with it. The
                # per-controller guard below catches the ordinary case; this
                # covers the walk itself.
                log.opt(exception=True).error("The deck reader watchdog sweep failed")

    def stop(self) -> None:
        """Stop the loop before the next sweep. Idempotent."""
        self._stop_event.set()

    def sweep(self) -> None:
        """One pass over the registered controllers. It is split out of run()
        so a scenario drives it with no thread, as the media writer's
        drain_control_queue is."""
        controllers = list(self._deck_manager.deck_controller)
        for gone in [c for c in self._supervisors if c not in controllers]:
            del self._supervisors[gone]
        for controller in controllers:
            try:
                self._check(controller)
            except Exception:
                log.opt(exception=True).debug(
                    "The deck reader watchdog could not check a controller")

    def supervisor_for(self, controller: "DeckController") -> DeckReaderSupervisor:
        """This controller's supervisor, created on first use."""
        supervisor = self._supervisors.get(controller)
        if supervisor is None:
            supervisor = DeckReaderSupervisor(controller)
            self._supervisors[controller] = supervisor
        return supervisor

    def _check(self, controller: "DeckController") -> None:
        deck = getattr(controller, "deck", None)
        if deck is None or getattr(controller, "_closing", False):
            return
        if not reader_is_dead(deck):
            return
        if not _still_connected(deck):
            # Unplugged. The USB disconnect sweep removes this controller, and
            # a reopen would race that teardown for the handle.
            return
        self.supervisor_for(controller).request_reopen()
