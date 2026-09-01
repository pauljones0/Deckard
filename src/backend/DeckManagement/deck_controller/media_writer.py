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

The media writer runs one MediaPlayerThread per deck, plus the units it
executes. That thread is the sole writer to its device. Every key image,
touchscreen image, brightness change and blank reaches the hardware from
inside its loop, so this module decides the ordering between them. A paint
carries the page and generation it was rendered for, and the write boundary
judges it stale. A control message, for brightness, clear, clear-and-close,
a stashed-input release or a reader reopen, has no page affinity, drains
first on every wake, and always executes, FIFO.

This module also holds the native JPEG encoders that every paint funnels
through, and the FIFO transport lock that stops a write burst from starving
the device's HID read poll. The device-write task values live beside it, so
the loop keeps its ordering vocabulary without growing past one module.
"""
import collections
import io
import itertools
import os
import threading
import time
from dataclasses import dataclass

from PIL import Image
from loguru import logger as log

from src.backend.DeckManagement.fair_lock import FairLock
from src.backend.DeckManagement.InputIdentifier import Input, InputIdentifier
from src.backend.DeckManagement.Subclasses.media_pipeline_profiler import media_prof
from src.backend.DeckManagement import media_loop
from src.backend.DeckManagement.media_loop import MEDIA_LOOP_FPS
from src.backend.DeckManagement.deck_controller.loop_metrics import WorkRateMonitor
from src.backend.DeckManagement.deck_controller.paint_protocol import PaintTicket
from src.backend.DeckManagement.deck_controller.media_tasks import (
    MediaPlayerSetImageTask,
    MediaPlayerSetTouchscreenImageTask,
)
from src.backend.DeckManagement.deck_controller.paint_queue import PaintQueue
from src.backend.PageManagement.Page import Page
from src.backend import ui_port

import globals as gl

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast, ParamSpec

_Params = ParamSpec("_Params")
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.deck_controller.input_latency import LatencySample
    from src.backend.DeckManagement.BetterDeck import BetterDeck
    from src.backend.DeckManagement.reader_supervisor import DeckReaderSupervisor
    from src.backend.DeckManagement.deck_controller.inputs import (
        ControllerDial,
        ControllerKey,
        ControllerTouchScreen,
    )
    from src.backend.DeckManagement.deck_controller.paint_protocol import PresentState


# JPEG quality for native key encoding; it is part of the tile-cache key.
KEY_ENCODE_QUALITY = 90


def encode_native_key(deck: "BetterDeck", image: "Image.Image", quality: int = KEY_ENCODE_QUALITY) -> bytes:
    """Encode a key in PILHelper's native format with configurable JPEG quality.

    The library fixes quality at 100; lower quality reduces serial USB HID writes.
    """
    fmt = deck.key_image_format()
    if image.size != fmt["size"]:
        image.thumbnail(fmt["size"])
    if fmt["rotation"]:
        image = image.rotate(fmt["rotation"])
    if fmt["flip"][0]:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if fmt["flip"][1]:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    with io.BytesIO() as buf:
        save_kwargs = {"quality": quality}
        if fmt["format"] == "JPEG":
            # Below q95, 4:2:0 adds about 4% desaturation and smear on busy 120 px tiles.
            # It halves chroma; force 4:4:4 at q90 for 17% more bytes and the same speed.
            save_kwargs["subsampling"] = 0
        image.save(buf, fmt["format"], **save_kwargs)
        return buf.getvalue()


def encode_native_touchscreen(deck: "BetterDeck", image: "Image.Image", quality: int = 90) -> bytes:
    """Encode a touchscreen at configurable quality without mutating the caller.

    Copying preserves UI reuse; lower quality cuts the largest device write and mutex hold.
    image is the strip in the frame the user sees, so the fit measures the logical size,
    the device buffer's transpose on a quarter-turned deck, which the turn then expands
    into. A wrongly sized composite lands well inside that buffer once turned, so the
    turned size is checked as well.
    """
    fmt = deck.touchscreen_image_format()
    if image.size != (logical_size := deck.logical_touchscreen_size() or fmt["size"]):
        image = image.copy()
        image.thumbnail(logical_size)
    if turn := (deck.touchscreen_image_rotation() + fmt["rotation"]) % 360:
        image = image.rotate(turn, expand=True)
    if image.size != fmt["size"]:
        raise ValueError(f"the strip composite is {image.size} after the turn, and the device buffer is {fmt['size']}")
    for axis, flip in zip((Image.Transpose.FLIP_LEFT_RIGHT, Image.Transpose.FLIP_TOP_BOTTOM), fmt["flip"]):
        if flip:
            image = image.transpose(axis)
    with io.BytesIO() as buf:
        save_kwargs = {"quality": quality}
        if fmt["format"] == "JPEG":
            # Force 4:4:4 because Pillow uses visibly desaturated 4:2:0 below
            # quality 95.
            save_kwargs["subsampling"] = 0
        image.save(buf, fmt["format"], **save_kwargs)
        return buf.getvalue()


@dataclass
class MediaPlayerTask:
    deck_controller: "DeckController"
    # Active page at submission, or None during boot and teardown; the write
    # boundary uses identity only, so a page-less task becomes stale.
    page: Page | None
    _callable: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]

    def run(self) -> None:
        self._callable(*self.args, **self.kwargs)

@dataclass
class SetBrightnessMsg:
    """Set deck brightness on the sole media-writer thread."""
    value: float


@dataclass
class ClearMsg:
    """Blank after dropping paints older than seq; later paints preserve clear-then-paint order.

    expects_repaint lets a late transition clear recover but keeps terminal blank states blank.
    """
    seq: int
    expects_repaint: bool = False


@dataclass
class ClearAndCloseMsg:
    """Drop pending paints, blank and close the device if possible, then stop the loop."""
    pass


@dataclass
class ReleaseStashedInputsMsg:
    """Close and clear stashed inputs as a page-independent FIFO control operation.

    Do not use add_task(); a page change can drop it before the screensaver releases the old page.
    """
    stashed_inputs: "dict[type[InputIdentifier], list[ControllerKey | ControllerDial | ControllerTouchScreen]]"


@dataclass
class ReopenDeckMsg:
    """Release and reopen a live device after its input reader thread dies.

    The sole writer does this because close waits for in-flight writes under the device lock.
    """
    supervisor: "DeckReaderSupervisor"


def _env_float(name: str, default: float) -> float:
    """Read an environment float, warning and returning default when malformed.

    This must not raise during writer initialization, which would prevent the deck from loading.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(f"Ignoring malformed {name}={raw!r}; using the default {default}")
        return default


def _install_fair_transport_lock(deck: Any) -> bool:
    """Install a FIFO transport mutex; this must run before deck.open().

    A missing device or mutex prevents install; a held mutex stays, and reopen keeps a FairLock.
    """
    device = getattr(deck, "device", None)
    if device is None:
        # A FakeDeck or a RemoteDeck has no HID transport to order.
        log.info(f"Deck {type(deck).__name__} has no transport device; "
                 f"keeping the stock lock")
        return False

    mutex = getattr(device, "mutex", None)
    if mutex is None:
        log.warning(f"Transport {type(device).__name__} has no mutex attribute; "
                    f"skipping the fair transport lock (library drift?)")
        return False

    if isinstance(mutex, FairLock):
        return True

    locked = getattr(mutex, "locked", None)
    if callable(locked) and locked():
        # Only reachable if this call ever moves after open(). A swap of a
        # held lock lets the holder and a new acquirer into hidapi together.
        log.warning("Transport mutex is held; skipping the fair transport lock")
        return False

    device.mutex = FairLock()
    log.info(f"Installed the fair (FIFO) transport lock on "
             f"{type(device).__name__}")
    return True


class MediaPlayerThread(threading.Thread):
    # Batches at or above this size are bulk repaints with inter-write yields;
    # smaller interactive batches write immediately.
    BULK_BATCH_THRESHOLD = 4
    # Within a bulk batch, yield after every N writes. See the batch loop in
    # perform_media_player_tasks.
    YIELD_STRIDE = 3
    # Quiet render ticks required before quiescence resumes after a generation
    # change; three full-FPS ticks cost about 100 ms while the user is away.
    GATE_SETTLE_TICKS = 3
    # Bound a gate window that otherwise rearms while queues remain nonempty.
    # A 10 Hz producer can hold it; expiry can leave transparent keys stale for one away window.
    GATE_WINDOW_MAX_S = 0.5

    def __init__(self, deck_controller: "DeckController"):
        # Include deck serial in the thread name to identify concurrent writers.
        try:
            _serial = deck_controller.serial_number()
        except Exception:
            _serial = "unknown"
        super().__init__(name=f"MediaPlayerThread-{_serial}", daemon=True)
        self.deck_controller: DeckController = deck_controller
        self.FPS = MEDIA_LOOP_FPS

        # Cap background-video renders and writes at the configured rate; zero disables it.
        # High-entropy content can produce about 270 key writes per second before deduplication.
        self._video_write_hz = _env_float("DECKARD_VIDEO_WRITE_HZ", 30.0)
        self._last_video_write = 0.0
        # Apply the same write budget to touchscreen frames so dial videos and
        # scrolling labels cannot starve HID reads.
        self._last_touch_write = 0.0

        # Optional bulk-batch inter-write delay; FIFO ordering makes the default
        # zero, while DECKARD_WRITE_YIELD_MS can enable pacing without a rebuild.
        self._inter_write_yield = _env_float("DECKARD_WRITE_YIELD_MS", 0.0) / 1000.0

        self.running = False
        self.media_ticks = 0
        # Ticks that skipped animation while the user was away; subtract from
        # media_ticks to get the number of rendered ticks.
        self.gated_ticks = 0
        # Settle-window render ticks make an open window observable while
        # gated_ticks does not advance.
        self.gate_window_ticks = 0
        # Generation seen while gated, remaining settle ticks, and monotonic
        # window deadline.
        self._gated_generation: int | None = None
        self._gate_render_ticks = 0
        self._gate_window_deadline = 0.0

        self._stop = False

        self.tasks: list[MediaPlayerTask] = []
        self.image_tasks: dict[int, MediaPlayerSetImageTask] = {}
        self.touchscreen_task: MediaPlayerSetTouchscreenImageTask | None = None
        # Serialize latest-paint slots so read-then-clear cannot lose a new task
        # after its enqueued hash was stamped.
        self._slot_lock = threading.Lock()
        self._paint_queue = PaintQueue(self)
        self._wake_event = threading.Event()

        # GIL-atomic deque operations need no extra lock; every wake drains
        # controls before animation or paint work.
        self.control_q: "collections.deque[SetBrightnessMsg | ClearMsg | ClearAndCloseMsg | ReleaseStashedInputsMsg | ReopenDeckMsg]" = collections.deque()
        # Keep submit ordering deck-wide so Clear can compare every target;
        # allocate stamps under _slot_lock with assignment to preserve latest wins.
        self._submit_seq = itertools.count()
        # Highest submit sequence whose task returned on the media thread;
        # Clear uses it to detect later paint attempts that ran before its blanks.
        self._max_executed_seq: int = -1

        # Wall-clock gap detection. A gap much larger than the loop's own
        # wait interval means the process suspended on a system sleep and then
        # resumed. See check_resume_gap().
        self._last_iter_ts: float = time.time()

        # Per-tick work-rate window and low-FPS warning state; loop_metrics
        # owns the semantics.
        self.metrics = WorkRateMonitor(
            self.FPS,
            gl.settings_manager.app().enable_fps_warnings,
            self.set_banner_revealed)

        # Suspend writes while the reader supervisor lacks a live handle; renders
        # continue for previews, while dropped writes avoid errors and repaint retries.
        self.device_writes_suspended: bool = False

        # Rate-limit loop-error logs so persistent failures cannot storm sinks;
        # the in-loop guard keeps the sole device writer alive.
        self._last_tick_error_log: float = 0.0
        self._suppressed_tick_errors: int = 0

    def run(self) -> None:
        self.running = True

        # Catch tick exceptions inside the loop so the sole writer survives;
        # @log.catch on run() would log once and let the thread exit.
        try:
            while True:
                try:
                    if not self._run_one_tick():
                        break
                except Exception:
                    now = time.time()
                    if now - self._last_tick_error_log >= 5.0:
                        suffix = (f" ({self._suppressed_tick_errors} earlier repeats were suppressed)"
                                  if self._suppressed_tick_errors else "")
                        log.opt(exception=True).error(
                            f"media writer tick failed -- survived, continuing{suffix}")
                        self._last_tick_error_log = now
                        self._suppressed_tick_errors = 0
                    else:
                        self._suppressed_tick_errors += 1
                    # A paint exception loses its failing paint and unrun siblings,
                    # not completed earlier paints; schedule the 2 s rate-limited repaint.
                    self.deck_controller._schedule_full_repaint()
                    # Back off for 250 ms without delaying stop; re-wait because
                    # producers also set _wake_event and could drive retry rate.
                    deadline = time.monotonic() + 0.25
                    while not self._stop:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._wake_event.wait(remaining)
                        self._wake_event.clear()
                    if self._stop:
                        break
        finally:
            # Clear running in finally so BaseException exits do not make
            # stop() wait on a dead thread.
            self.running = False

    def _open_gate_window(self) -> None:
        """Open rendering for GATE_SETTLE_TICKS quiet ticks, bounded by GATE_WINDOW_MAX_S.

        Media-writer thread only.
        """
        self._gate_render_ticks = self.GATE_SETTLE_TICKS
        self._gate_window_deadline = media_loop.now() + self.GATE_WINDOW_MAX_S

    def _run_one_tick(self) -> bool:
        """One iteration of the writer loop. Returns False to stop."""
        # Monotonic: gates, deadlines and waits must not follow wall steps.
        start = media_loop.now()

        # Drain controls before stop checks and all other tick work so terminal
        # close runs and persistent later failures cannot starve control messages.
        if not self.drain_control_queue():
            return False
        if self._stop:
            return False

        self.check_resume_gap(start)
        repaint_fired = self.deck_controller._run_pending_repaint()

        # After controls and stop checks, quiescence skips animation, decode,
        # composition, input ticks, and scrolling; queued paints still drain.
        gated = self.deck_controller.animations_gated()
        force_render = False
        gate_window_open = False
        if gated:
            # Gated generation changes render transparent video keys for required quiet ticks.
            # Page-load queues rearm the bounded window; _page_gen_lock supplies its snapshot.
            with self.deck_controller._page_gen_lock:
                current_gen = self.deck_controller._page_load_generation
            if current_gen != self._gated_generation:
                self._gated_generation = current_gen
                self._open_gate_window()
            if repaint_fired:
                # Resume and write-failure recovery repaints do not change generation;
                # open the window for their transparent video keys while the user is away.
                self._open_gate_window()
            if self._gate_render_ticks > 0:
                if media_loop.now() >= self._gate_window_deadline:
                    # Close at GATE_WINDOW_MAX_S even when a steady producer
                    # keeps queues nonempty.
                    self._gate_render_ticks = 0
                else:
                    gated = False
                    gate_window_open = True
                    self.gate_window_ticks += 1
                    # Force settle passes past video deadlines; the window bound
                    # limits how long a video can exceed its own rate.
                    force_render = True
                    if self.tasks or self.image_tasks or self.touchscreen_task:
                        self._gate_render_ticks = self.GATE_SETTLE_TICKS
                    else:
                        self._gate_render_ticks -= 1

        if gated:
            self.gated_ticks += 1

        # The FPS throttle below reads this even when paused.
        has_bg_video = False

        bg_strip_dirty = False
        video_repaint = False
        # True only on a tick the background rendered a new frame.
        bg_frame_new = False

        # Snapshot once, because Background.set_video(None) from another
        # thread must not null this between the check and the reads.
        video = self.deck_controller.background.video
        if video is not None and not gated:
            if video.page is self.deck_controller.active_page:
                has_bg_video = True
                # Gate video rendering, not only writes, to _video_write_hz.
                min_gap = 1.0 / self._video_write_hz if self._video_write_hz > 0 else 0
                if start - self._last_video_write >= min_gap:
                    video_repaint = True
                    self._last_video_write = start
                # The source deadline renders swaps immediately and skips work
                # between slow frames; force_render bypasses it for settle passes.
                if video_repaint and (force_render or video.frame_due(start)):
                    self.deck_controller.background.update_tiles()
                    bg_frame_new = True
                    # A video extended onto the strip needs the shared
                    # touchscreen re-composited for the new frame.
                    bg_strip_dirty = self.deck_controller.background.get_touchscreen_image() is not None

        # Advance due still-image slideshows on their load-seeded monotonic
        # clock; videos and single images no-op, and quiescence pauses it.
        if not gated:
            self.deck_controller.background.slideshow_tick()

        if not gated and (bg_frame_new or self._needs_key_ticks()):
            # Snapshot the complete inputs dict because screensaver swaps it
            # from another thread; use .get(), as _needs_key_ticks does.
            inputs = self.deck_controller.inputs
            for key in inputs.get(Input.Key, []):
                cast("ControllerKey", key).on_media_player_tick(start, bg_frame_new)

            # Dials and per-touchscreen videos share one strip, so composite it
            # at most once per frame.
            dials = inputs.get(Input.Dial, [])
            touchscreens = inputs.get(Input.Touchscreen, [])
            touchscreen_dirty = False
            for dial in dials:
                if cast("ControllerDial", dial).on_media_player_tick(start):
                    touchscreen_dirty = True
            for touchscreen in touchscreens:
                if cast("ControllerTouchScreen", touchscreen).on_media_player_tick(start):
                    touchscreen_dirty = True
            if (touchscreen_dirty or bg_strip_dirty) and touchscreens:
                cast("ControllerTouchScreen", touchscreens[0]).update()

        self.perform_media_player_tasks()

        self.media_ticks += 1

        end = media_loop.now()

        if media_prof:
            media_prof.add("tick", end - start)
            media_prof.maybe_report()

        # Queued paints and control-adjacent work stay full-rate; gate beats cached animation.
        # The 2 Hz gate stays below the 5 s resume-gap threshold; unlocked reads affect one tick.
        has_pending = bool(self.tasks or self.image_tasks or self.touchscreen_task)
        if has_pending:
            target_fps = self.FPS
        elif gate_window_open:
            # Run settle windows at full FPS because 2 Hz can sleep through the
            # bound and leave transparent keys unpainted after early passes.
            target_fps = self.FPS
        elif gated:
            target_fps = 2
        elif has_bg_video or getattr(self, '_cached_needs_ticks', False):
            target_fps = self.FPS
        else:
            target_fps = 2

        # Recorded before the wait below on purpose: the value is this
        # tick's work-rate, not the achieved cadence.
        self.metrics.record(1 / (end - start))
        self.metrics.update_warning()
        wait = max(0, 1/target_fps - (end - start))
        # Event waits let controls and interactive paints wake either path immediately.
        self._wake_event.wait(wait)
        self._wake_event.clear()

        # Check _stop only after the next control drain so stop cannot strand
        # a terminal message.
        return True

    def next_submit_seq(self) -> int:
        """Allocate the next deck-wide submit sequence for paints and Clear snapshots."""
        return next(self._submit_seq)

    def submit_control(self, msg: "SetBrightnessMsg | ClearMsg | ClearAndCloseMsg | ReleaseStashedInputsMsg | ReopenDeckMsg") -> bool:
        """Append and wake without blocking; GIL-atomic deque append is safe from any thread.

        Reject after stop because no loop can drain it; callers can then clear in-flight state.
        """
        if self._stop:
            return False
        self.control_q.append(msg)
        self._wake_event.set()
        return True

    def drain_control_queue(self) -> bool:
        """Run controls FIFO until terminal ClearAndClose runs, then return False.

        Later messages stay undrained; this also works without a running writer.
        """
        while self.control_q:
            msg = self.control_q.popleft()
            if isinstance(msg, SetBrightnessMsg):
                self._exec_set_brightness(msg)
            elif isinstance(msg, ClearMsg):
                self._exec_clear(msg)
            elif isinstance(msg, ClearAndCloseMsg):
                self._exec_clear_and_close()
                return False
            elif isinstance(msg, ReleaseStashedInputsMsg):
                self._exec_release_stashed_inputs(msg)
            elif isinstance(msg, ReopenDeckMsg):
                # Reopen blocks for at most the supervisor deadline; paints lack a valid handle.
                # _stop shortens it after the current open and one retry gap.
                msg.supervisor.run_attempt(stopping=lambda: self._stop)
        return True

    def _exec_set_brightness(self, msg: "SetBrightnessMsg") -> None:
        # Write directly to avoid DeckController.set_brightness() resubmission;
        # attempt, report through _on_write_result, and swallow failures.
        if self.device_writes_suspended:
            return
        try:
            self.deck_controller.deck.set_brightness(int(msg.value))
            self.deck_controller._on_write_result(True)
        except Exception as e:
            log.error(f"Failed to set brightness: {e}")
            self.deck_controller._on_write_result(False)

    def _exec_release_stashed_inputs(self, msg: "ReleaseStashedInputsMsg") -> None:
        """Close stashed resources on the media thread, serialized with renders and writes.

        This page-independent control must not use add_task().
        """
        stashed_inputs = msg.stashed_inputs
        for inputs in list(stashed_inputs.values()):
            for controller_input in list(inputs):
                try:
                    controller_input.close_resources()
                except Exception:
                    log.opt(exception=True).warning(
                        "Failed to close a stashed screensaver input (ReleaseStashedInputsMsg)"
                    )
        stashed_inputs.clear()

    def check_resume_gap(self, now: float | None = None) -> bool:
        """Detect a wall-clock gap of 5s or more between media-loop
        iterations, which is the signature of a process suspend and resume
        cycle. It is split out of run() so a unit-tier scenario drives it
        without a running thread, as drain_control_queue is. Returns whether
        it detected a gap, and not whether a repaint fired, because
        _schedule_full_repaint() applies its own rate limit."""
        if now is None:
            now = time.time()
        gap = now - self._last_iter_ts
        self._last_iter_ts = now
        if gap >= 5.0:
            log.info(f"Media loop observed a {gap:.1f}s gap since its last iteration "
                      f"(likely a suspend/resume); scheduling a full repaint.")
            self.deck_controller._schedule_full_repaint()
            return True
        return False

    def _exec_clear(self, msg: "ClearMsg") -> None:
        # Under _slot_lock, discard only paints older than Clear so later work
        # survives and concurrent assignments, including touchscreen, cannot be deleted.
        self._paint_queue.discard_before_clear(msg.seq)
        # Reset current-input hashes before blanking so identical repaints are
        # not deduplicated against pre-clear content.
        self.deck_controller._reset_dedup_hashes()
        try:
            self.deck_controller._write_blank_frames()
        except Exception as e:
            log.error(f"Failed to write blank frames for Clear: {e}")

        # A late transition Clear can blank a later paint; require intent and a higher executed seq.
        # Ordinary clears stay blank; queue occupancy cannot prove that the later paint ran.
        if msg.expects_repaint and self._max_executed_seq > msg.seq:
            self.deck_controller._schedule_full_repaint()

    def _exec_clear_and_close(self) -> None:
        # Set _stop before terminal work so late controls are rejected before
        # the queue becomes undrained.
        self._stop = True
        self._paint_queue.discard_all("terminal_clear")
        self.deck_controller._reset_dedup_hashes()
        try:
            self.deck_controller._write_blank_frames()
        except Exception as e:
            log.error(f"Failed to write blank frames during ClearAndClose: {e}")
        # A release, never a bare close: the reader thread must stop first, or
        # its resume loop re-opens the handle this just gave back.
        self.deck_controller._release_handle()

    def _needs_key_ticks(self) -> bool:
        # True when an input has animated content that advances on the media
        # tick, such as a key or dial video, or a scrolling label.
        needs = False
        for key in self.deck_controller.inputs.get(Input.Key, []):
            state = key.get_active_state()
            if state.key_video is not None or state.label_manager.get_has_scroll_labels():
                needs = True
                break
        if not needs:
            for dial in self.deck_controller.inputs.get(Input.Dial, []):
                state = dial.get_active_state()
                if state.video is not None or state.label_manager.get_has_scroll_labels():
                    needs = True
                    break
        if not needs:
            for touchscreen in self.deck_controller.inputs.get(Input.Touchscreen, []):
                state = touchscreen.get_active_state()
                if state is not None and state.background_video is not None:
                    needs = True
                    break
        self._cached_needs_ticks = needs
        return needs

    def set_show_fps_warnings(self, state: bool) -> None:
        self.metrics.set_enabled(state)

    def set_banner_revealed(self, state: bool) -> None:
        ui_port.get().set_low_fps_warning(self.deck_controller, state)


    def wake(self) -> None:
        """Wake the media loop without work; Event.set() makes this safe from any thread.

        External gate-state transitions use this so the next tick re-evaluates quiescence.
        """
        self._wake_event.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop = True
        self._wake_event.set()  # wake an idle loop so it sees _stop promptly
        start = time.time()
        while self.running and time.time() - start < timeout:
            time.sleep(0.05)

    def add_task(self, method: Callable[_Params, object], *args: _Params.args, **kwargs: _Params.kwargs) -> None:
        self.tasks.append(MediaPlayerTask(
            deck_controller=self.deck_controller,
            page=self.deck_controller.active_page,
            _callable=method,
            args=args,
            kwargs=kwargs
        ))
        self._wake_event.set()

    def add_touchscreen_task(self, native_image: bytes, page: "Page | None" = None, config_gen: "int | None" = None, present: "PresentState | None" = None, img_hash: "int | None" = None, latency_sample: "LatencySample | None" = None) -> None:
        task = MediaPlayerSetTouchscreenImageTask(
            deck_controller=self.deck_controller,
            ticket=PaintTicket(
                present=present,
                page=page if page is not None else self.deck_controller.active_page,
                config_gen=config_gen,
                native_image=native_image,
                img_hash=img_hash,
                latency_sample=latency_sample,
            ),
        )
        # Allocate touchscreen sequence with assignment under _slot_lock so
        # racing producers preserve latest-wins order and Clear sees consistent state.
        self._paint_queue.replace_touchscreen(task, self.next_submit_seq)
        self._wake_event.set()

    def add_image_task(self, key_index: int, native_image: bytes, page: "Page | None" = None, config_gen: "int | None" = None, present: "PresentState | None" = None, img_hash: "int | None" = None, latency_sample: "LatencySample | None" = None) -> None:
        task = MediaPlayerSetImageTask(
            deck_controller=self.deck_controller,
            ticket=PaintTicket(
                present=present,
                page=page if page is not None else self.deck_controller.active_page,
                config_gen=config_gen,
                native_image=native_image,
                img_hash=img_hash,
                latency_sample=latency_sample,
            ),
            key_index=key_index,
        )
        # Stamp inside the lock as add_touchscreen_task does. The per-key
        # slots have the same producer-against-producer shape.
        self._paint_queue.replace_image(key_index, task, self.next_submit_seq)
        self._wake_event.set()

    def discard_paint_tasks(self, reason: str) -> None:
        """Remove every queued paint and name the terminal measurement reason."""
        self._paint_queue.discard_all(reason)

    def perform_media_player_tasks(self) -> None:
        # Drain before the page-generation snapshot so every task predates it;
        # the reverse order can drop a new-page task queued after the snapshot.
        task_batch = self.tasks.copy()
        for task in task_batch:
            try:
                self.tasks.remove(task)
            except ValueError:
                pass

        image_batch = []
        for key in list(self.image_tasks.keys()):
            try:
                image_batch.append(self.image_tasks.pop(key))
            except KeyError:
                continue

        # Take _slot_lock so read-then-clear cannot lose a touchscreen frame
        # assigned after its enqueued hash was stamped.
        touch_task = self._paint_queue.take_touchscreen()

        # Snapshot page and generation together under the lock used by load_page.
        with self.deck_controller._page_gen_lock:
            active_page = self.deck_controller.active_page
            current_gen = self.deck_controller._page_load_generation

        def _is_current(task: "MediaPlayerSetImageTask | MediaPlayerSetTouchscreenImageTask") -> bool:
            # Drop a paint for a page the deck left, or for a superseded
            # generation. config_gen is the generation the paint rendered at.
            ticket = task.ticket
            if ticket.page is not active_page:
                return False
            if ticket.config_gen is not None and ticket.config_gen != current_gen:
                return False
            return True

        for task in task_batch:
            try:
                if task.page is active_page:
                    task.run()
            except Exception:
                pending: list[MediaPlayerSetImageTask | MediaPlayerSetTouchscreenImageTask] = list(image_batch)
                if touch_task is not None:
                    pending.append(touch_task)
                self._paint_queue.account_writer_tick_exception(pending)
                raise

        # Drop writes while no handle is live to avoid repeated failures and
        # repaint retries; previews remain, and reopen resets state before repaint.
        if self.device_writes_suspended:
            for image_task in image_batch:
                image_task.ticket.discarded(self.deck_controller, "device_writes_suspended")
            if touch_task is not None:
                touch_task.ticket.discarded(self.deck_controller, "device_writes_suspended")
            return

        # YIELD_STRIDE paces bulk writes; interactive batches never yield.
        # Per-write yields cost about 12 ms per high-entropy frame at 19 FPS.
        bulk = len(image_batch) >= self.BULK_BATCH_THRESHOLD
        writes_since_yield = 0
        for index, image_task in enumerate(image_batch):
            if _is_current(image_task):
                if bulk and writes_since_yield >= self.YIELD_STRIDE and self._inter_write_yield > 0:
                    time.sleep(self._inter_write_yield)
                    writes_since_yield = 0
                try:
                    image_task.run()
                except Exception:
                    pending: list[MediaPlayerSetImageTask | MediaPlayerSetTouchscreenImageTask] = list(image_batch[index:])
                    if touch_task is not None:
                        pending.append(touch_task)
                    self._paint_queue.account_writer_tick_exception(pending)
                    raise
                self._note_executed(image_task)
                writes_since_yield += 1
            else:
                image_task.ticket.discarded(self.deck_controller, "stale_paint")

        if touch_task is not None and _is_current(touch_task):
            # Share the video cap across background, dial, scrolling-label, and interactive paints.
            # Return only the newest over-budget frame; shared timing delays it at most one window.
            now = media_loop.now()
            min_gap = 1.0 / self._video_write_hz if self._video_write_hz > 0 else 0
            if min_gap and now - self._last_touch_write < min_gap:
                # Under _slot_lock, put back this frame only if no newer
                # producer filled the slot.
                self._paint_queue.defer_rate_limited_touchscreen(touch_task)
            else:
                self._last_touch_write = now
                if bulk and writes_since_yield >= self.YIELD_STRIDE and self._inter_write_yield > 0:
                    time.sleep(self._inter_write_yield)
                try:
                    touch_task.run()
                except Exception:
                    self._paint_queue.account_writer_tick_exception([touch_task])
                    raise
                self._note_executed(touch_task)
        elif touch_task is not None:
            touch_task.ticket.discarded(self.deck_controller, "stale_paint")

    def _note_executed(self, task: "MediaPlayerSetImageTask | MediaPlayerSetTouchscreenImageTask") -> None:
        """Advance executed sequence after return; stale or deferred paints do not count.

        Swallowed errors count; _on_write_result(False) schedules recovery; media thread only.
        """
        seq = task.submit_seq
        if seq is not None and seq > self._max_executed_seq:
            self._max_executed_seq = seq
