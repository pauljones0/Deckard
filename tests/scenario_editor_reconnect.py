"""Sidebar rows must stay wired after a mid-load return.

Every row loader disconnects its value-changed handler, looks the input up,
then reconnects. A lookup that returns early (an input the deck does not carry,
a page not yet loaded) once left the handler disconnected: the user edited the
spinner and nothing was saved, silently. The alignment row escalated it,
because its disconnect had no guard, so the next load raised TypeError and
aborted the whole sidebar load before the label, action and background editors
ran.

This harness builds no real GTK widget, so it drives the real SizeRow and
AlignmentRow loaders on duck-typed stand-ins, the same pattern
scenario_store_branch_contract uses.
"""

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)
import globals as gl

from src.backend import services
from src.windows.mainWindow.elements.Sidebar.elements.ImageEditor import (
    AlignmentRow,
    SizeRow,
)

# A non-None stand-in for gl.app. The loaders only test it for None.
_APP = object()


class FakeButton:
    """A spin button that records its value-changed handlers by id.

    Supports both wiring calls the code may make: connect/disconnect by the
    tracked id (the fix), and disconnect_by_func (the old spelling), so the one
    scenario file exercises the code before and after the fix.
    """

    def __init__(self) -> None:
        self._handlers: dict[int, object] = {}
        self._next = 1
        self._value = 0.0

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

    def set_value(self, value):
        self._value = value

    def get_value(self):
        return self._value

    def fire(self):
        """Deliver a value-changed to every connected handler, as GTK would."""
        for handler in list(self._handlers.values()):
            handler(self)

    def handler_count(self):
        return len(self._handlers)


class FakeRevert:
    def __init__(self) -> None:
        self.visible = None

    def set_visible(self, value):
        self.visible = value


class FakeSpinner:
    def __init__(self) -> None:
        self.button = FakeButton()
        self.revert_button = FakeRevert()


class FakeLayout:
    def __init__(self, size=None, valign=0.0, halign=0.0) -> None:
        self.size = size
        self.valign = valign
        self.halign = halign


class FakeLayoutManager:
    def __init__(self, layout, use_props) -> None:
        self._layout = layout
        self._use = use_props

    def get_use_page_layout_properties(self):
        return self._use

    def get_composed_layout(self):
        return self._layout


class FakeState:
    def __init__(self, layout, use_props) -> None:
        self.layout_manager = FakeLayoutManager(layout, use_props)


class FakeControllerInput:
    def __init__(self, layout, use_props) -> None:
        self._state = FakeState(layout, use_props)

    def get_active_state(self):
        return self._state


class FakeController:
    def __init__(self, controller_input) -> None:
        self._ci = controller_input

    def get_input(self, identifier):
        return self._ci


class FakeVisibleChild:
    def __init__(self, controller) -> None:
        self.deck_controller = controller


class FakeDeckStack:
    def __init__(self, visible_child) -> None:
        self._vc = visible_child

    def get_visible_child(self):
        return self._vc


class FakeLeftArea:
    def __init__(self, deck_stack) -> None:
        self.deck_stack = deck_stack


class FakePage:
    def __init__(self) -> None:
        self.media_size = None
        self.media_valign = None
        self.calls: list[tuple] = []

    def set_media_size(self, identifier, state, size):
        self.media_size = size
        self.calls.append(("size", identifier, state, size))

    def set_media_valign(self, identifier, state, value):
        self.media_valign = value
        self.calls.append(("valign", identifier, state, value))


class FakeMainWindow:
    def __init__(self, controller, page) -> None:
        self._controller = controller
        self._page = page
        self.leftArea = FakeLeftArea(FakeDeckStack(FakeVisibleChild(controller)))

    def get_active_controller(self):
        return self._controller

    def get_active_page(self):
        return self._page


class FakeSizeRow:
    """The real SizeRow loaders on a duck-typed body."""

    load_for_identifier = SizeRow.load_for_identifier
    update_values = SizeRow.update_values
    connect_signals = SizeRow.connect_signals
    disconnect_signals = SizeRow.disconnect_signals
    on_size_changed = SizeRow.on_size_changed

    def __init__(self) -> None:
        self.size_spinner = FakeSpinner()
        self.active_identifier = None
        self.active_state = None
        self._value_handler = None
        self.connect_signals()


class FakeAlignmentRow:
    """The real AlignmentRow loaders on a duck-typed body."""

    load_for_identifier = AlignmentRow.load_for_identifier
    update_values = AlignmentRow.update_values
    connect_signals = AlignmentRow.connect_signals
    disconnect_signals = AlignmentRow.disconnect_signals
    on_alignment_changed = AlignmentRow.on_alignment_changed

    def __init__(self) -> None:
        self.alignment_spinner = FakeSpinner()
        self.property_name = "valign"
        self.active_identifier = None
        self.active_state = None
        self._value_handler = None
        self.connect_signals()


def _install(controller, page) -> None:
    gl.app = _APP
    mw = FakeMainWindow(controller, page)
    services.require_main_window = lambda: mw


def test_size_row_reconnects_after_midload_return() -> None:
    """A load that cannot resolve the input must still leave the spinner wired,
    so the next edit is saved."""
    page = FakePage()
    # Controller present, but the input is not carried: a mid-load return.
    _install(FakeController(None), page)

    row = FakeSizeRow()
    assert row.size_spinner.button.handler_count() == 1, "row must start wired"

    identifier = object()
    row.load_for_identifier(identifier, 0)

    assert row.size_spinner.button.handler_count() == 1, (
        "a mid-load return left the size spinner disconnected"
    )

    # A later edit must reach the page.
    row.size_spinner.button.set_value(150)
    row.size_spinner.button.fire()
    assert page.media_size == 1.5, (
        f"the spinner change was dropped after a mid-load return; "
        f"page.media_size={page.media_size!r}"
    )


def test_size_row_wires_exactly_once_on_load() -> None:
    """A full load must leave exactly one handler, so one edit writes once."""
    page = FakePage()
    layout = FakeLayout(size=0.8)
    controller = FakeController(FakeControllerInput(layout, {"size": True}))
    _install(controller, page)

    row = FakeSizeRow()
    row.load_for_identifier(object(), 0)

    assert row.size_spinner.button.handler_count() == 1, (
        f"load wired {row.size_spinner.button.handler_count()} handlers; a "
        "duplicate fires the page setter twice per edit"
    )
    assert row.size_spinner.button.get_value() == 80.0, (
        "the spinner must show the composed size"
    )

    row.size_spinner.button.fire()
    assert len(page.calls) == 1, f"one edit produced {len(page.calls)} writes"


def test_alignment_row_survives_repeated_midload_returns() -> None:
    """Repeated mid-load returns must not raise and must leave the row wired.

    The alignment disconnect once had no guard, so the second load raised
    TypeError and aborted the whole sidebar load."""
    page = FakePage()
    _install(FakeController(None), page)

    row = FakeAlignmentRow()
    assert row.alignment_spinner.button.handler_count() == 1, "row must start wired"

    identifier = object()
    row.load_for_identifier(identifier, 0)
    # Pre-fix this second call raised TypeError, aborting the sidebar load.
    row.load_for_identifier(identifier, 0)

    assert row.alignment_spinner.button.handler_count() == 1, (
        "repeated mid-load returns left the alignment spinner disconnected"
    )

    row.alignment_spinner.button.set_value(0.5)
    row.alignment_spinner.button.fire()
    assert page.media_valign == 0.5, (
        f"the alignment change was dropped; page.media_valign={page.media_valign!r}"
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_editor_reconnect")
    test_size_row_reconnects_after_midload_return()
    test_size_row_wires_exactly_once_on_load()
    test_alignment_row_survives_repeated_midload_returns()
    print("PASS: scenario_editor_reconnect")


if __name__ == "__main__":
    main()
