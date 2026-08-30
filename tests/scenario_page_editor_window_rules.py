"""Verify window-rule commits, immediate rechecks, and close cleanup."""

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)
import globals as gl


class StubWindowGrabber:
    """Count editor requests without probing the desktop."""

    def __init__(self):
        self.rechecks = 0
        self.gate_passes = 0

    def recheck_active_window(self) -> None:
        self.rechecks += 1

    def refresh_watch_state(self) -> None:
        self.gate_passes += 1

    def get_all_matching_windows(self, class_regex: str, title_regex: str) -> list:
        # The matching-window list refreshes on every applied pattern, on a
        # background thread.
        return []


class StubLM:
    """Every label the editor builds goes through gl.lm."""

    def get(self, key, *a, **k):
        return key


def _rule(page_path: str) -> dict:
    return gl.page_manager.get_auto_change_settings(page_path)


def check_focus_leave_commits_typed_text(group, page_path, grabber) -> None:
    """Typed text that the focus leaves behind must reach the page."""
    before = grabber.rechecks
    group.title_entry.set_text("Mozilla Firefox")
    group.title_focus.emit("leave")

    assert _rule(page_path).get("title") == "Mozilla Firefox", (
        f"the title the user typed was left in the widget when the focus "
        f"moved on, so the page kept its old pattern: {_rule(page_path)}"
    )
    assert grabber.rechecks == before + 1, (
        "a committed pattern must ask for a re-check of the window in front"
    )

    before = grabber.rechecks
    group.wm_class_entry.set_text("firefox")
    group.wm_class_focus.emit("leave")

    assert _rule(page_path).get("wm-class") == "firefox", (
        f"the wm-class the user typed never reached the page: {_rule(page_path)}"
    )
    assert grabber.rechecks == before + 1


def check_focus_leave_without_an_edit_rechecks_only(group, page_path, grabber) -> None:
    """Recheck on unchanged focus leave without rewriting the rule."""
    rechecks_before = grabber.rechecks
    gate_passes_before = grabber.gate_passes

    group.title_focus.emit("leave")
    group.wm_class_focus.emit("leave")

    assert grabber.gate_passes == gate_passes_before, (
        "a focus leave that carries no edit must not write the page"
    )
    assert grabber.rechecks == rechecks_before + 2, (
        f"each focus leave must re-check the window in front, got "
        f"{grabber.rechecks - rechecks_before} for two leaves"
    )
    assert _rule(page_path).get("title") == "Mozilla Firefox"


def check_toggles_recheck(group, page_path, grabber) -> None:
    """The enable and stay-on-page rows carry the same duty as the entries."""
    before = grabber.rechecks
    group.enable_toggle.set_active(True)

    assert _rule(page_path).get("enable") is True
    assert grabber.rechecks == before + 1, (
        "switching a rule on must apply it to the window in front, or the "
        "rule appears to do nothing until another window takes focus"
    )

    before = grabber.rechecks
    group.stay_on_page_toggle.set_active(False)

    assert _rule(page_path).get("stay-on-page") is False
    assert grabber.rechecks == before + 1, (
        "switching stay-on-page off must hand a taken-over deck back at once"
    )


def check_editor_without_a_grabber(group, page_path) -> None:
    """A headless run has no window grabber, and an edit must still reach the
    page rather than raise out of a GTK handler."""
    grabber = gl.window_grabber
    gl.window_grabber = None
    try:
        group.title_entry.set_text("Firefox Nightly")
        group.title_focus.emit("leave")

        assert _rule(page_path).get("title") == "Firefox Nightly", (
            f"the commit must not depend on a window grabber being there, "
            f"got {_rule(page_path)}"
        )
        group.title_focus.emit("leave")
    finally:
        gl.window_grabber = grabber


def check_window_close_commits_and_disconnects(window, editor, page_path, grabber) -> None:
    """Commit pending text and disconnect row handlers before window teardown."""
    group = editor.auto_change_group
    group.title_entry.set_text("Thunderbird")
    rechecks_before = grabber.rechecks

    window.on_close()

    assert _rule(page_path).get("title") == "Thunderbird", (
        f"text the user typed was lost when the window closed, got "
        f"{_rule(page_path)}"
    )
    assert grabber.rechecks == rechecks_before + 1, (
        "the pattern committed on close must reach the window in front too"
    )

    # Every row handler is gone: a leave now writes nothing and re-checks
    # nothing, whatever the entry holds.
    rechecks_before = grabber.rechecks
    gate_passes_before = grabber.gate_passes
    group.title_entry.set_text("Deckard")
    group.title_focus.emit("leave")
    group.wm_class_focus.emit("leave")
    group.enable_toggle.set_active(False)

    assert grabber.rechecks == rechecks_before, (
        "a row handler survived the window close and ran against widgets that "
        "are going away"
    )
    assert grabber.gate_passes == gate_passes_before, (
        "a row handler survived the window close and wrote the page"
    )
    assert _rule(page_path).get("title") == "Thunderbird"


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_page_editor_window_rules")

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    if not Gtk.init_check():
        print("SKIP: no display available; scenario needs GTK")
        return
    Adw.init()

    fixtures._install_integration_globals()
    gl.lm = StubLM()

    grabber = StubWindowGrabber()
    gl.window_grabber = grabber

    from src.windows.PageManager.PageManager import PageManager

    class FakeMainWindow(Adw.ApplicationWindow):
        """Stands in for the main window the Page Manager is transient for."""

    page_path = fixtures.seed_page("Browser")
    window = PageManager(FakeMainWindow())
    editor = window.page_editor
    editor.load_for_page(page_path)
    group = editor.auto_change_group

    assert _rule(page_path) == {}, (
        "the seeded page must start with no window rule at all"
    )

    check_focus_leave_commits_typed_text(group, page_path, grabber)
    check_focus_leave_without_an_edit_rechecks_only(group, page_path, grabber)
    check_toggles_recheck(group, page_path, grabber)
    check_editor_without_a_grabber(group, page_path)
    check_window_close_commits_and_disconnects(window, editor, page_path, grabber)

    print("PASS: scenario_page_editor_window_rules")


if __name__ == "__main__":
    main()
