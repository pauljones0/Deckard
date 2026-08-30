"""Portable checks for exact input-latency correlation and report validity."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from src.backend.DeckManagement.deck_controller.input_latency import (
    dispatch_deck_event,
    InputLatencyTracker,
    InputLatencyRun,
    make_input_latency_tracker,
    mirror_input_image,
    write_input_latency_report,
)
from src.backend.DeckManagement.deck_events import KeyEvent
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.deck_controller.media_tasks import MediaPlayerSetTouchscreenImageTask
from src.backend.DeckManagement.deck_controller.media_writer import ClearMsg
from src.backend.DeckManagement.deck_controller.paint_protocol import KeyPresentState, PaintTicket
from src.windows.ui_adapter import GtkUIAdapter, _MirrorFrame, _MirrorSlot


class Clock:
    def __init__(self, start: float = 10.0) -> None:
        self.value = start

    def __call__(self) -> float:
        self.value += 0.001
        return self.value


def complete(tracker: InputLatencyTracker, sample: object) -> None:
    tracker.action_started(sample)
    tracker.render_started(sample)
    tracker.paint_enqueued(sample)
    tracker.writer_started(sample)
    tracker.usb_presented(sample)
    tracker.gtk_painted(sample)


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_input_latency_contract")

    # A delayed worker retains its captured token. A newer press cannot steal
    # its action timestamp merely because it arrived first on the main thread.
    tracker = InputLatencyTracker(clock=Clock())
    first = tracker.input_received()
    second = tracker.input_received()
    worker = threading.Thread(
        target=lambda: tracker.run_with_sample(
            first, lambda: tracker.action_started(tracker.current_sample())),
    )
    worker.start()
    worker.join()
    assert first.action_start_at is not None
    assert second.action_start_at is None
    complete(tracker, first)

    report = tracker.report(deck_model="Stream Deck +", video_saturated=True)
    assert report["schema_version"] == 3
    assert report["validity"] == {
        "valid_for_comparison": False,
        "complete_samples": 1,
        "incomplete_samples": 1,
        "dropped_incomplete_samples": 0,
        "unfinished_samples": 1,
        "untracked_samples": 0,
    }
    assert report["percentiles_ms"]["population"] == "complete_samples_only"
    assert report["percentiles_ms"]["sample_count"] == 1
    assert report["percentiles_ms"]["input_to_gtk_paint"]["p99"] == 7.0
    assert report["funnel"]["input"] == 2
    assert report["funnel"]["gtk_paint"] == 1

    output = Path(fixtures.DATA_DIR) / "latency.json"
    tracker.write_report(output, deck_model="Stream Deck +", video_saturated=True,
                         run_id="portable-run")
    persisted = json.loads(output.read_text())
    assert persisted["run_id"] == "portable-run"
    try:
        tracker.write_report(output, deck_model="Stream Deck +", video_saturated=True,
                             run_id="portable-run")
    except FileExistsError:
        pass
    else:
        raise AssertionError("a report writer must reject an existing run output")

    # Teardown consumes the model captured during owned initialization; it
    # never reaches back through the live device handle to obtain deck_type.
    cached_dir = Path(fixtures.DATA_DIR) / "cached-model-report"
    cached_tracker = InputLatencyTracker(clock=Clock(15.0))
    cached_sample = cached_tracker.input_received()
    complete(cached_tracker, cached_sample)

    class LiveDeckMustNotBeRead:
        def deck_type(self):
            raise AssertionError("teardown must use cached deck metadata")

    cached_controller = SimpleNamespace(
        input_latency=cached_tracker,
        input_latency_run=InputLatencyRun(
            report_dir=cached_dir,
            run_id="cached-model-run",
            video_saturated=True,
            video_page="Video stress",
        ),
        input_latency_model="Cached Stream Deck",
        serial_number=lambda: "cached-serial",
        deck=LiveDeckMustNotBeRead(),
    )
    write_input_latency_report(cached_controller)
    cached_report = json.loads((cached_dir / "cached-serial.json").read_text())
    assert cached_report["deck_model"] == "Cached Stream Deck"

    # The HID entry itself creates and correlates the sample: a press
    # arriving through the deck's key callback reaches event_callback under
    # an active token, and a release passes through untracked.
    class _HidController:
        def __init__(self, tracker):
            self.input_latency = tracker
            self.seen: list = []

        def event_callback(self, identifier, event):
            self.seen.append(
                (identifier, event, self.input_latency.current_sample()))

    hid_tracker = InputLatencyTracker(clock=Clock(15.0))
    hid_controller = _HidController(hid_tracker)
    dispatch_deck_event(hid_controller, Input.Key("0x0"), KeyEvent(pressed=True))
    dispatch_deck_event(hid_controller, Input.Key("0x0"), KeyEvent(pressed=False))
    assert len(hid_controller.seen) == 2, "both edges must reach event_callback"
    press_ident, press_event, press_sample = hid_controller.seen[0]
    assert press_event == KeyEvent(pressed=True) and press_sample is not None, (
        "a physical press must run under a fresh correlated sample")
    assert press_sample.input_at is not None, (
        "the sample must be stamped at the HID callback")
    _release_ident, release_event, release_sample = hid_controller.seen[1]
    assert release_event == KeyEvent(pressed=False) and release_sample is None, (
        "a release is not a measured input and must pass through untracked")

    # Construction parity: the three deck adapters build exactly the
    # typed events the injection paths (control plane, emulation, the
    # deck-plus widgets) construct, and the key adapter still maps the
    # library's key index into the logical identifier.
    from src.backend.DeckManagement.deck_events import DialEvent, TouchscreenEvent
    from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType

    from types import MethodType

    from src.backend.DeckManagement.deck_controller.controller import DeckController

    parity_controller, _pw, _pm = fixtures.make_stub_controller(n_keys=2)
    for name in ("key_event_callback", "dial_event_callback",
                 "touchscreen_event_callback", "index_to_coords"):
        setattr(parity_controller, name,
                MethodType(getattr(DeckController, name), parity_controller))
    funneled: list = []
    parity_controller.event_callback = lambda ident, event: funneled.append((ident, event))
    parity_controller.key_event_callback(object(), 1, True)
    parity_controller.dial_event_callback(object(), 0, DialEventType.TURN, -1)
    parity_controller.touchscreen_event_callback(
        object(), TouchscreenEventType.SHORT, {"x": 3, "y": 4})
    assert funneled == [
        (Input.Key("1x0"), KeyEvent(pressed=True)),
        (Input.Dial("0"), DialEvent(kind=DialEventType.TURN, value=-1)),
        (Input.Touchscreen("sd-plus"),
         TouchscreenEvent(kind=TouchscreenEventType.SHORT,
                          value={"x": 3, "y": 4})),
    ], (
        f"the adapters funneled {funneled}; they must construct the same "
        f"event objects the injection paths construct, with the key index "
        f"mapped into the logical identifier")

    # Writer latest-wins keeps the selected paint's token. The displaced frame
    # is counted instead of disappearing from the percentile funnel.
    controller, media_player, _manager = fixtures.make_stub_controller(n_keys=1)
    writer_tracker = InputLatencyTracker(clock=Clock(20.0))
    controller.input_latency = writer_tracker
    state = KeyPresentState(0)
    paint_a = writer_tracker.input_received()
    paint_b = writer_tracker.input_received()
    assert writer_tracker.run_with_sample(
        paint_a, state.offer, media_player, page=controller.active_page,
        config_gen=controller._page_load_generation, img_hash=41,
        encode=lambda: b"first",
    )
    assert writer_tracker.run_with_sample(
        paint_b, state.offer, media_player, page=controller.active_page,
        config_gen=controller._page_load_generation, img_hash=42,
        encode=lambda: b"second",
    )
    task = media_player.image_tasks[0]
    assert task.ticket.latency_sample is paint_b
    task.run()
    assert paint_a.drop_reasons == {"writer_slot_superseded": 1}
    assert paint_a.usb_present_at is None
    assert paint_b.usb_present_at is not None
    writer_report = writer_tracker.report(
        deck_model="Fake Deck", video_saturated=True)
    assert writer_report["validity"]["dropped_incomplete_samples"] == 1
    assert writer_report["completion"]["dropped_by_reason"] == {
        "writer_slot_superseded": 1,
    }

    # A background recomposite offers with no sample of its own, but it
    # shows the same pressed state, so its device write answers the press.
    # The displaced sample rides the superseding frame instead of dropping.
    # This is the video-saturation shape, where every press paint is
    # replaced by a video composite before the writer drains the slot.
    inherit_controller, inherit_writer, _manager = fixtures.make_stub_controller(n_keys=1)
    inherit_tracker = InputLatencyTracker(clock=Clock(30.0))
    inherit_controller.input_latency = inherit_tracker
    inherit_state = KeyPresentState(0)
    press = inherit_tracker.input_received()
    inherit_tracker.action_started(press)
    inherit_tracker.render_started(press)
    assert inherit_tracker.run_with_sample(
        press, inherit_state.offer, inherit_writer,
        page=inherit_controller.active_page,
        config_gen=inherit_controller._page_load_generation,
        img_hash=61, encode=lambda: b"press",
    )
    assert inherit_state.offer(
        inherit_writer, page=inherit_controller.active_page,
        config_gen=inherit_controller._page_load_generation,
        img_hash=62, encode=lambda: b"video-frame",
    )
    inherited_task = inherit_writer.image_tasks[0]
    assert inherited_task.ticket.latency_sample is press, (
        "an unsampled superseding frame must inherit the displaced sample")
    assert inherited_task.ticket.native_image == b"video-frame", (
        "inheritance must ride the newer frame, not resurrect the old one")
    inherited_task.run()
    inherit_tracker.gtk_painted(press)
    assert press.drop_reasons == {}, (
        f"the press recorded drops {press.drop_reasons} although its input "
        f"reached the device on the superseding frame")
    assert press.usb_present_at is not None, (
        "the superseding frame's device write must complete the press")
    inherit_report = inherit_tracker.report(
        deck_model="Fake Deck", video_saturated=True)
    assert inherit_report["validity"]["valid_for_comparison"], (
        "a press completed by a superseding frame must yield a comparable "
        "report")

    # A producer may issue two frames while handling one physical input. The
    # latest writer ticket wins, but its discarded predecessor is not a lost
    # input: the same token still reaches USB and yields a valid report.
    same_controller, same_writer, _manager = fixtures.make_stub_controller(n_keys=1)
    same_tracker = InputLatencyTracker(clock=Clock(25.0))
    same_controller.input_latency = same_tracker
    same_state = KeyPresentState(0)
    same_sample = same_tracker.input_received()
    same_tracker.action_started(same_sample)
    same_tracker.render_started(same_sample)
    assert same_tracker.run_with_sample(
        same_sample, same_state.offer, same_writer,
        page=same_controller.active_page,
        config_gen=same_controller._page_load_generation,
        img_hash=51, encode=lambda: b"same-first",
    )
    assert same_tracker.run_with_sample(
        same_sample, same_state.offer, same_writer,
        page=same_controller.active_page,
        config_gen=same_controller._page_load_generation,
        img_hash=52, encode=lambda: b"same-second",
    )
    same_writer.image_tasks[0].run()
    same_tracker.gtk_painted(same_sample)
    assert same_sample.drop_reasons == {"writer_slot_superseded": 1}
    same_report = same_tracker.report(deck_model="Fake Deck", video_saturated=True)
    assert same_report["validity"]["valid_for_comparison"]
    assert same_report["frame_funnel"]["dropped_by_reason"] == {
        "writer_slot_superseded": 1,
    }

    # A writer batch can contain two frames for one press. A stale sibling
    # still contributes a frame-drop reason, but the press remains valid when
    # another frame from that exact token reaches USB and GTK.
    batch_controller, batch_writer, _manager = fixtures.make_stub_controller(n_keys=2)
    batch_tracker = InputLatencyTracker(clock=Clock(26.0))
    batch_controller.input_latency = batch_tracker
    batch_same = batch_tracker.input_received()
    batch_tracker.action_started(batch_same)
    batch_tracker.render_started(batch_same)
    batch_tracker.paint_enqueued(batch_same)
    batch_tracker.paint_enqueued(batch_same)
    batch_writer.add_image_task(
        0, b"batch-good", page=batch_controller.active_page,
        config_gen=batch_controller._page_load_generation,
        latency_sample=batch_same,
    )
    batch_writer.add_image_task(
        1, b"batch-stale", page=object(),
        config_gen=batch_controller._page_load_generation,
        latency_sample=batch_same,
    )
    batch_writer.perform_media_player_tasks()
    batch_tracker.gtk_painted(batch_same)
    assert batch_same.drop_reasons == {"stale_paint": 1}
    assert batch_tracker.report(
        deck_model="Fake Deck", video_saturated=True)["validity"]["valid_for_comparison"]

    # A stale frame carrying a different token has no successful sibling and
    # remains an invalid, accounted-for physical sample.
    lost = batch_tracker.input_received()
    batch_tracker.action_started(lost)
    batch_tracker.render_started(lost)
    batch_tracker.paint_enqueued(lost)
    batch_writer.add_image_task(
        1, b"different-stale", page=object(),
        config_gen=batch_controller._page_load_generation,
        latency_sample=lost,
    )
    batch_writer.perform_media_player_tasks()
    assert lost.drop_reasons == {"stale_paint": 1}
    assert not batch_tracker.report(
        deck_model="Fake Deck", video_saturated=True)["validity"]["valid_for_comparison"]

    # An unexpected writer failure marks both the failing ticket and every
    # later popped ticket before the loop guard schedules recovery.
    failure_controller, failure_writer, _manager = fixtures.make_stub_controller(n_keys=3)
    failure_tracker = InputLatencyTracker(clock=Clock(26.5))
    failure_controller.input_latency = failure_tracker
    successful = failure_tracker.input_received()
    failing = failure_tracker.input_received()
    unrun = failure_tracker.input_received()
    for sample in (successful, failing, unrun):
        failure_tracker.action_started(sample)
        failure_tracker.render_started(sample)
        failure_tracker.paint_enqueued(sample)
    failure_writer.add_image_task(
        0, b"good", page=failure_controller.active_page,
        config_gen=failure_controller._page_load_generation,
        latency_sample=successful,
    )
    failure_writer.add_image_task(
        1, b"boom", page=failure_controller.active_page,
        config_gen=failure_controller._page_load_generation,
        latency_sample=failing,
    )
    failure_writer.add_image_task(
        2, b"unrun", page=failure_controller.active_page,
        config_gen=failure_controller._page_load_generation,
        latency_sample=unrun,
    )
    original_write = failure_controller.deck.set_key_image

    def fail_key_one(key: int, image: bytes) -> None:
        if key == 1:
            raise TypeError("unexpected writer failure")
        original_write(key, image)

    failure_controller.deck.set_key_image = fail_key_one
    try:
        failure_writer.perform_media_player_tasks()
    except TypeError:
        pass
    else:
        raise AssertionError("an unexpected device exception must escape the batch")
    assert successful.drop_reasons == {}
    assert failing.drop_reasons == {"writer_tick_exception": 1}
    assert unrun.drop_reasons == {"writer_tick_exception": 1}

    # The touchscreen write cap can discard an old local task in favor of a
    # concurrently queued one. Preserve same-token work and name a different
    # token's lost path explicitly.
    rate_controller, rate_writer, _manager = fixtures.make_stub_controller(
        n_keys=1, has_touchscreen=True)
    rate_tracker = InputLatencyTracker(clock=Clock(27.0))
    rate_controller.input_latency = rate_tracker
    rate_old = rate_tracker.input_received()
    rate_same = MediaPlayerSetTouchscreenImageTask(
        deck_controller=rate_controller,
        ticket=PaintTicket(None, rate_controller.active_page, 0, b"same", 61,
                           rate_old),
    )
    rate_writer.touchscreen_task = rate_same
    rate_writer._paint_queue.defer_rate_limited_touchscreen(
        MediaPlayerSetTouchscreenImageTask(
            deck_controller=rate_controller,
            ticket=PaintTicket(None, rate_controller.active_page, 0, b"old", 60,
                               rate_old),
        ))
    assert rate_old.drop_reasons == {"touchscreen_rate_cap_superseded": 1}

    rate_lost = rate_tracker.input_received()
    rate_replacement = rate_tracker.input_received()
    rate_writer.touchscreen_task = MediaPlayerSetTouchscreenImageTask(
        deck_controller=rate_controller,
        ticket=PaintTicket(None, rate_controller.active_page, 0, b"new", 63,
                           rate_replacement),
    )
    rate_writer._paint_queue.defer_rate_limited_touchscreen(
        MediaPlayerSetTouchscreenImageTask(
            deck_controller=rate_controller,
            ticket=PaintTicket(None, rate_controller.active_page, 0, b"old", 62,
                               rate_lost),
        ))
    assert rate_lost.drop_reasons == {"touchscreen_rate_cap_superseded": 1}

    # Clear paths are terminal for their removed tickets and retain a reason
    # instead of dropping a measurement from the report population.
    clear_controller, clear_writer, _manager = fixtures.make_stub_controller(n_keys=1)
    clear_tracker = InputLatencyTracker(clock=Clock(28.0))
    clear_controller.input_latency = clear_tracker
    clear_sample = clear_tracker.input_received()
    clear_writer.add_image_task(
        0, b"clear", page=clear_controller.active_page,
        config_gen=clear_controller._page_load_generation,
        latency_sample=clear_sample,
    )
    clear_writer._exec_clear(ClearMsg(seq=clear_writer.next_submit_seq()))
    assert clear_sample.drop_reasons == {"clear_preceding_paint": 1}

    clear_survivor = clear_tracker.input_received()
    clear_writer.add_image_task(
        0, b"old-same", page=clear_controller.active_page,
        config_gen=clear_controller._page_load_generation,
        latency_sample=clear_survivor,
    )
    clear_seq = clear_writer.next_submit_seq()
    clear_writer.add_image_task(
        1, b"new-same", page=clear_controller.active_page,
        config_gen=clear_controller._page_load_generation,
        latency_sample=clear_survivor,
    )
    clear_writer._exec_clear(ClearMsg(seq=clear_seq))
    assert clear_survivor.drop_reasons == {"clear_preceding_paint": 1}

    queue_sample = clear_tracker.input_received()
    clear_writer.add_image_task(
        0, b"queue", page=clear_controller.active_page,
        config_gen=clear_controller._page_load_generation,
        latency_sample=queue_sample,
    )
    from src.backend.DeckManagement.DeckController import DeckController
    DeckController.clear_media_player_tasks(clear_controller)
    assert queue_sample.drop_reasons == {"controller_queue_cleared": 1}

    terminal_sample = clear_tracker.input_received()
    clear_writer.add_image_task(
        0, b"terminal", page=clear_controller.active_page,
        config_gen=clear_controller._page_load_generation,
        latency_sample=terminal_sample,
    )
    clear_writer._exec_clear_and_close()
    assert terminal_sample.drop_reasons == {"terminal_clear": 1}

    # The mirror slot carries the sample with its payload. A video frame has
    # no sample and therefore cannot satisfy a press that arrives before GTK
    # drains it; a superseded press frame is counted independently.
    mirror_tracker = InputLatencyTracker(clock=Clock(30.0))
    video_slot = _MirrorSlot()
    video_slot.offer(_MirrorFrame("video", None))
    press = mirror_tracker.input_received()
    video_frame = video_slot.take()
    assert isinstance(video_frame, _MirrorFrame)
    mirror_tracker.gtk_painted(video_frame.latency_sample)
    assert press.gtk_paint_at is None

    first_slot = _MirrorSlot()
    mirror_a = mirror_tracker.input_received()
    mirror_b = mirror_tracker.input_received()
    first_slot.offer(_MirrorFrame("press-a", mirror_a),
                     on_superseded=lambda frame: mirror_tracker.drop(
                         frame.latency_sample, "ui_slot_superseded"))
    first_slot.offer(_MirrorFrame("press-b", mirror_b),
                     on_superseded=lambda frame: mirror_tracker.drop(
                         frame.latency_sample, "ui_slot_superseded"))
    winner = first_slot.take()
    assert isinstance(winner, _MirrorFrame)
    mirror_tracker.gtk_painted(winner.latency_sample)
    assert mirror_a.drop_reasons == {"ui_slot_superseded": 1}
    assert mirror_a.gtk_paint_at is None
    assert mirror_b.gtk_paint_at is not None

    # Exercise the adapter's real drain too: its older video payload cannot
    # stamp a press that arrived before GTK processed that payload, and its
    # latest-wins replacement preserves the winning press token.
    class MirrorController:
        def __init__(self, tracker: InputLatencyTracker) -> None:
            self.input_latency = tracker
            self.ui_image_changes_while_hidden: dict[object, bool] = {}

    class MirrorWidget:
        def prepare_mirror_frame(self, image: object) -> object:
            return image

        def paint_mirror_frame(self, payload: object) -> bool:
            return False

    adapter_tracker = InputLatencyTracker(clock=Clock(40.0))
    adapter_controller = MirrorController(adapter_tracker)
    adapter = GtkUIAdapter()
    widget = MirrorWidget()
    child = SimpleNamespace(
        page_settings=SimpleNamespace(
            deck_config=SimpleNamespace(screenbar=SimpleNamespace(image=widget))))
    adapter.bind(adapter_controller, child)
    adapter._window_mapped = True
    strip = Input.Touchscreen("sd-plus")
    assert adapter.push_input_image(adapter_controller, strip, "video")
    adapter_press = adapter_tracker.input_received()
    adapter._drain_mirror(adapter_controller, strip)
    assert adapter_press.gtk_paint_at is None
    assert adapter.push_input_image(
        adapter_controller, strip, "press", latency_sample=adapter_press)
    adapter._drain_mirror(adapter_controller, strip)
    assert adapter_press.gtk_paint_at is not None

    adapter_a = adapter_tracker.input_received()
    adapter_b = adapter_tracker.input_received()
    assert adapter.push_input_image(adapter_controller, strip, "press-a", latency_sample=adapter_a)
    assert adapter.push_input_image(adapter_controller, strip, "press-b", latency_sample=adapter_b)
    adapter._drain_mirror(adapter_controller, strip)
    assert adapter_a.drop_reasons == {"ui_slot_superseded": 1}
    assert adapter_a.gtk_paint_at is None
    assert adapter_b.gtk_paint_at is not None

    adapter_same = adapter_tracker.input_received()
    assert adapter.push_input_image(
        adapter_controller, strip, "same-a", latency_sample=adapter_same)
    assert adapter.push_input_image(
        adapter_controller, strip, "same-b", latency_sample=adapter_same)
    adapter._drain_mirror(adapter_controller, strip)
    assert adapter_same.drop_reasons == {"ui_slot_superseded": 1}
    assert adapter_same.gtk_paint_at is not None

    # A GLib scheduling failure owns its terminal drop and dirty marker. The
    # engine sees the structured outcome and must not add ui_unavailable.
    import src.windows.ui_adapter as ui_adapter_module

    schedule_sample = adapter_tracker.input_received()
    slot = adapter._mirror_slots[(adapter_controller, strip)]
    slot._last_drain = float("-inf")
    original_idle_add = ui_adapter_module.GLib.idle_add

    def fail_idle_add(*args: object, **kwargs: object) -> int:
        raise RuntimeError("main-loop scheduling failed")

    ui_adapter_module.GLib.idle_add = fail_idle_add
    try:
        scheduled = adapter_tracker.run_with_sample(
            schedule_sample, mirror_input_image, adapter_controller, strip,
            "schedule-failure", adapter.push_input_image,
        )
    finally:
        ui_adapter_module.GLib.idle_add = original_idle_add
    assert scheduled is True
    assert schedule_sample.drop_reasons == {"ui_schedule_failed": 1}
    assert adapter_controller.ui_image_changes_while_hidden.get(strip) is True

    unavailable_sample = adapter_tracker.input_received()
    rejected = adapter_tracker.run_with_sample(
        unavailable_sample, mirror_input_image, adapter_controller, strip,
        "unavailable", lambda *args, **kwargs: False,
    )
    assert rejected is False
    assert unavailable_sample.drop_reasons == {"ui_unavailable": 1}

    unbound_sample = adapter_tracker.input_received()
    assert adapter.push_input_image(
        adapter_controller, strip, "unbound", latency_sample=unbound_sample)
    adapter.unbind(adapter_controller)
    assert unbound_sample.drop_reasons == {"ui_unbound": 1}

    # report() copies every mutable sample field while its lock is held. The
    # concurrent drop cannot mutate drop_reasons mid-serialization, so the
    # completed snapshot is always a valid JSON-compatible report.
    class SnapshotGateTracker(InputLatencyTracker):
        def __init__(self) -> None:
            super().__init__(clock=Clock(50.0))
            self.snapshot_entered = threading.Event()
            self.release_snapshot = threading.Event()

        def _snapshot(self, sample):
            self.snapshot_entered.set()
            assert self.release_snapshot.wait(timeout=3)
            return super()._snapshot(sample)

    snapshot_tracker = SnapshotGateTracker()
    snapshot_sample = snapshot_tracker.input_received()
    complete(snapshot_tracker, snapshot_sample)
    snapshot_result: dict[str, object] = {}
    report_done = threading.Event()

    def build_report() -> None:
        try:
            snapshot_result["report"] = snapshot_tracker.report(
                deck_model="Fake Deck", video_saturated=True)
        except BaseException as error:
            snapshot_result["error"] = error
        finally:
            report_done.set()

    drop_started = threading.Event()
    drop_finished = threading.Event()

    def concurrent_drop() -> None:
        drop_started.set()
        snapshot_tracker.drop(snapshot_sample, "concurrent_drop")
        drop_finished.set()

    reporter = threading.Thread(target=build_report)
    reporter.start()
    assert snapshot_tracker.snapshot_entered.wait(timeout=3)
    dropper = threading.Thread(target=concurrent_drop)
    dropper.start()
    assert drop_started.wait(timeout=3)
    assert not drop_finished.wait(timeout=0.1)
    snapshot_tracker.release_snapshot.set()
    assert report_done.wait(timeout=3)
    assert drop_finished.wait(timeout=3)
    reporter.join()
    dropper.join()
    assert "error" not in snapshot_result
    concurrent_report = snapshot_result["report"]
    assert isinstance(concurrent_report, dict)
    assert concurrent_report["validity"]["valid_for_comparison"] is True
    json.dumps(concurrent_report)

    # No configured run means no tracker object, samples, or instrumentation
    # lock on ordinary input/render/writer hot paths.
    assert make_input_latency_tracker(None) is None

    print("PASS: scenario_input_latency_contract")


if __name__ == "__main__":
    main()
