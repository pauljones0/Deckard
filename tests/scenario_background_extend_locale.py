"""
Regression test for the deck background "extend to touchscreen" label.

LocaleManager.get resolves an absent key to the key itself, before it looks at
the caller's fallback. A label whose key is missing from locales.csv therefore
renders the raw key in the UI.
"""

# The key the background group asks for must exist in locales.csv for every
# shipped locale. Removing the CSV row makes get() return the raw key, which is
# what the assertions below reject.
import os

import fixtures  # noqa: F401  (isolated --data tempdir; import first)

from locales.LocaleManager import LocaleManager


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(REPO_ROOT, "locales", "locales.csv")

KEY = "deck.background-group.extend-background-to-touchscreen"


def test_key_resolves_to_a_sentence() -> None:
    lm = LocaleManager(CSV_PATH)
    for language in lm.available_locales:
        lm.set_language(language)
        value = lm.get(KEY)
        assert value != KEY, (
            f"{language}: the label renders the raw key, so the CSV row is missing"
        )
        assert value.strip(), f"{language}: the label must not be empty"
        assert " " in value, f"{language}: the label must be a sentence, got {value!r}"


def test_every_shipped_locale_is_filled() -> None:
    lm = LocaleManager(CSV_PATH)
    row = lm.locale_data.get(KEY)
    assert row is not None, "the CSV must carry a row for the label"
    assert len(lm.available_locales) >= 5, (
        f"expected at least the five shipped locales, found {lm.available_locales}"
    )
    for language in lm.available_locales:
        assert row.get(language), f"{language} has no translation for the label"


def test_background_group_asks_for_that_key() -> None:
    source_path = os.path.join(
        REPO_ROOT, "src", "windows", "mainWindow", "elements", "DeckSettings",
        "BackgroundGroup.py",
    )
    with open(source_path) as source_file:
        source = source_file.read()
    assert f'gl.lm.get("{KEY}")' in source, (
        "the background group must look the label up by its locale key"
    )
    assert "Extend Background To Touchscreen" not in source, (
        "the raw sentence-case key must be gone from the call site"
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_background_extend_locale")
    test_key_resolves_to_a_sentence()
    test_every_shipped_locale_is_filled()
    test_background_group_asks_for_that_key()
    print("scenario_background_extend_locale: PASS")


if __name__ == "__main__":
    main()
