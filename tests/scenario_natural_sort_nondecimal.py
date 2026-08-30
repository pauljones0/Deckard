"""Keep non-decimal digit characters as text in natural-sort keys."""

# Convert only decimal runs accepted by int().
# All sort helpers and the page selector use this shared key builder.
import fixtures  # noqa: F401  (isolated --data tempdir; import first)

from src.backend.DeckManagement.HelperMethods import (
    natural_keys,
    natural_sort,
    natural_sort_by_filenames,
)


# Every one of these is true for str.isdigit() but raises in int().
NON_DECIMAL_DIGITS = ["²", "³", "¹", "⁴", "₅"]


def test_keys_do_not_raise() -> None:
    for char in NON_DECIMAL_DIGITS:
        for name in (f"page{char}", char, f"{char}2{char}", f"a{char}b"):
            key = natural_keys(name)
            assert isinstance(key, list), f"{name!r} must yield a list key"
            for part in key:
                assert isinstance(part, (int, str)), (
                    f"{name!r} yielded an unexpected key part {part!r}"
                )


def test_key_shape_unchanged() -> None:
    assert natural_keys("page10") == ["page", 10, ""], "digit runs stay integers"
    assert natural_keys("Page2b") == ["page", 2, "b"], "text runs stay lowercased"
    assert natural_keys("") == [""], "an empty name yields one empty text run"
    # The superscript never becomes an integer, so it compares as text.
    assert natural_keys("page²") == ["page²"], (
        "a non-decimal digit stays in the text run"
    )


def test_sorting_survives_and_stays_stable() -> None:
    plain = ["page10", "page2", "page1", "Page20"]
    assert natural_sort(plain) == ["page1", "page2", "page10", "Page20"], (
        "plain names keep their numeric ordering"
    )

    mixed = ["page10", "page²", "page2", "page1"]
    ordered = natural_sort(mixed)
    assert set(ordered) == set(mixed), "sorting must not drop or invent names"
    assert [n for n in ordered if n != "page²"] == ["page1", "page2", "page10"], (
        "plain names keep their relative order next to a superscript name"
    )

    paths = ["/a/page10.json", "/b/page².json", "/c/page2.json"]
    by_file = natural_sort_by_filenames(paths)
    assert set(by_file) == set(paths), "path sorting must not drop or invent paths"


def test_page_selector_comparator() -> None:
    # Superscript runs stay text and sort after converted numeric runs.
    ordered = sorted(["page²", "page2", "page10"], key=natural_keys)
    assert ordered == ["page2", "page10", "page²"], (
        f"superscript names must sort as text after numbered ones: {ordered}")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_natural_sort_nondecimal")
    test_keys_do_not_raise()
    test_key_shape_unchanged()
    test_sorting_survives_and_stays_stable()
    test_page_selector_comparator()
    print("scenario_natural_sort_nondecimal: PASS")


if __name__ == "__main__":
    main()
