"""Pins the search behaviour of the AssetManager asset choosers as pure logic.

The three chooser pages share one filter_func and sort_func on
GenericAssetChooserPage, and their pack grids share one card filter. No GTK
widget, no display, no deck.
"""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import ast
import functools
import inspect
import textwrap
import types

from src.windows.AssetManager.ChooserPage import ChooserPage
from src.windows.AssetManager.CustomAssets.Chooser import CustomAssetChooser
from src.windows.AssetManager.GenericAssetChooser import (
    GenericAssetChooserPage,
    GenericAssetFlowBox,
    GenericAssetPreview,
    GenericPackChooserPage,
    GenericPackChooserStack,
    GenericPackFlowBox,
    GenericPackPreview,
    asset_display_name,
    asset_matches_search,
    compare_assets,
)
from src.windows.AssetManager.asset_search import SCORE_CONTAINS, SEARCH_SCORE_THRESHOLD
from src.windows.AssetManager.IconPacks.Icons.IconChooser import IconChooserPage
from src.windows.AssetManager.IconPacks.PackChooser import IconPackChooser
from src.windows.AssetManager.IconPacks.Stack import IconPackChooserStack
from src.windows.AssetManager.SDPlusBarWallpaperPacks.PackChooser import SDPlusBarWallpaperPackChooser
from src.windows.AssetManager.SDPlusBarWallpaperPacks.SDPlusBarWallpaper.SDPlusBarWallpaperChooser import (
    SDPlusBarWallpaperChooserPage,
)
from src.windows.AssetManager.SDPlusBarWallpaperPacks.Stack import SDPlusBarWallpaperPackChooserStack
from src.windows.AssetManager.WallpaperPacks.PackChooser import WallpaperPackChooser
from src.windows.AssetManager.WallpaperPacks.Stack import WallpaperPackChooserStack
from src.windows.AssetManager.WallpaperPacks.Wallpapers.WallpaperChooser import WallpaperChooserPage


CHOOSER_CLASSES = {
    "icons": IconChooserPage,
    "wallpapers": WallpaperChooserPage,
    "sd+bar wallpapers": SDPlusBarWallpaperChooserPage,
}

PACK_CHOOSER_CLASSES = {
    "icons": IconPackChooser,
    "wallpapers": WallpaperPackChooser,
    "sd+bar wallpapers": SDPlusBarWallpaperPackChooser,
}

STACK_CLASSES = (IconPackChooserStack, WallpaperPackChooserStack,
                 SDPlusBarWallpaperPackChooserStack)


def asset(path: str):
    """An asset stand-in. The choosers read only .path off one."""
    return types.SimpleNamespace(path=path)


# Mixed directories, extensions and capitalisation.
CORPUS = [
    asset("/packs/icons/volume_up.png"),
    asset("/packs/icons/volume_down.svg"),
    asset("/other/brightness.jpeg"),
    asset("/packs/icons/Zebra.png"),
    asset("apple.gif"),
]
NAMES = ["volume_up", "volume_down", "brightness", "Zebra", "apple"]

# The ladder scores for the queries below, as documentation. The checks assert
# orderings and rungs, not the ranking key itself.
#   volume: volume_up 90 and volume_down 90, both prefixes, and volume_up
#           first because it is the shorter name; nothing else matches.
#   bright: brightness 90, nothing else matches.


def make_page(cls, search: str):
    """A chooser instance reduced to what filter_func and sort_func read."""
    page = cls.__new__(cls)
    page.search_entry = types.SimpleNamespace(get_text=lambda: search)
    return page


def names_of(items) -> list[str]:
    return [asset_display_name(item) for item in items]


def apply_chooser(page, items) -> list[str]:
    """Filter and sort exactly as DynamicFlowBox.get_items_to_show does."""
    kept = [item for item in items if page.filter_func(item)]
    ordered = sorted(kept, key=functools.cmp_to_key(page.sort_func))
    return names_of(ordered)


def test_display_name_strips_extension() -> None:
    assert asset_display_name(asset("/packs/icons/volume_up.png")) == "volume_up"
    assert asset_display_name(asset("apple.gif")) == "apple"
    assert asset_display_name(asset("/no/extension/here")) == "here"
    # os.path.splitext strips the last extension only.
    assert asset_display_name(asset("/a/archive.tar.gz")) == "archive.tar"
    print("PASS: the search name is the basename without its extension")


def test_three_types_key_on_path() -> None:
    for label, cls in CHOOSER_CLASSES.items():
        assert cls.ASSET_PATH_ATTR == "path", f"{label} keys on {cls.ASSET_PATH_ATTR!r}"
    print("PASS: all three asset types key the search on .path")


def test_three_types_share_implementation() -> None:
    filters = {cls.filter_func for cls in CHOOSER_CLASSES.values()}
    sorts = {cls.sort_func for cls in CHOOSER_CLASSES.values()}
    assert filters == {GenericAssetChooserPage.filter_func}, (
        f"asset types no longer share one filter_func: {filters}")
    assert sorts == {GenericAssetChooserPage.sort_func}, (
        f"asset types no longer share one sort_func: {sorts}")
    print("PASS: the three chooser classes share one filter_func/sort_func")


def test_three_types_share_their_widgets() -> None:
    """One grid shell and one card serve all three asset types.

    Each type held a copy of both, and the copies drifted: one card read a
    different global for the window it reports to, and one dropped the
    original-url field the other two passed on.
    """
    flow_boxes = {cls.FLOW_BOX_CLASS for cls in CHOOSER_CLASSES.values()}
    previews = {cls.PREVIEW_CLASS for cls in CHOOSER_CLASSES.values()}
    assert flow_boxes == {GenericAssetFlowBox}, (
        f"asset types no longer share one asset grid: {flow_boxes}")
    assert previews == {GenericAssetPreview}, (
        f"asset types no longer share one asset card: {previews}")

    pack_flow_boxes = {cls.PACK_FLOW_BOX_CLASS for cls in PACK_CHOOSER_CLASSES.values()}
    pack_previews = {cls.PACK_PREVIEW_CLASS for cls in PACK_CHOOSER_CLASSES.values()}
    assert pack_flow_boxes == {GenericPackFlowBox}, (
        f"asset types no longer share one pack grid: {pack_flow_boxes}")
    assert pack_previews == {GenericPackPreview}, (
        f"asset types no longer share one pack card: {pack_previews}")
    print("PASS: the three types share one pack grid, pack card, asset grid "
          "and asset card")


# What a pack stack may still define for itself. The icon stack defers a
# pre-selection until both its pages have built, and it is the one stack that
# AssetChooser.show_for_path routes to.
ALLOWED_STACK_METHODS = {
    "IconPackChooserStack": {"prepare", "show_for_path", "get_is_build_finished",
                             "on_load_finished"},
    "WallpaperPackChooserStack": set(),
    "SDPlusBarWallpaperPackChooserStack": set(),
}


def test_no_pack_stack_regrows_the_shared_body() -> None:
    """A stack subclass names its two page classes and its leaf title. The
    construction of the pair belongs to the shared base."""
    offences = []
    for cls in STACK_CLASSES:
        assert cls.__name__ in ALLOWED_STACK_METHODS, (
            f"{cls.__name__} is a new pack stack. Add it to "
            "ALLOWED_STACK_METHODS with the reason it needs a body."
        )
        allowed = ALLOWED_STACK_METHODS[cls.__name__]
        # Methods only. The class attributes that name the two page classes
        # are the point of a subclass, and a class object is callable too.
        own = {name for name, value in vars(cls).items()
               if isinstance(value, (types.FunctionType, staticmethod, classmethod))
               and not name.startswith("__")}
        regrown = sorted(own - allowed)
        if regrown:
            offences.append(f"{cls.__name__} defines {regrown}")
        assert issubclass(cls, GenericPackChooserStack), (
            f"{cls.__name__} no longer builds on the shared stack")
    assert not offences, (
        "per-family pack-stack copies came back: " + "; ".join(offences))

    # The base must still carry what the subclasses are barred from holding.
    for name in ("__init__", "build", "prepare"):
        assert name in vars(GenericPackChooserStack), (
            f"GenericPackChooserStack no longer defines {name}; this check "
            "would pass over nothing")
    print("PASS: the three pack stacks share one constructor and one build")


def test_empty_query_sorts_alphabetically() -> None:
    expected = ["Zebra", "apple", "brightness", "volume_down", "volume_up"]
    for label, cls in CHOOSER_CLASSES.items():
        page = make_page(cls, "")
        got = apply_chooser(page, CORPUS)
        assert got == expected, f"{label}: empty-query order {got} != {expected}"
    # The empty-query branch compares raw names, so case matters and Zebra
    # sorts before apple.
    assert compare_assets(asset("Zebra.png"), asset("apple.png"), "") == -1
    # An unchanged name pair ties, so sorted keeps the input order.
    assert compare_assets(asset("/a/x.png"), asset("/b/x.svg"), "") == 0
    print("PASS: an empty query keeps every asset and sorts it alphabetically")


def test_query_filters_below_threshold() -> None:
    for label, cls in CHOOSER_CLASSES.items():
        page = make_page(cls, "volume")
        kept = names_of([i for i in CORPUS if page.filter_func(i)])
        assert kept == ["volume_up", "volume_down"], f"{label}: kept {kept}"

        page = make_page(cls, "bright")
        kept = names_of([i for i in CORPUS if page.filter_func(i)])
        assert kept == ["brightness"], f"{label}: kept {kept}"

        # No match at all gives an empty grid, not the empty-query result.
        page = make_page(cls, "zzzz")
        assert [i for i in CORPUS if page.filter_func(i)] == [], f"{label}: zzzz matched"

        # A word of the name, not of the file path: the directory and the
        # extension never enter the search.
        page = make_page(cls, "icons")
        assert [i for i in CORPUS if page.filter_func(i)] == [], (
            f"{label}: the directory reached the search")
    assert SEARCH_SCORE_THRESHOLD == SCORE_CONTAINS, "the chooser threshold moved"
    print(f"PASS: a query keeps only names scoring at least {SEARCH_SCORE_THRESHOLD}")


def test_query_orders_by_descending_score() -> None:
    for label, cls in CHOOSER_CLASSES.items():
        page = make_page(cls, "volume")
        got = apply_chooser(page, CORPUS)
        assert got == ["volume_up", "volume_down"], f"{label}: order {got}"

    # The comparator ranks the closer name first whatever the input order, and
    # it is antisymmetric.
    up, down = asset("volume_up.png"), asset("volume_down.png")
    assert compare_assets(up, down, "volume") == -1
    assert compare_assets(down, up, "volume") == 1
    # Directory and extension never enter the score.
    assert compare_assets(asset("/deep/dir/volume_up.png"),
                          asset("volume_up.svg"), "volume") == 0
    print("PASS: a query orders assets by descending relevance")


def test_comparator_returns_int() -> None:
    pairs = [(CORPUS[0], CORPUS[1]), (CORPUS[1], CORPUS[0]), (CORPUS[0], CORPUS[0])]
    for search in ("", "volume", "zzzz"):
        for a, b in pairs:
            result = compare_assets(a, b, search)
            assert isinstance(result, int) and not isinstance(result, bool), (
                f"comparator returned {type(result).__name__} for {search!r} "
                f"-- GTK's sort contract wants an int")
    print("PASS: the sort comparator returns ints (GTK's sort contract)")


def test_helpers_and_methods_agree() -> None:
    """The bound methods must be the module helpers over the search entry.

    No per-class fixup may come back.
    """
    for search in ("", "volume", "bright", "zzzz"):
        page = make_page(IconChooserPage, search)
        for item in CORPUS:
            assert page.filter_func(item) == asset_matches_search(item, search)
        for a in CORPUS:
            for b in CORPUS:
                assert page.sort_func(a, b) == compare_assets(a, b, search)
    print("PASS: the chooser methods are the shared helpers over the search entry")


# The pack grid searches too. Its cards carry a pack, and the query filters on
# the pack name.

def pack_card(name: str):
    """A pack card stand-in. The filter reads only .pack.name off one."""
    return types.SimpleNamespace(pack=types.SimpleNamespace(name=name))


PACKS = ["Material Icons", "Tabler Icons", "Font Awesome", "simple-icons"]


def make_pack_page(cls, search: str):
    """A pack chooser reduced to what its filter reads."""
    page = cls.__new__(cls)
    page.search_entry = types.SimpleNamespace(get_text=lambda: search)
    return page


def test_pack_grid_filters_on_the_pack_name() -> None:
    for label, cls in PACK_CHOOSER_CLASSES.items():
        page = make_pack_page(cls, "")
        kept = [name for name in PACKS if page.filter_pack_child(pack_card(name))]
        assert kept == PACKS, f"{label}: an empty query dropped packs: {kept}"

        page = make_pack_page(cls, "icons")
        kept = [name for name in PACKS if page.filter_pack_child(pack_card(name))]
        assert kept == ["Material Icons", "Tabler Icons", "simple-icons"], (
            f"{label}: 'icons' kept {kept}")

        page = make_pack_page(cls, "awesome")
        kept = [name for name in PACKS if page.filter_pack_child(pack_card(name))]
        assert kept == ["Font Awesome"], f"{label}: 'awesome' kept {kept}"

        page = make_pack_page(cls, "zzzz")
        kept = [name for name in PACKS if page.filter_pack_child(pack_card(name))]
        assert kept == [], f"{label}: 'zzzz' kept {kept}"

        # A child that carries no pack is left alone: hiding a widget this
        # page never built would be the wrong answer.
        assert page.filter_pack_child(types.SimpleNamespace()) is True
    print("PASS: the pack grids filter their cards on the pack name")


def test_pack_grid_installs_its_filter() -> None:
    """The grid must be handed the predicate, or the search box does nothing.

    Reading the source, because the install line sits in the main-loop
    callback that builds the widgets.
    """
    source = inspect.getsource(GenericPackChooserPage._build_ui)
    tree = ast.parse(textwrap.dedent(source))
    installed = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "set_filter_func"]
    assert len(installed) == 1, (
        f"the pack grid takes {len(installed)} filter installs; without one "
        f"the search entry of the pack page changes nothing")
    handed = installed[0].args[0]
    assert isinstance(handed, ast.Attribute) and handed.attr == "filter_pack_child", (
        "the pack grid is handed something other than filter_pack_child, so "
        "nothing here can say what the search does")
    print("PASS: the pack grid installs the pack filter on its flow box")


def test_every_search_box_reaches_a_page() -> None:
    """No page may leave the base hook in place.

    ChooserPage.apply_search does nothing, so a page that keeps it shows a
    search entry that changes nothing. Three pack pages did.
    """
    pages = dict(PACK_CHOOSER_CLASSES)
    pages.update({f"{label} assets": cls for label, cls in CHOOSER_CLASSES.items()})
    pages["custom assets"] = CustomAssetChooser

    inert = [label for label, cls in pages.items()
             if cls.apply_search is ChooserPage.apply_search]
    assert not inert, (
        "these pages carry a search entry that changes nothing: "
        + ", ".join(sorted(inert)))

    # The base hook must stay a no-op, or the check above passes over nothing.
    body = ast.parse(textwrap.dedent(
        inspect.getsource(ChooserPage.apply_search))).body[0]
    assert isinstance(body, ast.FunctionDef)
    statements = [node for node in body.body
                  if not (isinstance(node, ast.Expr)
                          and isinstance(node.value, ast.Constant))]
    assert statements == [], (
        "ChooserPage.apply_search grew a body; it is the hook a page is meant "
        "to override, and the check above compares against it")
    print(f"PASS: all {len(pages)} chooser pages act on their search entry")


def main() -> int:
    fixtures.start_watchdog(60, label="scenario_asset_chooser_logic")

    test_display_name_strips_extension()
    test_three_types_key_on_path()
    test_three_types_share_implementation()
    test_three_types_share_their_widgets()
    test_no_pack_stack_regrows_the_shared_body()
    test_empty_query_sorts_alphabetically()
    test_query_filters_below_threshold()
    test_query_orders_by_descending_score()
    test_comparator_returns_int()
    test_helpers_and_methods_agree()
    test_pack_grid_filters_on_the_pack_name()
    test_pack_grid_installs_its_filter()
    test_every_search_box_reaches_a_page()

    print("ALL PASS: scenario_asset_chooser_logic")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
