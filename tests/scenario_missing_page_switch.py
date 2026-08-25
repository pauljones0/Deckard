"""A named page that does not build must never blank a deck.

get_page() answers None where a page path resolves and the page behind it
does not build, such as a page whose file was removed from disk after a list
was filled. load_page(None) clears the deck, so any surface that forwards
that None takes the user's keys away and reports nothing.

Three forward surfaces are covered: the header page selector, the automatic
window switch, and the restore back to the manually chosen page. Each must
keep the page the deck already shows, and each must say why it did nothing.
"""
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
    """The controller reduced to the page state these surfaces touch.

    load_page records every call, including a call with None, because a None
    reaching the real controller is the whole defect.
    """

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
    """A page manager whose named pages resolve and do not build.

    missing holds the paths get_page() answers None for, which is the state a
    deleted page file leaves behind.
    """

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


class RecordingNotify:
    def __init__(self):
        self.errors: list[str] = []
        self.infos: list[str] = []

    def error(self, text: str, title: str | None = None) -> None:
        self.errors.append(text)

    def info(self, text: str, title: str | None = None) -> None:
        self.infos.append(text)


def check_selector_keeps_the_page(page_manager: FakePageManager) -> None:
    """Picking a page whose file went away must leave the deck alone.

    change_page is bound to a stand-in holding the one field it reads, so the
    check needs no display: the defect is in the forwarding, not in the list.
    """
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


def check_hand_pick_ends_the_automatic_mark(
        page_manager: FakePageManager) -> None:
    """A page picked by hand must end the deck's automatic state.

    Nothing outside the window grabber clears the mark. A deck left marked
    after the user picks a page of their own is taken back to the page it
    left the next time no rule matches, which is a page the user already
    walked away from.
    """
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


def check_a_load_that_raises_keeps_the_mark(
        page_manager: FakePageManager) -> None:
    """A page load that raises must leave the deck's mark as it found it.

    A deck torn down mid-call takes the load down with it. The deck still
    shows the automatic page, so a deck left with the mark off has the next
    automatic switch write that page down as the user's own choice.
    """
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

    # Two kinds of load claim a deck, the restore and a pick by hand. One
    # that retired the other's claim would take the guard off a load still
    # running, so a claim only retires on its own token.
    with grabber.manual_page_load(controller, picked_path):
        grabber._end_manual_load(controller, object())
        assert grabber._pending_manual_path(controller) == picked_path, (
            "a claim must survive another routing's retire")
    assert grabber._pending_manual_path(controller) is None
    print("PASS: a claim on a deck retires only on the token of the routing "
          "that made it")


def check_switch_during_a_restore_keeps_the_way_back(
        page_manager: FakePageManager) -> None:
    """An automatic switch landing during a restore must not record its page.

    The restore clears the automatic mark and then loads, and the load
    marshals onto the GTK main thread, so the deck shows the automatic page
    for the whole of it. A switch that reads the deck there sees an automatic
    page with no mark on it and writes that page down as the user's choice.
    """
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

    check_failed_restore_keeps_the_way_back(page_manager, controller,
                                            auto_page, manual_path)


def check_failed_restore_keeps_the_way_back(page_manager: FakePageManager,
                                            controller: FakeController,
                                            auto_page: FakePage,
                                            manual_path: str) -> None:
    """A restore that does not build must leave the deck able to try again.

    The deck sits on an automatically loaded page it could not leave. Marking
    it as no longer automatic there strands it: no later window change carries
    the restore, and the next automatic switch reads the stranded page as the
    user's own choice and overwrites the remembered path with it.
    """
    grabber = WindowGrabber.__new__(WindowGrabber)
    grabber._dispatch_lock = threading.RLock()

    assert controller.page_auto_loaded is True, (
        "a restore that did not build must leave the deck marked as "
        "automatically loaded, so a later window change tries again")
    assert controller.last_manual_loaded_page_path == manual_path, (
        f"the failed restore must keep the way back, it holds "
        f"{controller.last_manual_loaded_page_path}")

    # Focus moves to a second matching window, so the deck switches from one
    # automatic page to another. A deck wrongly marked as manual here has the
    # page it could not leave written down as the user's own choice.
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
    check_hand_pick_ends_the_automatic_mark(FakePageManager(page_dir))
    check_a_load_that_raises_keeps_the_mark(FakePageManager(page_dir))
    check_switch_during_a_restore_keeps_the_way_back(
        FakePageManager(page_dir))

    print("ALL PASS: scenario_missing_page_switch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
