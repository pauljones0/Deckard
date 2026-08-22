"""PresentState.offer must judge a paint against both of its hashes.

The pair is an AND with two independent halves, and each half covers a case
the other cannot. The presented hash alone goes stale when a paint is
dropped at the write boundary, and the enqueued hash alone goes stale when a
paint in flight is superseded. A skip on either half alone therefore
swallows the repaint that would have corrected the device.

This drives the present state directly, with a recording stand-in for the
writer, because no rendered content can put the pair into either of those
states on purpose.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

from src.backend.DeckManagement.deck_controller.paint_protocol import KeyPresentState  # noqa: E402

KEY_INDEX = 3
H, A, B = 1111, 2222, 3333  # a paint's hash, and two hashes that differ from it


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


def new_state():
    return KeyPresentState(KEY_INDEX), RecordingWriter()


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_present_state")

    # A first paint of anything reaches the writer, and carries the slot, the
    # hash and the present state the write boundary stamps back.
    state, writer = new_state()
    enqueued, encodes = offer(state, writer, H)
    assert enqueued and encodes == 1, "a first paint must be encoded and enqueued"
    assert writer.enqueued == [(KEY_INDEX, b"native-bytes", H, state)], (
        f"the enqueue must name the key slot, the hash and the present state, "
        f"got {writer.enqueued}"
    )
    assert state.last_enqueued_hash == H, "an enqueued paint must be recorded in flight"

    # In-flight revert. The device holds H, and a newer paint of B is on its
    # way. A repaint of H must reach the device, or the device keeps B.
    state, writer = new_state()
    state.note_presented(H)
    state.last_enqueued_hash = B
    enqueued, _ = offer(state, writer, H)
    assert enqueued, (
        "a repaint must survive when only the presented hash matches -- the "
        "paint in flight would otherwise leave its own content on the device"
    )

    # Dropped paint. H was enqueued and never presented, because the write
    # boundary judged it stale; the device holds A. The correcting repaint of
    # H must reach the device.
    state, writer = new_state()
    state.last_enqueued_hash = H
    state.last_presented_hash = A
    enqueued, _ = offer(state, writer, H)
    assert enqueued, (
        "a repaint must survive when only the enqueued hash matches -- the "
        "paint it matches was dropped and never reached the device"
    )

    # Both halves agree, so the device already holds this image and nothing
    # newer is on its way.
    state, writer = new_state()
    state.note_presented(H)
    state.last_enqueued_hash = H
    enqueued, encodes = offer(state, writer, H)
    assert not enqueued and not writer.enqueued, (
        "an image the device already holds must not be enqueued again"
    )
    assert encodes == 0, (
        "a skipped paint must not be encoded -- the encode is the expensive "
        "half, and skipping it is the point of the hash pair"
    )

    # force paints whatever the hashes say.
    enqueued, encodes = offer(state, writer, H, force=True)
    assert enqueued and encodes == 1, "force must paint through an agreeing pair"

    # reset() is the third writer of the pair. After it, the same image paints
    # again, which is what a Clear and a full repaint rely on.
    state, writer = new_state()
    state.note_presented(H)
    state.last_enqueued_hash = H
    state.reset()
    assert state.last_presented_hash is None and state.last_enqueued_hash is None
    enqueued, _ = offer(state, writer, H)
    assert enqueued, "after a reset, identical content must reach the device again"

    print("PASS: scenario_present_state")


if __name__ == "__main__":
    main()
