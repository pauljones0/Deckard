"""Verify normalization, fallback, battery aliases, and mixed-script search.
The corpus uses Material, Tabler, Font Awesome, and simple-icons names."""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import ast
import functools
import os

from rapidfuzz import fuzz

from src.windows.AssetManager import asset_search
from src.windows.AssetManager.asset_search import (
    SCORE_CONTAINS,
    SCORE_EXACT,
    SCORE_NO_MATCH,
    SCORE_PREFIX,
    SCORE_WORD_PREFIX,
    SEARCH_SCORE_THRESHOLD,
    QueryRanker,
    compare,
    is_empty_query,
    matches,
    normalize,
    rank_key,
    ranker,
    score,
)


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Every name of an icon pack that holds "battery", plus names that hold none.
BATTERY = [
    "battery",
    "battery_std",
    "battery_full",
    "battery-full",
    "battery_alert",
    "battery_charging_full",
    "battery-charging-outline",
    "battery_charging_20_symbolic",
    "battery-automotive-filled",
    "battery-vertical-3-outline",
    "car-battery",
    "device_battery",
]
NOT_BATTERY = ["acer", "afterpay", "alacritty", "alteryx", "bacteria", "category"]


def test_normalize_folds_separators() -> None:
    assert normalize("Battery_Charging-Full") == "battery charging full"
    assert normalize("media.next") == "media next"
    assert normalize("a/b\\c+d") == "a b c d"
    assert normalize("  volume___up  ") == "volume up"
    assert normalize("") == ""
    # A query of separators alone asks for nothing.
    assert normalize(" _-. ") == ""
    print("PASS: the ladder compares lower case with one space per separator run")


def test_ladder_rungs() -> None:
    """Each rung, over the name that earns it."""
    assert score("battery", "battery") == SCORE_EXACT
    assert score("Battery", "battery") == SCORE_EXACT, "case must not lower a rung"
    assert score("battery-full", "battery full") == SCORE_EXACT, (
        "a separator must not lower a rung")

    assert score("battery_charging_full", "battery") == SCORE_PREFIX
    assert score("batterysaver", "battery") == SCORE_PREFIX

    assert score("car-battery", "battery") == SCORE_WORD_PREFIX
    assert score("device_battery_low", "battery") == SCORE_WORD_PREFIX

    assert score("airplay", "play") == SCORE_CONTAINS
    assert score("googlehome", "home") == SCORE_CONTAINS

    assert score("brightness", "battery") == SCORE_NO_MATCH
    assert score("battery", "zzzz") == SCORE_NO_MATCH

    # The rungs must stay ordered, or the ranking below means nothing.
    assert SCORE_EXACT > SCORE_PREFIX > SCORE_WORD_PREFIX > SCORE_CONTAINS > SCORE_NO_MATCH
    print("PASS: exact, prefix, word prefix, contains and miss each score their rung")


def test_tokens_are_an_and() -> None:
    # Every word of the query must be in the name.
    assert matches("volume_up", "volume up")
    assert not matches("volume_up", "volume down")
    assert not matches("volume_up", "volume up mute")

    # Their order does not matter.
    assert score("volume_up", "up volume") == SCORE_WORD_PREFIX
    assert matches("battery_charging_full", "full charging")

    # A query scores as its weakest word: "volume" is a prefix (90) and "up"
    # only starts a word (80).
    assert score("volume_up-inv", "volume up") == SCORE_WORD_PREFIX
    print("PASS: the words of a query are an AND, and their order is free")


def test_empty_query_keeps_everything() -> None:
    for query in ("", "   ", "_-."):
        assert is_empty_query(query), f"{query!r} must ask for nothing"
        for name in BATTERY + NOT_BATTERY:
            assert matches(name, query), f"{name} dropped out of the empty query {query!r}"
            assert score(name, query) == SCORE_EXACT
    assert not is_empty_query("battery")
    print("PASS: an empty query keeps every name")


def test_empty_query_costs_nothing() -> None:
    """Avoid memoization when an empty query accepts every grid item."""
    empty = QueryRanker("")
    for name in BATTERY + NOT_BATTERY:
        assert empty.matches(name)
    assert empty._keys == {}, (
        f"the empty query memoized {len(empty._keys)} names for an answer it "
        f"does not need to compute")

    # A caller that ranks an empty query gets alphabetical order, not the
    # length order that the scoring key would otherwise leave behind.
    names = ["zebra", "a-very-long-name-indeed", "apple", "Bee"]
    assert sorted(names, key=lambda name: rank_key(name, "")) == [
        "a-very-long-name-indeed", "apple", "Bee", "zebra"]
    print("PASS: an empty query builds no memo and sorts alphabetically")


def test_cache_is_released_on_demand() -> None:
    """The window that searched says when the memo is spent."""
    held = ranker("battery")
    held.rank_key("battery_full")
    assert asset_search._cached_ranker is held

    asset_search.release_cache()
    assert asset_search._cached_ranker is None, "the cached ranker survived"

    # The next query builds a fresh one, and scoring still answers the same.
    assert ranker("battery") is not held
    assert score("battery_full", "battery") == SCORE_PREFIX
    print("PASS: the scoring cache is released on demand")


def test_a_word_written_with_a_separator() -> None:
    """Retry missed tokens against a separator-free name at its earned rung."""
    for name in ("wi-fi", "wi_fi", "wi.fi"):
        assert score(name, "wifi") == SCORE_EXACT, f"{name} did not answer wifi"
    assert score("e-mail", "email") == SCORE_EXACT
    assert score("micro-sd-card", "microsd") == SCORE_PREFIX

    # The joined exact form outranks a match that starts at a word boundary.
    assert score("ho-me", "home") == SCORE_EXACT
    assert score("go-home", "home") == SCORE_WORD_PREFIX

    # Two names on one rung are ordered by length, so the shorter of a name
    # written with a separator and one written without it comes first.
    assert ordered(["wi-fi", "wifi"], "wifi") == ["wifi", "wi-fi"]

    # It buys a real match, and it costs matches that cross a word boundary:
    # "onoff" now finds balloon-off, which holds "on" and "off" as neighbours.
    assert score("balloon-off", "onoff") == SCORE_CONTAINS
    assert score("brightness", "wifi") == SCORE_NO_MATCH
    print("PASS: a word written with a separator answers the word")


def test_threshold_is_the_lowest_rung() -> None:
    """Use the contains rung as default threshold; allow stricter callers."""
    assert SEARCH_SCORE_THRESHOLD == SCORE_CONTAINS, "the search threshold moved"

    assert matches("airplay", "play")
    assert not matches("airplay", "play", threshold=SCORE_WORD_PREFIX)
    assert matches("play_arrow", "play", threshold=SCORE_WORD_PREFIX)

    # Nothing scores between a miss and the threshold, so no other cut point
    # in that range means anything.
    seen = {score(name, query)
            for query in ("battery", "play", "volume up", "mic")
            for name in BATTERY + NOT_BATTERY + ["airplay", "volume_up", "mic_off"]}
    between = {value for value in seen if SCORE_NO_MATCH < value < SCORE_CONTAINS}
    assert not between, f"scores landed between a miss and the threshold: {between}"
    print(f"PASS: the threshold is the contains rung ({SEARCH_SCORE_THRESHOLD})")


def test_battery_regression() -> None:
    """Keep long battery matches and reject unrelated short edit-ratio matches."""
    for name in BATTERY:
        assert matches(name, "battery"), f"{name} no longer answers 'battery'"
    for name in NOT_BATTERY:
        assert not matches(name, "battery"), f"{name} answers 'battery'"

    kept_by_ratio = [name for name in BATTERY
                     if fuzz.ratio(name.lower(), "battery") >= 50]
    assert len(kept_by_ratio) < len(BATTERY), (
        "the edit-ratio model kept every battery name here, so this check "
        "would prove nothing; pick longer names")
    # Representative long names score below the old edit-ratio threshold.
    assert fuzz.ratio("battery_charging_20_symbolic", "battery") < 50
    assert fuzz.ratio("battery-charging-outline", "battery") < 50
    assert score("battery_charging_20_symbolic", "battery") == SCORE_PREFIX
    assert score("battery-charging-outline", "battery") == SCORE_PREFIX
    print("PASS: every battery name answers 'battery' and no unrelated name does")


def ordered(names: list[str], query: str) -> list[str]:
    """The names a grid would show, best match first."""
    kept = [name for name in names if matches(name, query)]
    return sorted(kept, key=functools.cmp_to_key(
        lambda a, b: compare(a, b, query)))


def test_ranking_order() -> None:
    got = ordered(BATTERY, "battery")
    assert got[0] == "battery", f"the exact name is not first: {got}"
    # The prefixes come before the names that only hold the word.
    assert got.index("battery_std") < got.index("car-battery"), got
    assert got.index("battery_full") < got.index("device_battery"), got

    # Two names on one rung: the shorter goes first.
    assert ordered(["battery_charging_full", "battery_std"], "battery") == [
        "battery_std", "battery_charging_full"]

    # Two names of one length on one rung: an earlier hit goes first.
    assert ordered(["ab_play", "a_play_b"], "play") == ["a_play_b", "ab_play"]

    # A contains hit sinks below a word hit whatever the length.
    assert ordered(["airplay", "play_x"], "play") == ["play_x", "airplay"]
    print("PASS: the ranking puts the closest name first")


def test_comparator_contract() -> None:
    """GTK sorts with this, so it must hand back ints and stay antisymmetric."""
    names = ["battery", "battery_full", "car-battery", "acer"]
    for query in ("", "battery", "zzzz"):
        for name1 in names:
            for name2 in names:
                result = compare(name1, name2, query)
                assert isinstance(result, int) and not isinstance(result, bool), (
                    f"comparator returned {type(result).__name__}")
                assert result == -compare(name2, name1, query), (
                    f"{name1}/{name2} under {query!r} is not antisymmetric")
                if name1 == name2:
                    assert result == 0, f"{name1} does not tie with itself"
    # Names that rank alike tie, whatever their spelling of the separator.
    assert compare("battery-full", "battery_full", "battery") == 0
    print("PASS: the comparator returns ints and is antisymmetric")


def test_rank_key_orders_the_same_way() -> None:
    """Keep rank-key order equal to comparator order across merged packs."""
    for query in ("battery", "play", "volume up"):
        names = BATTERY + NOT_BATTERY + ["airplay", "volume_up", "volume_down"]
        kept = [name for name in names if matches(name, query)]
        by_key = sorted(kept, key=lambda name: rank_key(name, query))
        by_compare = sorted(kept, key=functools.cmp_to_key(
            lambda a, b: compare(a, b, query)))
        assert by_key == by_compare, f"{query!r}: {by_key} != {by_compare}"
    print("PASS: sorting by key gives the comparator's order")


def test_ranker_is_reused_and_bounded() -> None:
    first = ranker("battery")
    assert ranker("battery") is first, "the ranker of one query is rebuilt per call"
    assert ranker("battery ") is not first, "a changed query kept the old ranker"

    # A ranker of its own needs no cache and answers the same.
    own = QueryRanker("battery")
    for name in BATTERY + NOT_BATTERY:
        assert own.score(name) == score(name, "battery")
        assert own.matches(name) == matches(name, "battery")

    # The memo has an end. Nothing in the window scores that many names, so
    # this check lowers the limit rather than building 50000 of them.
    limit = asset_search._MEMO_LIMIT
    try:
        asset_search._MEMO_LIMIT = 8
        bounded = QueryRanker("battery")
        for index in range(40):
            bounded.rank_key(f"battery_{index}")
            assert len(bounded._keys) <= 8, (
                f"the memo grew to {len(bounded._keys)} past its limit of 8")
    finally:
        asset_search._MEMO_LIMIT = limit
    assert asset_search._MEMO_LIMIT == limit
    print("PASS: one ranker serves a query, and its memo has an end")


def test_module_stays_headless() -> None:
    """Keep the shared scoring module free of GTK and application globals."""
    path = os.path.join(REPO_ROOT, "src", "windows", "AssetManager", "asset_search.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), path)

    banned = {"gi", "gi.repository", "globals", "GtkHelper"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offences = sorted(name for name in imported
                      if name in banned or name.split(".")[0] in banned)
    assert not offences, (
        f"asset_search.py imports {offences}; the scoring module must stay "
        f"free of GTK and of the globals it drags in")
    assert imported, "the import scan found nothing, so it checks nothing"
    print(f"PASS: the scoring module imports only {sorted(imported)}")


def main() -> int:
    fixtures.start_watchdog(60, label="scenario_asset_search_ladder")

    test_normalize_folds_separators()
    test_ladder_rungs()
    test_tokens_are_an_and()
    test_empty_query_keeps_everything()
    test_empty_query_costs_nothing()
    test_cache_is_released_on_demand()
    test_a_word_written_with_a_separator()
    test_threshold_is_the_lowest_rung()
    test_battery_regression()
    test_ranking_order()
    test_comparator_contract()
    test_rank_key_orders_the_same_way()
    test_ranker_is_reused_and_bounded()
    test_module_stays_headless()

    print("ALL PASS: scenario_asset_search_ladder")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
