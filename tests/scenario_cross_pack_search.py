"""Pins the search that reaches across every pack of one asset type.

A query typed into the pack grid gathers the assets of all of its packs on a
worker thread, ranks the merged names with one ranker, and shows the matches
in the leaf page. The leaf page then owns the query: a changed one gathers
again, an emptied one goes back to the pack grid.

The legs drive the real classes over stand-ins for the widgets, so the whole
file is logic and threads. No GTK widget is built, and no display is needed.
The one main-loop dependency is real: the worker hands its result over with
GLib.idle_add, and the legs pump the default context for it.
"""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import ast
import functools
import inspect
import textwrap
import threading
import time
import types

from gi.repository import GLib

import globals as gl

from src.windows.AssetManager import asset_search
from src.windows.AssetManager.GenericAssetChooser import (
    PACK_CHOOSER_CHILD_NAME,
    GenericAssetChooserPage,
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
    """One icon pack. get_icons is what the leaf page asks it for."""

    def __init__(self, name: str, names: list[str], gate: "threading.Event | None" = None,
                 release: "threading.Event | None" = None) -> None:
        self.name = name
        self.gate = gate
        self.release = release
        self.icons = [FakeIcon(self, f"/packs/{name}/{leaf}.png") for leaf in names]

    def get_icons(self) -> "list[FakeIcon]":
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

    def get_icon_packs(self) -> dict[str, FakePack]:
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


def install_packs(packs: list[FakePack]) -> None:
    gl.icon_pack_manager = FakeManager(packs)


def make_pack_page(entry_text: str = "", stack: "FakeStack | None" = None,
                   asset_manager: "FakeAssetManager | None" = None,
                   leaf: "IconChooserPage | None" = None) -> IconPackChooser:
    page = IconPackChooser.__new__(IconPackChooser)
    page.search_entry = FakeEntry(entry_text)
    page.pack_flow = FakePackFlow()
    page.stack = stack if stack is not None else FakeStack()
    page.stack.leaf_chooser = leaf
    page.asset_manager = asset_manager if asset_manager is not None else FakeAssetManager()
    return page


def make_leaf_page(stack: "FakeStack | None" = None,
                   asset_manager: "FakeAssetManager | None" = None) -> IconChooserPage:
    page = IconChooserPage.__new__(IconChooserPage)
    page.search_entry = FakeEntry("")
    page.asset_flow = FakeAssetFlow()
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
    page = make_pack_page()
    page.stack.leaf_chooser = make_leaf_page()
    assert page.collect_matching_assets("zzzz", page, page._search_generation) == []
    print("PASS: a query no pack answers gathers nothing")


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

    # The typing follows the query to the page that now holds it.
    pump()
    assert leaf.search_entry.focused == 1, (
        "the entry the user typed into went away and no other entry took the "
        "typing over")
    assert leaf.search_entry.position == -1, (
        "the cursor did not go to the end of the query, so the next keystroke "
        "replaces it")
    print("PASS: a query on the pack grid drills into the results across packs")


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
    pack_page._clear_search_on_return()
    assert pack_page.search_entry.get_text() == "", (
        "the query the grid drilled in with survived the way back")

    # The base connects the catch-up handler for map, and this page connects
    # its own beside it. Both must be there, and in that order, or a grid
    # that fell behind while it was hidden never catches up.
    source = textwrap.dedent(inspect.getsource(GenericPackChooserPage.__init__))
    connected = [node.args[1].attr for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "connect"
                 and len(node.args) == 2
                 and isinstance(node.args[0], ast.Constant)
                 and node.args[0].value == "map"
                 and isinstance(node.args[1], ast.Attribute)]
    assert connected == ["_clear_search_on_return"], (
        f"the pack grid connects {connected} to map; it owns exactly the one "
        f"handler that empties the entry, beside the base's own")
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

    The gather reads a whole installation, so it can still be running when
    the window goes. The pack whose scan is held open below is what makes
    that deterministic here.
    """
    gate, release = threading.Event(), threading.Event()
    held = FakePack("Held Icons", ["volume_mute"], gate=gate, release=release)
    install_packs([held, MATERIAL])
    pack_page, leaf, stack, asset_manager = make_pair("volume")

    pack_page.search_across_packs("volume", pack_page, pack_page._search_generation)
    assert gate.wait(10), "the search worker never started"

    # What unmapping the page does: every pass in flight goes stale.
    pack_page.invalidate_search()
    release.set()

    pump(0.4)
    assert leaf.asset_flow.items is None, (
        f"a pass that the teardown invalidated rendered {leaf.asset_flow.items}")
    assert stack.child == PACK_CHOOSER_CHILD_NAME, (
        "an invalidated pass moved the window to the results anyway")
    assert asset_manager.back_visible is None

    # The same guard again inside the main-loop callback, because the query
    # can move on between the gather and the render.
    stale = pack_page._search_generation
    pack_page._search_showing = True
    pack_page._search_generation += 1
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
    print("PASS: a teardown mid-gather drops the render, at both guards")


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
                 "_show_matching_assets", "_clear_search_on_return"):
        owned = {getattr(cls, name) for cls in pack_classes}
        assert owned == {getattr(GenericPackChooserPage, name)}, (
            f"the pack grids no longer share one {name}: {owned}")
    for name in ("load_search_results", "leave_search_results", "apply_search",
                 "pack_label_for"):
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
    fixtures.start_watchdog(120, label="scenario_cross_pack_search")

    test_aggregation_merges_and_ranks()
    test_a_query_that_matches_nothing()
    test_pack_grid_drills_into_the_results()
    test_the_results_grid_reproduces_the_search_order()
    test_pack_label_names_the_pack()
    test_selection_delivers_the_per_pack_payload()
    test_a_changed_query_searches_again()
    test_empty_query_returns_to_the_pack_grid()
    test_the_pack_grid_clears_the_query_it_drilled_in_with()
    test_a_pack_drill_in_drops_the_search()
    test_teardown_drops_a_gathering_pass()
    test_a_hidden_page_renders_nothing()
    test_a_search_that_lands_before_the_grid_is_held()
    test_every_family_shares_the_search()
    test_the_gather_stays_off_the_main_thread()

    print("ALL PASS: scenario_cross_pack_search")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
