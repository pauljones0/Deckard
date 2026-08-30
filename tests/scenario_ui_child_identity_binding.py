"""Verify identity-based UI child binding, rescans, replacement, mirror pushes,
and window-attachment reconciliation."""

# The binding lands at DeckStack.add_page and lifts at remove_page. The fakes
# below carry no name information at all, so a name-matching lookup fails.
import time
from types import SimpleNamespace

import fixtures

import globals as gl
from gi.repository import GLib

from src.backend import ui_port
from src.backend.DeckManagement.InputIdentifier import Input
from src.windows.ui_adapter import GtkUIAdapter


def _pump(duration: float = 0.1) -> None:
    """Run the default MainContext's pending idles. The adapter queues its
    stack mutations with GLib.idle_add and this harness runs no main loop."""
    context = GLib.MainContext.default()
    deadline = time.time() + duration
    while time.time() < deadline:
        while context.iteration(False):
            pass
        time.sleep(0.005)


class _FakeStack:
    def __init__(self, children):
        # Model a trailing None from a ListModel removal during iteration.
        self._pages = [
            None if c is None else SimpleNamespace(get_child=lambda c=c: c)
            for c in children
        ]
        self.added = []
        self.removed = []

    def get_pages(self):
        return list(self._pages)

    def get_child_by_name(self, name):
        # Name-free fakes make name-based lookup miss cleanly.
        return None

    def add_page(self, controller):
        self.added.append(controller)

    def remove_page(self, controller):
        self.removed.append(controller)


class _FakeButton:
    """The two halves the adapter drives. Conversion on the producer, paint on
    the main loop."""

    def __init__(self):
        self.prepared = []
        self.painted = []

    def prepare_mirror_frame(self, image):
        self.prepared.append(image)
        return image

    def paint_mirror_frame(self, payload):
        self.painted.append(payload)
        return False


def _fake_grid(rows=1, cols=1):
    return SimpleNamespace(buttons=[[_FakeButton() for _ in range(rows)] for _ in range(cols)])


def _fake_child(controller, grid):
    return SimpleNamespace(
        deck_controller=controller,
        page_settings=SimpleNamespace(deck_config=SimpleNamespace(grid=grid)),
        # Absorb background low-FPS updates.
        low_fps_banner=SimpleNamespace(set_revealed=lambda *_: None),
    )


def _fake_window(deck_stack):
    # Expose the mapped, signal, and typed stack access used by attach_window.
    return SimpleNamespace(
        get_deck_stack=lambda: deck_stack,
        get_mapped=lambda: False,
        connect=lambda *args: None,
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_ui_child_identity_binding")
    controller = fixtures.make_headless_controller(serial="identity-1")
    adapter = GtkUIAdapter()
    ui_port.install(adapter)
    try:
        grid = _fake_grid()
        child = _fake_child(controller, grid)

        # 1. add_page's bind (by object) is what every lookup uses.
        adapter.bind(controller, child)
        assert adapter.query_deck_widget(controller, "deck_stack_child") is child, (
            "the bound DeckStackChild was not honored"
        )
        assert adapter.query_deck_widget(controller, "key_grid") is grid, (
            "the key grid did not resolve through the bound child"
        )

        # Cold resolution must bind name-free children by controller identity.
        adapter.unbind(controller)
        assert adapter.query_deck_widget(controller, "deck_stack_child") is None
        adapter._window = _fake_window(_FakeStack([child]))
        adapter.rescan_children()
        assert adapter.query_deck_widget(controller, "deck_stack_child") is child, (
            "the identity rescan failed to find the controller's own child "
            "-- previews would silently stop reaching the visible grid"
        )

        # 3. No false positive. A stack of other controllers' children.
        adapter.unbind(controller)
        stranger = _fake_child(object(), _fake_grid())
        adapter._window = _fake_window(_FakeStack([stranger]))
        adapter.rescan_children()
        assert adapter.query_deck_widget(controller, "deck_stack_child") is None, (
            "the rescan matched a child belonging to another controller"
        )

        # A trailing None from mid-scan removal must not raise.
        adapter._window = _fake_window(_FakeStack([stranger, None]))
        adapter.rescan_children()
        assert adapter.query_deck_widget(controller, "deck_stack_child") is None, (
            "a scan over a mutating stack did not terminate cleanly"
        )

        # Rebinding after a widget-tree replacement must serve the new grid.
        new_grid = _fake_grid()
        new_child = _fake_child(controller, new_grid)
        adapter.bind(controller, new_child)
        assert adapter.query_deck_widget(controller, "key_grid") is new_grid, (
            "a stale binding survived the re-bind -- pushes would land in the "
            "orphaned old widget tree"
        )

        # Bound mirror pushes paint; unbound pushes return False for replay.
        identifier = Input.Key("0x0")
        adapter._window_mapped = True
        assert adapter.push_input_image(controller, identifier, object()) is True, (
            "push was refused despite a bound, mapped grid"
        )
        assert len(new_grid.buttons[0][0].prepared) == 0, (
            "conversion ran at push time; only the drain's winning frame "
            "pays conversion"
        )
        # The drain resolves the widget again, converts, and paints, so both
        # halves land on the bound grid rather than on any orphan.
        assert adapter._drain_mirror(controller, identifier) is False
        assert len(new_grid.buttons[0][0].prepared) == 1, (
            "the drain did not convert on the bound grid's button"
        )
        assert len(new_grid.buttons[0][0].painted) == 1, (
            "the drain did not paint into the bound grid's button"
        )

        adapter.unbind(controller)
        assert adapter.push_input_image(controller, identifier, object()) is False, (
            "push was accepted for an unbound controller -- the frame would "
            "be silently lost instead of dirty-marked"
        )
        assert len(new_grid.buttons[0][0].prepared) == 1, "an unbound push still reached the button"

        # Attachment reconciles hotplug events missed during window construction.
        assert controller in gl.deck_manager.deck_controller, "fixture invariant"

        # 6a. A missed add. The deck is live and the stack was built without
        # it. detach_window runs first, to drop the strangers bound above.
        adapter.detach_window()
        stack = _FakeStack([])
        adapter.attach_window(_fake_window(stack))
        _pump()
        assert stack.added == [controller], (
            f"attach_window queued add_page for {stack.added}, expected the "
            "one live-but-unbound controller -- a deck plugged in during "
            "window construction would never get a stack child"
        )
        assert stack.removed == [], "attach_window removed a live deck's page"

        # Remove and unbind a stale child after an unplug during construction.
        ghost = object()
        ghost_child = _fake_child(ghost, _fake_grid())
        stack = _FakeStack([ghost_child])
        adapter.attach_window(_fake_window(stack))
        _pump()
        assert stack.removed == [ghost], (
            f"attach_window queued remove_page for {stack.removed}, expected "
            "the stale child of an unplugged deck"
        )
        assert adapter.query_deck_widget(ghost, "deck_stack_child") is None, (
            "the stale binding survived the reconcile"
        )

        # 6b'. No deck manager at all. Reconciling against an unknown world
        # must do nothing, rather than read as "no decks exist".
        settled_child = _fake_child(controller, _fake_grid())
        stack = _FakeStack([settled_child])
        real_deck_manager = gl.deck_manager
        gl.deck_manager = None
        try:
            adapter.attach_window(_fake_window(stack))
            _pump()
        finally:
            gl.deck_manager = real_deck_manager
        assert stack.removed == [], (
            "reconcile tore down bound children when it could not see a deck "
            f"manager: {stack.removed}"
        )

        # A stack that matches the deck manager must not churn on attachment.
        settled_child = _fake_child(controller, _fake_grid())
        stack = _FakeStack([settled_child])
        adapter.attach_window(_fake_window(stack))
        _pump()
        assert stack.added == [] and stack.removed == [], (
            f"a settled stack was churned: added={stack.added} "
            f"removed={stack.removed}"
        )

        print("PASS: identity binding resolves, rejects strangers, heals re-binds, gates pushes")
    finally:
        ui_port.install(None)
        fixtures.teardown(controller)

    print("PASS: scenario_ui_child_identity_binding")


if __name__ == "__main__":
    main()
