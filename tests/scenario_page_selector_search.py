"""The header page selector must let a user find a page by typing.

The header held a plain combo box over a list store, so a deck with many
pages could only be changed by scrolling the whole list. The selector is a
search entry over a filtered list now.

Five legs. Three need no display: every locale key the module asks for must
exist in the CSV, or the widget renders the raw key; the filter and the sort
must narrow and rank the rows the list shows; and the focus re-arm must stop
its idle source after one run. Two need one: the rendered list must follow
the search text, and a pick must load the page through the wiring that was
already there, with the Signals refresh keeping the list current after a
page is added, renamed or deleted.
"""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import csv
import os
import re
import time
import types
from functools import cmp_to_key

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

import globals as gl

from locales.LocaleManager import LocaleManager
from src.backend.DeckManagement.HelperMethods import natural_sort


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOCALES_CSV = os.path.join(REPO_ROOT, "locales", "locales.csv")
MODULE_PATH = os.path.join(REPO_ROOT, "src", "windows", "mainWindow",
                           "elements", "PageSelector.py")

# The page names the corpus holds, and the order the backend hands them over
# in, which is natural order by file name.
PAGE_NAMES = ["brightness", "gaming", "volume_down", "volume_up", "work"]
ALPHABETICAL = list(PAGE_NAMES)

# rapidfuzz scores against the page names, recomputed here as documentation.
# The selector drops anything at or under 50. The checks assert which names
# survive and in what order, not raw values, so a scoring bump surfaces as a
# ranking change and not as a float mismatch.
#   volume: volume_up 80.0, volume_down 70.6, work 20.0, gaming 16.7,
#           brightness 12.5
#   gam:    gaming 66.7, brightness 16.7, volume_down 0.0, volume_up 0.0,
#           work 0.0
QUERIES = [
    ("", ALPHABETICAL),
    ("volume", ["volume_up", "volume_down"]),
    ("gam", ["gaming"]),
    ("zzzz", []),
]


# The fakes the selector reads


class FakePage:
    def __init__(self, json_path: str):
        self.json_path = json_path


class FakeController:
    def __init__(self):
        self.active_page = None
        self.loaded = []

    def load_page(self, page):
        self.loaded.append(page)
        self.active_page = page


class FakeDeckStack:
    def __init__(self, controller):
        self.child = types.SimpleNamespace(deck_controller=controller)

    def get_visible_child(self):
        return self.child


class FakePageManager:
    """The backend reduced to what the selector calls on it.

    get_pages() answers natural order by file name, which is what the real
    PageManagerBackend does, because the selector's alphabetical ordering
    rides on that order.
    """

    def __init__(self, page_dir: str, names):
        self.page_dir = page_dir
        self.names = list(names)

    def path_of(self, name: str) -> str:
        return os.path.join(self.page_dir, f"{name}.json")

    def get_pages(self, *args, **kwargs):
        return [self.path_of(name) for name in natural_sort(self.names)]

    def get_page(self, path: str, deck_controller=None):
        return FakePage(path)


class FakeRow:
    """A row with only the two fields the filter and the sort read."""

    def __init__(self, page_path: str, order: int):
        self.page_path = page_path
        self.order = order


class StubSelector:
    """The filter and the sort over plain objects, with no GTK.

    Stands in where no display exists, so the ranking checks read the same
    code either way.
    """

    def __init__(self, page_manager: FakePageManager):
        from src.windows.mainWindow.elements.PageSelector import PageSelector

        self.filter_func = PageSelector.filter_func.__get__(self)
        self.sort_func = PageSelector.sort_func.__get__(self)
        self.text = ""
        self.search_entry = types.SimpleNamespace(get_text=lambda: self.text)
        self.page_rows = [FakeRow(path, order) for order, path
                          in enumerate(page_manager.get_pages())]

    def query(self, search: str) -> None:
        self.text = search

    def visible_names(self):
        kept = [row for row in self.page_rows if self.filter_func(row)]
        kept.sort(key=cmp_to_key(self.sort_func))
        return [os.path.splitext(os.path.basename(row.page_path))[0]
                for row in kept]


# Helpers


def pump_until(condition, timeout: float, what: str) -> None:
    """Iterate the default main context until condition() holds.

    The search entry emits search-changed off a timer and the Signals fan-out
    lands on an idle source, so neither reaches the widget while this thread
    holds it.
    """
    context = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        while context.iteration(False):
            pass
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout}s: {what}")


def has_display() -> bool:
    """A display the widgets can actually be built against.

    Gtk.init_check() answers True with no display at all, and the first widget
    then dies in the GDK backend, so the default display is the thing to ask
    about.
    """
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


def visible_rows(selector):
    """The rows the list shows now, in the order it shows them.

    A filtered row keeps its place in the box and loses its child visibility,
    so the index walk covers every row and the flag says which are shown.
    """
    rows = []
    index = 0
    while True:
        row = selector.list_box.get_row_at_index(index)
        if row is None:
            return rows
        if row.get_child_visible():
            rows.append(row)
        index += 1


def visible_names(selector):
    return [os.path.splitext(os.path.basename(row.page_path))[0]
            for row in visible_rows(selector)]


def row_named(selector, name: str):
    for row in selector.page_rows:
        if os.path.splitext(os.path.basename(row.page_path))[0] == name:
            return row
    raise AssertionError(f"no row for page {name!r}: {visible_names(selector)}")


# 1. Every locale key the module asks for must exist in the CSV.

def check_locale_keys() -> None:
    """A key absent from the CSV renders as the raw key in the header.

    LocaleManager.get() falls back to the key itself, so a missing row shows
    the user 'header-page-selector-search-hint' where a placeholder belongs.
    """
    with open(LOCALES_CSV, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle, delimiter=";", quotechar='"',
                            skipinitialspace=True)
        header = next(reader)
        locales = header[1:]
        rows = {row[0]: dict(zip(locales, row[1:])) for row in reader if row}

    with open(MODULE_PATH, encoding="utf-8") as handle:
        source = handle.read()
    keys = sorted(set(re.findall(r'gl\.lm\.get\(\s*"([^"]+)"', source)))

    assert keys, "the module asks for no locale key at all -- the scan broke"
    for key in keys:
        assert key in rows, (
            f"{key!r} is asked for by the page selector but has no row in "
            f"locales/locales.csv, so the header renders the raw key")
        for locale in locales:
            assert rows[key].get(locale), (
                f"{key!r} has no {locale} value, so that locale falls back "
                f"to English or to the raw key")

    assert "header-page-selector-search-hint" in keys, (
        "the search entry must carry a translated placeholder")
    print(f"PASS: all {len(keys)} locale keys the page selector asks for have "
          f"a row in every one of the {len(locales)} locales")


# 2. The filter narrows and the sort ranks.

def check_filter_and_sort(selector, label: str) -> None:
    for search, expected in QUERIES:
        selector.query(search)
        got = selector.visible_names()
        if search == "":
            assert got == expected, (
                f"{label}: an empty query must keep every page and list it "
                f"as {expected}, got {got}")
        else:
            assert got == expected, (
                f"{label}: the query {search!r} must leave {expected}, "
                f"closest first, got {got}")
    print(f"PASS: {label} narrows the list to the query and ranks the closest "
          f"page first")


# 3. The focus re-arm must run once.

def check_focus_rearm_stops() -> None:
    """grab_focus() answers True, and a truthy idle callback runs forever.

    The re-arm must therefore return SOURCE_REMOVE of its own, or the header
    pins a core for as long as the app runs.
    """
    from src.windows.mainWindow.elements.PageSelector import PageSelector

    grabs = []

    class Stub:
        page_button = types.SimpleNamespace(
            grab_focus=lambda: (grabs.append(1), True)[1])

    result = PageSelector.grab_page_button_focus(Stub())
    assert grabs == [1], "the re-arm must actually grab the focus"
    assert not result, (
        f"the focus re-arm returned {result!r}; an idle callback that answers "
        f"anything truthy is kept and runs again on every main-loop turn")
    print("PASS: the focus re-arm grabs the focus once and removes its source")


# 4. The rendered list follows the search text.

class RecordingGLib:
    """Stands in for the module's GLib, recording what it defers.

    The module reads GLib off its own globals, so swapping the attribute
    catches the idle_add the re-arm makes without touching the real one.
    """

    SOURCE_REMOVE = GLib.SOURCE_REMOVE

    def __init__(self):
        self.idle_calls = []

    def idle_add(self, func, *args, **kwargs):
        self.idle_calls.append(func)
        return GLib.idle_add(func, *args, **kwargs)


class RealSelector:
    """The widget behind the same query()/visible_names() pair as the stub."""

    def __init__(self, selector):
        self.selector = selector

    def query(self, search: str) -> None:
        self.selector.search_entry.set_text(search)
        pump_until(lambda: self.selector.search_entry.get_text() == search, 10,
                   f"the search entry never took the text {search!r}")
        # search-changed lands off a timer, so wait for the list to settle on
        # a state that matches the text rather than reading it straight away.
        expected = dict(QUERIES).get(search)
        if expected is not None:
            pump_until(lambda: visible_names(self.selector) == expected, 10,
                       f"the list never settled on {expected} for the query "
                       f"{search!r}")

    def visible_names(self):
        return visible_names(self.selector)


def check_selection_loads_page(selector, controller, page_manager) -> None:
    """A pick must reach the deck through the wiring that was there before."""
    import src.windows.mainWindow.elements.PageSelector as module

    selector.search_entry.set_text("")
    pump_until(lambda: visible_names(selector) == ALPHABETICAL, 10,
               "the list never returned to the whole corpus")

    row = row_named(selector, "gaming")
    recorder = RecordingGLib()
    saved_glib = module.GLib
    module.GLib = recorder
    try:
        selector.list_box.emit("row-activated", row)
    finally:
        module.GLib = saved_glib

    assert len(controller.loaded) == 1, (
        f"activating a row must load exactly one page, the controller got "
        f"{controller.loaded!r}")
    assert controller.loaded[0].json_path == page_manager.path_of("gaming"), (
        f"the wrong page was loaded: {controller.loaded[0].json_path!r}")
    assert recorder.idle_calls == [selector.grab_page_button_focus], (
        f"a pick must re-arm the header focus through idle_add, the module "
        f"deferred {recorder.idle_calls!r}")

    # The deck already holds this page now, so a second pick of the same row
    # must not start a second load of it.
    selector.list_box.emit("row-activated", row)
    assert len(controller.loaded) == 1, (
        f"picking the page the deck already holds must not load it again, "
        f"the controller got {controller.loaded!r}")
    print("PASS: a pick loads the page through the deck controller, re-arms "
          "the header focus and refuses a repeat of the active page")


def check_signal_refresh(selector, controller, page_manager) -> None:
    """Add, rename and delete must all reach the list through Signals."""
    from src.Signals import Signals

    # Add
    page_manager.names.append("alpha")
    gl.signal_manager.trigger_signal(Signals.PageAdd)
    pump_until(lambda: "alpha" in visible_names(selector), 10,
               f"an added page never reached the list: {visible_names(selector)}")
    assert visible_names(selector) == ["alpha"] + ALPHABETICAL, (
        f"the added page must land in name order, got "
        f"{visible_names(selector)}")

    # Rename
    page_manager.names[page_manager.names.index("alpha")] = "omega"
    gl.signal_manager.trigger_signal(Signals.PageRename)
    pump_until(lambda: "omega" in visible_names(selector), 10,
               f"a renamed page never reached the list: {visible_names(selector)}")
    assert "alpha" not in visible_names(selector), (
        f"the old name must go with the rename, got {visible_names(selector)}")

    # Delete
    page_manager.names.remove("omega")
    gl.signal_manager.trigger_signal(Signals.PageDelete)
    pump_until(lambda: "omega" not in visible_names(selector), 10,
               f"a deleted page stayed in the list: {visible_names(selector)}")
    assert visible_names(selector) == ALPHABETICAL, (
        f"the list must be back to the whole corpus, got "
        f"{visible_names(selector)}")

    # A page change on the deck must mark the button and the row.
    target = page_manager.path_of("work")
    controller.active_page = FakePage(target)
    gl.signal_manager.trigger_signal(Signals.ChangePage)
    pump_until(lambda: selector.selected_page_path == target, 10,
               f"the deck's page change never reached the selector, it holds "
               f"{selector.selected_page_path!r}")
    assert selector.page_label.get_label() == "work", (
        f"the header must name the page the deck holds, it shows "
        f"{selector.page_label.get_label()!r}")
    assert selector.list_box.get_selected_row() is row_named(selector, "work"), (
        "the list must mark the page the deck holds")
    print("PASS: the list follows a page add, rename and delete, and the "
          "header follows the deck's own page change")


def check_reopen_clears_query(selector) -> None:
    """A query left over from the last open would hide pages silently."""
    selector.search_entry.set_text("gam")
    pump_until(lambda: visible_names(selector) == ["gaming"], 10,
               f"the query never narrowed the list: {visible_names(selector)}")

    selector.page_button.popup()
    pump_until(lambda: selector.popover.get_visible(), 10,
               "the page list never opened")
    assert selector.search_entry.get_text() == "", (
        "opening the list must clear the last query")
    pump_until(lambda: visible_names(selector) == ALPHABETICAL, 10,
               f"opening the list must show every page, it shows "
               f"{visible_names(selector)}")

    selector.page_button.popdown()
    pump_until(lambda: not selector.popover.get_visible(), 10,
               "the page list never closed")
    print("PASS: opening the list clears the last query and shows every page")


def main() -> int:
    fixtures.start_watchdog(180, label="scenario_page_selector_search")

    check_locale_keys()
    check_focus_rearm_stops()

    page_dir = os.path.join(gl.DATA_PATH, "page-selector-search")
    os.makedirs(page_dir, exist_ok=True)
    page_manager = FakePageManager(page_dir, PAGE_NAMES)

    gl.lm = LocaleManager(csv_path=LOCALES_CSV)
    gl.lm.set_language("en_US")
    gl.lm.set_fallback_language("en_US")

    from src.Signals.SignalManager import SignalManager
    gl.signal_manager = SignalManager()
    gl.page_manager = page_manager

    check_filter_and_sort(StubSelector(page_manager), "the hooks over stubs")

    if not has_display():
        print("SKIP(real-widget): no display; the hooks ran against stubs and "
              "the wiring legs need a rendered list")
        print("ALL PASS: scenario_page_selector_search")
        return 0

    Adw.init()

    from src.windows.mainWindow.elements.PageSelector import PageSelector

    controller = FakeController()
    main_window = types.SimpleNamespace(
        leftArea=types.SimpleNamespace(deck_stack=FakeDeckStack(controller)))
    selector = PageSelector(main_window, page_manager)

    # A real window, because the list opens as a popover and a popover only
    # pops up over a mapped widget.
    window = Gtk.Window()
    window.set_child(selector)
    window.present()
    pump_until(selector.get_mapped, 10, "the page selector never mapped")

    assert visible_names(selector) == ALPHABETICAL, (
        f"the list must start on every page in name order, it shows "
        f"{visible_names(selector)}")
    placeholder = selector.search_entry.get_property("placeholder-text")
    assert placeholder != "header-page-selector-search-hint", (
        "the search entry shows the raw locale key as its placeholder")

    check_filter_and_sort(RealSelector(selector), "the rendered list")
    check_reopen_clears_query(selector)
    check_selection_loads_page(selector, controller, page_manager)
    check_signal_refresh(selector, controller, page_manager)

    window.destroy()
    print("ALL PASS: scenario_page_selector_search")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
