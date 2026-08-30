"""Exact, opt-in timestamps for a physical input's first visible response."""
from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, cast
from collections.abc import Callable

from loguru import logger as log
from StreamDeck.Devices.StreamDeck import DialEventType

from src.backend.DeckManagement.InputIdentifier import Input, InputIdentifier


_STAGES = (
    "action_start",
    "render_start",
    "paint_enqueued",
    "writer_started",
    "usb_present",
    "gtk_paint",
)


@dataclass(frozen=True, slots=True)
class InputLatencyRun:
    """The explicit opt-in configuration for one hardware capture."""

    report_dir: Path
    run_id: str
    video_saturated: bool
    video_page: str | None

    @classmethod
    def from_environment(cls) -> "InputLatencyRun | None":
        report_dir = os.environ.get("DECKARD_INPUT_LATENCY_REPORT_DIR")
        run_id = os.environ.get("DECKARD_INPUT_LATENCY_RUN_ID")
        if report_dir is None and run_id is None:
            return None
        if not report_dir or not run_id:
            raise ValueError(
                "DECKARD_INPUT_LATENCY_REPORT_DIR and "
                "DECKARD_INPUT_LATENCY_RUN_ID must be set together"
            )
        return cls(
            report_dir=Path(report_dir),
            run_id=run_id,
            video_saturated=os.environ.get("DECKARD_INPUT_LATENCY_VIDEO_SATURATED") == "1",
            video_page=os.environ.get("DECKARD_INPUT_LATENCY_VIDEO_PAGE"),
        )


@dataclass(eq=False, slots=True)
class LatencySample:
    """One opaque physical-input token carried through every measured stage."""

    sequence: int
    input_at: float
    _owner: object
    action_start_at: float | None = None
    render_start_at: float | None = None
    paint_enqueued_at: float | None = None
    writer_started_at: float | None = None
    usb_present_at: float | None = None
    gtk_paint_at: float | None = None
    paint_enqueued_frames: int = 0
    writer_started_frames: int = 0
    usb_present_frames: int = 0
    gtk_paint_frames: int = 0
    drop_reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _LatencySnapshot:
    sequence: int
    input_at: float
    action_start_at: float | None
    render_start_at: float | None
    paint_enqueued_at: float | None
    writer_started_at: float | None
    usb_present_at: float | None
    gtk_paint_at: float | None
    paint_enqueued_frames: int
    writer_started_frames: int
    usb_present_frames: int
    gtk_paint_frames: int
    drop_reasons: tuple[tuple[str, int], ...]


class InputLatencyTracker:
    """Collect exact sample tokens and conservative completed-only summaries.

    A sample is never re-resolved from a target object. The physical callback
    creates it, synchronous input work carries it in this tracker's
    ``ContextVar``, and asynchronous writer/UI hand-offs carry the token in
    their immutable payloads. Tokens from another tracker are ignored rather
    than relying on an object id that a retired input could reuse.
    """

    def __init__(self, *, clock: Callable[[], float] = time.perf_counter,
                 max_samples: int = 10_000) -> None:
        self._clock = clock
        self._max_samples = max_samples
        self._owner = object()
        self._next_sequence = 0
        self._samples: list[LatencySample] = []
        self._untracked_samples = 0
        self._lock = threading.Lock()
        self._current: ContextVar[LatencySample | None] = ContextVar(
            "input_latency_sample", default=None)

    def input_received(self) -> LatencySample | None:
        """Create a token at the real HID callback boundary.

        The bounded collector fails closed: beyond its capacity, the report
        records untracked inputs and becomes invalid for comparison instead of
        dropping them from a favorable percentile population.
        """
        with self._lock:
            self._next_sequence += 1
            if len(self._samples) >= self._max_samples:
                self._untracked_samples += 1
                return None
            sample = LatencySample(
                sequence=self._next_sequence,
                input_at=self._clock(),
                _owner=self._owner,
            )
            self._samples.append(sample)
            return sample

    def current_sample(self) -> LatencySample | None:
        sample = self._current.get()
        return sample if self._owns(sample) else None

    @contextmanager
    def correlation(self, sample: LatencySample | None) -> Iterator[None]:
        """Install one captured token for synchronous or worker-thread work."""
        if not self._owns(sample):
            yield
            return
        token: Token[LatencySample | None] = self._current.set(sample)
        try:
            yield
        finally:
            self._current.reset(token)

    def run_with_sample(self, sample: LatencySample | None,
                        callback: Callable[..., Any], *args: Any,
                        **kwargs: Any) -> Any:
        """Run a captured callback under its original physical-input token."""
        with self.correlation(sample):
            return callback(*args, **kwargs)

    def action_started(self, sample: LatencySample | None) -> None:
        self._mark(sample, "action_start_at")

    def render_started(self, sample: LatencySample | None) -> None:
        self._mark(sample, "render_start_at")

    def paint_enqueued(self, sample: LatencySample | None) -> None:
        self._mark(sample, "paint_enqueued_at", "paint_enqueued_frames")

    def writer_started(self, sample: LatencySample | None) -> None:
        self._mark(sample, "writer_started_at", "writer_started_frames")

    def usb_presented(self, sample: LatencySample | None) -> None:
        self._mark(sample, "usb_present_at", "usb_present_frames")

    def gtk_painted(self, sample: LatencySample | None) -> None:
        self._mark(sample, "gtk_paint_at", "gtk_paint_frames")

    def drop(self, sample: LatencySample | None, reason: str) -> None:
        """Record one lost frame without pre-judging its physical sample."""
        if not self._owns(sample):
            return
        assert sample is not None
        with self._lock:
            sample.drop_reasons[reason] = sample.drop_reasons.get(reason, 0) + 1

    def _owns(self, sample: LatencySample | None) -> bool:
        return sample is not None and sample._owner is self._owner

    def _mark(self, sample: LatencySample | None, field: str,
              frame_counter: str | None = None) -> None:
        if not self._owns(sample):
            return
        assert sample is not None
        with self._lock:
            if getattr(sample, field) is None:
                setattr(sample, field, self._clock())
            if frame_counter is not None:
                setattr(sample, frame_counter, getattr(sample, frame_counter) + 1)

    def _snapshot(self, sample: LatencySample) -> _LatencySnapshot:
        """Copy every mutable field while the caller owns ``_lock``."""
        return _LatencySnapshot(
            sequence=sample.sequence,
            input_at=sample.input_at,
            action_start_at=sample.action_start_at,
            render_start_at=sample.render_start_at,
            paint_enqueued_at=sample.paint_enqueued_at,
            writer_started_at=sample.writer_started_at,
            usb_present_at=sample.usb_present_at,
            gtk_paint_at=sample.gtk_paint_at,
            paint_enqueued_frames=sample.paint_enqueued_frames,
            writer_started_frames=sample.writer_started_frames,
            usb_present_frames=sample.usb_present_frames,
            gtk_paint_frames=sample.gtk_paint_frames,
            drop_reasons=tuple(sorted(sample.drop_reasons.items())),
        )

    def report(self, *, deck_model: str, video_saturated: bool,
               deck_serial: str | None = None,
               run_id: str | None = None,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return an honest, JSON-compatible hardware-capture report.

        Percentiles deliberately use only samples that reached every required
        boundary. Frame drops remain visible in ``frame_funnel``; a completed
        sibling frame can recover the same physical input. Any input still
        missing a boundary, or any collector overflow, makes comparison invalid.
        """
        with self._lock:
            samples = [self._snapshot(sample) for sample in self._samples]
            untracked_samples = self._untracked_samples
        complete = [sample for sample in samples if self._complete(sample)]
        complete_sequences = {sample.sequence for sample in complete}
        dropped_incomplete = [
            sample for sample in samples
            if sample.sequence not in complete_sequences and sample.drop_reasons
        ]
        incomplete = [
            sample for sample in samples
            if sample.sequence not in complete_sequences and not sample.drop_reasons
        ]
        total_inputs = len(samples) + untracked_samples
        funnel = {"input": total_inputs}
        funnel.update({stage: sum(
            getattr(sample, f"{stage}_at") is not None for sample in samples
        ) for stage in _STAGES})
        missing_stages = {
            stage: sum(getattr(sample, f"{stage}_at") is None for sample in samples)
            for stage in _STAGES
        }
        drop_reasons: dict[str, int] = {}
        for sample in samples:
            for reason, count in sample.drop_reasons:
                drop_reasons[reason] = drop_reasons.get(reason, 0) + count
        report: dict[str, Any] = {
            "schema_version": 3,
            "generated_at": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "writer_pid": os.getpid(),
            "deck_model": deck_model,
            "deck_serial": deck_serial,
            "video_saturated": video_saturated,
            "sample_count": total_inputs,
            "funnel": funnel,
            "validity": {
                "valid_for_comparison": (
                    total_inputs > 0
                    and len(complete) == total_inputs
                    and untracked_samples == 0
                ),
                "complete_samples": len(complete),
                "incomplete_samples": len(samples) - len(complete),
                "dropped_incomplete_samples": len(dropped_incomplete),
                "unfinished_samples": len(incomplete),
                "untracked_samples": untracked_samples,
            },
            "completion": {
                "missing_stages": missing_stages,
                "dropped_by_reason": drop_reasons,
            },
            "frame_funnel": {
                "paint_enqueued": sum(sample.paint_enqueued_frames for sample in samples),
                "writer_started": sum(sample.writer_started_frames for sample in samples),
                "usb_present": sum(sample.usb_present_frames for sample in samples),
                "gtk_paint": sum(sample.gtk_paint_frames for sample in samples),
                "dropped": sum(
                    sum(count for _reason, count in sample.drop_reasons)
                    for sample in samples
                ),
                "dropped_by_reason": drop_reasons,
            },
            "percentiles_ms": {
                "population": "complete_samples_only",
                "sample_count": len(complete),
                "input_to_action_start": self._elapsed(complete, "action_start_at"),
                "input_to_render_start": self._elapsed(complete, "render_start_at"),
                "input_to_paint_enqueued": self._elapsed(complete, "paint_enqueued_at"),
                "input_to_writer_start": self._elapsed(complete, "writer_started_at"),
                "input_to_usb_present": self._elapsed(complete, "usb_present_at"),
                "input_to_gtk_paint": self._elapsed(complete, "gtk_paint_at"),
                "writer_queue_age": self._queue_age(complete),
            },
        }
        if extra:
            report["extra"] = extra
        return report

    @staticmethod
    def _complete(sample: _LatencySnapshot) -> bool:
        return all(getattr(sample, f"{stage}_at") is not None for stage in _STAGES)

    @staticmethod
    def _elapsed(samples: list[_LatencySnapshot], field: str) -> dict[str, float | None]:
        values = [
            (getattr(sample, field) - sample.input_at) * 1000
            for sample in samples
        ]
        return InputLatencyTracker._percentiles(values)

    @staticmethod
    def _queue_age(samples: list[_LatencySnapshot]) -> dict[str, float | None]:
        return InputLatencyTracker._percentiles([
            (sample.writer_started_at - sample.paint_enqueued_at) * 1000
            for sample in samples
            if sample.writer_started_at is not None
            and sample.paint_enqueued_at is not None
        ])

    @staticmethod
    def _percentiles(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"p50": None, "p95": None, "p99": None, "max": None}
        ordered = sorted(values)

        def percentile(percent: float) -> float:
            return round(ordered[max(0, math.ceil(percent * len(ordered)) - 1)], 3)

        return {
            "p50": percentile(0.50),
            "p95": percentile(0.95),
            "p99": percentile(0.99),
            "max": round(ordered[-1], 3),
        }

    def write_report(self, path: str | Path, *, deck_model: str,
                     video_saturated: bool, deck_serial: str | None = None,
                     run_id: str | None = None,
                     extra: dict[str, Any] | None = None) -> Path:
        """Publish one completed report atomically without replacing evidence."""
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = self.report(
            deck_model=deck_model,
            deck_serial=deck_serial,
            video_saturated=video_saturated,
            run_id=run_id,
            extra=extra,
        )
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            os.link(temporary, output)
        except FileExistsError:
            raise FileExistsError(f"refusing to replace existing report: {output}") from None
        finally:
            temporary.unlink(missing_ok=True)
        return output


def make_input_latency_tracker(
    run: InputLatencyRun | None,
) -> InputLatencyTracker | None:
    """Create no object at all unless an explicit hardware run configured it."""
    return InputLatencyTracker() if run is not None else None


def mark_render_started(controller: Any) -> LatencySample | None:
    """Mark the callback token currently carried by this render's thread."""
    tracker = getattr(controller, "input_latency", None)
    if tracker is None:
        return None
    sample = cast("LatencySample | None", tracker.current_sample())
    tracker.render_started(sample)
    return sample


def mirror_input_image(controller: Any, identifier: InputIdentifier, image: Any,
                       push: Callable[..., object]) -> bool:
    """Offer a UI frame with the exact current token, if this is a run."""
    tracker = getattr(controller, "input_latency", None)
    if tracker is None:
        return bool(push(controller, identifier, image))
    sample = tracker.current_sample()
    if sample is None:
        return bool(push(controller, identifier, image))
    outcome = push(controller, identifier, image, latency_sample=sample)
    if getattr(outcome, "recorded_drop", False):
        return True
    if not outcome:
        tracker.drop(sample, "ui_unavailable")
        return False
    return True


def _dispatch_physical_event(controller: Any, identifier: InputIdentifier,
                             args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    tracker = getattr(controller, "input_latency", None)
    if tracker is None:
        controller.event_callback(identifier, *args, **kwargs)
        return
    sample = tracker.input_received()
    if sample is None:
        controller.event_callback(identifier, *args, **kwargs)
        return
    with tracker.correlation(sample):
        controller.event_callback(identifier, *args, **kwargs)


def dispatch_key_callback(controller: Any, key: int, args: tuple[Any, ...],
                          kwargs: dict[str, Any]) -> None:
    coords = controller.index_to_coords(key)
    identifier = Input.Key(f"{coords[0]}x{coords[1]}")
    if args and args[0] is True:
        _dispatch_physical_event(controller, identifier, args, kwargs)
    else:
        controller.event_callback(identifier, *args, **kwargs)


def dispatch_dial_callback(controller: Any, dial: Any, args: tuple[Any, ...],
                           kwargs: dict[str, Any]) -> None:
    identifier = Input.Dial(str(dial))
    event = args[0] if args else None
    value = args[1] if len(args) > 1 else None
    if event == DialEventType.TURN or (event == DialEventType.PUSH and bool(value)):
        _dispatch_physical_event(controller, identifier, args, kwargs)
    else:
        controller.event_callback(identifier, *args, **kwargs)


def dispatch_touchscreen_callback(controller: Any, args: tuple[Any, ...],
                                  kwargs: dict[str, Any]) -> None:
    _dispatch_physical_event(controller, Input.Touchscreen("sd-plus"), args, kwargs)


def write_input_latency_report(controller: Any) -> None:
    """Write the configured run's report using metadata cached before readers run."""
    tracker = getattr(controller, "input_latency", None)
    run = getattr(controller, "input_latency_run", None)
    model = getattr(controller, "input_latency_model", None)
    if tracker is None or run is None or model is None:
        return
    try:
        serial = controller.serial_number()
        output = run.report_dir / f"{serial}.json"
        tracker.write_report(
            output,
            deck_model=model,
            deck_serial=serial,
            video_saturated=run.video_saturated,
            run_id=run.run_id,
            extra={"video_page": run.video_page},
        )
        log.info(f"Wrote input latency report to {output}")
    except Exception:
        log.opt(exception=True).warning("Could not write the input latency report")
