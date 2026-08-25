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

The watchdog thread only detects. It sweeps the registered controllers, and
for a healthy deck its predicate costs one attribute read and one thread
state. Only a deck that fails the predicate is asked whether it is still
connected, which on real hardware enumerates the HID bus.

The media thread performs. It is the sole device writer, so it is the only
thread that may close and re-open a handle, and the only one that can afford
to wait on the device lock a wedged write holds. The watchdog submits a
ReopenDeckMsg, and the writer runs the attempt in its control drain.

The recovery policy is a count of consecutive attempts, not a rate.

An attempt counts until a reopen has held, which means its reader stayed
alive for HOLD_WINDOW_S. Five attempts that never held mean this deck is not
recovering, and it is given up for good. A window of attempts per unit of
time cannot state that: attempts are serialized by the media thread and
spaced by the sweep, so at the shipped deadline they arrive too slowly to
fill any window, and a deck that reopens and dies again forever resets that
window on every success. Both of those read as healthy to a rate cap, and
both are exactly the failure this exists to bound.

A deck that is given up is left alone in full. Its handle stays closed, and
the writer drops every device write instead of raising into the error path,
which would arm a full repaint every two seconds for the rest of the session
and bury the give-up line in the log. Renders still run, so the window
previews stay live, and the deck's screens keep the last picture they were
given.
"""
import threading
import time

from loguru import logger as log

from src.backend.DeckManagement.deck_controller.media_writer import ReopenDeckMsg

import globals as gl

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

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
# How often one attempt may ask whether the device is still on the bus. The
# answer costs a full HID enumeration on real hardware, taken under the same
# transport lock every write waits on, so the retry loop asks at this rate
# instead of once per try.
CONNECTED_PROBE_GAP_S = 1.0
# Consecutive attempts that never held before this deck is given up. The
# count is a plain counter and not a rate, so it is independent of every
# timing constant above; see the module docstring.
MAX_CONSECUTIVE_ATTEMPTS = 5
# How long a reopened reader must stay alive before the attempt that made it
# counts as held and the consecutive count goes back to zero. A deck that
# reopens and loses its reader again inside this window is flapping, and its
# attempts keep accumulating.
HOLD_WINDOW_S = 30.0
# Rate limit on the log line that a given-up deck stays down. The predicate
# below stays true for as long as the deck lives, so an unlimited line here
# would repeat once per sweep for the rest of the session.
GIVE_UP_LOG_GAP_S = 60.0

# Escalation hook, installed once at wiring time through
# set_give_up_escalation(). The give-up latch calls it with the controller,
# on the thread that latched and outside every lock. A recovery step beyond
# a reopen, such as a targeted reset of the USB device, layers on here
# instead of racing the latch. A step that may have changed the device hands
# the deck back through allow_one_more_round(). It must return promptly,
# because the watchdog sweep waits for it; anything it raises is logged and
# dropped.
_give_up_escalation: "Callable[[DeckController], None] | None" = None


def set_give_up_escalation(callback: "Callable[[DeckController], None] | None") -> None:
    """Install the escalation hook, or clear it with None. See
    _give_up_escalation."""
    global _give_up_escalation
    _give_up_escalation = callback


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
    """One deck's recovery state: the consecutive-attempt count, the hold a
    successful reopen must serve, the in-flight marker and the give-up latch.

    The watchdog thread decides whether to submit an attempt and the media
    thread runs it, so both touch these fields. One lock covers them. Every
    critical section is a few statements of bookkeeping, with no device call
    and no logging inside it, so no thread waits here on a device or a log
    sink.
    """

    def __init__(self, controller: "DeckController"):
        self.controller = controller
        self._lock = threading.Lock()
        # Attempts since the last one that held. Public, because the
        # scenarios read it; written under the lock.
        self.consecutive_attempts = 0
        # Monotonic instant the current reopen counts as held, or None when
        # no reopen is waiting to prove itself.
        self._hold_deadline: float | None = None
        self._in_flight = False
        # Latched. The deck stays down until a replug builds a new controller.
        self.given_up = False
        self._last_give_up_log = 0.0
        # Whether this deck's handle is down, so the writer must drop device
        # writes. It is a plain flag read on the writer's hot path and mirrored
        # onto the writer itself by _set_handle_down, so no tick takes this
        # lock.
        self._handle_down = False
        # Counters for the scenarios and for a field log read.
        self.attempts_started = 0
        self.reopens = 0
        self.holds = 0

    def attempt_in_flight(self) -> bool:
        """Whether an attempt is queued on the writer or running there. The
        watchdog submits no second attempt while it is."""
        with self._lock:
            return self._in_flight

    def request_reopen(self) -> bool:
        """Submit one reopen attempt to the media thread, and report whether
        it was submitted. Watchdog thread only.

        It refuses while an attempt is in flight, after the give-up latch,
        when the deck has no running writer to perform the attempt, and when
        MAX_CONSECUTIVE_ATTEMPTS attempts in a row have failed to hold, which
        is where it latches.
        """
        media_player = getattr(self.controller, "media_player", None)
        if media_player is None or not media_player.running:
            # Nothing would drain the message. A controller in this state is
            # being torn down or failed to build, and neither is this
            # supervisor's business.
            return False
        now = time.monotonic()
        latch_now = False
        with self._lock:
            if self.given_up or self._in_flight:
                return False
            if self.consecutive_attempts >= MAX_CONSECUTIVE_ATTEMPTS:
                self.given_up = True
                self._last_give_up_log = now
                latch_now = True
            else:
                self._in_flight = True
        if latch_now:
            # Outside the lock: the handle mirror reaches into the writer, the
            # log line reaches a sink, and the hook is third-party code.
            self._set_handle_down(True)
            log.error(
                f"Deck {self._serial()}: {MAX_CONSECUTIVE_ATTEMPTS} reopen attempts in a "
                f"row did not bring the input reader back. This deck is now left alone: "
                f"it takes no input, it receives no more writes, and its screens keep "
                f"the last picture they were given. Replug it to recover.")
            self._escalate()
            return False
        if not media_player.submit_control(ReopenDeckMsg(supervisor=self)):
            # The writer stopped between the check above and here, so nothing
            # will ever drain this message. Give the reservation back, and
            # count no attempt: no reopen was tried.
            with self._lock:
                self._in_flight = False
            return False
        with self._lock:
            self.consecutive_attempts += 1
            self.attempts_started += 1
            # A new attempt supersedes the hold the previous one was serving.
            self._hold_deadline = None
        return True

    def note_reader_alive(self) -> None:
        """Settle a hold once its reader has stayed alive long enough.
        Watchdog thread only.

        A reopen does not clear the attempt count on its own. The reader it
        started has to survive HOLD_WINDOW_S first, or a deck that reopens and
        dies again forever would never accumulate a count and never be given
        up.
        """
        with self._lock:
            deadline = self._hold_deadline
            if deadline is None or time.monotonic() < deadline:
                return
            self._hold_deadline = None
            self.consecutive_attempts = 0
            self.holds += 1
        log.info(f"Deck {self._serial()}: the reopened input reader held; "
                 f"the attempt count is clear.")

    def allow_one_more_round(self) -> None:
        """Lift the give-up latch and clear the attempt count, so the policy
        allows one more run of attempts. Watchdog thread only.

        The give-up escalation calls it after a step that may have changed the
        device, such as a targeted USB reset. It is the only way back from a
        give-up short of a replug, and it starts no attempt of its own: the
        next sweep decides whether the deck still needs one, and the same cap
        bounds the round it starts. The handle stays down and device writes
        stay suspended until an attempt opens the handle again.
        """
        with self._lock:
            self.given_up = False
            self.consecutive_attempts = 0
            self._hold_deadline = None
        log.warning(f"Deck {self._serial()}: the deck was given up and then reset, so it "
                    f"takes one more round of reopen attempts.")

    def note_still_down(self) -> None:
        """Say, at most once per GIVE_UP_LOG_GAP_S, that a given-up deck is
        still down. Watchdog thread only."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_give_up_log < GIVE_UP_LOG_GAP_S:
                return
            self._last_give_up_log = now
        log.warning(
            f"Deck {self._serial()}: the input reader is still down and this deck was "
            f"given up. It takes no input and receives no writes. Replug it to recover.")

    def run_attempt(self, stopping: "Callable[[], bool]") -> bool:
        """Release the handle and open it again. Media thread only.

        stopping() reports that the writer is stopping. It ends the retry loop
        at its next turn, so a quit waits at most for the open in flight and
        one retry gap, not for the whole reopen deadline.

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
            self._set_handle_down(True)
            return False
        finally:
            with self._lock:
                self._in_flight = False

    def _reopen(self, stopping: "Callable[[], bool]") -> bool:
        controller = self.controller
        deck = getattr(controller, "deck", None)
        if deck is None:
            return False

        def still_wanted() -> bool:
            # Re-checked on this thread, and again under the device lock right
            # before the open. The watchdog decided up to one sweep ago, and a
            # quit or an unplug teardown can land in between. close() empties
            # the control queue and releases the handle, so a reopen that ran
            # afterwards would lift the shadow that teardown installed and hand
            # the next process a busy device.
            return (not stopping() and gl.threads_running
                    and not getattr(controller, "_closing", False))

        if not still_wanted():
            return False
        if not reader_is_dead(deck):
            # The library's own recovery won the race. Nothing to do, and the
            # hold this arms is what clears the attempt count.
            self._arm_hold()
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
        self._set_handle_down(True)

        deadline = time.monotonic() + REOPEN_DEADLINE_S
        last_probe = 0.0
        while True:
            if not still_wanted():
                return False
            try:
                # Through the seam, which lifts the release shadow the line
                # above installed and re-checks still_wanted() under the device
                # lock. A bare open() on a released handle is silently ignored.
                # The transport's FIFO lock lives on the Device instance, which
                # the reopen reuses, so nothing has to reinstall it.
                if not deck.open_handle(guard=still_wanted):
                    return False
                break
            except Exception as e:
                now = time.monotonic()
                if now - last_probe >= CONNECTED_PROBE_GAP_S:
                    last_probe = now
                    if not _still_connected(deck):
                        log.warning(
                            f"Deck {self._serial()}: the device left the bus during the "
                            f"reopen. The disconnect sweep owns it now.")
                        return False
                if now >= deadline:
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

        self._set_handle_down(False)
        # A gesture that was in flight when the reader died has no release to
        # collect: the physical up event went nowhere. Without this, the first
        # callback after the reopen dispatches a hold stop or an up into the
        # snapshot taken before the outage, against a page the deck may have
        # left. The touchscreen inherits the gesture fields but never populates
        # them, so its cancel clears state that is already clear.
        self._cancel_gestures()
        # The device lost its handle and took a new one, so no present state
        # describes what it shows any more. The repaint clears them itself
        # when it fires, but its rate limit can defer that by two seconds, and
        # a paint offered by any producer in between would be judged against
        # hashes that name what the deck showed before it went deaf, and
        # skipped as a repeat.
        controller._reset_dedup_hashes()
        controller._schedule_full_repaint()
        self._arm_hold()
        self.reopens += 1
        log.info(f"Deck {self._serial()}: the input reader is back and the deck repaints. "
                 f"It counts as recovered once it holds for {HOLD_WINDOW_S:g}s.")
        return True

    def _arm_hold(self) -> None:
        """Start the window this reopen has to survive before it clears the
        attempt count. note_reader_alive() settles it."""
        with self._lock:
            self._hold_deadline = time.monotonic() + HOLD_WINDOW_S

    def _cancel_gestures(self) -> None:
        # Read the input dict once. The screensaver swaps the whole dict from
        # another thread, so a second read can sweep a different input set and
        # leave a gesture armed on the one it missed. Every input type is
        # swept: the touchscreen dispatches no gesture, so its cancel clears
        # state that is already clear.
        for controller_inputs in self.controller.inputs.values():
            for controller_input in controller_inputs:
                controller_input.cancel_gesture()

    def _set_handle_down(self, down: bool) -> None:
        """Record that the handle is down or back, and mirror it onto the
        writer, which drops every device write while it is down. Both are
        plain flag stores, so the writer's hot path takes no lock and a torn
        read is not possible."""
        self._handle_down = down
        media_player = getattr(self.controller, "media_player", None)
        if media_player is not None:
            media_player.device_writes_suspended = down

    def _escalate(self) -> None:
        escalation = _give_up_escalation
        if escalation is None:
            return
        try:
            escalation(self.controller)
        except Exception:
            log.opt(exception=True).error(
                f"Deck {self._serial()}: the give-up escalation hook raised")

    def _serial(self) -> str:
        return cast(str, getattr(self.controller, "_serial_number", None) or "unknown")


class DeckReaderWatchdog(threading.Thread):
    """The detection half: one thread that sweeps every registered controller.

    One thread serves the whole process, not one per deck. A healthy deck
    costs an attribute read and a thread state per sweep, and nothing else:
    connected() is asked only about a deck whose reader has exited, and never
    about one that was already given up, because that answer enumerates the
    HID bus under the same transport lock every device write waits on.

    The loop stops on app quit, which clears gl.threads_running before it
    closes any deck, and on stop(), which DeckManager calls where it stops the
    USB monitor. That ordering is what keeps a sweep from submitting a reopen
    into a controller the quit path is closing, and the attempt itself
    re-checks the same flag on the media thread and again under the device
    lock.
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
        supervisor = self._supervisors.get(controller)
        if not reader_is_dead(deck):
            if supervisor is not None:
                # A live reader is the only thing that settles a hold, and
                # this is the sweep that sees it.
                supervisor.note_reader_alive()
            return
        if supervisor is not None and supervisor.given_up:
            # Ahead of the connectivity probe on purpose. A given-up deck
            # stays down for the rest of the session, and a probe per sweep
            # would enumerate the HID bus every two seconds for nothing, under
            # the transport lock every other deck's writes queue on.
            supervisor.note_still_down()
            return
        if not _still_connected(deck):
            # Unplugged. The USB disconnect sweep removes this controller, and
            # a reopen would race that teardown for the handle.
            return
        self.supervisor_for(controller).request_reopen()
