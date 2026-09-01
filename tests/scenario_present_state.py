"""Verify PresentState.offer skips only when presented and enqueued hashes match.
Independent dropped-paint and superseded-paint states must still enqueue a corrective repaint."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

from src.backend.DeckManagement.deck_controller.paint_protocol import KeyPresentState  # noqa: E402

KEY_INDEX = 3
TARGET_HASH, OTHER_PRESENTED_HASH, OTHER_ENQUEUED_HASH = 1111, 2222, 3333  # a paint's hash, and two hashes that differ from it


class RecordingWriter:
    """Stand-in for MediaPlayerThread that records what reached the slot."""

    def __init__(self):
        self.enqueued = []

    def add_image_task(self, key_index, native_image, page=None, config_gen=None,
                       present=None, img_hash=None):
        self.enqueued.append((key_index, native_image, img_hash, present))


def offer(state, writer, img_hash, force=False):
    """One offer. Returns (enqueued, how many times encode ran)."""
    encode_calls = []

    def encode():
        encode_calls.append(1)
        return b"native-bytes"

    enqueued = state.offer(writer, page=None, config_gen=7, img_hash=img_hash,
                           encode=encode, force=force)
    return enqueued, len(encode_calls)


def make_present_fixture():
    return KeyPresentState(KEY_INDEX), RecordingWriter()


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_present_state")

    # A first paint of anything reaches the writer, and carries the slot, the
    # hash and the present state the write boundary stamps back.
    state, writer = make_present_fixture()
    enqueued, encode_count = offer(state, writer, TARGET_HASH)
    assert enqueued and encode_count == 1, "a first paint must be encoded and enqueued"
    assert writer.enqueued == [(KEY_INDEX, b"native-bytes", TARGET_HASH, state)], (
        f"the enqueue must name the key slot, the hash and the present state, "
        f"got {writer.enqueued}"
    )
    assert state.last_enqueued_hash == TARGET_HASH, "an enqueued paint must be recorded in flight"

    # In-flight revert. The device holds the target, and another paint is on its
    # way. A repaint of the target must reach the device, or the other paint remains.
    state, writer = make_present_fixture()
    state.note_presented(TARGET_HASH)
    state.last_enqueued_hash = OTHER_ENQUEUED_HASH
    enqueued, _ = offer(state, writer, TARGET_HASH)
    assert enqueued, (
        "a repaint must survive when only the presented hash matches -- the "
        "paint in flight would otherwise leave its own content on the device"
    )

    # The target was enqueued but dropped as stale while the device kept another image,
    # so a new offer of the target must still reach the device.
    state, writer = make_present_fixture()
    state.last_enqueued_hash = TARGET_HASH
    state.last_presented_hash = OTHER_PRESENTED_HASH
    enqueued, _ = offer(state, writer, TARGET_HASH)
    assert enqueued, (
        "a repaint must survive when only the enqueued hash matches -- the "
        "paint it matches was dropped and never reached the device"
    )

    # Both halves agree, so the device already holds this image and nothing
    # newer is on its way.
    state, writer = make_present_fixture()
    state.note_presented(TARGET_HASH)
    state.last_enqueued_hash = TARGET_HASH
    enqueued, encode_count = offer(state, writer, TARGET_HASH)
    assert not enqueued and not writer.enqueued, (
        "an image the device already holds must not be enqueued again"
    )
    assert encode_count == 0, (
        "a skipped paint must not be encoded -- the encode is the expensive "
        "half, and skipping it is the point of the hash pair"
    )

    # Force bypasses both hash checks.
    enqueued, encode_count = offer(state, writer, TARGET_HASH, force=True)
    assert enqueued and encode_count == 1, "force must paint through an agreeing pair"

    # reset() is the third writer of the pair. After it, the same image paints
    # again, which is what a Clear and a full repaint rely on.
    state, writer = make_present_fixture()
    state.note_presented(TARGET_HASH)
    state.last_enqueued_hash = TARGET_HASH
    state.reset()
    assert state.last_presented_hash is None and state.last_enqueued_hash is None
    enqueued, _ = offer(state, writer, TARGET_HASH)
    assert enqueued, "after a reset, identical content must reach the device again"

    # An encode that answers empty bytes is a mid-turn straggler. It must not
    # reach the writer slot, where it would displace a valid pending paint
    # with a write that never happens, and it must leave no hash trace.
    state, writer = make_present_fixture()
    enqueued = state.offer(writer, page=None, config_gen=7, img_hash=TARGET_HASH,
                           encode=lambda: b"")
    assert enqueued is False, "an empty encode must report nothing enqueued"
    assert writer.enqueued == [], f"an empty encode reached the slot: {writer.enqueued}"
    assert state.last_enqueued_hash != TARGET_HASH, (
        "an empty encode advanced last_enqueued_hash; the dropped paint must "
        "leave no trace")

    print("PASS: scenario_present_state")


if __name__ == "__main__":
    main()
