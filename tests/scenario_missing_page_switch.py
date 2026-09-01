"""Keep the current deck page when a named page cannot be built.
Selectors, automatic switches, and restores must not forward None to load_page."""
import fixtures  # noqa: F401  (import first: isolates DATA_PATH)

import os
import threading
import types

import globals as gl
from locales.LocaleManager import LocaleManager
from src.backend.WindowGrabber.Window import Window
from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

WM_CLASS = "firefox"
FIREFOX = Window(wm_class=WM_CLASS, title="Mozilla Firefox")
OTHER = Window(wm_class="kitty", title="terminal")
SECOND = Window(wm_class="code", title="editor")


class FakePage:
    def __init__(self, json_path: str):
        self.json_path = json_path


class FakeDeck:
    def __init__(self, serial: str):
        self._serial = serial

    def is_open(self) -> bool:
        return True

    def get_serial_number(self) -> str:
        return self._serial


class FakeController:
    """Record all page loads, including an invalid None load."""

    def __init__(self, serial: str, active_page: FakePage | None):
        self.deck = FakeDeck(serial)
        self._serial = serial
        self.active_page = active_page
        self.page_auto_loaded = False
        self.last_manual_loaded_page_path: str | None = None
        self.loaded: list[object] = []

    def serial_number(self) -> str:
        return self._serial

    def load_page(self, page, allow_reload: bool = True) -> None:
        self.loaded.append(page)
        self.active_page = page


class FakePageManager:
    """Resolve names while returning None for paths whose page file is missing."""

    def __init__(self, page_dir: str):
        self.page_dir = page_dir
        self.missing: set[str] = set()
        self.auto_change: dict[str, dict] = {}

    def path_of(self, name: str) -> str:
        return os.path.join(self.page_dir, f"{name}.json")

    def get_pages(self, *args, **kwargs):
        return sorted(self.auto_change)

    def get_page(self, path=None, deck_controller=None):
        if path in self.missing:
            return None
        return FakePage(path)

    def get_auto_change_settings(self, page_path: str) -> dict:
        return self.auto_change.get(page_path, {})

    def find_matching_page_path(self, name: str) -> str | None:
        """Resolve full page paths only; these checks do not use bare names."""
        if name.startswith(self.page_dir):
            return name
        return None


class RecordingNotify:
    def __init__(self):
        self.errors: list[str] = []
        self.infos: list[str] = []

    def error(self, text: str, title: str | None = None) -> None:
        self.errors.append(text)

    def info(self, text: str, title: str | None = None) -> None:
        self.infos.append(text)


def check_selector_keeps_the_page(page_manager: FakePageManager) -> None:
    """Require a missing selected page to leave the deck unchanged and report failure."""
    from src.windows.mainWindow.elements.PageSelector import PageSelector

    held = FakePage(page_manager.path_of("home"))
    controller = FakeController("SEL", held)
    deck_stack = types.SimpleNamespace(
        get_visible_child=lambda: types.SimpleNamespace(
            deck_controller=controller))
    selector = types.SimpleNamespace(
        main_window=types.SimpleNamespace(
            leftArea=types.SimpleNamespace(deck_stack=deck_stack)))

    notify = RecordingNotify()
    gl.notify = notify

    gone = page_manager.path_of("deleted")
    page_manager.missing.add(gone)
    PageSelector.change_page.__get__(selector)(gone)

    assert controller.loaded == [], (
        f"picking a page that does not build must load nothing, the "
        f"controller was handed {controller.loaded!r}")
    assert controller.active_page is held, (
        "the deck must keep the page it shows when the pick does not build")
    assert len(notify.errors) == 1, (
        f"the failed pick must tell the user once, it raised "
        f"{notify.errors!r}")
    assert "page-selector-load-failed" not in notify.errors[0], (
        "the toast shows the raw locale key, so the CSV has no row for it")

    # A page that does build still loads, so the guard cannot be a blanket
    # refusal to switch.
    good = page_manager.path_of("work")
    PageSelector.change_page.__get__(selector)(good)
    assert len(controller.loaded) == 1 and controller.loaded[0] is not None, (
        f"a page that builds must still load, the controller took "
        f"{controller.loaded!r}")
    assert controller.active_page.json_path == good
    assert len(notify.errors) == 1, "a page that loads must raise no error"
    print("PASS: the page selector keeps the deck's page and reports the "
          "failure when the picked page does not build")


def check_auto_switch_keeps_the_page(page_manager: FakePageManager) -> None:
    """An automatic switch to a page that does not build must not clear."""
    gl.page_manager = page_manager
    rule_path = page_manager.path_of("auto")
    held = FakePage(page_manager.path_of("home"))
    controller = FakeController("AUTO", held)
    page_manager.auto_change = {
        rule_path: {"enable": True, "decks": ["AUTO"],
                    "wm-class": WM_CLASS, "title": ".*",
                    "stay-on-page": False},
    }
    page_manager.missing.add(rule_path)

    grabber = WindowGrabber.__new__(WindowGrabber)
    grabber._dispatch_lock = threading.RLock()

    grabber._apply_auto_change(controller, FIREFOX)

    assert controller.loaded == [], (
        f"an automatic switch to a page that does not build must load "
        f"nothing, the controller took {controller.loaded!r}")
    assert controller.active_page is held, (
        "the deck must keep its page when the matched page does not build")
    assert controller.page_auto_loaded is False, (
        "a switch that did not build must leave the deck on the user's page "
        "and marked as the user's own choice")
    assert controller.last_manual_loaded_page_path is None, (
        f"a switch that did not build must write nothing down as the way "
        f"back, it holds {controller.last_manual_loaded_page_path}")

    # The page file returns, so the same window now switches the deck. The
    # page the deck sat on the whole time is the one to come back to.
    page_manager.missing.discard(rule_path)
    grabber._apply_auto_change(controller, FIREFOX)
    assert controller.active_page.json_path == rule_path, (
        f"a matched page that builds must load, the deck shows "
        f"{controller.active_page.json_path}")
    assert controller.page_auto_loaded is True
    assert controller.last_manual_loaded_page_path == held.json_path, (
        f"the deck must come back to the page it left, it points at "
        f"{controller.last_manual_loaded_page_path}")
    print("PASS: the automatic window switch keeps the deck's page when the "
          "matched page does not build, and marks nothing until one does")


def check_manual_pick_clears_auto_mark(
        page_manager: FakePageManager) -> None:
    """End automatic state when the user selects a page.
    The selected page becomes the next automatic restore destination."""
    from src.windows.mainWindow.elements.PageSelector import PageSelector

    gl.page_manager = page_manager
    home = FakePage(page_manager.path_of("home"))
    auto_path = page_manager.path_of("auto")
    picked_path = page_manager.path_of("picked")
    controller = FakeController("PICK", home)
    page_manager.auto_change = {
        auto_path: {"enable": True, "decks": ["PICK"],
                    "wm-class": WM_CLASS, "title": ".*",
                    "stay-on-page": False},
    }

    grabber = WindowGrabber.__new__(WindowGrabber)
    grabber._dispatch_lock = threading.RLock()
    gl.window_grabber = grabber
    gl.notify = RecordingNotify()

    grabber._apply_auto_change(controller, FIREFOX)
    assert controller.active_page.json_path == auto_path
    assert controller.page_auto_loaded is True

    deck_stack = types.SimpleNamespace(
        get_visible_child=lambda: types.SimpleNamespace(
            deck_controller=controller))
    selector = types.SimpleNamespace(
        main_window=types.SimpleNamespace(
            leftArea=types.SimpleNamespace(deck_stack=deck_stack)))
    PageSelector.change_page.__get__(selector)(picked_path)
    assert controller.active_page.json_path == picked_path, (
        f"the pick must load, the deck shows "
        f"{controller.active_page.json_path}")
    assert controller.page_auto_loaded is False, (
        "a page picked by hand must end the deck's automatic state")

    # Focus moves to a window no rule matches. The deck is on the user's own
    # page, so there is nothing to undo and nothing to load.
    controller.loaded.clear()
    grabber._apply_auto_change(controller, OTHER)
    assert controller.loaded == [], (
        f"a deck on a page the user picked must not be taken anywhere, the "
        f"controller took {controller.loaded!r}")
    assert controller.active_page.json_path == picked_path, (
        f"the deck must keep the page the user picked, it shows "
        f"{controller.active_page.json_path}")

    # The next automatic switch comes back to the picked page, not to the one
    # the user left before it.
    grabber._apply_auto_change(controller, FIREFOX)
    assert controller.last_manual_loaded_page_path == picked_path, (
        f"the deck must come back to the page the user picked, it points at "
        f"{controller.last_manual_loaded_page_path}")
    gl.window_grabber = None
    print("PASS: a page picked by hand ends the automatic state and becomes "
          "the page the deck comes back to")


def check_command_switch_clears_auto_mark(
        page_manager: FakePageManager) -> None:
    """End automatic state for successful control-plane page commands.
    Failed and already-active commands must preserve the existing mark."""
    from src.backend import control_plane

    gl.page_manager = page_manager
    home = FakePage(page_manager.path_of("home"))
    auto_path = page_manager.path_of("auto")
    named_path = page_manager.path_of("named")
    broken_path = page_manager.path_of("broken")
    controller = FakeController("CMD", home)
    page_manager.auto_change = {
        auto_path: {"enable": True, "decks": ["CMD"],
                    "wm-class": WM_CLASS, "title": ".*",
                    "stay-on-page": False},
    }
    page_manager.missing = {broken_path}

    grabber = WindowGrabber.__new__(WindowGrabber)
    grabber._dispatch_lock = threading.RLock()
    gl.window_grabber = grabber

    plane = control_plane.get()

    grabber._apply_auto_change(controller, FIREFOX)
    assert controller.active_page.json_path == auto_path
    assert controller.page_auto_loaded is True, (
        "an automatic switch marks the deck it moved")

    # A page that does not build is refused, and the deck keeps both its page
    # and its mark: no load happened, so there is no choice to record.
    controller.loaded.clear()
    result = plane.change_page_on(controller, broken_path)
    assert not result.ok and result.code == "page-build-failed", (
        f"a page that does not build must be refused, the plane answered "
        f"{result.code!r}")
    assert controller.loaded == [], (
        f"a refused switch must load nothing, the controller took "
        f"{controller.loaded!r}")
    assert controller.page_auto_loaded is True, (
        "a refused switch must leave the deck's mark alone")

    # The page the deck already shows is a no-op, and it too leaves the mark.
    result = plane.change_page_on(controller, auto_path)
    assert result.ok and result.code == "already-active", (
        f"the active page must be a no-op, the plane answered {result.code!r}")
    assert controller.loaded == [], (
        f"a no-op switch must load nothing, the controller took "
        f"{controller.loaded!r}")
    assert controller.page_auto_loaded is True, (
        "a no-op switch must leave the deck's mark alone, so the automatic "
        "page stays undoable")

    result = plane.change_page_on(controller, named_path)
    assert result.ok, f"the named page must load: {result.message}"
    assert controller.active_page.json_path == named_path, (
        f"the command must load its page, the deck shows "
        f"{controller.active_page.json_path}")
    assert controller.page_auto_loaded is False, (
        "a page named by a command must end the deck's automatic state")

    # Focus moves to a window no rule matches. The deck is on the page the
    # command named, so there is nothing to undo and nothing to load.
    controller.loaded.clear()
    grabber._apply_auto_change(controller, OTHER)
    assert controller.loaded == [], (
        f"a deck on a page a command named must not be taken anywhere, the "
        f"controller took {controller.loaded!r}")
    assert controller.active_page.json_path == named_path, (
        f"the deck must keep the page the command named, it shows "
        f"{controller.active_page.json_path}")

    # The next automatic switch comes back to the named page, not to the one
    # the deck was on before the command.
    grabber._apply_auto_change(controller, FIREFOX)
    assert controller.last_manual_loaded_page_path == named_path, (
        f"the deck must come back to the page the command named, it points "
        f"at {controller.last_manual_loaded_page_path}")

    # A switch during a command load must record the commanded destination.
    # The pending claim identifies it while the deck still shows the old page.
    second_path = page_manager.path_of("auto-second")
    page_manager.auto_change[second_path] = {
        "enable": True, "decks": ["CMD"], "wm-class": SECOND.wm_class,
        "title": ".*", "stay-on-page": False,
    }
    commanded_path = page_manager.path_of("commanded")
    switched: list[bool] = []
    original_load = controller.load_page

    def load_page(page, allow_reload: bool = True) -> None:
        if not switched:
            switched.append(True)
            grabber._apply_auto_change(controller, SECOND)
        original_load(page, allow_reload)

    controller.load_page = load_page
    result = plane.change_page_on(controller, commanded_path)
    assert result.ok, f"the commanded page must load: {result.message}"
    assert switched, "the switch must have run inside the command's load"
    assert controller.last_manual_loaded_page_path == commanded_path, (
        f"a switch inside a command's load must record the page the command "
        f"named, the deck points at "
        f"{controller.last_manual_loaded_page_path}")
    controller.load_page = original_load

    gl.window_grabber = None
    print("PASS: a page named by a command ends the automatic state and "
          "becomes the page the deck comes back to")


def check_failed_load_preserves_auto_mark(
        page_manager: FakePageManager) -> None:
    """Restore the automatic mark when a manual page load raises.
    The deck still shows the automatic page and must not record it as manual."""
    gl.page_manager = page_manager
    auto_page = FakePage(page_manager.path_of("auto"))
    manual_path = page_manager.path_of("manual")
    picked_path = page_manager.path_of("picked")
    controller = FakeController("RAISE", auto_page)
    controller.page_auto_loaded = True
    controller.last_manual_loaded_page_path = manual_path

    grabber = WindowGrabber.__new__(WindowGrabber)
    grabber._dispatch_lock = threading.RLock()

    def load_page(page, allow_reload: bool = True) -> None:
        raise RuntimeError("the deck went away mid-load")

    controller.load_page = load_page

    raised = False
    try:
        with grabber.manual_page_load(controller, picked_path):
            controller.load_page(FakePage(picked_path))
    except RuntimeError:
        raised = True

    assert raised, "the load's error must reach the caller"
    assert controller.active_page is auto_page, (
        "a load that raised leaves the deck on the page it showed")
    assert controller.page_auto_loaded is True, (
        "a load that raised must leave the deck marked as it was, so the "
        "page it still shows is not read as the user's own choice")
    assert grabber._pending_manual_path(controller) is None, (
        "a load that raised must retire its claim on the deck")

    # The mark still stands, so the next automatic switch remembers the real
    # manual page rather than the page the failed load left on the deck.
    second_path = page_manager.path_of("auto-second")
    page_manager.auto_change = {
        second_path: {"enable": True, "decks": ["RAISE"],
                      "wm-class": SECOND.wm_class, "title": ".*",
                      "stay-on-page": False},
    }
    controller.load_page = types.MethodType(FakeController.load_page,
                                            controller)
    grabber._apply_auto_change(controller, SECOND)
    assert controller.last_manual_loaded_page_path == manual_path, (
        f"the deck must keep its way back after a load that raised, it "
        f"points at {controller.last_manual_loaded_page_path}")
    print("PASS: a page load that raises leaves the deck's mark and its way "
          "back as they were")

    # Restore and manual loads can claim the same deck concurrently.
    # A claim must retire only with its own token.
    with grabber.manual_page_load(controller, picked_path):
        grabber._end_manual_load(controller, object())
        assert grabber._pending_manual_path(controller) == picked_path, (
            "a claim must survive another routing's retire")
    assert grabber._pending_manual_path(controller) is None
    print("PASS: a claim on a deck retires only on the token of the routing "
          "that made it")


def check_restore_race_preserves_manual_path(
        page_manager: FakePageManager) -> None:
    """Keep the manual destination when an automatic switch lands during restore.
    The restore claim disambiguates the still-visible automatic page."""
    gl.page_manager = page_manager
    auto_page = FakePage(page_manager.path_of("auto"))
    manual_path = page_manager.path_of("manual")
    second_path = page_manager.path_of("auto-second")
    controller = FakeController("RACE", auto_page)
    controller.page_auto_loaded = True
    controller.last_manual_loaded_page_path = manual_path
    page_manager.auto_change = {
        auto_page.json_path: {"enable": True, "decks": ["RACE"],
                              "wm-class": WM_CLASS, "title": ".*",
                              "stay-on-page": False},
        second_path: {"enable": True, "decks": ["RACE"],
                      "wm-class": SECOND.wm_class, "title": ".*",
                      "stay-on-page": False},
    }

    grabber = WindowGrabber.__new__(WindowGrabber)
    grabber._dispatch_lock = threading.RLock()

    # The switch runs at the point the restore has claimed the load and the
    # deck still shows the page it is leaving.
    switched: list[bool] = []
    original_load = controller.load_page

    def load_page(page, allow_reload: bool = True) -> None:
        if not switched:
            switched.append(True)
            grabber._apply_auto_change(controller, SECOND)
        original_load(page, allow_reload)

    controller.load_page = load_page

    grabber._apply_auto_change(controller, OTHER)

    assert switched, "the switch must have run inside the restore's load"
    assert controller.last_manual_loaded_page_path == manual_path, (
        f"a switch inside a restore must not write the automatic page down "
        f"as the user's choice, the deck points at "
        f"{controller.last_manual_loaded_page_path}")
    print("PASS: an automatic switch landing during a restore keeps the "
          "deck's way back")


def check_manual_restore_keeps_the_page(page_manager: FakePageManager) -> None:
    """The restore to the manual page must not clear it away either."""
    gl.page_manager = page_manager
    auto_page = FakePage(page_manager.path_of("auto"))
    manual_path = page_manager.path_of("manual")
    controller = FakeController("REST", auto_page)
    controller.page_auto_loaded = True
    controller.last_manual_loaded_page_path = manual_path
    page_manager.auto_change = {
        auto_page.json_path: {"enable": True, "decks": ["REST"],
                              "wm-class": WM_CLASS, "title": ".*",
                              "stay-on-page": False},
    }
    page_manager.missing = {manual_path}

    grabber = WindowGrabber.__new__(WindowGrabber)
    grabber._dispatch_lock = threading.RLock()

    # No rule matches this window, which is the path that restores.
    grabber._apply_auto_change(controller, OTHER)

    assert controller.loaded == [], (
        f"a restore to a page that does not build must load nothing, the "
        f"controller took {controller.loaded!r}")
    assert controller.active_page is auto_page, (
        "the deck must keep its page when the manual page does not build")
    print("PASS: the restore to the manually chosen page keeps the deck's "
          "page when that page does not build")

    check_failed_restore_preserves_manual_path(page_manager, controller,
                                               auto_page, manual_path)


def check_failed_restore_preserves_manual_path(page_manager: FakePageManager,
                                               controller: FakeController,
                                               auto_page: FakePage,
                                               manual_path: str) -> None:
    """Keep the automatic mark and manual path when a restore cannot build.
    A later window change must be able to retry the restore."""
    grabber = WindowGrabber.__new__(WindowGrabber)
    grabber._dispatch_lock = threading.RLock()

    assert controller.page_auto_loaded is True, (
        "a restore that did not build must leave the deck marked as "
        "automatically loaded, so a later window change tries again")
    assert controller.last_manual_loaded_page_path == manual_path, (
        f"the failed restore must keep the way back, it holds "
        f"{controller.last_manual_loaded_page_path}")

    # Switch between automatic pages without recording either as the manual choice.
    second_path = page_manager.path_of("auto-second")
    page_manager.auto_change[second_path] = {
        "enable": True, "decks": ["REST"], "wm-class": SECOND.wm_class,
        "title": ".*", "stay-on-page": False,
    }
    grabber._apply_auto_change(controller, SECOND)
    assert controller.active_page.json_path == second_path, (
        f"the second rule must switch the deck, it shows "
        f"{controller.active_page.json_path}")
    assert controller.last_manual_loaded_page_path == manual_path, (
        f"an automatic page must never be recorded as the manual one, the "
        f"deck now points back to "
        f"{controller.last_manual_loaded_page_path}")
    controller.loaded.clear()

    # The user restores the page file. The next focus change away must take
    # the deck back, which is only reachable while the flag stands.
    page_manager.missing.discard(manual_path)
    grabber._apply_auto_change(controller, OTHER)

    assert len(controller.loaded) == 1, (
        f"a restore that builds must load the manual page, the controller "
        f"took {controller.loaded!r}")
    assert controller.active_page.json_path == manual_path, (
        f"the deck must sit on the manual page again, it shows "
        f"{controller.active_page.json_path}")
    assert controller.page_auto_loaded is False, (
        "a restore that loaded must mark the deck as no longer automatic")
    assert auto_page is not controller.active_page
    print("PASS: a restore that does not build keeps the deck's way back, and "
          "the restore succeeds once the page file returns")


def main() -> int:
    page_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(page_dir, exist_ok=True)

    # The real locale manager, because a key with no CSV row falls back to
    # the raw key and the user reads that in the toast.
    gl.lm = LocaleManager(csv_path=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "locales",
        "locales.csv"))
    page_manager = FakePageManager(page_dir)
    gl.page_manager = page_manager

    check_selector_keeps_the_page(page_manager)
    check_auto_switch_keeps_the_page(FakePageManager(page_dir))
    check_manual_restore_keeps_the_page(FakePageManager(page_dir))
    check_manual_pick_clears_auto_mark(FakePageManager(page_dir))
    check_command_switch_clears_auto_mark(FakePageManager(page_dir))
    check_failed_load_preserves_auto_mark(FakePageManager(page_dir))
    check_restore_race_preserves_manual_path(
        FakePageManager(page_dir))

    print("ALL PASS: scenario_missing_page_switch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
