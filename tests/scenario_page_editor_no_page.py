"""Verify PageEditor reads safely and refuses writes when no page is selected."""

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)
import globals as gl


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_page_editor_no_page")

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    if not fixtures.has_usable_display():
        print("SKIP: no usable display; scenario needs GTK")
        return
    Adw.init()

    fixtures._install_integration_globals()

    class StubLM:
        """Every label the editor builds goes through gl.lm."""

        def get(self, key, *a, **k):
            return key

    gl.lm = StubLM()

    from src.windows.PageManager.elements.PageEditor import PageEditor

    class FakePageManagerWindow(Adw.ApplicationWindow):
        """Provide the dialog parent required by PageEditor."""

    parent = FakePageManagerWindow()
    editor = PageEditor(parent)

    assert editor.active_page_path is None, (
        "the editor must start with no page; load_for_page binds one"
    )
    assert editor.main_stack.get_visible_child_name() == "no-page", (
        "the editor must open on its no-page child"
    )

    # The readers each group builds must all tolerate the absent path.
    assert gl.page_manager.get_serial_numbers_from_page(None) == [], (
        "get_serial_numbers_from_page must answer an absent path with no decks"
    )
    assert gl.page_manager.get_background_settings(None) == {}, (
        "get_background_settings must answer an absent path with no settings"
    )
    assert gl.page_manager.get_screensaver_settings(None) == {}, (
        "get_screensaver_settings must answer an absent path with no settings"
    )

    # The writers must refuse it, because canonical_path() rejects a None and
    # would raise deeper down, naming neither the editor nor the row.
    try:
        editor.require_active_page_path()
    except RuntimeError:
        pass
    else:
        raise AssertionError(
            "require_active_page_path must refuse an absent page, so an "
            "override row cannot write through it"
        )

    # With a page bound it hands the path straight back.
    editor.active_page_path = "/tmp/deckard-test-page.json"
    assert editor.require_active_page_path() == "/tmp/deckard-test-page.json"

    print("PASS: scenario_page_editor_no_page")


if __name__ == "__main__":
    main()
