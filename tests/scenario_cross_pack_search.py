"""Pins the search that reaches across every pack of one asset type.

A query typed into the pack grid gathers the assets of all of its packs on a
worker thread, ranks the merged names with one ranker, and shows the matches
in the leaf page. The leaf page then owns the query: a changed one gathers
again, an emptied one goes back to the pack grid.

The legs drive the real classes over stand-ins for the widgets, so most of the
file is logic and threads. No GTK widget is built, and no display is needed,
except in the one leg that binds a real card: it says SKIP without a display,
and the structural leg beside it covers the same wiring anywhere.

The staleness aborts each have an observable, because a guard that only makes
a later assertion redundant is a guard a mutant can delete unnoticed. The
packs count the scans they take, and the render callback counts the times it
is reached.
"""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import ast
import functools
import inspect
import os
import textwrap
import threading
import time
import types

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk

import globals as gl

gl.lm = types.SimpleNamespace(get=lambda key, *a, **k: key)

from src.windows.AssetManager import asset_search
from src.windows.AssetManager.GenericAssetChooser import (
    PACK_CHOOSER_CHILD_NAME,
    GenericAssetChooserPage,
    GenericAssetPreview,
    GenericPackChooserPage,
    asset_display_name,
)
from src.windows.AssetManager.IconPacks.Icons.IconChooser import IconChooserPage
from src.windows.AssetManager.IconPacks.PackChooser import IconPackChooser
from src.windows.AssetManager.SDPlusBarWallpaperPacks.PackChooser import SDPlusBarWallpaperPackChooser
from src.windows.AssetManager.SDPlusBarWallpaperPacks.SDPlusBarWallpaper.SDPlusBarWallpaperChooser import (
    SDPlusBarWallpaperChooserPage,
)
from src.windows.AssetManager.WallpaperPacks.PackChooser import WallpaperPackChooser
from src.windows.AssetManager.WallpaperPacks.Wallpapers.WallpaperChooser import WallpaperChooserPage


LEAF_CHILD_NAME = IconPackChooser.LEAF_CHILD_NAME
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REAL_IMAGE = os.path.join(REPO_ROOT, "Assets", "Onboarding", "icon.png")


# The stand-ins. Each one carries what the pages read off a widget and
# records what they did to it.

class FakeEntry:
    """A search entry that holds text and records the focus it was given.

    Gtk.SearchEntry delays its search-changed emission, and the legs below
    drive on_search_changed themselves where an emission matters, so this
    stand-in emits nothing of its own.
    """

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.focused = 0
        self.position = None

    def get_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text

    def grab_focus(self) -> None:
        self.focused += 1

    def set_position(self, position: int) -> None:
        self.position = position


class FakeLabel:
    def __init__(self) -> None:
        self.visible = False

    def set_visible(self, visible: bool) -> None:
        self.visible = visible

    def get_visible(self) -> bool:
        return self.visible


class FakeAssetFlow:
    """The recycler grid, reduced to the list it holds and the range shown."""

    N_ITEMS_PER_PAGE = 50

    def __init__(self) -> None:
        self.items = None
        self.ranges: list[tuple[int, int]] = []

    def set_item_list(self, items) -> None:
        self.items = items

    def show_range(self, start: int, end: int) -> None:
        self.ranges.append((start, end))


class FakePackFlow:
    def __init__(self) -> None:
        self.invalidations = 0
        self.flow_box = types.SimpleNamespace(
            invalidate_filter=self._invalidate)

    def _invalidate(self) -> None:
        self.invalidations += 1


class FakeStack:
    """The two-page stack, reduced to the child that shows."""

    def __init__(self, child: str = PACK_CHOOSER_CHILD_NAME) -> None:
        self.child = child
        self.switches: list[str] = []

    def get_visible_child_name(self) -> str:
        return self.child

    def set_visible_child_name(self, name: str) -> None:
        self.child = name
        self.switches.append(name)


class FakeAssetManager:
    def __init__(self) -> None:
        self.back_visible = None
        self.delivered: list[str] = []
        self.back_button = types.SimpleNamespace(set_visible=self._set_back)

    def _set_back(self, visible: bool) -> None:
        self.back_visible = visible

    def deliver_selection(self, path: str) -> None:
        self.delivered.append(path)


class FakePack:
    """One icon pack. get_icons is what the leaf page asks it for.

    It counts the scans it takes, which is what shows whether a gather left
    off where a guard says it should have.
    """

    def __init__(self, name: str, names: list[str], gate: "threading.Event | None" = None,
                 release: "threading.Event | None" = None) -> None:
        self.name = name
        self.gate = gate
        self.release = release
        self.scans = 0
        self.icons = [FakeIcon(self, f"/packs/{name}/{leaf}.png") for leaf in names]

    def get_icons(self) -> "list[FakeIcon]":
        self.scans += 1
        if self.gate is not None:
            self.gate.set()
        if self.release is not None:
            # Held open so a leg can invalidate the pass while the scan runs.
            assert self.release.wait(10), "the leg never released the pack scan"
        return list(self.icons)


class FakeIcon:
    def __init__(self, pack: FakePack, path: str) -> None:
        self.pack = pack
        self.path = path
        self.name = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


class FakeManager:
    def __init__(self, packs: list[FakePack]) -> None:
        self.packs = packs
        self.discoveries = 0

    def get_icon_packs(self) -> dict[str, FakePack]:
        self.discoveries += 1
        return {pack.name: pack for pack in self.packs}


# The corpus. The same name appears in more than one pack, and each pack
# holds a name the others do not, so a merge that drops or duplicates a pack
# shows up in the result.
MATERIAL = FakePack("Material Icons",
                    ["volume_up", "volume_down", "brightness", "settings"])
TABLER = FakePack("Tabler Icons",
                  ["volume_up", "volume-off", "brightness-half", "wi-fi"])
SIMPLE = FakePack("simple-icons",
                  ["volume", "apple", "Zebra", "high_volume_alert"])
ALL_PACKS = [MATERIAL, TABLER, SIMPLE]


def install_packs(packs: list[FakePack]) -> FakeManager:
    manager = FakeManager(packs)
    gl.icon_pack_manager = manager
    return manager


def make_pack_page(entry_text: str = "", stack: "FakeStack | None" = None,
                   asset_manager: "FakeAssetManager | None" = None,
                   leaf: "IconChooserPage | None" = None) -> IconPackChooser:
    page = IconPackChooser.__new__(IconPackChooser)
    page.search_entry = FakeEntry(entry_text)
    page.pack_flow = FakePackFlow()
    page.stack = stack if stack is not None else FakeStack()
    page.stack.leaf_chooser = leaf
    page.asset_manager = asset_manager if asset_manager is not None else FakeAssetManager()
    # Nothing is set up for the one-worker handoff here on purpose: that state
    # carries class-level defaults, because the base connects the search entry
    # before a subclass has run a line of its own.
    return page


def make_leaf_page(stack: "FakeStack | None" = None,
                   asset_manager: "FakeAssetManager | None" = None) -> IconChooserPage:
    page = IconChooserPage.__new__(IconChooserPage)
    page.search_entry = FakeEntry("")
    page.asset_flow = FakeAssetFlow()
    page.empty_label = FakeLabel()
    page.stack = stack if stack is not None else FakeStack()
    page.asset_manager = asset_manager if asset_manager is not None else FakeAssetManager()
    page.selected_path = None
    page._pending_pack = None
    page._pending_results = None
    page._pack_search_source = None
    page._pack_search_query = ""
    return page


def make_pair(entry_text: str = ""):
    """A pack grid and its leaf page, on one stack and one window."""
    stack = FakeStack()
    asset_manager = FakeAssetManager()
    leaf = make_leaf_page(stack, asset_manager)
    pack_page = make_pack_page(entry_text, stack, asset_manager, leaf)
    stack.leaf_chooser = leaf
    return pack_page, leaf, stack, asset_manager


def record_gathers(page: IconPackChooser) -> list[str]:
    """Record the queries a page actually gathers for.

    The worker reads the method off the instance, so this stands in front of
    the real one.
    """
    gathered: list[str] = []
    real = page.collect_matching_assets

    def recording(query, requester, generation):
        gathered.append(query)
        return real(query, requester, generation)

    page.collect_matching_assets = recording
    return gathered


def record_renders(page: IconPackChooser) -> list[str]:
    """Record the queries the main-loop render callback is reached for."""
    reached: list[str] = []
    real = page._show_matching_assets

    def recording(query, requester, generation, assets):
        reached.append(query)
        return real(query, requester, generation, assets)

    page._show_matching_assets = recording
    return reached


def pump(seconds: float = 0.2) -> None:
    context = GLib.MainContext.default()
    deadline = time.time() + seconds
    while time.time() < deadline:
        while context.iteration(False):
            pass
        time.sleep(0.005)


def pump_until(condition, timeout: float, what: str) -> None:
    context = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        while context.iteration(False):
            pass
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout}s: {what}")


def names_of(assets) -> list[str]:
    return [asset_display_name(asset) for asset in assets]


def expected_order(query: str, packs: list[FakePack]) -> list[str]:
    """The answer, worked out here rather than taken from the page."""
    ranker = asset_search.QueryRanker(query)
    matched = [asset for pack in packs for asset in pack.icons
               if ranker.matches(asset_display_name(asset))]
    matched.sort(key=lambda asset: ranker.rank_key(asset_display_name(asset)))
    return names_of(matched)


# The legs

def test_aggregation_merges_and_ranks() -> None:
    """One ranker over every pack's names, best match first."""
    install_packs(ALL_PACKS)
    page = make_pack_page()
    page.stack.leaf_chooser = make_leaf_page()

    got = names_of(page.collect_matching_assets("volume", page,
                                                page._search_generation))
    expected = expected_order("volume", ALL_PACKS)
    assert got == expected, f"merged order {got} != {expected}"

    # Every pack contributed, so the merge reads all of them and not the
    # first one it finds.
    assert set(got) >= {"volume_up", "volume-off", "volume"}, got
    # A pack writes the same name as another, and both come through: the
    # result is the assets and not a set of names.
    assert got.count("volume_up") == 2, f"one pack's volume_up went missing: {got}"
    # Nothing below the threshold is in there.
    assert "brightness" not in got and "apple" not in got, got

    # The ranking key is total, so the order the packs come in does not
    # reach the answer.
    install_packs(list(reversed(ALL_PACKS)))
    reversed_page = make_pack_page()
    reversed_page.stack.leaf_chooser = make_leaf_page()
    reversed_got = names_of(reversed_page.collect_matching_assets(
        "volume", reversed_page, reversed_page._search_generation))
    assert reversed_got == got, (
        f"the pack order reached the result: {reversed_got} != {got}")
    print(f"PASS: a search across the packs merges and ranks {len(got)} assets "
          f"from {len(ALL_PACKS)} packs")


def test_a_query_that_matches_nothing() -> None:
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair("tshirt")
    pack_page.apply_search("tshirt")
    pump_until(lambda: leaf.asset_flow.items is not None, 10,
               "the search never reached the leaf page")

    assert leaf.asset_flow.items == []
    assert leaf.empty_label.get_visible() is True, (
        "a search that answers nothing shows an empty grid and no word about "
        "it, which reads as a page that has not loaded")

    # The notice goes as soon as the page holds something again.
    leaf.load_for_pack(MATERIAL)
    assert leaf.empty_label.get_visible() is False, (
        "the notice outlived the search it belonged to")
    print("PASS: a query no pack answers says so on the results page")


def test_pack_grid_drills_into_the_results() -> None:
    """A non-empty query on the pack grid opens the results page."""
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair("volume")

    pack_page.apply_search("volume")
    # The pack cards narrow too, so the grid the user watches while the
    # search runs answers the query as far as it can.
    assert pack_page.pack_flow.invalidations == 1

    pump_until(lambda: leaf.asset_flow.items is not None, 10,
               "the search never reached the leaf page")

    assert stack.child == LEAF_CHILD_NAME, (
        f"the window stayed on {stack.child!r} instead of the results")
    assert asset_manager.back_visible is True, "no way back to the pack grid"
    assert names_of(leaf.asset_flow.items) == expected_order("volume", ALL_PACKS)
    assert leaf.asset_flow.ranges[-1] == (0, FakeAssetFlow.N_ITEMS_PER_PAGE), (
        "the results grid did not start at its first page")
    assert leaf.search_entry.get_text() == "volume", (
        "the results page does not hold the query, so its own sort ranks by "
        "something else than the search did")
    assert leaf._pack_search_source is pack_page
    assert leaf.empty_label.get_visible() is False

    # Only now, with the results on screen, does the page that asked count as
    # current with its entry.
    assert pack_page._searched_text == "volume", (
        "the pack grid does not know it rendered this query")

    # The typing follows the query to the page that now holds it.
    pump()
    assert leaf.search_entry.focused == 1, (
        "the entry the user typed into went away and no other entry took the "
        "typing over")
    assert leaf.search_entry.position == -1, (
        "the cursor did not go to the end of the query, so the next keystroke "
        "replaces it")
    print("PASS: a query on the pack grid drills into the results across packs")


def test_a_dropped_gather_leaves_the_page_behind() -> None:
    """A gather that never renders must not settle the catch-up.

    Settled at the start, a page turn mid-gather leaves the grid on the
    results of the query before while the page believes it is current: showing
    it again finds nothing to catch up with, and page two of that grid filters
    by a query nobody can see.
    """
    gate, release = threading.Event(), threading.Event()
    held = FakePack("Held Icons", ["volume_mute"], gate=gate, release=release)
    install_packs([held])
    pack_page, leaf, stack, asset_manager = make_pair("volume")

    # Through the entry and the queued pass, not straight into apply_search:
    # what a page records as rendered is decided on that path.
    pack_page.on_search_changed(None)
    pump_until(gate.is_set, 10, "the search worker never started")
    pack_page.invalidate_search()
    release.set()
    pump(0.3)

    assert leaf.asset_flow.items is None, "the dropped gather rendered"
    assert pack_page._searched_text == "", (
        f"the page recorded {pack_page._searched_text!r} as rendered for a "
        f"gather that was dropped; it will never catch itself up")

    # Showing the page again therefore searches once more.
    gathered = record_gathers(pack_page)
    release.clear()
    gate.clear()
    pack_page._search_showing = False
    pack_page._searched_text = ""
    pack_page.search_entry.set_text("volume")
    pack_page._on_map()
    pump(0.1)
    release.set()
    pump_until(lambda: not pack_page._search_running, 10,
               "the worker never finished")
    # on_shown empties the entry as the page shows, so the catch-up searches
    # for nothing rather than for the query the page is dropping.
    assert gathered == [], (
        f"showing the page again started a gather for a query it throws away "
        f"in the same handler: {gathered}")
    assert pack_page.search_entry.get_text() == ""
    print("PASS: a dropped gather leaves the page knowing it fell behind")


def test_a_failing_gather_leaves_the_page_behind() -> None:
    """A gather that raises must fail the same way a dropped one does."""
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair("volume")

    def raising(query, requester, generation):
        raise RuntimeError("deliberate: a pack folder went away mid-scan")

    pack_page.collect_matching_assets = raising
    print("NOTE: the next ERROR traceback is DELIBERATE -- this leg makes the "
          "gather raise and checks the page recovers.")
    pack_page.apply_search("volume")
    pump_until(lambda: not pack_page._search_running, 10,
               "the worker never finished after the failure")

    assert leaf.asset_flow.items is None, "a failed gather rendered"
    assert pack_page._searched_text == "", (
        "the page counted a failed gather as rendered, so it will never try "
        "again on its own")

    # The worker is not wedged: the next query gathers.
    del pack_page.collect_matching_assets
    gathered = record_gathers(pack_page)
    pack_page.search_entry.set_text("bright")
    pack_page.apply_search("bright")
    pump_until(lambda: leaf.asset_flow.items is not None, 10,
               "the worker never ran again after a failure")
    assert gathered == ["bright"], gathered
    print("PASS: a gather that raises leaves the page able to try again")


def test_the_results_grid_reproduces_the_search_order() -> None:
    """The grid's own filter and sort must agree with the search.

    The page filters and sorts what it is handed, every time it renders a
    range. Ranked one way and rendered another, the grid would answer a
    different question on its second page than on its first.
    """
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair("volume")
    assets = pack_page.collect_matching_assets("volume", pack_page,
                                               pack_page._search_generation)
    leaf.load_search_results(pack_page, assets, "volume")

    kept = [asset for asset in assets if leaf.filter_func(asset)]
    assert kept == assets, "the results page filtered out its own results"
    ordered = sorted(kept, key=functools.cmp_to_key(leaf.sort_func))
    assert names_of(ordered) == names_of(assets), (
        f"the grid re-ordered the search: {names_of(ordered)} != "
        f"{names_of(assets)}")
    print("PASS: the results grid renders the order the search ranked")


def test_pack_label_names_the_pack() -> None:
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair()

    material = MATERIAL.icons[0]
    tabler = TABLER.icons[0]
    assert leaf.pack_label_for(material) is None, (
        "a grid of one pack names that pack on every card, which says nothing")

    leaf.load_search_results(pack_page, [material, tabler], "volume")
    assert leaf.pack_label_for(material) == "Material Icons"
    assert leaf.pack_label_for(tabler) == "Tabler Icons"

    # Back to one pack, and the line goes with the search.
    leaf.load_for_pack(MATERIAL)
    assert leaf.pack_label_for(material) is None, (
        "a card kept the pack line after the grid went back to one pack")
    print("PASS: a result card names its pack, and only in a search")


def test_the_card_is_handed_the_pack_line() -> None:
    """The label must reach the card, not only exist on the page.

    Structural, because the one call that carries it sits in the factory the
    recycler drives, and deleting that call changes nothing a stub grid can
    see.
    """
    source = textwrap.dedent(inspect.getsource(GenericAssetChooserPage.preview_factory))
    handed = [node for node in ast.walk(ast.parse(source))
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)
              and node.func.attr == "set_subtitle"]
    assert len(handed) == 1, (
        f"the card factory makes {len(handed)} calls that set the second line; "
        f"without one a result card never names its pack")
    argument = handed[0].args[0]
    assert (isinstance(argument, ast.Call)
            and isinstance(argument.func, ast.Attribute)
            and argument.func.attr == "pack_label_for"), (
        "the second line of a card comes from something other than "
        "pack_label_for, so nothing here can say what it shows")
    print("PASS: the card factory hands the pack line to the card")


def test_a_real_card_shows_and_clears_the_pack_line() -> None:
    """The recycled card is the case that matters.

    The pool binds a card from a search across the packs into a grid of one
    pack, where the line it carried would name the wrong thing.
    """
    if not Gtk.init_check():
        print("SKIP(real-card): no display; the structural leg above still ran")
        return

    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair()
    asset = FakeIcon(MATERIAL, REAL_IMAGE)
    preview = GenericAssetPreview()

    leaf.load_search_results(pack_page, [asset], "volume")
    leaf.preview_factory(preview, asset)
    assert preview.subtitle.get_text() == "Material Icons", (
        f"the card shows {preview.subtitle.get_text()!r} under the name")
    assert preview.subtitle.get_visible() is True, "the pack line is hidden"

    # The same card, recycled into a grid of one pack.
    leaf.load_for_pack(MATERIAL)
    leaf.preview_factory(preview, asset)
    assert preview.subtitle.get_text() == "", (
        f"a recycled card kept {preview.subtitle.get_text()!r} in a grid of "
        f"one pack")
    assert preview.subtitle.get_visible() is False, (
        "a recycled card kept an empty pack line taking room")
    print("PASS: a real card shows the pack line and clears it when recycled")


def test_selection_delivers_the_per_pack_payload() -> None:
    """A pick from the results must deliver what a pick from a pack does."""
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair()
    asset = MATERIAL.icons[0]
    child = types.SimpleNamespace(asset=asset)

    leaf.load_for_pack(MATERIAL)
    leaf.on_child_activated(None, child)
    per_pack = list(asset_manager.delivered)

    asset_manager.delivered.clear()
    leaf.load_search_results(pack_page, [asset], "volume")
    leaf.on_child_activated(None, child)
    from_search = list(asset_manager.delivered)

    assert per_pack == [asset.path], f"the per-pack path delivered {per_pack}"
    assert from_search == per_pack, (
        f"a pick from the results delivered {from_search}, the per-pack path "
        f"delivered {per_pack}")
    print(f"PASS: both paths deliver the same payload ({per_pack[0]!r})")


def test_a_changed_query_searches_again() -> None:
    """The results grid holds one query's answer, so a new query is a new
    search. A filter of what is there could only ever narrow, and a user who
    deletes a letter asks for more."""
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair("volume")
    assets = pack_page.collect_matching_assets("volume", pack_page,
                                               pack_page._search_generation)
    leaf.load_search_results(pack_page, assets, "volume")
    stack.set_visible_child_name(LEAF_CHILD_NAME)
    stack.switches.clear()

    leaf.search_entry.set_text("bright")
    leaf.apply_search("bright")
    pump_until(lambda: names_of(leaf.asset_flow.items) == expected_order(
        "bright", ALL_PACKS), 10, "the changed query never gathered again")

    assert leaf._pack_search_query == "bright"
    assert leaf._searched_text == "bright", (
        "the results page does not know it rendered the query it asked for")
    assert stack.switches == [], (
        f"the results of a second query moved the window again: {stack.switches}")
    # The same query renders the grid again and gathers nothing.
    leaf.asset_flow.ranges.clear()
    leaf.apply_search("bright")
    pump()
    assert leaf.asset_flow.ranges == [(0, FakeAssetFlow.N_ITEMS_PER_PAGE)], (
        f"an unchanged query did something other than render page one: "
        f"{leaf.asset_flow.ranges}")
    print("PASS: a changed query in the results gathers again, an unchanged "
          "one renders")


def test_empty_query_returns_to_the_pack_grid() -> None:
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair("volume")
    assets = pack_page.collect_matching_assets("volume", pack_page,
                                               pack_page._search_generation)
    leaf.load_search_results(pack_page, assets, "volume")
    stack.set_visible_child_name(LEAF_CHILD_NAME)
    asset_manager.back_visible = True

    leaf.search_entry.set_text("")
    leaf.apply_search("")

    assert stack.child == PACK_CHOOSER_CHILD_NAME, (
        f"an emptied query left the window on {stack.child!r}")
    assert asset_manager.back_visible is False, "the way back outlived the search"
    assert leaf.asset_flow.items == [], (
        "the results page kept every pack's assets after the search ended")
    assert leaf._pack_search_source is None and leaf._pack_search_query == ""
    assert leaf.empty_label.get_visible() is False
    assert leaf._searched_text == "", (
        "the page turn is this pass's answer, so the page must count it as "
        "rendered")

    # The typing follows the query back to the grid that owns it.
    pump()
    assert pack_page.search_entry.focused == 1, (
        "no entry took the typing over when the window went back")

    # A query of separators alone asks for nothing either.
    leaf.load_search_results(pack_page, assets, "volume")
    stack.set_visible_child_name(LEAF_CHILD_NAME)
    leaf.apply_search("  -- ")
    assert stack.child == PACK_CHOOSER_CHILD_NAME, (
        "a query that normalizes to nothing kept the results open")
    print("PASS: an emptied query goes back to the pack grid")


def test_the_pack_grid_clears_the_query_it_drilled_in_with() -> None:
    """This grid filters its own cards on the same query.

    Left there, the query would show a pack grid narrowed to whatever pack is
    named like it, which is usually no pack at all.
    """
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair("volume")
    pack_page.on_shown()
    assert pack_page.search_entry.get_text() == "", (
        "the query the grid drilled in with survived the way back")

    # It is the base's hook, which runs inside the map handler before the
    # catch-up pass. Connected as a second map handler instead, it would run
    # after that pass had already started a gather for the dying query.
    assert IconPackChooser.on_shown is GenericPackChooserPage.on_shown
    source = textwrap.dedent(inspect.getsource(GenericPackChooserPage.__init__))
    connects = [node for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"]
    assert connects == [], (
        "the pack grid connects a signal handler of its own again; settling "
        "the entry belongs in on_shown, which the base runs first")
    print("PASS: coming back to the pack grid empties the query")


def test_a_pack_drill_in_drops_the_search() -> None:
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair()
    leaf.load_search_results(pack_page, list(MATERIAL.icons), "volume")
    assert leaf.search_entry.get_text() == "volume"

    leaf.load_for_pack(TABLER)
    assert leaf._pack_search_source is None
    assert leaf._pack_search_query == ""
    assert leaf.search_entry.get_text() == "", (
        "the query of a search across the packs stayed on to filter one pack")
    assert leaf.asset_flow.items == TABLER.icons
    print("PASS: drilling into one pack drops the search that filled the grid")


def test_teardown_drops_a_gathering_pass() -> None:
    """A page turn or a hidden window mid-gather renders nothing.

    Each abort carries an observable, because an abort whose only effect is to
    make a later guard redundant can be deleted with every assertion still
    passing. The packs count their scans, and the render callback counts the
    times it is reached.
    """
    gate, release = threading.Event(), threading.Event()
    held = FakePack("Held Icons", ["volume_mute"], gate=gate, release=release)
    later = FakePack("Later Icons", ["volume_up"])
    last = FakePack("Last Icons", ["volume_down"])
    manager = install_packs([held, later, last])
    pack_page, leaf, stack, asset_manager = make_pair("volume")
    reached = record_renders(pack_page)

    pack_page.search_across_packs("volume", pack_page, pack_page._search_generation)
    assert gate.wait(10), "the search worker never started"

    # What unmapping the page does: every pass in flight goes stale.
    pack_page.invalidate_search()
    release.set()
    pump_until(lambda: not pack_page._search_running, 10,
               "the worker never finished")
    pump(0.2)

    assert leaf.asset_flow.items is None, (
        f"a pass that the teardown invalidated rendered {leaf.asset_flow.items}")
    assert stack.child == PACK_CHOOSER_CHILD_NAME, (
        "an invalidated pass moved the window to the results anyway")
    assert asset_manager.back_visible is None
    # The per-pack abort: the scan stops at the pack it was inside.
    assert (later.scans, last.scans) == (0, 0), (
        f"the gather read {later.scans + last.scans} more packs after the pass "
        f"went stale; the scan of a whole installation carried on for an "
        f"answer nothing would render")
    # The abort before the render is queued: the callback is never reached.
    assert reached == [], (
        f"a stale pass queued its render anyway and leaned on the guard "
        f"inside it: {reached}")

    # The abort before the discovery: a pass that is already stale must not
    # even read the pack folders.
    discoveries = manager.discoveries
    stale = pack_page._search_generation
    pack_page._search_showing = True
    pack_page._search_generation += 1
    assert pack_page.collect_matching_assets("volume", pack_page, stale) == []
    assert manager.discoveries == discoveries, (
        "a stale gather paid for the pack discovery, which is most of what a "
        "gather costs")

    # The same guard again inside the main-loop callback, because the query
    # can move on between the gather and the render.
    pack_page._show_matching_assets("volume", pack_page, stale,
                                    list(MATERIAL.icons))
    assert leaf.asset_flow.items is None, (
        "the render ran for a pass a later one had overtaken")

    # And the fresh generation does render, so the guard is not simply off.
    pack_page._show_matching_assets("volume", pack_page,
                                    pack_page._search_generation,
                                    list(MATERIAL.icons))
    assert leaf.asset_flow.items == MATERIAL.icons, (
        "the guard rejects the pass it is meant to let through")
    print("PASS: a teardown mid-gather drops the render, at all three guards")


def test_a_hidden_page_renders_nothing() -> None:
    """_search_showing is half of the guard.

    A hidden window keeps its generation, so a guard on the generation alone
    would render into a page nobody sees.
    """
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair("volume")
    generation = pack_page._search_generation
    pack_page._search_showing = False
    assert pack_page.search_is_current(generation) is False

    pack_page._show_matching_assets("volume", pack_page, generation,
                                    list(MATERIAL.icons))
    assert leaf.asset_flow.items is None, (
        "a hidden page took a render")
    print("PASS: a page that stopped showing renders nothing")


def test_the_render_asks_the_page_it_writes_into() -> None:
    """The guard on the requester is not a guard on the target.

    It holds only because a Gtk.Stack unmaps the child it leaves, which
    invalidates the page that asked. That is GTK's behaviour, one
    set_transition_type away from an overlap, and not a promise this module
    makes. A window that drilled into one pack while a gather ran must keep
    that pack's grid.
    """
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair("volume")

    # The user opened a pack while the gather ran.
    leaf.load_for_pack(MATERIAL)
    stack.set_visible_child_name(LEAF_CHILD_NAME)
    assert leaf.shows_pack_search is False

    pack_page._show_matching_assets("volume", pack_page,
                                    pack_page._search_generation,
                                    list(TABLER.icons))
    assert leaf.asset_flow.items == MATERIAL.icons, (
        "a gather replaced the grid of the pack the user had opened")
    assert pack_page._searched_text == "", (
        "a render that was refused was recorded as done")

    # A results page that is already showing takes the next results.
    leaf.load_search_results(pack_page, list(MATERIAL.icons), "volume")
    assert leaf.shows_pack_search is True
    pack_page._show_matching_assets("bright", pack_page,
                                    pack_page._search_generation,
                                    list(TABLER.icons))
    assert leaf.asset_flow.items == TABLER.icons, (
        "the results page refused results it was waiting for")
    print("PASS: the render asks the page it writes into, not only the page "
          "that asked")


def test_one_worker_serves_the_newest_query() -> None:
    """Three quick queries must not start three scans at once.

    A gather reads every pack folder of the installation. A thread per pass
    puts as many of those scans in flight as the user types pauses, and all
    but the last are thrown away.
    """
    gate, release = threading.Event(), threading.Event()
    held = FakePack("Held Icons", ["volume_mute"], gate=gate, release=release)
    manager = install_packs([held, MATERIAL])
    pack_page, leaf, stack, asset_manager = make_pair("volume")
    gathered = record_gathers(pack_page)

    generation = pack_page._search_generation
    pack_page.search_across_packs("q-one", pack_page, generation)
    assert gate.wait(10), "the search worker never started"
    pack_page.search_across_packs("q-two", pack_page, generation)
    pack_page.search_across_packs("q-three", pack_page, generation)
    release.set()
    pump_until(lambda: not pack_page._search_running, 10,
               "the worker never finished")

    assert gathered == ["q-one", "q-three"], (
        f"the worker gathered {gathered}; one gather runs at a time and the "
        f"newest request is the one it takes up next")
    assert manager.discoveries == 2, (
        f"{manager.discoveries} pack discoveries for three queries")
    assert pack_page._search_pending is None
    print("PASS: one gather runs at a time and the newest query wins")


def test_the_worker_flag_survives_a_raise_outside_the_catch() -> None:
    """The flag that says a gather runs must fall whatever ends the worker.

    The narrow catch covers the gather. A raise from anywhere else in the
    worker, which is the marshal of the render and the logger inside that
    catch, would otherwise leave the flag set for the life of the window:
    every later request posts itself and returns, and the search never runs
    again.
    """
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair("volume")

    def raising_past_the_catch(query, requester, generation):
        # Not an Exception, so the arm that catches a failed gather does not
        # see it. It stands in for a raise from the marshal below that arm.
        raise KeyboardInterrupt("deliberate: a raise the worker does not catch")

    pack_page.collect_matching_assets = raising_past_the_catch
    print("NOTE: the next 'Exception in thread' report is DELIBERATE -- this "
          "leg raises past the worker's catch and checks it recovers.")
    pack_page.search_across_packs("volume", pack_page, pack_page._search_generation)
    pump_until(lambda: not pack_page._search_running, 10,
               "the worker died with the flag still set, so no later search "
               "can ever start one")

    # And a later query really does gather.
    del pack_page.collect_matching_assets
    gathered = record_gathers(pack_page)
    pack_page.search_entry.set_text("bright")
    pack_page.apply_search("bright")
    pump_until(lambda: leaf.asset_flow.items is not None, 10,
               "the search never ran again after the worker died")
    assert gathered == ["bright"], gathered
    print("PASS: a raise past the worker's catch leaves the search able to run")


def test_a_request_that_arrives_as_the_worker_dies_is_taken_up() -> None:
    """The dying worker hands the flag on rather than dropping the request.

    A request that arrives while a worker is on its way out finds the flag
    set, so it posts itself and starts nothing. Something has to pick it up.
    """
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair("volume")
    gathered = record_gathers(pack_page)

    # The state a worker leaves behind when it dies with a request waiting.
    pack_page._search_running = True
    pack_page._search_pending = ("volume", pack_page, pack_page._search_generation)
    pack_page._release_search_worker()

    pump_until(lambda: leaf.asset_flow.items is not None, 10,
               "the request that arrived as the worker died was dropped")
    assert gathered == ["volume"], gathered
    assert pack_page._search_running is False
    assert pack_page._search_pending is None

    # With nothing waiting, it only drops the flag.
    pack_page._search_running = True
    pack_page._release_search_worker()
    assert pack_page._search_running is False
    print("PASS: a request left by a dying worker is taken up")


def test_the_search_state_carries_class_defaults() -> None:
    """The base connects the search entry from its own constructor.

    An emission can therefore reach apply_search, and so this state, before a
    subclass has run a line of its own. Every field it touches needs a default
    on the class, as the flow boxes and the pending requests already have.
    """
    for name in ("_search_lock", "_search_pending", "_search_running",
                 "pack_flow"):
        assert name in vars(GenericPackChooserPage), (
            f"GenericPackChooserPage declares no class-level {name}; a search "
            f"emission during the build then raises AttributeError")
    for name in ("_pending_pack", "_pending_results", "_pack_search_source",
                 "_pack_search_query", "empty_label", "asset_flow"):
        assert name in vars(GenericAssetChooserPage), (
            f"GenericAssetChooserPage declares no class-level {name}")

    # A page that never ran its constructor still searches, which is what the
    # defaults are for.
    install_packs(ALL_PACKS)
    bare = IconPackChooser.__new__(IconPackChooser)
    assert bare._search_running is False
    assert bare._search_pending is None
    assert bare._search_lock is GenericPackChooserPage._search_lock
    print("PASS: the search state a build-time emission touches has class "
          "defaults")


def test_a_search_that_lands_before_the_grid_is_held() -> None:
    """The leaf page builds its grid inside a main-loop callback.

    A search that lands first has nowhere to render, and dropping it loses
    what the user asked for.
    """
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair()
    leaf.asset_flow = None

    leaf.load_search_results(pack_page, list(MATERIAL.icons), "volume")
    assert leaf._pending_results is not None, "the search was dropped"
    assert leaf._pending_pack is None

    # Each request drops the other, so only one of the two ever drains.
    leaf.load_for_pack(TABLER)
    assert leaf._pending_results is None and leaf._pending_pack is TABLER

    # A build that failed strands neither.
    leaf._reset_build_state()
    assert leaf._pending_pack is None and leaf._pending_results is None

    # The drain lives in the main-loop callback that builds the grid.
    source = textwrap.dedent(inspect.getsource(GenericAssetChooserPage._build_ui))
    drained = {node.attr for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.Attribute)
               and node.attr in ("_pending_pack", "_pending_results")}
    assert drained == {"_pending_pack", "_pending_results"}, (
        f"the grid's build drains {sorted(drained)}; a request it does not "
        f"drain strands without a word")
    print("PASS: a search that lands before the grid is held and drains once")


def test_every_family_shares_the_search() -> None:
    """Icons, wallpapers and SD+ bar wallpapers search the same way.

    Each family names its classes and its packs; none of them owns a copy of
    the search.
    """
    pack_classes = (IconPackChooser, WallpaperPackChooser, SDPlusBarWallpaperPackChooser)
    leaf_classes = (IconChooserPage, WallpaperChooserPage, SDPlusBarWallpaperChooserPage)

    for name in ("search_across_packs", "collect_matching_assets",
                 "_show_matching_assets", "on_shown"):
        owned = {getattr(cls, name) for cls in pack_classes}
        assert owned == {getattr(GenericPackChooserPage, name)}, (
            f"the pack grids no longer share one {name}: {owned}")
    for name in ("load_search_results", "leave_search_results", "apply_search",
                 "pack_label_for", "show_empty_notice"):
        owned = {getattr(cls, name) for cls in leaf_classes}
        assert owned == {getattr(GenericAssetChooserPage, name)}, (
            f"the leaf pages no longer share one {name}: {owned}")

    # The two wallpaper families read their assets through the same hook the
    # gather calls, so the gather reaches them without knowing what they are.
    for cls in leaf_classes:
        assert cls.get_assets is not GenericAssetChooserPage.get_assets, (
            f"{cls.__name__} left get_assets to the base, which raises")
    print("PASS: all three asset families share one search across packs")


def test_the_gather_stays_off_the_main_thread() -> None:
    """The gather reads a whole installation, so it may not run on the loop."""
    install_packs(ALL_PACKS)
    pack_page, leaf, stack, asset_manager = make_pair("volume")

    seen: list[threading.Thread] = []
    real_get_packs = IconPackChooser.get_packs

    def watching(self):
        seen.append(threading.current_thread())
        return real_get_packs(self)

    IconPackChooser.get_packs = watching
    try:
        pack_page.apply_search("volume")
        pump_until(lambda: leaf.asset_flow.items is not None, 10,
                   "the search never reached the leaf page")
    finally:
        IconPackChooser.get_packs = real_get_packs

    assert seen, "the gather never read the packs, so this leg checks nothing"
    on_main = [thread for thread in seen if thread is threading.main_thread()]
    assert not on_main, (
        f"{len(on_main)}/{len(seen)} pack reads ran on the main loop; a gather "
        f"of a whole installation there freezes the window")
    print(f"PASS: all {len(seen)} pack reads of a search run off the main loop")


def main() -> int:
    # Under run_all.py's 90 s per-scenario default, so it fires first and
    # names this scenario instead of leaving a bare subprocess timeout.
    fixtures.start_watchdog(60, label="scenario_cross_pack_search")

    test_aggregation_merges_and_ranks()
    test_a_query_that_matches_nothing()
    test_pack_grid_drills_into_the_results()
    test_a_dropped_gather_leaves_the_page_behind()
    test_a_failing_gather_leaves_the_page_behind()
    test_the_results_grid_reproduces_the_search_order()
    test_pack_label_names_the_pack()
    test_the_card_is_handed_the_pack_line()
    test_selection_delivers_the_per_pack_payload()
    test_a_changed_query_searches_again()
    test_empty_query_returns_to_the_pack_grid()
    test_the_pack_grid_clears_the_query_it_drilled_in_with()
    test_a_pack_drill_in_drops_the_search()
    test_teardown_drops_a_gathering_pass()
    test_a_hidden_page_renders_nothing()
    test_the_render_asks_the_page_it_writes_into()
    test_one_worker_serves_the_newest_query()
    test_the_worker_flag_survives_a_raise_outside_the_catch()
    test_a_request_that_arrives_as_the_worker_dies_is_taken_up()
    test_the_search_state_carries_class_defaults()
    test_a_search_that_lands_before_the_grid_is_held()
    test_every_family_shares_the_search()
    test_the_gather_stays_off_the_main_thread()
    # Last: it initialises GTK, which the legs above do without.
    test_a_real_card_shows_and_clears_the_pack_line()

    print("ALL PASS: scenario_cross_pack_search")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
