"""Check that fake-deck relayout reloads the page and queues old-input release.

Only the media writer may close replaced inputs because a tick can still reference them.
"""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import json
import os
from types import SimpleNamespace

import globals as gl

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.deck_controller.media_writer import (
    ReleaseStashedInputsMsg,
)
from src.windows.mainWindow.elements.DeckSettings.FakeDeckGroup import Layout

PAGE_NAME = "RelayoutPage"
LABEL_POSITION = "center"
LABEL_TEXT = "relayout"

# Key present in both the default and requested layouts
KEY_IDENTIFIER = "0x0"

# Shape that differs from the default on both axes
NEW_LAYOUT = [3, 3]


class RecordingGrid:
    """The editor grid reduced to the two calls the relayout makes on it."""

    def __init__(self):
        self.calls: list[str] = []

    def regenerate_buttons(self) -> None:
        self.calls.append("regenerate_buttons")

    def build(self) -> None:
        self.calls.append("build")


def seed_labelled_page() -> str:
    """A page whose key 0x0 carries a label, written into the temp data dir."""
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{PAGE_NAME}.json")
    with open(path, "w") as handle:
        json.dump({"keys": {KEY_IDENTIFIER: {"states": {"0": {
            "labels": {LABEL_POSITION: {"text": LABEL_TEXT}},
        }}}}, "dials": {}, "touchscreens": {}}, handle)
    return path


def label_text_of(controller, identifier) -> "str | None":
    """The page label on the given key's first state, or None if it has none."""
    controller_input = controller.get_input(identifier)
    if controller_input is None:
        return None
    state = controller_input.states.get(0)
    if state is None:
        return None
    label = state.label_manager.page_labels.get(LABEL_POSITION)
    return None if label is None else label.text


def wait_for_label(controller, identifier) -> bool:
    """Wait until the asynchronous input load applies the page label."""
    return fixtures.wait_until(
        lambda: label_text_of(controller, identifier) == LABEL_TEXT, timeout=10.0)


class ControlSpy:
    """Record and forward control messages to verify writer-owned release."""

    def __init__(self, media_player):
        self.media_player = media_player
        self.messages: list = []
        self.original = media_player.submit_control
        media_player.submit_control = self.run

    def run(self, msg):
        self.messages.append(msg)
        return self.original(msg)

    def release_messages(self) -> list:
        return [m for m in self.messages
                if isinstance(m, ReleaseStashedInputsMsg)]


def check_relayout() -> None:
    page_path = seed_labelled_page()
    controller = fixtures.make_headless_controller(serial="relayout-1")
    try:
        page = gl.page_manager.get_page(page_path, controller)
        assert page is not None, "the labelled page did not build"
        controller.load_page(page, allow_reload=True)

        identifier = Input.Key(KEY_IDENTIFIER)
        assert wait_for_label(controller, identifier), (
            "the page did not load onto the original inputs, so the checks "
            "below would prove nothing"
        )

        outgoing_registry = controller.inputs
        outgoing = [i for inputs in outgoing_registry.values() for i in inputs]
        assert outgoing, "the controller has no inputs to replace"
        control_spy = ControlSpy(controller.media_player)

        grid = RecordingGrid()
        settings_page = SimpleNamespace(
            deck_controller=controller,
            deck_stack_child=SimpleNamespace(
                page_settings=SimpleNamespace(grid_page=grid)))
        # apply_key_layout needs only these settings-page attributes.
        Layout.apply_key_layout(SimpleNamespace(settings_page=settings_page),
                                list(NEW_LAYOUT))

        rows, columns = fixtures.raw_deck(controller).key_layout()
        assert [rows, columns] == NEW_LAYOUT, (
            f"the deck reports {[rows, columns]} after the relayout, expected "
            f"{NEW_LAYOUT}")

        keys = controller.inputs[Input.Key]
        assert len(keys) == rows * columns, (
            f"the rebuilt registry holds {len(keys)} keys, expected "
            f"{rows * columns}")

        replaced = [i for i in keys if i in outgoing]
        assert not replaced, (
            "init_inputs() did not replace the input objects, so the sweep "
            "check below would read the live set")

        assert wait_for_label(controller, identifier), (
            "the relayout left the new inputs blank -- the page load that has "
            "to follow init_inputs() did not run")
        print("PASS: the relayout loads the page onto the rebuilt inputs")

        released = control_spy.release_messages()
        assert len(released) == 1, (
            f"the relayout submitted {len(released)} release messages to the "
            f"media writer, expected exactly one -- an inline close would "
            f"race a tick that still holds the old inputs")
        assert released[0].stashed_inputs is outgoing_registry, (
            "the release message must carry the registry the relayout "
            "replaced, not some other mapping")

        # The writer empties this mapping in place after it closes the old inputs.
        assert fixtures.wait_until(
            lambda: not any(outgoing_registry.values()), timeout=10.0), (
            "the media writer never drained the release, so the outgoing "
            "inputs still hold their media")

        print("PASS: the relayout releases the outgoing inputs through the "
              "media writer")

        assert grid.calls == ["regenerate_buttons", "build"], (
            f"the editor grid saw {grid.calls}, expected a regenerate and a "
            f"build")
    finally:
        fixtures.teardown(controller)


fixtures.start_watchdog(120, "fake deck relayout")
check_relayout()
print("SCENARIO PASS")
