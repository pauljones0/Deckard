"""Reject submit_control messages after terminal ClearAndClose.

Nothing drains the stopped writer's queue, so accepted messages would accumulate.
"""

# This is unit tier, so it runs in a subprocess where the unit tier is the only
# tier.
import fixtures


def test_submit_control_rejected_after_stop() -> None:
    from src.backend.DeckManagement.DeckController import ClearAndCloseMsg, SetBrightnessMsg

    controller, media_player, _ = fixtures.make_stub_controller(serial="submit-reject-1")

    # A normal submission enqueues. The thread never starts at the unit
    # tier, so nothing drains this automatically.
    media_player.submit_control(SetBrightnessMsg(50))
    assert len(media_player.control_q) == 1, "fixture sanity: submit_control should enqueue before stop"
    media_player.control_q.clear()

    # Drive the queue directly because the unit-tier writer thread never starts.
    media_player.submit_control(ClearAndCloseMsg())
    still_running = media_player.drain_control_queue()
    assert still_running is False, "ClearAndCloseMsg must signal the caller to stop the loop"
    assert media_player._stop is True, "_exec_clear_and_close must set _stop itself (not just rely on stop())"

    # Reject later submissions because no consumer remains.
    media_player.submit_control(SetBrightnessMsg(75))
    assert len(media_player.control_q) == 0, "submit_control after stop must be a no-op"

    print("PASS: submit_control rejects messages once the writer is stopped")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_submit_control_reject")
    test_submit_control_rejected_after_stop()
    print("PASS: scenario_submit_control_reject")


if __name__ == "__main__":
    main()
