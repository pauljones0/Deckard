"""Verify complete, idempotent ActionCore teardown at framework drop sites.
Cleanup must continue when a plugin removal hook raises."""
import time

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

from src.backend.PluginManager.ActionCore import ActionCore
from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI
from src.Signals.Signals import PageDelete


class _FakeGenUI(GenerativeUI):
    """Exercise cleanup for both built and unbuilt GenerativeUI objects."""

    def __init__(self, action_core: "ActionCore", var_name: str, with_widget: bool = False):
        def build():
            self._widget = Gtk.Label(label="fake")
        super().__init__(action_core, var_name, default_value=None, build=build if with_widget else None)
        if with_widget:
            _ = self.widget  # GenerativeUI builds on first .widget access

    def connect_signals(self):
        pass

    def disconnect_signals(self):
        pass

    def set_ui_value(self, value):
        pass


class _FakeAction(ActionCore):
    """Provide dummy collaborators that clean_up does not dereference."""

    def __init__(self, page, raise_in_hook: bool = False):
        super().__init__(
            action_id="test::fake",
            action_name="Fake",
            deck_controller=page.deck_controller,
            page=page,
            plugin_base=None,
            state=0,
            input_ident=None,
        )
        self._raise_in_hook = raise_in_hook
        self.hook_called = False

    def on_removed_from_cache(self):
        self.hook_called = True
        if self._raise_in_hook:
            raise RuntimeError("simulated plugin bug: on_removed_from_cache raises")


def _pump_glib(timeout: float = 2.0) -> None:
    """Service idle-queued cleanup without a running GTK main loop."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and ctx.pending():
        ctx.iteration(False)


def check_idempotent(page) -> None:
    action = _FakeAction(page)
    cb = lambda *a, **k: None
    action.connect(PageDelete, cb)
    assert len(action._connected_signals) == 1

    # Spy on the backend-release step to prove the second clean_up() never
    # reaches the teardown body.
    release_calls = []
    real_release = action._release_backend_resources

    def _counting_release():
        release_calls.append(1)
        real_release()

    action._release_backend_resources = _counting_release

    action.clean_up()
    action.clean_up()

    assert action._cleaned_up is True
    assert len(release_calls) == 1, f"_release_backend_resources ran {len(release_calls)} times, expected 1"
    assert action._connected_signals == []
    assert cb not in gl.signal_manager.connected_signals.get(PageDelete, [])
    print("PASS: clean_up() is idempotent")


def check_generative_ui_disposed(page) -> None:
    action = _FakeAction(page)
    unbuilt = _FakeGenUI(action, "var_unbuilt", with_widget=False)
    built = _FakeGenUI(action, "var_built", with_widget=True)
    assert action.generative_ui_objects == [unbuilt, built]
    assert unbuilt._widget is None
    assert built._widget is not None

    action.clean_up()
    # The list is empty the instant clean_up() returns, although GLib.idle_add
    # still holds the GTK destroy() call on the main loop.
    assert action.generative_ui_objects == [], "generative_ui_objects not cleared synchronously"

    _pump_glib()
    # The never-built widget was skipped; it had nothing to unparent. The built
    # one went through destroy() and was unparented and cleared.
    assert unbuilt._widget is None
    assert built._widget is None, "built GenerativeUI's widget was not destroyed by the idle batch"
    print("PASS: generative_ui_objects emptied synchronously; idle destroy pass ran cleanly")


def check_hook_raises_still_cleans_up(page) -> None:
    action = _FakeAction(page, raise_in_hook=True)
    cb = lambda *a, **k: None
    action.connect(PageDelete, cb)

    # Exercise page.clear_action_objects, which page-cache eviction also calls.
    page.action_objects.setdefault("keys", {})["fake-teardown-test"] = {0: {0: action}}

    page.clear_action_objects()

    assert action.hook_called, "on_removed_from_cache() was never invoked"
    assert action._cleaned_up is True, "clean_up() did not run after the hook raised"
    assert action._connected_signals == [], "signal was not disconnected"
    assert cb not in gl.signal_manager.connected_signals.get(PageDelete, [])
    print("PASS: clean_up() runs even when on_removed_from_cache() raises")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_action_teardown")
    controller = fixtures.make_headless_controller(serial="teardown-1")
    page = controller.active_page

    check_idempotent(page)
    check_generative_ui_disposed(page)
    check_hook_raises_still_cleans_up(page)

    fixtures.teardown(controller)
    print("PASS: scenario_action_teardown")


if __name__ == "__main__":
    main()
