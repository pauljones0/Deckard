"""Verify edited auto-switch rules apply at once on a background thread.
One pattern matches alone, while a rule with neither pattern stays inert."""

# Use a stub integration to test matching and rechecks without a live desktop.
# Watcher gating has a separate scenario.
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
# Keep the held-load bound well above observation time so timeout release
# cannot look like unblocked routing.
HELD_LOAD_TIMEOUT_S = 30.0
OBSERVE_S = 2.0

FIREFOX = Window(wm_class="firefox", title="Mozilla Firefox")
TERMINAL = Window(wm_class="kitty", title="terminal")
# The page editor itself is in front while a rule is being typed.
EDITOR = Window(wm_class="deckard", title="Deckard")


class StubIntegration(Integration):
    """Provide a field-backed window source with an optional query gate."""

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
        self._load_lock = threading.RLock()
        # Create an overlap window for legs that race two routings.
        self.load_delay = 0.0

    def serial_number(self) -> str:
        return self._serial

    def load_page(self, page, allow_reload: bool = True) -> None:
        # Match the controller's locked identity check for allow_reload=False.
        # This prevents duplicate or overlapping loads in the test stub.
        with self._load_lock:
            if not allow_reload and self.active_page is page:
                return
            if self.load_delay:
                time.sleep(self.load_delay)
            self.loaded_pages.append(page.json_path)
            self.active_page = page


def _install_stub_selector() -> None:
    """Replace environment-based integration selection with StubIntegration."""
    window_grabber_module.select_integration_class = (
        lambda environment_components, server: StubIntegration
    )


_pages_written = 0


def _write_page(name: str) -> str:
    """Write an empty page under a unique name to avoid cached settings."""
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
    """Settle the previous grabber before its queued worker builds in the next check."""
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
    """Return this grabber's integration rather than the class-wide newest one."""
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
    """Register a stub deck and cache its pages to avoid real Page construction."""
    gl.deck_manager.deck_controller.append(controller)
    gl.page_manager.pages[controller] = {
        path: {"page": StubPage(path), "lru_stamp": number}
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
    """Treat a cleared pattern as absent when the other pattern is set."""
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


def check_rule_without_patterns_is_inert() -> None:
    """Keep an enabled rule with neither pattern inert."""
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


def check_cleared_pattern_stays_inert() -> None:
    """Keep the deck stable while its only rule pattern is cleared for editing."""
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


def check_recheck_applies_foreground_window() -> None:
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
    """Restore the deck when one rule is disabled but the watcher stays active."""
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


def check_recheck_without_foreground_window() -> None:
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


def check_recheck_inert_without_rules() -> None:
    """Skip integration, desktop, and D-Bus work when no rule is enabled."""
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
    """Coalesce a recheck burst into the running pass plus one further pass."""
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
    """Load once when watcher and recheck concurrently route the same window."""
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")
    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, wm_class="firefox", regex_title=".*",
        stay_on_page=True, decks=["SERIAL"],
    )

    grabber = _fresh_grabber()
    controller = StubDeckController("SERIAL", active_page=StubPage(manual_path))
    # Keep the first load open long enough to create an overlap window.
    controller.load_delay = 0.25

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


def check_reported_window_routes_off_caller() -> None:
    """Route reports from any session-bus caller off the GTK caller thread.
    A route there can self-wait for 30 s and leave the page half built."""
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")
    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, wm_class="firefox", regex_title=".*",
        stay_on_page=True, decks=["SERIAL"],
    )

    grabber = _fresh_grabber()

    order: list[str] = []
    load_started = threading.Event()
    main_thread_returned = threading.Event()

    class MarshallingController(StubDeckController):
        def load_page(self, page, allow_reload: bool = True) -> None:
            load_started.set()
            assert main_thread_returned.wait(TIMEOUT_S), (
                "the load waited for the main thread, and the main thread was "
                "waiting inside a routing of its own: the notify method must "
                "not route on the thread a load marshals onto"
            )
            landed_before = len(self.loaded_pages)
            super().load_page(page, allow_reload=allow_reload)
            if len(self.loaded_pages) > landed_before:
                # Record only the load that changes the page; either routing may win.
                # The controller rejects the second load by page identity.
                order.append("load")

    controller = MarshallingController("SERIAL", active_page=StubPage(manual_path))

    dispatch_threads: list[threading.Thread] = []
    dispatch = grabber.on_active_window_changed

    def record(window: Window) -> None:
        try:
            dispatch(window)
        finally:
            dispatch_threads.append(threading.current_thread())

    grabber.on_active_window_changed = record

    api_instance = api.DeckardAPI()
    original_instance = api._api_instance
    api._api_instance = api_instance

    with _registered(controller, [manual_path, rule_path]):
        try:
            grabber.recheck_active_window()
            assert load_started.wait(TIMEOUT_S), (
                "the background routing never reached the page load"
            )

            # The main thread, inside the D-Bus method, while that load waits.
            api_instance.NotifyForegroundWindow(FIREFOX.title, FIREFOX.wm_class)
            order.append("notify")
            main_thread_returned.set()

            assert fixtures.wait_until(lambda: len(dispatch_threads) >= 2, TIMEOUT_S), (
                f"the reported window was never routed, got "
                f"{len(dispatch_threads)} routings"
            )
            _settle_recheck(grabber)
        finally:
            main_thread_returned.set()
            api._api_instance = original_instance

    assert order == ["notify", "load"], (
        f"the notify method must return while the load it met is still in "
        f"flight, and the deck must take that load once, got {order}"
    )
    assert threading.main_thread() not in dispatch_threads, (
        "a routing ran on the main thread, which is the thread a page load "
        "marshals onto"
    )
    assert controller.loaded_pages == [rule_path], (
        f"the deck must load the page once for two routings carrying the same "
        f"window, got {controller.loaded_pages}"
    )


def check_deck_loads_do_not_block_routing() -> None:
    """Keep per-deck decisions atomic without holding the routing lock during load."""
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")
    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, wm_class="firefox", regex_title=".*",
        stay_on_page=True, decks=["QUICK", "SLOW"],
    )

    grabber = _fresh_grabber()

    load_started = threading.Event()
    release_load = threading.Event()

    class BlockingController(StubDeckController):
        def load_page(self, page, allow_reload: bool = True) -> None:
            load_started.set()
            # Keep this bound above observation time so timeout release cannot
            # look like unblocked routing.
            assert release_load.wait(HELD_LOAD_TIMEOUT_S), (
                "the held load was never released"
            )
            super().load_page(page, allow_reload=allow_reload)

    # The deck that needs no load comes first, so a routing reaches it before
    # it meets the one that blocks.
    quick = StubDeckController("QUICK", active_page=StubPage(rule_path))
    slow = BlockingController("SLOW", active_page=StubPage(manual_path))

    def route() -> None:
        grabber.on_active_window_changed(FIREFOX)

    with _registered(quick, [manual_path, rule_path]):
        with _registered(slow, [manual_path, rule_path]):
            blocked_routing = threading.Thread(target=route, name="StubRoutingHeld")
            concurrent_routing = threading.Thread(target=route, name="StubRoutingFree")
            try:
                blocked_routing.start()
                assert load_started.wait(TIMEOUT_S), (
                    "the first routing never reached the held load"
                )

                # The first routing is inside the load now. Clear what it left
                # on the quick deck, so only the second routing can set it.
                quick.page_auto_loaded = False
                concurrent_routing.start()

                assert fixtures.wait_until(
                    lambda: quick.page_auto_loaded is True, OBSERVE_S
                ), (
                    "a routing waited for a page load on another deck: the "
                    "decision holds the routing lock, and a load must not"
                )
            finally:
                release_load.set()
                blocked_routing.join(TIMEOUT_S)
                concurrent_routing.join(TIMEOUT_S)

            assert not blocked_routing.is_alive() and not concurrent_routing.is_alive()
            assert slow.loaded_pages == [rule_path], (
                f"the held deck must end on the page its rule names, once, "
                f"got {slow.loaded_pages}"
            )


def check_restore_once_under_race() -> None:
    """Restore once when two routings concurrently find no matching rule.
    Read the manual path and clear the automatic flag atomically."""
    _clear_pages()
    manual_path = _write_page("Manual")
    rule_path = _write_page("Browser")
    gl.page_manager.overwrite_auto_change_settings(
        path=rule_path, enable=True, wm_class="firefox", regex_title=".*",
        stay_on_page=False, decks=["SERIAL"],
    )

    grabber = _fresh_grabber()
    controller = StubDeckController("SERIAL", active_page=StubPage(rule_path))
    controller.page_auto_loaded = True
    controller.last_manual_loaded_page_path = manual_path
    controller.load_delay = 0.05

    start = threading.Barrier(3)

    def route() -> None:
        start.wait(TIMEOUT_S)
        grabber.on_active_window_changed(TERMINAL)

    with _registered(controller, [manual_path, rule_path]):
        routings = [
            threading.Thread(target=route, name=f"StubRouting{index}")
            for index in range(2)
        ]
        for routing in routings:
            routing.start()
        start.wait(TIMEOUT_S)
        for routing in routings:
            routing.join(TIMEOUT_S)
            assert not routing.is_alive(), "a routing never finished"

        assert controller.loaded_pages == [manual_path], (
            f"the deck must go back to the page the user chose once, got "
            f"{controller.loaded_pages}"
        )
        assert controller.page_auto_loaded is False
        assert controller.last_manual_loaded_page_path == manual_path, (
            f"the path back to the user's page was overwritten during the "
            f"undo, got {controller.last_manual_loaded_page_path}"
        )


def check_foreground_publish_on_change() -> None:
    """Publish PropertiesChanged only when the foreground window changes."""
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
    fixtures.start_watchdog(60, label="scenario_autoswitch_rule_edit")
    _install_stub_selector()
    fixtures._install_integration_globals()

    check_title_only_rule_fires()
    check_wm_class_only_rule_fires()
    check_empty_pattern_matches_every_window()
    check_rule_without_patterns_is_inert()
    check_cleared_pattern_stays_inert()
    check_recheck_applies_foreground_window()
    check_recheck_restores_a_disabled_rule()
    check_recheck_without_foreground_window()
    check_recheck_inert_without_rules()
    check_recheck_coalesces()
    check_recheck_and_watcher_load_once()
    check_reported_window_routes_off_caller()
    check_deck_loads_do_not_block_routing()
    check_restore_once_under_race()
    check_foreground_publish_on_change()

    print("PASS: scenario_autoswitch_rule_edit")


if __name__ == "__main__":
    main()
