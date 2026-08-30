"""Verify get_deck_stack, get_sidebar, get_page_selector, and
get_active_controller return built targets or None for incomplete chains."""

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

from types import SimpleNamespace

# The accessors are class methods on real GTK windows. Importing the class
# needs no display; only realising a window would.
from src.windows.PageManager.PageManager import PageManager  # noqa: E402
from src.windows.mainWindow.mainWindow import MainWindow  # noqa: E402

WATCHDOG_SECONDS = 30


def check_get_deck_stack() -> None:
    stack = object()
    built = SimpleNamespace(leftArea=SimpleNamespace(deck_stack=stack))
    assert MainWindow.get_deck_stack(built) is stack, (
        "a built window must hand back leftArea.deck_stack itself"
    )

    # A missing first segment must return None.
    no_left_area = SimpleNamespace()
    assert MainWindow.get_deck_stack(no_left_area) is None, (
        "a window with no leftArea must read None, not raise"
    )

    # leftArea present but deck_stack not yet set: the second segment is the
    # one missing. Still None.
    partial = SimpleNamespace(leftArea=SimpleNamespace())
    assert MainWindow.get_deck_stack(partial) is None, (
        "leftArea without deck_stack must read None, not raise"
    )

    print("PASS: get_deck_stack returns the stack when built and None per missing segment")


def check_get_sidebar() -> None:
    sidebar = object()
    built = SimpleNamespace(sidebar=sidebar)
    assert MainWindow.get_sidebar(built) is sidebar, "a built window hands back sidebar itself"

    no_sidebar = SimpleNamespace()
    assert MainWindow.get_sidebar(no_sidebar) is None, (
        "a window with no sidebar must read None, not raise"
    )

    print("PASS: get_sidebar returns the sidebar when built and None while absent")


def check_get_active_controller() -> None:
    # A missing deck stack must return None before visible-child access.
    no_stack = SimpleNamespace(get_deck_stack=lambda: None)
    assert MainWindow.get_active_controller(no_stack) is None, (
        "no deck stack means no active controller"
    )

    # A deck stack with nothing selected: still None, and no deck_controller
    # read happens. A mutant that drops the visible-child None check raises.
    empty_stack = SimpleNamespace(
        get_deck_stack=lambda: SimpleNamespace(get_visible_child=lambda: None)
    )
    assert MainWindow.get_active_controller(empty_stack) is None, (
        "an empty deck stack has no active controller"
    )

    # A selected child carries the controller.
    controller = object()
    selected = SimpleNamespace(deck_controller=controller)
    live_stack = SimpleNamespace(
        get_deck_stack=lambda: SimpleNamespace(get_visible_child=lambda: selected)
    )
    assert MainWindow.get_active_controller(live_stack) is controller, (
        "a selected child's deck_controller is the active controller"
    )

    print("PASS: get_active_controller skips on a missing chain and returns the controller when present")


def check_get_page_selector() -> None:
    selector = object()
    built = SimpleNamespace(page_selector=selector)
    assert PageManager.get_page_selector(built) is selector, (
        "a built page manager hands back page_selector itself"
    )

    no_selector = SimpleNamespace()
    assert PageManager.get_page_selector(no_selector) is None, (
        "a page manager with no page_selector must read None, not raise"
    )

    print("PASS: get_page_selector returns the selector when built and None while absent")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_ui_reachability_accessors")

    check_get_deck_stack()
    check_get_sidebar()
    check_get_active_controller()
    check_get_page_selector()

    print("ALL PASS: scenario_ui_reachability_accessors")


if __name__ == "__main__":
    main()
