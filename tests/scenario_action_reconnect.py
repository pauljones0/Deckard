"""Action rows must stay wired after a mid-update return or raise.

The comment group disconnects its changed handler, reads the comment, then
reconnects. A read that raised left the row disconnected, so every later
comment edit was dropped silently, and the unguarded disconnect raised
TypeError on the next load.

The allow-image, allow-background and label toggles do the same around
set_active. A raise between the disconnect and the reconnect left the button
permanently dead, so the user could no longer hand media, background or label
control to another action. The label toggle strands all three of its buttons at
once, because it disconnects and reconnects them in a loop.

This harness builds no real GTK widget: it drives the real loaders and setters
on duck-typed stand-ins, the same pattern scenario_editor_reconnect uses.
"""

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)
import globals as gl

from src.backend import services
from src.windows.mainWindow.elements.Sidebar.elements.ActionConfigurator import (
    CommentGroup,
)
from src.windows.mainWindow.elements.Sidebar.elements.ActionManager import (
    ActionRow,
    ActionRowLabelToggle,
)

# A non-None stand-in for gl.app. The loaders only test it for None.
_APP = object()


class FakeWidget:
    """A widget that records its handlers by id, as GTK does.

    Supports both wiring calls the code may make: connect/disconnect by the
    tracked id (the fix), and disconnect_by_func (the old spelling), so the one
    scenario file exercises the code before and after the fix.
    """

    def __init__(self) -> None:
        self._handlers: dict[int, object] = {}
        self._next = 1
        self.raise_on_set = False

    def connect(self, signal, handler):
        hid = self._next
        self._next += 1
        self._handlers[hid] = handler
        return hid

    def disconnect(self, hid):
        del self._handlers[hid]

    def disconnect_by_func(self, func):
        matches = [hid for hid, h in self._handlers.items() if h == func]
        if not matches:
            # GTK raises TypeError when nothing is connected for the function.
            raise TypeError("nothing connected for that function")
        for hid in matches:
            del self._handlers[hid]

    def fire(self):
        """Deliver the signal to every connected handler, as GTK would."""
        for handler in list(self._handlers.values()):
            handler(self)

    def handler_count(self):
        return len(self._handlers)


class FakeEntryRow(FakeWidget):
    def __init__(self) -> None:
        super().__init__()
        self._text = ""

    def set_text(self, text):
        if self.raise_on_set:
            raise RuntimeError("set_text failed mid-update")
        self._text = text

    def get_text(self):
        return self._text


class FakeToggle(FakeWidget):
    def __init__(self) -> None:
        super().__init__()
        self._active = False

    def set_active(self, value):
        if self.raise_on_set:
            raise RuntimeError("set_active failed mid-update")
        self._active = value

    def get_active(self):
        return self._active


class FakePage:
    def __init__(self) -> None:
        self.comments: dict[int, str] = {}
        self.raise_on_get = False

    def get_action_comment(self, index, state, identifier):
        if self.raise_on_get:
            raise RuntimeError("page read failed mid-load")
        return self.comments.get(index)

    def set_action_comment(self, index, comment, state, identifier):
        self.comments[index] = comment


class FakeController:
    def __init__(self, page) -> None:
        self.active_page = page


class FakeDeckStack:
    def __init__(self, visible_child) -> None:
        self.visible_child = visible_child

    def get_visible_child(self):
        return self.visible_child


class FakeLeftArea:
    def __init__(self, deck_stack) -> None:
        self.deck_stack = deck_stack


class FakeVisibleChild:
    def __init__(self, controller) -> None:
        self.deck_controller = controller


class FakeActionEditor:
    def __init__(self) -> None:
        self.loads = 0

    def load_for_identifier(self, identifier, state):
        self.loads += 1


class FakeKeyEditor:
    def __init__(self) -> None:
        self.action_editor = FakeActionEditor()


class FakeSidebar:
    def __init__(self) -> None:
        self.key_editor = FakeKeyEditor()


class FakeMainWindow:
    def __init__(self, page) -> None:
        controller = FakeController(page)
        self.leftArea = FakeLeftArea(FakeDeckStack(FakeVisibleChild(controller)))
        self.sidebar = FakeSidebar()


class FakeAction:
    input_ident = object()
    state = 0


class FakeCommentGroup:
    """The real CommentGroup wiring on a duck-typed body."""

    load_for_action = CommentGroup.load_for_action
    connect_signals = CommentGroup.connect_signals
    disconnect_signals = CommentGroup.disconnect_signals
    on_comment_changed = CommentGroup.on_comment_changed
    get_comment = CommentGroup.get_comment
    set_comment = CommentGroup.set_comment

    def __init__(self) -> None:
        self.comment_row = FakeEntryRow()
        self.action = None
        self.index = 0
        self._comment_handler = None
        self.connect_signals()


class FakeIndicator:
    def __init__(self) -> None:
        self.classes = ["action-row-label-toggle-inactive"]

    def set_css_classes(self, classes):
        self.classes = list(classes)

    def get_css_classes(self):
        return self.classes


class FakeCheckButton(FakeToggle):
    def __init__(self, index) -> None:
        super().__init__()
        self.index = index

    def get_name(self):
        return str(self.index)


class FakeLabelToggle:
    """The real ActionRowLabelToggle wiring on a duck-typed body."""

    set_active = ActionRowLabelToggle.set_active
    connect_signals = ActionRowLabelToggle.connect_signals
    disconnect_signals = ActionRowLabelToggle.disconnect_signals
    on_label_toggled = ActionRowLabelToggle.on_label_toggled

    def __init__(self, action_row) -> None:
        self.action_row = action_row
        self.indicators = [FakeIndicator() for _ in range(3)]
        self.config_buttons = [FakeCheckButton(i) for i in range(3)]
        self._label_handlers: dict[int, int] = {}
        self.connect_signals()


class FakeLabelHost:
    def __init__(self) -> None:
        self.label_toggles: list[tuple[int, bool]] = []

    def label_toggled(self, i, value) -> None:
        self.label_toggles.append((i, value))


class FakeActionRow:
    """The real ActionRow toggle wiring on a duck-typed body."""

    set_image_toggled = ActionRow.set_image_toggled
    set_background_toggled = ActionRow.set_background_toggled
    connect_image_signal = ActionRow.connect_image_signal
    disconnect_image_signal = ActionRow.disconnect_image_signal
    connect_background_signal = ActionRow.connect_background_signal
    disconnect_background_signal = ActionRow.disconnect_background_signal

    def __init__(self) -> None:
        self.allow_image_toggle = FakeToggle()
        self.allow_background_toggle = FakeToggle()
        self._image_handler = None
        self._background_handler = None
        self.image_toggles = 0
        self.background_toggles = 0
        self.connect_image_signal()
        self.connect_background_signal()

    def on_allow_image_toggled(self, button) -> None:
        self.image_toggles += 1

    def on_allow_background_toggled(self, button) -> None:
        self.background_toggles += 1


def _install(page) -> None:
    gl.app = _APP
    mw = FakeMainWindow(page)
    services.require_main_window = lambda: mw


def test_comment_row_reconnects_after_midload_raise() -> None:
    """A load that raises mid-update must still leave the comment row wired."""
    page = FakePage()
    _install(page)

    group = FakeCommentGroup()
    assert group.comment_row.handler_count() == 1, "row must start wired"

    page.raise_on_get = True
    try:
        group.load_for_action(FakeAction(), 3)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the injected read failure did not propagate")

    assert group.comment_row.handler_count() == 1, (
        "a mid-load raise left the comment row disconnected"
    )

    # A later edit must reach the page.
    page.raise_on_get = False
    group.comment_row._text = "hello"
    group.comment_row.fire()
    assert page.comments.get(3) == "hello", (
        f"the comment edit was dropped after a mid-load raise; "
        f"page.comments={page.comments!r}"
    )


def test_comment_row_wires_exactly_once_across_loads() -> None:
    """Repeated loads must not raise and must leave exactly one handler."""
    page = FakePage()
    page.comments[1] = "note"
    _install(page)

    group = FakeCommentGroup()
    group.load_for_action(FakeAction(), 1)
    # Pre-fix the unguarded disconnect could raise TypeError here.
    group.load_for_action(FakeAction(), 1)

    assert group.comment_row.handler_count() == 1, (
        f"load wired {group.comment_row.handler_count()} handlers; a duplicate "
        "writes the comment twice per edit"
    )
    assert group.comment_row.get_text() == "note", (
        "the row must show the stored comment"
    )

    group.comment_row.fire()
    assert page.comments[1] == "note"


def test_image_toggle_reconnects_after_midupdate_raise() -> None:
    """A set_image_toggled that raises must still leave the toggle wired."""
    row = FakeActionRow()
    assert row.allow_image_toggle.handler_count() == 1, "toggle must start wired"

    row.allow_image_toggle.raise_on_set = True
    try:
        row.set_image_toggled(True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the injected set_active failure did not propagate")

    assert row.allow_image_toggle.handler_count() == 1, (
        "a mid-update raise left the image toggle permanently disconnected"
    )

    row.allow_image_toggle.raise_on_set = False
    row.allow_image_toggle.fire()
    assert row.image_toggles == 1, (
        "the image toggle stopped reporting clicks after a mid-update raise"
    )


def test_background_toggle_reconnects_after_midupdate_raise() -> None:
    """A set_background_toggled that raises must still leave the toggle wired."""
    row = FakeActionRow()
    assert row.allow_background_toggle.handler_count() == 1, "toggle must start wired"

    row.allow_background_toggle.raise_on_set = True
    try:
        row.set_background_toggled(True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the injected set_active failure did not propagate")

    assert row.allow_background_toggle.handler_count() == 1, (
        "a mid-update raise left the background toggle permanently disconnected"
    )

    row.allow_background_toggle.raise_on_set = False
    row.allow_background_toggle.fire()
    assert row.background_toggles == 1, (
        "the background toggle stopped reporting clicks after a mid-update raise"
    )


def test_toggles_wire_exactly_once_across_updates() -> None:
    """Repeated updates must leave one handler, so one click reports once."""
    row = FakeActionRow()
    row.set_image_toggled(True)
    row.set_image_toggled(False)
    row.set_background_toggled(True)
    row.set_background_toggled(False)

    assert row.allow_image_toggle.handler_count() == 1, (
        f"image toggle carries {row.allow_image_toggle.handler_count()} handlers"
    )
    assert row.allow_background_toggle.handler_count() == 1, (
        f"background toggle carries "
        f"{row.allow_background_toggle.handler_count()} handlers"
    )

    row.allow_image_toggle.fire()
    row.allow_background_toggle.fire()
    assert row.image_toggles == 1, f"one click reported {row.image_toggles} times"
    assert row.background_toggles == 1, (
        f"one click reported {row.background_toggles} times"
    )

    # The update itself must never re-enter the click handler.
    assert row.allow_image_toggle.get_active() is False


def test_label_toggle_reconnects_after_midupdate_raise() -> None:
    """A set_active that raises partway must still leave every button wired.

    The label toggle disconnects and reconnects its three buttons in a loop, so
    one raise mid-loop once stranded all of them at once."""
    host = FakeLabelHost()
    toggle = FakeLabelToggle(host)
    for button in toggle.config_buttons:
        assert button.handler_count() == 1, "every label button must start wired"

    # A values list longer than the indicator row: the loop raises IndexError
    # after it has already updated the earlier buttons.
    try:
        toggle.set_active([True, False, True, False])
    except IndexError:
        pass
    else:
        raise AssertionError("the over-long values list did not raise")

    for i, button in enumerate(toggle.config_buttons):
        assert button.handler_count() == 1, (
            f"a mid-update raise left label button {i} with "
            f"{button.handler_count()} handlers"
        )

    toggle.config_buttons[2].fire()
    assert host.label_toggles == [(2, True)], (
        f"the label button stopped reporting clicks; "
        f"host.label_toggles={host.label_toggles!r}"
    )


def test_label_toggle_wires_exactly_once_across_updates() -> None:
    """Repeated updates must leave one handler per button, so one click
    reports once."""
    host = FakeLabelHost()
    toggle = FakeLabelToggle(host)

    toggle.set_active([True, True, False])
    toggle.set_active([False, False, False])

    for i, button in enumerate(toggle.config_buttons):
        assert button.handler_count() == 1, (
            f"label button {i} carries {button.handler_count()} handlers; a "
            f"duplicate reports one click twice"
        )

    # The update itself must never re-enter the click handler.
    assert host.label_toggles == [], (
        f"set_active re-entered the handler; host.label_toggles={host.label_toggles!r}"
    )

    toggle.config_buttons[0].fire()
    assert len(host.label_toggles) == 1, (
        f"one click reported {len(host.label_toggles)} times"
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_action_reconnect")
    test_comment_row_reconnects_after_midload_raise()
    test_comment_row_wires_exactly_once_across_loads()
    test_image_toggle_reconnects_after_midupdate_raise()
    test_background_toggle_reconnects_after_midupdate_raise()
    test_toggles_wire_exactly_once_across_updates()
    test_label_toggle_reconnects_after_midupdate_raise()
    test_label_toggle_wires_exactly_once_across_updates()
    print("PASS: scenario_action_reconnect")


if __name__ == "__main__":
    main()
