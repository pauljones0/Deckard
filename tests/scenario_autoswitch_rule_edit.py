"""A window auto-change rule matches, and an edit to one applies at once.

Three properties hold together here. A rule carrying one of the two patterns
fires on that one alone, because the pattern it does not carry matches every
window. A rule carrying neither pattern matches nothing, which is what a rule
looks like while the user types the first pattern or clears one to retype it.
And an edit applies to the window in front there and then, on a background
thread, without a second page load when the watcher carries the same window.
"""

# A stub integration stands in for the five real ones, whose window queries
# need a live desktop. What is covered here is the matching and the re-check,
# not the watcher gate, which scenario_window_watcher_gating covers.
import fixtures  # noqa: F401  (must be imported first: isolates DATA_PATH)

import contextlib
import json
import os
import threading
import time

import globals as gl
import src.api as api
import src.backend.WindowGrabber.WindowGrabber as window_grabber_module
from src.backend.WindowGrabber.Integration import Integration
from src.backend.WindowGrabber.Window import Window
from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

TIMEOUT_S = 5.0

FIREFOX = Window(wm_class="firefox", title="Mozilla Firefox")
TERMINAL = Window(wm_class="kitty", title="terminal")
# The page editor itself is in front while a rule is being typed.
EDITOR = Window(wm_class="deckard", title="Deckard")


class StubIntegration(Integration):
    """One desktop's window source, answering from a field.

    It starts no watcher thread. Every check here drives the grabber directly.
    A gate can hold a query inside get_active_window, which is what makes the
    coalescing check deterministic.
    """

    instances: list["StubIntegration"] = []
    active_window: Window | None = FIREFOX
    query_gate: threading.Event | None = None

    def __init__(self, window_grabber):
        super().__init__(window_grabber=window_grabber)
        self.active_window_queries = 0
        StubIntegration.instances.append(self)

    def get_all_windows(self) -> list[Window]:
        window = StubIntegration.active_window
        return [] if window is None else [window]

    def get_active_window(self) -> Window | None:
        self.active_window_queries += 1
        gate = StubIntegration.query_gate
        if gate is not None:
            gate.wait(TIMEOUT_S)
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
        # A real load takes long enough for a second routing to start inside
        # it. Legs that race two routings set this.
        self.load_delay = 0.0

    def serial_number(self) -> str:
        return self._serial

    def load_page(self, page, allow_reload: bool = True) -> None:
        # The real controller compares identity under allow_reload=False, and
        # returns without loading. A stub that always records would count a
        # load the deck never performs.
        if not allow_reload and self.active_page is page:
            return
        if self.load_delay:
            time.sleep(self.load_delay)
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
    """One grabber of its own for a check, over a fresh integration.

    The grabber it replaces is settled first. A page write ahead of this call
    reaches that one, whose gate pass then runs on the background pool and can
    build an integration of its own while this check is already asserting.
    """
    previous = gl.window_grabber
    if isinstance(previous, WindowGrabber):
        _settle(previous)
        _settle_recheck(previous)

    StubIntegration.instances = []
    StubIntegration.active_window = FIREFOX
    StubIntegration.query_gate = None
    grabber = WindowGrabber()
    gl.window_grabber = grabber
    _settle(grabber)
    return grabber


def _integration_of(grabber: WindowGrabber) -> StubIntegration:
    """The integration this grabber built, never the newest one built anywhere.

    Another grabber can build one at any moment, so the class-wide list says
    nothing about which object this grabber queries.
    """
    integration = grabber.integration
    assert isinstance(integration, StubIntegration), (
        f"the grabber built no integration, got {integration!r}"
    )
    return integration


def _settle(grabber: WindowGrabber) -> None:
    """Waits for the watcher gate to finish deciding. Every rule write queues
    one, and it runs on the background pool."""
    assert grabber.wait_for_gate(TIMEOUT_S), (
        "the window watcher gate did not settle within the timeout"
    )


def _settle_recheck(grabber: WindowGrabber) -> None:
    assert grabber.wait_for_recheck(TIMEOUT_S), (
        "the active window re-check did not settle within the timeout"
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


@contextlib.contextmanager
def _recorded_foreground_windows():
    """Collects what the grabber publishes to the D-Bus API."""
    published: list[tuple[str, str]] = []
    original = window_grabber_module.notify_foreground_window_changed
    window_grabber_module.notify_foreground_window_changed = (
        lambda name, wm_class: published.append((name, wm_class))
    )
    try:
        yield published
    finally:
        window_grabber_module.notify_foreground_window_changed = original


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
    """An entry the user cleared writes an empty pattern. Beside a pattern
    that is filled in, it reads the same as an absent one, so the two cannot
    drift apart."""
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
            f"an empty wm-class pattern beside a filled-in title must match "
            f"every window, got {controller.loaded_pages}"
        )


# A rule that carries no pattern at all.

def check_rule_without_patterns_is_inert() -> None:
    """An enabled rule with neither pattern must match nothing.

    A rule switched on before either pattern is typed looks like this, and so
    does one carried over from a version that never wrote the missing key. A
    rule read as "matches every window" takes the deck over on the next window
    change, whatever is in front.
    """
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")

    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, stay_on_page=True, decks=["SERIAL"],
    )
    info = gl.page_manager.get_auto_change_settings(rule_path)
    assert "title" not in info and "wm-class" not in info, (
        f"this check stands on a rule that carries neither pattern, got {info}"
    )

    grabber = _fresh_grabber()
    controller = StubDeckController("SERIAL", active_page=StubPage(manual_path))

    with _registered(controller, [manual_path, rule_path]):
        grabber.on_active_window_changed(FIREFOX)
        assert controller.loaded_pages == [], (
            f"an enabled rule holding no pattern matched a window and took "
            f"the deck over, got {controller.loaded_pages}"
        )
        assert controller.page_auto_loaded is False


def check_cleared_pattern_does_not_take_over() -> None:
    """The editing sequence that reaches the same state.

    The user clears the one pattern the rule carries to retype it. Until the
    new text lands the rule holds nothing, and the window in front is the page
    editor itself. A rule read as a catch-all hands the deck to the page being
    edited, and the deck then stays there: the page it moved to asks to stay,
    so nothing hands it back.
    """
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")
    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, regex_title="Firefox",
        stay_on_page=True, decks=["SERIAL"],
    )

    grabber = _fresh_grabber()
    StubIntegration.active_window = EDITOR
    controller = StubDeckController("SERIAL", active_page=StubPage(manual_path))

    with _registered(controller, [manual_path, rule_path]):
        # The cleared entry commits, and the editor asks for a re-check.
        gl.page_manager.overwrite_auto_change_settings(path=rule_path, regex_title="")
        grabber.recheck_active_window()
        _settle_recheck(grabber)

        assert controller.loaded_pages == [], (
            f"the half-edited rule took the deck over against the editor's "
            f"own window, got {controller.loaded_pages}"
        )
        assert controller.active_page.json_path == manual_path

        # The user finishes typing, and the window the rule names comes up.
        gl.page_manager.overwrite_auto_change_settings(
            path=rule_path, regex_title="Mozilla Firefox")
        StubIntegration.active_window = FIREFOX
        grabber.recheck_active_window()
        _settle_recheck(grabber)

        assert controller.loaded_pages == [rule_path], (
            f"the finished rule must switch the deck, got "
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
    dispatch = grabber.on_active_window_changed

    def record(window: Window) -> None:
        dispatch_threads.append(threading.current_thread())
        dispatch(window)

    grabber.on_active_window_changed = record

    with _registered(controller, [manual_path, rule_path]):
        grabber.recheck_active_window()
        _settle_recheck(grabber)

        assert dispatch_threads, (
            "the re-check never applied the rules to the window in front"
        )
        assert dispatch_threads[0] is not threading.main_thread(), (
            "the re-check ran on the calling thread. The page editor calls it "
            "from a GTK handler, and it queries the desktop and can load a "
            "page, neither of which belongs on the GTK main thread"
        )
        assert controller.loaded_pages == [rule_path], (
            f"the re-check must switch the deck to the page whose rule matches "
            f"the window in front, got {controller.loaded_pages}"
        )
        assert _integration_of(grabber).active_window_queries >= 1, (
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
        _settle_recheck(grabber)

        assert controller.loaded_pages == [manual_path], (
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
    integration = _integration_of(grabber)
    StubIntegration.active_window = None

    dispatched: list[Window] = []
    grabber.on_active_window_changed = dispatched.append

    controller = StubDeckController("SERIAL", active_page=StubPage(manual_path))
    with _registered(controller, [manual_path, rule_path]):
        queries_before = integration.active_window_queries
        grabber.recheck_active_window()
        _settle_recheck(grabber)

        assert integration.active_window_queries > queries_before, (
            "the re-check never asked which window is in front"
        )
        assert dispatched == [], (
            f"with no window in front there is nothing to match the rules "
            f"against, got {dispatched}"
        )
        assert controller.loaded_pages == []


def check_recheck_is_inert_with_no_rule() -> None:
    """The re-check takes the same gate as the watcher.

    With no rule anywhere, nothing may build the integration, probe the
    desktop or publish a foreground window: the D-Bus property reports the
    desktop only while a page asks to follow it.
    """
    _clear_pages()
    _write_page("Plain")
    _write_page("Disabled")

    grabber = _fresh_grabber()
    assert grabber.integration is None, (
        "the gate builds no integration without a rule, and this check stands "
        "on that"
    )

    with _recorded_foreground_windows() as published:
        grabber.recheck_active_window()
        _settle_recheck(grabber)

        assert grabber.integration is None, (
            "a re-check with no rule anywhere must not build the integration: "
            "building one probes for a helper binary or opens a D-Bus proxy"
        )
        assert published == [], (
            f"a re-check with no rule must not publish a foreground window, "
            f"got {published}"
        )
    assert grabber.is_watching is False


def check_recheck_coalesces() -> None:
    """A burst of re-checks costs one further pass, not one per request.

    Every pass queries the desktop, which runs subprocesses, and the page
    editor asks for one on every edit that lands.
    """
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")
    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, wm_class="firefox", regex_title=".*",
        stay_on_page=True, decks=["SERIAL"],
    )

    grabber = _fresh_grabber()
    integration = _integration_of(grabber)
    controller = StubDeckController("SERIAL", active_page=StubPage(manual_path))

    gate = threading.Event()
    StubIntegration.query_gate = gate

    with _registered(controller, [manual_path, rule_path]):
        try:
            grabber.recheck_active_window()
            assert fixtures.wait_until(
                lambda: integration.active_window_queries >= 1, TIMEOUT_S
            ), "the first re-check never reached the desktop query"
            # Four more requests while the first pass is held inside the query.
            for _ in range(4):
                grabber.recheck_active_window()
        finally:
            StubIntegration.query_gate = None
            gate.set()

        _settle_recheck(grabber)

    assert integration.active_window_queries == 2, (
        f"five requests must settle into the running pass plus one more, "
        f"each re-reading the rules; got {integration.active_window_queries} "
        f"desktop queries"
    )


def check_recheck_and_watcher_load_once() -> None:
    """A re-check carrying the window the watcher just reported must not load
    the page a second time.

    Two routings that overlap both read the page the deck shows before either
    has loaded, so both decide the deck must change. The user sees the page
    build twice.
    """
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")
    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, wm_class="firefox", regex_title=".*",
        stay_on_page=True, decks=["SERIAL"],
    )

    grabber = _fresh_grabber()
    controller = StubDeckController("SERIAL", active_page=StubPage(manual_path))
    # Long enough that the second routing starts while the first one loads.
    controller.load_delay = 0.05

    start = threading.Barrier(2)

    def watcher_route() -> None:
        start.wait(TIMEOUT_S)
        grabber.on_active_window_changed(FIREFOX)

    with _registered(controller, [manual_path, rule_path]):
        watcher = threading.Thread(target=watcher_route, name="StubWatcher")
        watcher.start()
        start.wait(TIMEOUT_S)
        grabber.recheck_active_window()

        watcher.join(TIMEOUT_S)
        assert not watcher.is_alive(), "the watcher routing never finished"
        _settle_recheck(grabber)

        assert controller.loaded_pages == [rule_path], (
            f"the deck loaded the page more than once: a re-check and the "
            f"watcher routed the same window at the same time, got "
            f"{controller.loaded_pages}"
        )


# The D-Bus property the routing publishes.

def check_foreground_window_publishes_only_on_change() -> None:
    """Re-applying the rules re-publishes the window in front, and the value
    is the one the clients already hold. A PropertiesChanged for an unchanged
    value wakes every subscriber for nothing."""
    published: list[tuple] = []
    original_emit = api._emit_properties_changed
    original_instance = api._api_instance
    api._emit_properties_changed = lambda *args, **kwargs: published.append(args)
    api._api_instance = api.DeckardAPI()

    try:
        api.notify_foreground_window_changed("Mozilla Firefox", "firefox")
        assert len(published) == 1, (
            f"a new foreground window must reach the clients, got {published}"
        )

        api.notify_foreground_window_changed("Mozilla Firefox", "firefox")
        assert len(published) == 1, (
            f"the same window must not be published again, got {published}"
        )

        api.notify_foreground_window_changed("Inbox", "thunderbird")
        assert len(published) == 2, (
            f"a different window must reach the clients, got {published}"
        )
    finally:
        api._emit_properties_changed = original_emit
        api._api_instance = original_instance


def main() -> None:
    fixtures.start_watchdog(120, label="scenario_autoswitch_rule_edit")
    _install_stub_selector()
    fixtures._install_integration_globals()

    check_title_only_rule_fires()
    check_wm_class_only_rule_fires()
    check_empty_pattern_matches_every_window()
    check_rule_without_patterns_is_inert()
    check_cleared_pattern_does_not_take_over()
    check_recheck_applies_the_window_in_front()
    check_recheck_restores_a_disabled_rule()
    check_recheck_without_a_window_in_front()
    check_recheck_is_inert_with_no_rule()
    check_recheck_coalesces()
    check_recheck_and_watcher_load_once()
    check_foreground_window_publishes_only_on_change()

    print("PASS: scenario_autoswitch_rule_edit")


if __name__ == "__main__":
    main()
