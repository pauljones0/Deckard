"""A window auto-change rule matches, and an edit to one applies at once.

Two defects made automatic page switching read as broken. A rule made by
filling in one of the two patterns carries no key for the other, and a pattern
the page does not carry used to match nothing, so such a rule never fired. And
an edited rule applied at the next window change alone, so a rule written for
the window in front did nothing until something else took focus.
"""

# A stub integration stands in for the five real ones, whose window queries
# need a live desktop. What is covered here is the matching and the re-check,
# not the watcher gate, which scenario_window_watcher_gating covers.
import fixtures  # noqa: F401  (must be imported first: isolates DATA_PATH)

import contextlib
import json
import os
import threading

import globals as gl
import src.backend.WindowGrabber.WindowGrabber as window_grabber_module
from src.backend.WindowGrabber.Integration import Integration
from src.backend.WindowGrabber.Window import Window
from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

TIMEOUT_S = 5.0

FIREFOX = Window(wm_class="firefox", title="Mozilla Firefox")
TERMINAL = Window(wm_class="kitty", title="terminal")


class StubIntegration(Integration):
    """One desktop's window source, answering from a field.

    It starts no watcher thread. Every check here drives the grabber directly.
    """

    instances: list["StubIntegration"] = []
    active_window: Window | None = FIREFOX

    def __init__(self, window_grabber):
        super().__init__(window_grabber=window_grabber)
        self.active_window_queries = 0
        StubIntegration.instances.append(self)

    def get_all_windows(self) -> list[Window]:
        window = StubIntegration.active_window
        return [] if window is None else [window]

    def get_active_window(self) -> Window | None:
        self.active_window_queries += 1
        return StubIntegration.active_window


class StubPage:
    """Only json_path is read. A page's settings live in the document the page
    manager holds, not in the Page object."""

    def __init__(self, json_path: str):
        self.json_path = json_path


class StubDeck:
    def __init__(self, serial: str):
        self._serial = serial

    def is_open(self) -> bool:
        return True

    def get_serial_number(self) -> str:
        return self._serial


class StubDeckController:
    def __init__(self, serial: str, active_page: StubPage):
        self.deck = StubDeck(serial)
        self._serial = serial
        self.active_page = active_page
        self.page_auto_loaded = False
        self.last_manual_loaded_page_path: str | None = None
        self.loaded_pages: list[str] = []

    def serial_number(self) -> str:
        return self._serial

    def load_page(self, page, allow_reload: bool = True) -> None:
        self.loaded_pages.append(page.json_path)
        self.active_page = page


def _install_stub_selector() -> None:
    """Replaces the session sniffing with a fixed answer.
    select_integration_class is a pure function of the environment, which is
    what makes this a one-liner rather than an environment dance."""
    window_grabber_module.select_integration_class = (
        lambda environment_components, server: StubIntegration
    )


_pages_written = 0


def _write_page(name: str) -> str:
    """Writes an empty page and answers its path.

    Every page takes a name of its own across the whole scenario. The page
    manager keeps the settings of a page it has read, so a second page reusing
    a name would answer with the rule the first one carried.
    """
    global _pages_written
    _pages_written += 1

    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{name}{_pages_written}.json")
    with open(path, "w") as page_file:
        json.dump({"keys": {}, "dials": {}, "touchscreens": {}}, page_file)
    return path


def _clear_pages() -> None:
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    if not os.path.isdir(pages_dir):
        return
    for entry in os.listdir(pages_dir):
        if entry.endswith(".json"):
            os.remove(os.path.join(pages_dir, entry))


def _fresh_grabber() -> WindowGrabber:
    StubIntegration.instances = []
    StubIntegration.active_window = FIREFOX
    grabber = WindowGrabber()
    gl.window_grabber = grabber
    _settle(grabber)
    return grabber


def _settle(grabber: WindowGrabber) -> None:
    """Waits for the watcher gate to finish deciding. Every rule write queues
    one, and it runs on the background pool."""
    assert grabber.wait_for_gate(TIMEOUT_S), (
        "the window watcher gate did not settle within the timeout"
    )


@contextlib.contextmanager
def _registered(controller: StubDeckController, page_paths: list[str]):
    """Puts one deck in front of the grabber, with its pages in the cache.

    A page-cache hit keeps get_page from building a real Page against this
    stub deck.
    """
    gl.deck_manager.deck_controller.append(controller)
    gl.page_manager.pages[controller] = {
        path: {"page": StubPage(path), "page_number": number}
        for number, path in enumerate(page_paths)
    }
    try:
        yield controller
    finally:
        gl.deck_manager.deck_controller.remove(controller)
        gl.page_manager.pages.pop(controller, None)


# A rule that carries one pattern only.

def check_title_only_rule_fires() -> None:
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")

    # Written through the setter behind the page editor's title entry, so the
    # page holds exactly what applying that one row writes.
    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, regex_title="Mozilla Firefox",
        stay_on_page=True, decks=["SERIAL"],
    )
    assert "wm-class" not in gl.page_manager.get_auto_change_settings(rule_path), (
        "the setter writes only the fields it is given, and this check stands "
        "on a rule that carries no wm-class key at all"
    )

    grabber = _fresh_grabber()
    controller = StubDeckController("SERIAL", active_page=StubPage(manual_path))

    with _registered(controller, [manual_path, rule_path]):
        grabber.on_active_window_changed(FIREFOX)

        assert controller.loaded_pages == [rule_path], (
            f"a rule made by filling in the title alone never fired: the "
            f"missing wm-class pattern must match every window, got "
            f"{controller.loaded_pages}"
        )
        assert controller.page_auto_loaded is True

        # The pattern the rule does carry still decides.
        controller.loaded_pages.clear()
        grabber.on_active_window_changed(TERMINAL)
        assert controller.loaded_pages == [], (
            f"a window that matches neither the title pattern nor anything "
            f"else must not switch the deck, got {controller.loaded_pages}"
        )


def check_wm_class_only_rule_fires() -> None:
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")

    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, wm_class="firefox",
        stay_on_page=True, decks=["SERIAL"],
    )
    assert "title" not in gl.page_manager.get_auto_change_settings(rule_path)

    grabber = _fresh_grabber()
    controller = StubDeckController("SERIAL", active_page=StubPage(manual_path))

    with _registered(controller, [manual_path, rule_path]):
        # Any title, because the rule carries no title pattern.
        grabber.on_active_window_changed(Window(wm_class="firefox", title="Downloads"))

        assert controller.loaded_pages == [rule_path], (
            f"a rule made by filling in the wm-class alone never fired, got "
            f"{controller.loaded_pages}"
        )

        controller.loaded_pages.clear()
        grabber.on_active_window_changed(TERMINAL)
        assert controller.loaded_pages == [], (
            "a different wm-class must not match the rule"
        )


def check_empty_pattern_matches_every_window() -> None:
    """An entry the user cleared writes an empty pattern. That reads the same
    as an absent one, so the two cannot drift apart."""
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")

    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, wm_class="", regex_title="Mozilla Firefox",
        stay_on_page=True, decks=["SERIAL"],
    )

    grabber = _fresh_grabber()
    controller = StubDeckController("SERIAL", active_page=StubPage(manual_path))

    with _registered(controller, [manual_path, rule_path]):
        grabber.on_active_window_changed(FIREFOX)
        assert controller.loaded_pages == [rule_path], (
            f"an empty wm-class pattern must match every window, got "
            f"{controller.loaded_pages}"
        )


# The re-check after an edit.

def check_recheck_applies_the_window_in_front() -> None:
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")
    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, wm_class="firefox", regex_title=".*",
        stay_on_page=True, decks=["SERIAL"],
    )

    grabber = _fresh_grabber()
    controller = StubDeckController("SERIAL", active_page=StubPage(manual_path))

    dispatch_threads: list[threading.Thread] = []
    dispatched = threading.Event()
    dispatch = grabber.on_active_window_changed

    def record(window: Window) -> None:
        dispatch_threads.append(threading.current_thread())
        try:
            dispatch(window)
        finally:
            dispatched.set()

    grabber.on_active_window_changed = record

    with _registered(controller, [manual_path, rule_path]):
        grabber.recheck_active_window()

        assert dispatched.wait(TIMEOUT_S), (
            "the re-check never applied the rules to the window in front"
        )
        assert dispatch_threads[0] is not threading.main_thread(), (
            "the re-check ran on the calling thread. The page editor calls it "
            "from a GTK handler, and it queries the desktop and can load a "
            "page, neither of which belongs on the GTK main thread"
        )
        assert fixtures.wait_until(
            lambda: controller.loaded_pages == [rule_path], TIMEOUT_S
        ), (
            f"the re-check must switch the deck to the page whose rule matches "
            f"the window in front, got {controller.loaded_pages}"
        )
        assert StubIntegration.instances[0].active_window_queries >= 1, (
            "the re-check must ask the desktop which window is in front"
        )


def check_recheck_restores_a_disabled_rule() -> None:
    """Switching a rule off while another rule stays enabled leaves the
    watcher running, so no gate change carries the restore. The re-check must
    return the deck the rule had taken over."""
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")
    other_path = _write_page("Mail")
    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, wm_class="firefox", regex_title=".*",
        stay_on_page=False, decks=["SERIAL"],
    )
    gl.page_manager.overwrite_auto_change_settings(
        path=other_path, enable=True, wm_class="thunderbird", regex_title=".*",
        stay_on_page=False, decks=["SERIAL"],
    )

    grabber = _fresh_grabber()
    controller = StubDeckController("SERIAL", active_page=StubPage(rule_path))
    controller.page_auto_loaded = True
    controller.last_manual_loaded_page_path = manual_path

    with _registered(controller, [manual_path, rule_path, other_path]):
        gl.page_manager.overwrite_auto_change_settings(path=rule_path, enable=False)
        grabber.recheck_active_window()

        assert fixtures.wait_until(
            lambda: controller.loaded_pages == [manual_path], TIMEOUT_S
        ), (
            f"switching a rule off must hand the deck back to the page the "
            f"user chose, got {controller.loaded_pages}"
        )
        _settle(grabber)
        assert grabber.is_watching is True, (
            "the other page still carries an enabled rule, so the watcher "
            "must keep running and the restore must come from the re-check"
        )


def check_recheck_without_a_window_in_front() -> None:
    """A desktop that names no front window leaves nothing to match. The
    re-check must ask, find nothing, and change no deck."""
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")
    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, wm_class="firefox", regex_title=".*",
        stay_on_page=True, decks=["SERIAL"],
    )

    grabber = _fresh_grabber()
    integration = StubIntegration.instances[0]
    StubIntegration.active_window = None

    dispatched: list[Window] = []
    grabber.on_active_window_changed = dispatched.append

    controller = StubDeckController("SERIAL", active_page=StubPage(manual_path))
    with _registered(controller, [manual_path, rule_path]):
        queries_before = integration.active_window_queries
        grabber.recheck_active_window()

        assert fixtures.wait_until(
            lambda: integration.active_window_queries > queries_before, TIMEOUT_S
        ), "the re-check never asked which window is in front"
        assert dispatched == [], (
            f"with no window in front there is nothing to match the rules "
            f"against, got {dispatched}"
        )
        assert controller.loaded_pages == []


def main() -> None:
    fixtures.start_watchdog(90, label="scenario_autoswitch_rule_edit")
    _install_stub_selector()
    fixtures._install_integration_globals()

    check_title_only_rule_fires()
    check_wm_class_only_rule_fires()
    check_empty_pattern_matches_every_window()
    check_recheck_applies_the_window_in_front()
    check_recheck_restores_a_disabled_rule()
    check_recheck_without_a_window_in_front()

    print("PASS: scenario_autoswitch_rule_edit")


if __name__ == "__main__":
    main()
