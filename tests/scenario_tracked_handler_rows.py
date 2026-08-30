"""Verify idempotent handler-id connection and disconnection on duck-typed row
widgets, including failed updates and repeated loads."""

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import threading

import globals as gl  # noqa: F401  (import-time DATA_PATH resolution)

from GtkHelper.GenerativeUI.ColorButtonRow import ColorButtonRow
from GtkHelper.GenerativeUI.EntryRow import EntryRow
from GtkHelper.GenerativeUI.ScaleRow import ScaleRow as GenScaleRow
from GtkHelper.GenerativeUI.SpinRow import SpinRow
from GtkHelper.GenerativeUI.SwitchRow import SwitchRow
from GtkHelper.GenerativeUI.ToggleRow import ToggleRow
from GtkHelper.ScaleRow import ScaleRow
from src.windows.PageManager.elements.PageEditor import ScreensaverGroup
from src.windows.Settings.PluginSettingsWindow.PluginSettingsWindow import (
    PluginSettingsPage,
)


class FakeWidget:
    """Record handlers by id and reject function-based disconnection."""

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
        if hid not in self._handlers:
            # GTK warns and leaves the handler in place for an unknown id.
            raise ValueError(f"no handler with id {hid}")
        del self._handlers[hid]

    def disconnect_by_func(self, func):
        raise AssertionError("a row disconnected by function, not by tracked id")

    def fire(self):
        for handler in list(self._handlers.values()):
            handler(self, None)

    def handler_count(self):
        return len(self._handlers)

    def set_text(self, text):
        if self.raise_on_set:
            raise RuntimeError("set_text failed mid-update")
        self._text = text

    def get_text(self):
        return getattr(self, "_text", "")

    def get_position(self):
        return 0

    def set_position(self, position):
        pass

    def set_active(self, value):
        if self.raise_on_set:
            raise RuntimeError("set_active failed mid-update")


class FakeColorRow(FakeWidget):
    def __init__(self) -> None:
        super().__init__()
        self.color_button = FakeWidget()


class FakeScaleWidget(FakeWidget):
    def __init__(self) -> None:
        super().__init__()
        self.scale = FakeWidget()


class FakeToggleWidget(FakeWidget):
    def __init__(self) -> None:
        super().__init__()
        self.toggle_group = FakeWidget()


def _make_gen_row(cls, widget):
    """Build only the wiring state required by generative row signal methods."""
    row = object.__new__(cls)
    row._signal_handlers = {}
    row._widget = widget
    row._built = True
    row._build_flag_lock = threading.Lock()
    row._build_fn = None
    return row


def _check_row(cls, widget, carrier, label) -> None:
    """One generative-ui row wires once, unwires once, and survives both twice."""
    row = _make_gen_row(cls, widget)

    row.connect_signals()
    assert carrier.handler_count() == 1, (
        f"{label}: a first connect left {carrier.handler_count()} handlers"
    )

    row.connect_signals()
    assert carrier.handler_count() == 1, (
        f"{label}: a second connect stacked a handler, so one change reports twice"
    )

    row.disconnect_signals()
    assert carrier.handler_count() == 0, (
        f"{label}: the disconnect left {carrier.handler_count()} handlers on the widget"
    )

    # A second teardown must not raise. The row is disconnected in both cases.
    row.disconnect_signals()
    assert carrier.handler_count() == 0, f"{label}: a double disconnect changed the count"

    row.connect_signals()
    assert carrier.handler_count() == 1, (
        f"{label}: the row did not come back after a disconnect"
    )


def test_generative_rows_wire_once_and_unwire_once() -> None:
    switch = FakeWidget()
    _check_row(SwitchRow, switch, switch, "SwitchRow")

    entry = FakeWidget()
    _check_row(EntryRow, entry, entry, "EntryRow")

    color = FakeColorRow()
    _check_row(ColorButtonRow, color, color.color_button, "ColorButtonRow")

    scale = FakeScaleWidget()
    _check_row(GenScaleRow, scale, scale.scale, "GenerativeUI ScaleRow")

    toggle = FakeToggleWidget()
    _check_row(ToggleRow, toggle, toggle.toggle_group, "ToggleRow")


def test_spin_row_tracks_both_of_its_widgets() -> None:
    """The spin row tracks its adjustment and row handlers separately."""
    row = _make_gen_row(SpinRow, FakeWidget())
    row._adjustment = FakeWidget()

    row.connect_signals()
    row.connect_signals()
    assert row._adjustment.handler_count() == 1, "the adjustment stacked a handler"
    assert row._widget.handler_count() == 1, "the spin row stacked a handler"

    row.disconnect_signals()
    row.disconnect_signals()
    assert row._adjustment.handler_count() == 0, "the adjustment stayed wired"
    assert row._widget.handler_count() == 0, "the spin row stayed wired"


def test_entry_row_reset_leaves_one_handler() -> None:
    """A cursor-preserving reset reconnects once, including after a failed write."""
    widget = FakeWidget()
    row = _make_gen_row(EntryRow, widget)
    row.filter_func = None
    row.connect_signals()

    row._text_reset("abc")
    assert widget.handler_count() == 1, (
        f"a text reset left {widget.handler_count()} handlers on the row"
    )

    # A write that raises must still leave the row wired.
    widget.raise_on_set = True
    try:
        row._text_reset("def")
    except RuntimeError:
        pass
    else:
        raise AssertionError("the failing set_text did not raise")
    assert widget.handler_count() == 1, (
        f"a failed text reset left {widget.handler_count()} handlers on the row"
    )


def _make_scale_row(add_text_entry):
    row = ScaleRow.__new__(ScaleRow)
    row._handlers = {}
    row._add_text_entry = add_text_entry
    row._adjustment = FakeWidget()
    row.entry_row = FakeWidget()
    row.entry_row_controller = FakeWidget()
    row.scale = FakeWidget()
    return row


def test_scale_row_wires_every_binding_once() -> None:
    row = _make_scale_row(add_text_entry=True)

    row._connect_signals()
    row._connect_signals()
    assert row._adjustment.handler_count() == 1, "the adjustment stacked a handler"
    assert row.entry_row.handler_count() == 2, (
        f"the entry row carries {row.entry_row.handler_count()} handlers; it takes "
        f"exactly two, one for activate and one for changed"
    )
    assert row.entry_row_controller.handler_count() == 1, "the focus controller stacked"

    row._disconnect_signals()
    row._disconnect_signals()
    assert row._adjustment.handler_count() == 0, "the adjustment stayed wired"
    assert row.entry_row.handler_count() == 0, "the entry row stayed wired"
    assert row.entry_row_controller.handler_count() == 0, "the controller stayed wired"


def test_scale_row_reset_reconnects_after_raise() -> None:
    """A text write that raises must still leave the row wired."""
    row = _make_scale_row(add_text_entry=True)
    row._connect_signals()
    row.entry_row.raise_on_set = True
    row._adjustment._value = 1.0
    row._adjustment.get_value = lambda: 1.0

    try:
        row._reset_entry_row()
    except RuntimeError:
        pass
    else:
        raise AssertionError("the failing set_text did not raise")

    assert row.entry_row.handler_count() == 2, (
        f"a failed reset left {row.entry_row.handler_count()} handlers on the entry"
    )


def _make_screensaver_group():
    group = ScreensaverGroup.__new__(ScreensaverGroup)
    group._handlers = {}
    group.overwrite_expander = FakeWidget()
    group.enable_screensaver_toggle = FakeWidget()
    group.delay_spin = FakeWidget()
    group.loop_toggle = FakeWidget()
    group.fps_spin = FakeWidget()
    group.brightness_scale = FakeScaleWidget()
    group.media_selector_button = FakeWidget()
    return group


def test_page_editor_group_wires_once_per_load() -> None:
    """Repeated page loads leave one handler on each row."""
    group = _make_screensaver_group()
    group.load_config_settings = lambda page_path: None

    for _ in range(3):
        group.load_for_page("page.json")

    assert group.brightness_scale.scale.handler_count() == 1, (
        f"the brightness scale carries "
        f"{group.brightness_scale.scale.handler_count()} handlers"
    )
    assert group.brightness_scale.handler_count() == 0, (
        "the handler went on the row wrapper, not on the scale inside it"
    )
    assert group.fps_spin.handler_count() == 1, "the fps spinner stacked a handler"

    # A wire that follows no unwire must be a no-op too, so a group that is
    # already live cannot gain a second handler per row.
    group.connect_events()
    assert group.fps_spin.handler_count() == 1, (
        f"a repeated wire left {group.fps_spin.handler_count()} handlers on the "
        f"fps spinner; a duplicate saves one edit twice"
    )


def test_page_editor_group_reconnects_after_midload_raise() -> None:
    """A load that raises must still leave the group wired."""
    group = _make_screensaver_group()

    def _raise(page_path):
        raise RuntimeError("page read failed mid-load")

    group.load_config_settings = _raise

    try:
        group.load_for_page("page.json")
    except RuntimeError:
        pass
    else:
        raise AssertionError("the failing load did not raise")

    assert group.overwrite_expander.handler_count() == 1, (
        "a mid-load raise left the overwrite expander disconnected"
    )
    assert group.media_selector_button.handler_count() == 1, (
        "a mid-load raise left the media selector disconnected"
    )


def test_page_editor_group_teardown_is_idempotent() -> None:
    """Teardown runs after a load already dropped the handlers."""
    group = _make_screensaver_group()
    group.load_config_settings = lambda page_path: None
    group.load_for_page("page.json")

    group.disconnect_events()
    group.disconnect_events()

    assert group.loop_toggle.handler_count() == 0, "the loop toggle stayed wired"
    assert group.delay_spin.handler_count() == 0, "the delay spinner stayed wired"


def test_plugin_settings_flow_box_wires_once() -> None:
    page = PluginSettingsPage.__new__(PluginSettingsPage)
    page._flow_box_handler = None
    page.flow_box = FakeWidget()

    def _on_click(*args):
        pass

    page.connect_flow_box(_on_click)
    page.connect_flow_box(_on_click)
    assert page.flow_box.handler_count() == 1, (
        f"the tile grid carries {page.flow_box.handler_count()} handlers; a "
        f"duplicate opens two dialogs for one click"
    )

    page.disconnect_flow_box()
    page.disconnect_flow_box()
    assert page.flow_box.handler_count() == 0, "the tile grid stayed wired"


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_tracked_handler_rows")
    test_generative_rows_wire_once_and_unwire_once()
    test_spin_row_tracks_both_of_its_widgets()
    test_entry_row_reset_leaves_one_handler()
    test_scale_row_wires_every_binding_once()
    test_scale_row_reset_reconnects_after_raise()
    test_page_editor_group_wires_once_per_load()
    test_page_editor_group_reconnects_after_midload_raise()
    test_page_editor_group_teardown_is_idempotent()
    test_plugin_settings_flow_box_wires_once()
    print("PASS: scenario_tracked_handler_rows")


if __name__ == "__main__":
    main()
