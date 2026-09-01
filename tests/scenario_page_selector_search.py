"""Verify page-selector search, ranking, keyboard access, and synchronization."""
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
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk

import globals as gl

from locales.LocaleManager import LocaleManager
from src.backend.DeckManagement.HelperMethods import natural_sort


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOCALES_CSV = os.path.join(REPO_ROOT, "locales", "locales.csv")
MODULE_PATH = os.path.join(REPO_ROOT, "src", "windows", "mainWindow",
                           "elements", "PageSelector.py")

# Corpus with prefix queries whose whole-string scores are at or below the
# fuzzy threshold: vol 42.9, hom 22.2, and work 50.0.
PAGE_NAMES = ["Home Assistant Dashboard", "brightness", "gaming",
              "volume_down", "volume_mute", "volume_up", "work profile"]

# The backend hands the paths over in natural order by file name.
ALPHABETICAL = ["brightness", "gaming", "Home Assistant Dashboard",
                "volume_down", "volume_mute", "volume_up", "work profile"]

# Expected visible names in rank order, expressed as behavior rather than
# floating-point score values.
QUERIES = [
    # An empty query keeps every page, in name order.
    ("", ALPHABETICAL),
    # Prefix matches stay in one tier, with the ratio ranking volume_up first.
    ("vol", ["volume_up", "volume_down", "volume_mute"]),
    ("hom", ["Home Assistant Dashboard"]),
    ("work", ["work profile"]),
    ("gam", ["gaming"]),
    ("volume", ["volume_up", "volume_down", "volume_mute"]),
    # A run inside a name, not at its start.
    ("dash", ["Home Assistant Dashboard"]),
    ("profile", ["work profile"]),
    ("assistant", ["Home Assistant Dashboard"]),
    # A whole name: the exact page first, then the near names on the fuzzy
    # tier, so the ladder does not collapse to containment alone.
    ("volume_up", ["volume_up", "volume_mute", "volume_down"]),
    # Nothing matches, so the list empties and says so.
    ("zzzz", []),
]
QUERY_RESULTS = dict(QUERIES)

# Tier-order corpus: containing names must beat a higher-scoring resemblance.
# For "log", scores are logbook 60.0, catalog 26.1, and lob 66.7.
TIER_CORPUS = ["logbook", "catalog viewer panel", "lob"]
TIER_QUERY = "log"
TIER_EXPECTED = ["logbook", "catalog viewer panel", "lob"]


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
    """The deck stack, with a switch for the no-visible-deck state."""

    def __init__(self, controller):
        self.child = types.SimpleNamespace(deck_controller=controller)
        self.visible = True

    def get_visible_child(self):
        return self.child if self.visible else None


class FakePageManager:
    """Provide selector backend calls with natural filename ordering."""

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
    """Run the production filter and sort over plain objects without GTK."""

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
        return [name_of(row) for row in kept]


# Helpers


def name_of(row) -> str:
    return os.path.splitext(os.path.basename(row.page_path))[0]


def pump_until(condition, timeout: float, what: str) -> None:
    """Pump the default main context for the search timer, Signals idle, and
    popover scroll source until the condition holds."""
    context = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        while context.iteration(False):
            pass
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout}s: {what}")


def pump(seconds: float = 0.2) -> None:
    """Service the main context for a while, with nothing to wait for."""
    context = GLib.MainContext.default()
    deadline = time.time() + seconds
    while time.time() < deadline:
        while context.iteration(False):
            pass
        time.sleep(0.005)


def has_display() -> bool:
    """Require a default GDK display because Gtk.init_check can pass without one."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


def visible_names(selector):
    return [name_of(row) for row in selector.visible_rows()]


def row_named(selector, name: str):
    for row in selector.page_rows:
        if name_of(row) == name:
            return row
    raise AssertionError(f"no row for page {name!r}: {visible_names(selector)}")


def focus_is_inside(widget) -> bool:
    """Check focus on a widget or descendant, including SearchEntry's inner text."""
    root = widget.get_root()
    if root is None:
        return False
    focused = root.get_focus()
    while focused is not None:
        if focused is widget:
            return True
        focused = focused.get_parent()
    return False


def press(selector, keyval: int) -> bool:
    """Send one key press to the search entry's controller."""
    return selector.search_key_controller.emit(
        "key-pressed", keyval, 0, Gdk.ModifierType(0))


def open_list(selector) -> None:
    selector.page_button.popup()
    pump_until(lambda: selector.popover.get_visible(), 10,
               "the page list never opened")
    pump(0.2)


def close_list(selector) -> None:
    selector.page_button.popdown()
    pump_until(lambda: not selector.popover.get_visible(), 10,
               "the page list never closed")


def set_query(selector, search: str) -> None:
    """Type into the search entry and wait for the list to settle."""
    selector.search_entry.set_text(search)
    expected = QUERY_RESULTS.get(search)
    if expected is None:
        pump(0.3)
        return
    pump_until(lambda: visible_names(selector) == expected, 10,
               f"the list never settled on {expected} for the query "
               f"{search!r}, it shows {visible_names(selector)}")


# 1. Every locale key the module asks for must exist in the CSV.

def check_locale_keys() -> None:
    """Require every selector locale key and value to prevent raw-key fallback."""
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

    for required in ("header-page-selector-search-hint",
                     "header-page-selector-empty-hint"):
        assert required in keys, f"the module must ask for {required!r}"
    print(f"PASS: all {len(keys)} locale keys the page selector asks for have "
          f"a row in every one of the {len(locales)} locales")


# 2. The ladder narrows and ranks.

def check_match_ladder(selector, label: str) -> None:
    for search, expected in QUERIES:
        selector.query(search)
        got = selector.visible_names()
        assert got == expected, (
            f"{label}: the query {search!r} must leave {expected}, closest "
            f"first, got {got}")
    print(f"PASS: {label} keeps every page whose name holds the query and "
          f"ranks the closest first, over {len(QUERIES)} queries")


def check_containment_ranks_above_fuzzy_match(page_manager_factory) -> None:
    """Rank names containing the query above names that only resemble it."""
    stub = StubSelector(page_manager_factory(TIER_CORPUS))
    stub.query(TIER_QUERY)
    got = stub.visible_names()
    assert got == TIER_EXPECTED, (
        f"the query {TIER_QUERY!r} must rank the names that hold it above "
        f"the one that only scores well: {TIER_EXPECTED}, got {got}")
    print("PASS: a name holding the query outranks a closer-scoring name "
          "that does not hold it")


class RealSelector:
    """The widget behind the same query()/visible_names() pair as the stub."""

    def __init__(self, selector):
        self.selector = selector

    def query(self, search: str) -> None:
        set_query(self.selector, search)

    def visible_names(self):
        return visible_names(self.selector)


# 3. The keyboard must reach a row and open it.

def check_search_entry_focus(selector) -> None:
    """Put keyboard focus in the search entry whenever the list opens."""
    open_list(selector)
    pump_until(lambda: focus_is_inside(selector.search_entry), 10,
               "the search entry never took the keyboard focus when the list "
               "opened")
    print("PASS: opening the list puts the keyboard focus in the search entry")


def check_tab_row_focus(selector) -> None:
    """Move focus from the entry to a row without the scroller intercepting it."""
    assert not selector.scrolled_window.get_focusable(), (
        "the scrolled window is focusable, so it swallows the focus that "
        "leaves the search entry and no row can be reached")

    set_query(selector, "")
    selector.search_entry.grab_focus()
    pump(0.2)
    moved = selector.popover.child_focus(Gtk.DirectionType.TAB_FORWARD)
    pump(0.2)
    assert moved, "focus did not move out of the search entry at all"
    first = selector.visible_rows()[0]
    assert focus_is_inside(first), (
        f"focus left the search entry but did not land on the first row "
        f"{name_of(first)!r}")
    print("PASS: focus leaving the search entry lands on the first row")


def check_reopen_focus(selector) -> None:
    """Return focus from a selected row to the search entry when reopening."""
    first = selector.visible_rows()[0]
    first.grab_focus()
    pump_until(lambda: focus_is_inside(first), 10,
               "the row never took the keyboard focus")

    close_list(selector)
    open_list(selector)
    pump_until(lambda: focus_is_inside(selector.search_entry), 10,
               "reopening the list left the focus where it was, not in the "
               "search entry, so what the user types goes nowhere")
    print("PASS: reopening the list puts the focus back in the search entry")


def check_arrow_selection(selector) -> None:
    """Move list selection with arrows while the search entry retains focus."""
    set_query(selector, "")
    rows = selector.visible_rows()
    assert len(rows) >= 3, f"this check needs at least three rows, got {len(rows)}"

    # The controller has to be on the entry, or the keys never reach it and
    # driving it here would prove nothing.
    installed = selector.search_entry.observe_controllers()
    controllers = [installed.get_item(index) for index in range(installed.get_n_items())]
    assert selector.search_key_controller in controllers, (
        "the key controller is not installed on the search entry, so no key "
        "press ever reaches it")

    selector.search_entry.grab_focus()
    pump_until(lambda: focus_is_inside(selector.search_entry), 10,
               "the search entry never took the focus back")

    selector.list_box.select_row(None)
    assert press(selector, Gdk.KEY_Down) is True, "Down must be handled"
    assert selector.list_box.get_selected_row() is rows[0], (
        f"Down with nothing selected must enter the list at the top, it "
        f"selected {selector.list_box.get_selected_row()}")

    press(selector, Gdk.KEY_Down)
    assert selector.list_box.get_selected_row() is rows[1], (
        "a second Down must step to the next row")

    press(selector, Gdk.KEY_Up)
    assert selector.list_box.get_selected_row() is rows[0], (
        "Up must step back to the row above")

    press(selector, Gdk.KEY_Up)
    assert selector.list_box.get_selected_row() is rows[0], (
        "Up at the top must stay on the top row, not wrap or unselect")

    selector.list_box.select_row(None)
    press(selector, Gdk.KEY_Up)
    assert selector.list_box.get_selected_row() is rows[-1], (
        "Up with nothing selected must enter the list at the bottom")

    assert focus_is_inside(selector.search_entry), (
        "the arrow keys must leave the typing focus in the search entry")

    # The entry keeps every other key, or the user cannot type.
    assert press(selector, Gdk.KEY_a) is False, (
        "the controller must pass an ordinary key through to the entry")

    # An arrow over an empty list must not raise, and must not reach for a
    # row that is not there.
    set_query(selector, "zzzz")
    selector.list_box.select_row(None)
    assert press(selector, Gdk.KEY_Down) is True
    assert selector.list_box.get_selected_row() is None, (
        "Down over an empty list must select nothing")
    set_query(selector, "")
    print("PASS: Up and Down walk the selection, clamp at both ends and leave "
          "ordinary keys to the entry")


def check_enter_match_activation(selector, controller, page_manager) -> None:
    """Enter after typing must open the best match with no further keys."""
    open_list(selector)
    set_query(selector, "vol")
    selector.list_box.select_row(None)

    before = len(controller.loaded)
    selector.search_entry.emit("activate")
    pump(0.2)

    assert len(controller.loaded) == before + 1, (
        f"Enter must open the top match, the controller loaded "
        f"{len(controller.loaded) - before} pages")
    assert controller.loaded[-1].json_path == page_manager.path_of("volume_up"), (
        f"Enter opened {controller.loaded[-1].json_path!r}, not the top match")

    # With a row picked out by the arrows, Enter must take that row instead.
    open_list(selector)
    set_query(selector, "vol")
    press(selector, Gdk.KEY_Down)
    press(selector, Gdk.KEY_Down)
    picked = selector.list_box.get_selected_row()
    selector.search_entry.emit("activate")
    pump(0.2)
    assert controller.loaded[-1].json_path == picked.page_path, (
        f"Enter must open the row the arrows picked, {name_of(picked)!r}, it "
        f"opened {controller.loaded[-1].json_path!r}")

    # Enter over an empty list must do nothing at all.
    open_list(selector)
    set_query(selector, "zzzz")
    before = len(controller.loaded)
    selector.search_entry.emit("activate")
    pump(0.2)
    assert len(controller.loaded) == before, (
        "Enter with nothing in the list must load no page")
    close_list(selector)
    print("PASS: Enter opens the top match, or the row the arrows picked, and "
          "does nothing over an empty list")


# 4. The empty state must say something.

def check_empty_state(selector, page_manager) -> None:
    """A blank popover tells the user nothing about why it is blank."""
    open_list(selector)
    set_query(selector, "zzzz")
    pump_until(selector.list_placeholder.get_mapped, 10,
               "a query that matches nothing left the list blank with no "
               "message")
    assert selector.list_placeholder.get_label() != "header-page-selector-empty-hint", (
        "the empty-list message shows the raw locale key")

    set_query(selector, "")
    pump_until(lambda: not selector.list_placeholder.get_mapped(), 10,
               "the message stayed up after the query was cleared")

    # A install with no pages at all reaches the same message.
    saved = list(page_manager.names)
    page_manager.names.clear()
    selector.update()
    pump_until(selector.list_placeholder.get_mapped, 10,
               "an install with no pages left the list blank with no message")
    assert selector.visible_rows() == [], "no page must be listed"

    page_manager.names[:] = saved
    selector.update()
    pump_until(lambda: visible_names(selector) == ALPHABETICAL, 10,
               "the pages never came back")
    close_list(selector)
    print("PASS: a query that matches nothing and an install with no pages "
          "both show the empty-list message")


# 5. A pick reaches the deck.

def check_selection_loads_page(selector, controller, page_manager) -> None:
    """A pick must reach the deck through the wiring that was there before."""
    open_list(selector)
    set_query(selector, "")

    row = row_named(selector, "gaming")
    before = len(controller.loaded)
    selector.list_box.emit("row-activated", row)
    pump(0.2)

    assert len(controller.loaded) == before + 1, (
        f"activating a row must load exactly one page, the controller loaded "
        f"{len(controller.loaded) - before}")
    assert controller.loaded[-1].json_path == page_manager.path_of("gaming"), (
        f"the wrong page was loaded: {controller.loaded[-1].json_path!r}")

    # The deck already holds this page now, so a second pick of the same row
    # must not start a second load of it.
    selector.list_box.emit("row-activated", row)
    pump(0.2)
    assert len(controller.loaded) == before + 1, (
        f"picking the page the deck already holds must not load it again, "
        f"the controller loaded {len(controller.loaded) - before}")
    print("PASS: a pick loads the page through the deck controller and "
          "refuses a repeat of the active page")


# 6. The list follows the backend, and the header follows the deck.

def check_signal_refresh(selector, controller, page_manager) -> None:
    """Add, rename and delete must all reach the list through Signals."""
    from src.Signals import Signals

    page_manager.names.append("alpha")
    gl.signal_manager.trigger_signal(Signals.PageAdd)
    pump_until(lambda: "alpha" in visible_names(selector), 10,
               f"an added page never reached the list: {visible_names(selector)}")
    assert visible_names(selector) == ["alpha"] + ALPHABETICAL, (
        f"the added page must land in name order, got "
        f"{visible_names(selector)}")

    page_manager.names[page_manager.names.index("alpha")] = "omega"
    gl.signal_manager.trigger_signal(Signals.PageRename)
    pump_until(lambda: "omega" in visible_names(selector), 10,
               f"a renamed page never reached the list: {visible_names(selector)}")
    assert "alpha" not in visible_names(selector), (
        f"the old name must go with the rename, got {visible_names(selector)}")

    page_manager.names.remove("omega")
    gl.signal_manager.trigger_signal(Signals.PageDelete)
    pump_until(lambda: "omega" not in visible_names(selector), 10,
               f"a deleted page stayed in the list: {visible_names(selector)}")
    assert visible_names(selector) == ALPHABETICAL, (
        f"the list must be back to the whole corpus, got "
        f"{visible_names(selector)}")

    target = page_manager.path_of("work profile")
    controller.active_page = FakePage(target)
    gl.signal_manager.trigger_signal(Signals.ChangePage)
    pump_until(lambda: selector.selected_page_path == target, 10,
               f"the deck's page change never reached the selector, it holds "
               f"{selector.selected_page_path!r}")
    assert selector.page_label.get_label() == "work profile", (
        f"the header must name the page the deck holds, it shows "
        f"{selector.page_label.get_label()!r}")
    assert selector.list_box.get_selected_row() is row_named(selector, "work profile"), (
        "the list must mark the page the deck holds")
    print("PASS: the list follows a page add, rename and delete, and the "
          "header follows the deck's own page change")


def check_deleted_page_selection_cleanup(selector, controller,
                                            page_manager) -> None:
    """Clear the header and settings target when the selected page disappears."""
    from src.Signals import Signals

    target = page_manager.path_of("gaming")
    controller.active_page = FakePage(target)
    gl.signal_manager.trigger_signal(Signals.ChangePage)
    pump_until(lambda: selector.selected_page_path == target, 10,
               "the header never took the page the deck holds")

    page_manager.names.remove("gaming")
    gl.signal_manager.trigger_signal(Signals.PageDelete)
    pump_until(lambda: "gaming" not in visible_names(selector), 10,
               "the deleted page stayed in the list")

    assert selector.selected_page_path is None, (
        f"the selector still holds the deleted page "
        f"{selector.selected_page_path!r}")
    assert selector.page_label.get_label() == "", (
        f"the header still names the deleted page: "
        f"{selector.page_label.get_label()!r}")
    assert selector.list_box.get_selected_row() is None, (
        "the list still marks a row for the deleted page")

    # Keep rows standing while switching to an unlisted page, so only explicit
    # selection cleanup can clear the mark.
    page_manager.names.append("gaming")
    gl.signal_manager.trigger_signal(Signals.PageAdd)
    pump_until(lambda: visible_names(selector) == ALPHABETICAL, 10,
               "the corpus never came back")
    controller.active_page = FakePage(page_manager.path_of("gaming"))
    gl.signal_manager.trigger_signal(Signals.ChangePage)
    pump_until(lambda: selector.list_box.get_selected_row() is not None, 10,
               "the list never marked the page the deck holds")

    rows_before = len(selector.page_rows)
    controller.active_page = FakePage("/nowhere/a-page-this-list-has-not.json")
    gl.signal_manager.trigger_signal(Signals.ChangePage)
    pump_until(lambda: selector.selected_page_path is None, 10,
               f"a page the list does not carry left the header naming "
               f"{selector.selected_page_path!r}")
    assert len(selector.page_rows) == rows_before, (
        "this check needs the rows left standing, or a rebuild clears the "
        "mark on its own and proves nothing")
    assert selector.list_box.get_selected_row() is None, (
        "the list still marks the page it carried before, though the deck "
        "moved to one it does not carry")
    assert selector.page_label.get_label() == "", (
        f"the header still names a page the list does not carry: "
        f"{selector.page_label.get_label()!r}")

    page_manager.names.remove("gaming")
    gl.signal_manager.trigger_signal(Signals.PageDelete)
    pump_until(lambda: "gaming" not in visible_names(selector), 10,
               "the corpus never went back to the deleted state")

    # The page-settings button must not carry the dead path onward, and a
    # path the backend stopped listing must be refused even if it is held.
    activated = []
    saved_window = gl.page_manager_window
    gl.page_manager_window = types.SimpleNamespace(
        present=lambda: None,
        page_selector=types.SimpleNamespace(
            activate_page=lambda path: activated.append(path)))
    try:
        selector.on_click_open_page_settings(selector.open_settings_button)
        assert activated == [], (
            f"the settings button opened the manager on {activated!r}, which "
            f"names no page the backend lists")

        selector.selected_page_path = target
        selector.on_click_open_page_settings(selector.open_settings_button)
        assert activated == [], (
            f"a selection the backend no longer lists must be refused, the "
            f"settings button passed on {activated!r}")

        selector.selected_page_path = page_manager.path_of("brightness")
        selector.on_click_open_page_settings(selector.open_settings_button)
        assert activated == [page_manager.path_of("brightness")], (
            f"a listed page must still reach the manager, it got {activated!r}")
    finally:
        gl.page_manager_window = saved_window

    page_manager.names.append("gaming")
    controller.active_page = None
    gl.signal_manager.trigger_signal(Signals.PageAdd)
    pump_until(lambda: visible_names(selector) == ALPHABETICAL, 10,
               "the corpus never came back")
    print("PASS: deleting the page the deck holds blanks the header, and the "
          "settings button refuses a page the backend no longer lists")


def check_no_deck_button_state(selector, deck_stack) -> None:
    """With no deck on screen there is no page to switch, so the button goes."""
    deck_stack.visible = False
    selector.update_selected()
    assert not selector.page_button.get_sensitive(), (
        "with no visible deck the page button must be insensitive")

    deck_stack.visible = True
    selector.update_selected()
    assert selector.page_button.get_sensitive(), (
        "the page button must come back when a deck is on screen again")
    print("PASS: the page button follows whether a deck is on screen")


# 7. The list must open on the page the deck holds.

def check_selected_page_scroll(selector, controller, page_manager) -> None:
    """Open a long list with the deck's selected page inside the viewport."""
    from src.Signals import Signals

    page_manager.names[:] = [f"page_{index:02d}" for index in range(40)]
    gl.signal_manager.trigger_signal(Signals.PageAdd)
    pump_until(lambda: len(selector.page_rows) == 40, 10,
               "the long corpus never reached the list")

    target = page_manager.path_of("page_37")
    controller.active_page = FakePage(target)
    gl.signal_manager.trigger_signal(Signals.ChangePage)
    pump_until(lambda: selector.selected_page_path == target, 10,
               "the header never took the page the deck holds")

    close_list(selector)
    open_list(selector)

    adjustment = selector.scrolled_window.get_vadjustment()
    pump_until(lambda: adjustment.get_upper() > adjustment.get_page_size(), 10,
               "the list never grew past its view, so nothing can scroll")
    pump_until(lambda: adjustment.get_value() > 0, 10,
               f"the list opened at the top with the page the deck holds far "
               f"below it (value {adjustment.get_value()}, upper "
               f"{adjustment.get_upper()})")

    row = row_named(selector, "page_37")
    found, point = row.compute_point(selector.list_box, Graphene.Point().init(0, 0))
    assert found, "the row has no place in the list, so nothing can be checked"
    row_top = point.y
    view_top = adjustment.get_value()
    view_bottom = view_top + adjustment.get_page_size()
    assert view_top <= row_top <= view_bottom, (
        f"the page the deck holds sits at {row_top} and the view covers "
        f"{view_top} to {view_bottom}, so it opened off screen")
    close_list(selector)
    print("PASS: a long list opens scrolled to the page the deck holds")


def check_reopen_query_reset(selector) -> None:
    """Clear a prior query when reopening so the full page list is visible."""
    open_list(selector)
    set_query(selector, "gam")
    close_list(selector)
    open_list(selector)

    assert selector.search_entry.get_text() == "", (
        f"opening the list must clear the last query, it still holds "
        f"{selector.search_entry.get_text()!r}")
    pump_until(lambda: visible_names(selector) == ALPHABETICAL, 10,
               f"opening the list must show every page, it shows "
               f"{visible_names(selector)}")
    print("PASS: opening the list clears the last query and shows every page")


def check_no_backend_yet(main_window):
    """Build without a backend and return the selector to retain weak observers."""
    from src.windows.mainWindow.elements.PageSelector import PageSelector

    selector = PageSelector(main_window, None)
    assert selector.page_rows == [], (
        "a selector built before the page backend exists must list nothing")
    assert selector.selected_page_path is None, (
        "a selector with no backend must hold no page")
    print("PASS: the header builds with no page backend and lists nothing")
    return selector


def main() -> int:
    fixtures.start_watchdog(60, label="scenario_page_selector_search")

    check_locale_keys()

    page_dir = os.path.join(gl.DATA_PATH, "page-selector-search")
    os.makedirs(page_dir, exist_ok=True)
    page_manager = FakePageManager(page_dir, PAGE_NAMES)

    gl.lm = LocaleManager(csv_path=LOCALES_CSV)
    gl.lm.set_language("en_US")
    gl.lm.set_fallback_language("en_US")

    from src.Signals.SignalManager import SignalManager
    gl.signal_manager = SignalManager()
    gl.page_manager = page_manager

    check_match_ladder(StubSelector(page_manager), "the ladder over stubs")
    check_containment_ranks_above_fuzzy_match(
        lambda names: FakePageManager(page_dir, names))

    if not has_display():
        print("SKIP(real-widget): no display; the ladder ran against stubs "
              "and the rest need a rendered list")
        print("ALL PASS: scenario_page_selector_search")
        return 0

    Adw.init()

    from src.windows.mainWindow.elements.PageSelector import PageSelector

    controller = FakeController()
    deck_stack = FakeDeckStack(controller)
    main_window = types.SimpleNamespace(
        leftArea=types.SimpleNamespace(deck_stack=deck_stack))
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

    check_search_entry_focus(selector)
    check_match_ladder(RealSelector(selector), "the rendered list")
    check_tab_row_focus(selector)
    check_reopen_focus(selector)
    check_reopen_query_reset(selector)
    check_arrow_selection(selector)
    check_empty_state(selector, page_manager)
    check_enter_match_activation(selector, controller, page_manager)
    check_selection_loads_page(selector, controller, page_manager)
    check_signal_refresh(selector, controller, page_manager)
    check_deleted_page_selection_cleanup(selector, controller, page_manager)
    check_no_deck_button_state(selector, deck_stack)
    check_selected_page_scroll(selector, controller, page_manager)
    cold_start_selector = check_no_backend_yet(main_window)
    assert cold_start_selector is not None

    window.destroy()
    print("ALL PASS: scenario_page_selector_search")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
