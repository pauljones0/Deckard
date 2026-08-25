"""Sidebar and deck-settings rows must stay wired after a mid-update raise.

Four more rows disconnect a handler, update the widget, then reconnect: the
event-assigner combo row, the state switcher stack, the deck background row and
the screensaver row. A raise between the disconnect and the reconnect left the
row permanently dead, so every later selection, state switch or settings change
was dropped silently.

This harness builds no real GTK widget: it drives the real loaders and setters
on duck-typed stand-ins, the same pattern scenario_action_reconnect uses.
"""

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)
import globals as gl

from src.windows.mainWindow.elements.Sidebar.elements.ActionConfigurator import (
    EventAssignerRow,
)
from src.windows.mainWindow.elements.Sidebar.elements.StateSwitcher import StateSwitcher
from src.windows.mainWindow.elements.DeckSettings.BackgroundGroup import (
    BackgroundMediaRow,
)
from src.windows.mainWindow.elements.DeckSettings.DeckGroup import (
    Brightness,
    Rotation,
    Saturation,
    Screensaver,
)


class FakeWidget:
    """A widget that records its handlers by id, as GTK does."""

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
        for handler in list(self._handlers.values()):
            handler(self)

    def handler_count(self):
        return len(self._handlers)


class FakeSetValueWidget(FakeWidget):
    def __init__(self) -> None:
        super().__init__()
        self.value = None

    def set_active(self, value):
        if self.raise_on_set:
            raise RuntimeError("set_active failed mid-update")
        self.value = value

    def get_active(self):
        return self.value

    def set_value(self, value):
        if self.raise_on_set:
            raise RuntimeError("set_value failed mid-update")
        self.value = value

    def get_value(self):
        return self.value


class FakeBox:
    def __init__(self) -> None:
        self.visible = None

    def set_visible(self, value):
        self.visible = value


# --- 1. EventAssignerRow ----------------------------------------------------

class FakeModelItem:
    def __init__(self, id) -> None:
        self.id = id


class FakeModel:
    def __init__(self, items) -> None:
        self.items = list(items)

    def get_n_items(self):
        return len(self.items)

    def get_item(self, i):
        return self.items[i]

    def append(self, item):
        self.items.append(item)


class FakeEventAssignerRow(FakeWidget):
    """The real EventAssignerRow wiring on a duck-typed body."""

    _connect_signal = EventAssignerRow._connect_signal
    _disconnect_signal = EventAssignerRow._disconnect_signal
    select_event = EventAssignerRow.select_event
    set_available_events = EventAssignerRow.set_available_events

    def set_model(self, model):
        self._model = model

    def __init__(self, items) -> None:
        super().__init__()
        self._selected_handler = None
        self._model = FakeModel(items)
        self.selected = None
        self.changes = 0
        self._connect_signal()

    def get_model(self):
        return self._model

    def set_selected(self, index):
        if self.raise_on_set:
            raise RuntimeError("set_selected failed mid-update")
        self.selected = index

    def on_changed(self, *args):
        self.changes += 1


class FakeAssigner:
    def __init__(self, id) -> None:
        self.id = id


def test_event_row_reconnects_after_midselect_raise() -> None:
    row = FakeEventAssignerRow([FakeModelItem(None), FakeModelItem("a")])
    assert row.handler_count() == 1, "the row must start wired"

    row.raise_on_set = True
    try:
        row.select_event(FakeAssigner("a"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("the injected set_selected failure did not propagate")

    assert row.handler_count() == 1, (
        "a mid-update raise left the event row disconnected"
    )

    row.raise_on_set = False
    row.fire()
    assert row.changes == 1, (
        "the event row stopped reporting selections after a mid-update raise"
    )


def test_event_row_reconnects_after_midrefill_raise() -> None:
    row = FakeEventAssignerRow([])
    row.raise_on_set = True
    try:
        row.set_available_events([])
    except RuntimeError:
        pass
    else:
        raise AssertionError("the injected set_selected failure did not propagate")

    assert row.handler_count() == 1, (
        "a mid-refill raise left the event row disconnected"
    )

    row.raise_on_set = False
    row.set_available_events([])
    assert row.handler_count() == 1, (
        f"the event row carries {row.handler_count()} handlers after a refill"
    )
    assert row.changes == 0, "a refill must not fire the selection handler"


def test_event_row_reconnects_when_model_is_missing() -> None:
    row = FakeEventAssignerRow([])
    row._model = None
    row.select_event(None)
    assert row.handler_count() == 1, (
        "the early return on a missing model left the event row disconnected"
    )


def test_event_row_wires_exactly_once_across_selects() -> None:
    row = FakeEventAssignerRow([FakeModelItem(None), FakeModelItem("a")])
    row.select_event(FakeAssigner("a"))
    row.select_event(None)
    # No match at all: the fall-through path must also reconnect exactly once.
    row.select_event(FakeAssigner("missing"))

    assert row.handler_count() == 1, (
        f"the event row carries {row.handler_count()} handlers; a duplicate "
        "reports one selection twice"
    )
    assert row.changes == 0, "a programmatic select must not fire the handler"

    row.fire()
    assert row.changes == 1, f"one selection reported {row.changes} times"


# --- 2. StateSwitcher -------------------------------------------------------

class FakeStack(FakeWidget):
    def __init__(self) -> None:
        super().__init__()
        self.children: list[str] = []
        self.visible = None

    def set_visible_child_name(self, name):
        if self.raise_on_set:
            raise RuntimeError("set_visible_child_name failed mid-update")
        self.visible = name

    def get_visible_child_name(self):
        return self.visible

    def add_titled(self, child, name, title):
        if self.raise_on_set:
            raise RuntimeError("add_titled failed mid-update")
        self.children.append(name)

    def get_first_child(self):
        return self.children[0] if self.children else None

    def remove(self, child):
        self.children.remove(child)


class FakeStateSwitcher:
    """The real StateSwitcher wiring on a duck-typed body."""

    _connect_signal = StateSwitcher._connect_signal
    _disconnect_signal = StateSwitcher._disconnect_signal
    select_state = StateSwitcher.select_state
    set_n_states = StateSwitcher.set_n_states
    clear_stack = StateSwitcher.clear_stack
    on_state_switch = StateSwitcher.on_state_switch

    def __init__(self) -> None:
        self.stack = FakeStack()
        self._switch_handler = None
        self.switch_callbacks: list = []
        self.switches: list[int] = []
        self.switch_callbacks.append(lambda: self.switches.append(1))
        self.set_n_states(3)

    def get_n_states(self):
        return len(self.stack.children)


def test_state_switcher_reconnects_after_midselect_raise() -> None:
    switcher = FakeStateSwitcher()
    assert switcher.stack.handler_count() == 1, "the stack must start wired"

    switcher.stack.raise_on_set = True
    try:
        switcher.select_state(1)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the injected switch failure did not propagate")

    assert switcher.stack.handler_count() == 1, (
        "a mid-update raise left the state stack disconnected"
    )

    switcher.stack.raise_on_set = False
    switcher.stack.fire()
    assert switcher.switches == [1], (
        f"the stack stopped reporting state switches; switches={switcher.switches!r}"
    )


def test_state_switcher_reconnects_after_midfill_raise() -> None:
    switcher = FakeStateSwitcher()
    switcher.stack.raise_on_set = True
    try:
        switcher.set_n_states(2)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the injected add_titled failure did not propagate")

    assert switcher.stack.handler_count() == 1, (
        "a mid-fill raise left the state stack disconnected"
    )


def test_state_switcher_wires_exactly_once() -> None:
    switcher = FakeStateSwitcher()
    switcher.set_n_states(3)
    switcher.select_state(1)
    switcher.select_state(2)

    assert switcher.stack.handler_count() == 1, (
        f"the stack carries {switcher.stack.handler_count()} handlers; a "
        "duplicate reports one switch twice"
    )
    assert switcher.switches == [], (
        "a programmatic state select must not fire the switch callbacks"
    )

    switcher.stack.fire()
    assert len(switcher.switches) == 1, (
        f"one switch reported {len(switcher.switches)} times"
    )


# --- 3 and 4. Deck-settings rows -------------------------------------------

class FakeSection:
    def __init__(self, values, raise_on_get) -> None:
        self.values = values
        self.raise_on_get = raise_on_get

    def __getitem__(self, key):
        if self.raise_on_get:
            raise RuntimeError("settings read failed mid-load")
        return self.values[key]


class FakeDeckSettings:
    def __init__(self, values) -> None:
        self.values = values
        self.raise_on_get = False

    def section(self, name):
        return FakeSection(self.values, self.raise_on_get)

    def get(self, *keys, default=None):
        if self.raise_on_get:
            raise RuntimeError("settings read failed mid-load")
        return self.values[keys[-1]]


class FakeSettingsManager:
    def __init__(self, settings) -> None:
        self.settings = settings

    def deck(self, serial):
        return self.settings


class FakeDeck:
    def is_touch(self):
        return True


class FakeDeckController:
    deck = FakeDeck()


class FakeSettingsPage:
    deck_controller = FakeDeckController()


_BACKGROUND_VALUES = {
    "enable": True,
    "loop": False,
    "fps": 10,
    "extend-to-touchscreen": False,
    "media-path": None,
}

_SCREENSAVER_VALUES = {
    "enable": True,
    "time-delay": 5,
    "loop": False,
    "fps": 10,
    "brightness": 50,
    "media-path": None,
}


class FakeBackgroundRow:
    """The real BackgroundMediaRow wiring on a duck-typed body."""

    _signal_bindings = BackgroundMediaRow._signal_bindings
    connect_signals = BackgroundMediaRow.connect_signals
    disconnect_signals = BackgroundMediaRow.disconnect_signals
    load_defaults = BackgroundMediaRow.load_defaults

    def __init__(self) -> None:
        self.deck_serial_number = "FAKE"
        self.settings_page = FakeSettingsPage()
        self.on_map_tasks: list = []
        self._handlers: dict[str, int] = {}
        self.enable_switch = FakeSetValueWidget()
        self.media_selector_button = FakeWidget()
        self.loop_switch = FakeSetValueWidget()
        self.fps_spinner = FakeSetValueWidget()
        self.extend_touchscreen_switch = FakeSetValueWidget()
        self.config_box = FakeBox()
        self.extend_touchscreen_box = FakeBox()
        self.thumbnails = 0
        self.calls: list[str] = []
        self.connect_signals()

    def get_mapped(self):
        return True

    def set_thumbnail(self, path):
        self.thumbnails += 1

    def on_toggle_enable(self, *args):
        self.calls.append("enable")

    def on_choose_image(self, *args):
        self.calls.append("media")

    def on_toggle_loop(self, *args):
        self.calls.append("loop")

    def on_change_fps(self, *args):
        self.calls.append("fps")

    def on_toggle_extend_touchscreen(self, *args):
        self.calls.append("extend")


class FakeScreensaverRow:
    """The real Screensaver wiring on a duck-typed body."""

    _signal_bindings = Screensaver._signal_bindings
    connect_signals = Screensaver.connect_signals
    disconnect_signals = Screensaver.disconnect_signals
    load_defaults = Screensaver.load_defaults

    def __init__(self) -> None:
        self.deck_serial_number = "FAKE"
        self.settings_page = FakeSettingsPage()
        self._handlers: dict[str, int] = {}
        self.enable_switch = FakeSetValueWidget()
        self.time_spinner = FakeSetValueWidget()
        self.media_selector_button = FakeWidget()
        self.loop_switch = FakeSetValueWidget()
        self.fps_spinner = FakeSetValueWidget()
        self.scale = FakeSetValueWidget()
        self.config_box = FakeBox()
        self.thumbnails = 0
        self.calls: list[str] = []
        self.connect_signals()

    def set_thumbnail(self, path):
        self.thumbnails += 1

    def on_toggle_enable(self, *args):
        self.calls.append("enable")

    def on_change_time(self, *args):
        self.calls.append("time")

    def on_choose_image(self, *args):
        self.calls.append("media")

    def on_toggle_loop(self, *args):
        self.calls.append("loop")

    def on_change_fps(self, *args):
        self.calls.append("fps")

    def on_change_brightness(self, *args):
        self.calls.append("brightness")


def _install_settings(values):
    settings = FakeDeckSettings(values)
    gl.settings_manager = FakeSettingsManager(settings)
    return settings


def _widgets(row):
    return [widget for _key, widget, _s, _c in row._signal_bindings()]


def _run_settings_row_case(row, settings, label) -> None:
    for widget in _widgets(row):
        assert widget.handler_count() == 1, f"{label}: every widget must start wired"

    settings.raise_on_get = True
    try:
        row.load_defaults()
    except RuntimeError:
        pass
    else:
        raise AssertionError(f"{label}: the injected settings failure did not propagate")

    for widget in _widgets(row):
        assert widget.handler_count() == 1, (
            f"{label}: a mid-load raise left a widget with "
            f"{widget.handler_count()} handlers"
        )

    settings.raise_on_get = False
    row.load_defaults()
    row.load_defaults()

    for widget in _widgets(row):
        assert widget.handler_count() == 1, (
            f"{label}: load wired {widget.handler_count()} handlers; a duplicate "
            "writes the setting twice per change"
        )

    assert row.calls == [], f"{label}: a programmatic load must not fire the handlers"

    for widget in _widgets(row):
        widget.fire()
    assert len(row.calls) == len(_widgets(row)), (
        f"{label}: one change per widget reported {row.calls!r}"
    )


def test_background_row_reconnects_after_midload_raise() -> None:
    settings = _install_settings(_BACKGROUND_VALUES)
    _run_settings_row_case(FakeBackgroundRow(), settings, "background row")


def test_screensaver_row_reconnects_after_midload_raise() -> None:
    settings = _install_settings(_SCREENSAVER_VALUES)
    _run_settings_row_case(FakeScreensaverRow(), settings, "screensaver row")


# --- 5, 6 and 7. The single-widget deck-settings rows -----------------------

class FakeToggleGroup(FakeWidget):
    def __init__(self) -> None:
        super().__init__()
        self.active_name = None

    def set_active_name(self, name):
        if self.raise_on_set:
            raise RuntimeError("set_active_name failed mid-update")
        self.active_name = name

    def get_active_name(self):
        return self.active_name


class FakeRotationRow:
    """The real Rotation wiring on a duck-typed body."""

    connect_signal = Rotation.connect_signal
    disconnect_signal = Rotation.disconnect_signal
    load_default = Rotation.load_default

    def __init__(self) -> None:
        self.deck_serial_number = "FAKE"
        self._rotation_handler = None
        self.toggle_group = FakeToggleGroup()
        self.changes = 0
        self.connect_signal()

    @property
    def widget(self):
        return self.toggle_group

    def on_value_changed(self, *args):
        self.changes += 1


class FakeScaleRow:
    """A duck-typed body for the two scale rows, which share one shape."""

    def __init__(self) -> None:
        self.deck_serial_number = "FAKE"
        self._scale_handler = None
        self.scale = FakeSetValueWidget()
        self.on_map_tasks: list = []
        self.mapped = True
        self.changes = 0
        self.connect_signal()

    @property
    def widget(self):
        return self.scale

    def get_mapped(self):
        return self.mapped

    def on_value_changed(self, *args):
        self.changes += 1


class FakeBrightnessRow(FakeScaleRow):
    """The real Brightness wiring on a duck-typed body."""

    connect_signal = Brightness.connect_signal
    disconnect_signal = Brightness.disconnect_signal
    load_default = Brightness.load_default


class FakeSaturationRow(FakeScaleRow):
    """The real Saturation wiring on a duck-typed body."""

    connect_signal = Saturation.connect_signal
    disconnect_signal = Saturation.disconnect_signal
    load_default = Saturation.load_default


def _run_single_widget_row_case(row, settings, label) -> None:
    assert row.widget.handler_count() == 1, f"{label}: the widget must start wired"

    settings.raise_on_get = True
    try:
        row.load_default()
    except RuntimeError:
        pass
    else:
        raise AssertionError(f"{label}: the injected settings failure did not propagate")

    assert row.widget.handler_count() == 1, (
        f"{label}: a mid-load raise left the widget with "
        f"{row.widget.handler_count()} handlers"
    )

    settings.raise_on_get = False
    row.load_default()
    row.load_default()

    assert row.widget.handler_count() == 1, (
        f"{label}: load wired {row.widget.handler_count()} handlers; a duplicate "
        "saves and applies the value twice per change"
    )
    assert row.changes == 0, f"{label}: a programmatic load must not fire the handler"

    row.widget.fire()
    assert row.changes == 1, f"{label}: one change reported {row.changes} times"


def test_rotation_row_reconnects_after_midload_raise() -> None:
    settings = _install_settings({"rotation": 90})
    _run_single_widget_row_case(FakeRotationRow(), settings, "rotation row")


def test_brightness_row_reconnects_after_midload_raise() -> None:
    settings = _install_settings({"value": 40})
    _run_single_widget_row_case(FakeBrightnessRow(), settings, "brightness row")


def test_saturation_row_reconnects_after_midload_raise() -> None:
    settings = _install_settings({"saturation": 1.2})
    _run_single_widget_row_case(FakeSaturationRow(), settings, "saturation row")


def test_scale_rows_defer_before_they_disconnect() -> None:
    """An unmapped row must defer the load and keep its handler untouched."""
    for label, factory, values in (
        ("brightness row", FakeBrightnessRow, {"value": 40}),
        ("saturation row", FakeSaturationRow, {"saturation": 1.2}),
    ):
        settings = _install_settings(values)
        row = factory()
        row.mapped = False
        settings.raise_on_get = True
        # The deferral runs before the disconnect, so the read never happens.
        row.load_default()
        assert len(row.on_map_tasks) == 1, f"{label}: the load was not deferred"
        assert row.widget.handler_count() == 1, (
            f"{label}: the deferred load disturbed the handler"
        )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_sidebar_reconnect")
    test_event_row_reconnects_after_midselect_raise()
    test_event_row_reconnects_after_midrefill_raise()
    test_event_row_reconnects_when_model_is_missing()
    test_event_row_wires_exactly_once_across_selects()
    test_state_switcher_reconnects_after_midselect_raise()
    test_state_switcher_reconnects_after_midfill_raise()
    test_state_switcher_wires_exactly_once()
    test_background_row_reconnects_after_midload_raise()
    test_screensaver_row_reconnects_after_midload_raise()
    test_rotation_row_reconnects_after_midload_raise()
    test_brightness_row_reconnects_after_midload_raise()
    test_saturation_row_reconnects_after_midload_raise()
    test_scale_rows_defer_before_they_disconnect()
    print("PASS: scenario_sidebar_reconnect")


if __name__ == "__main__":
    main()
