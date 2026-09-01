"""Verify the localized deck-background extend-to-touchscreen label.
LocaleManager.get returns a missing key before fallback, which exposes the raw key in the UI."""

# The background-group key must exist for every shipped locale, or get() exposes the raw key.
import os

import fixtures  # noqa: F401  (isolated --data tempdir; import first)

from locales.LocaleManager import LocaleManager


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(REPO_ROOT, "locales", "locales.csv")

EXTEND_BACKGROUND_KEY = "deck.background-group.extend-background-to-touchscreen"


def test_key_resolves_to_a_sentence() -> None:
    lm = LocaleManager(CSV_PATH)
    for language in lm.available_locales:
        lm.set_language(language)
        value = lm.get(EXTEND_BACKGROUND_KEY)
        assert value != EXTEND_BACKGROUND_KEY, (
            f"{language}: the label renders the raw key, so the CSV row is missing"
        )
        assert value.strip(), f"{language}: the label must not be empty"
        assert " " in value, f"{language}: the label must be a sentence, got {value!r}"


def test_every_shipped_locale_is_filled() -> None:
    lm = LocaleManager(CSV_PATH)
    row = lm.locale_data.get(EXTEND_BACKGROUND_KEY)
    assert row is not None, "the CSV must carry a row for the label"
    assert len(lm.available_locales) >= 5, (
        f"expected at least the five shipped locales, found {lm.available_locales}"
    )
    for language in lm.available_locales:
        assert row.get(language), f"{language} has no translation for the label"


def test_background_group_uses_locale_keys() -> None:
    source_path = os.path.join(
        REPO_ROOT, "src", "windows", "mainWindow", "elements", "DeckSettings",
        "BackgroundGroup.py",
    )
    with open(source_path) as source_file:
        source = source_file.read()
    assert f'gl.lm.get("{EXTEND_BACKGROUND_KEY}")' in source, (
        "the background group must look the label up by its locale key"
    )
    assert "Extend Background To Touchscreen" not in source, (
        "the raw sentence-case key must be gone from the call site"
    )
    assert 'gl.lm.get("deck.background-group.media-select-label")' in source, (
        "the media-select label must ask for the background group's own key, "
        "not the deck group's"
    )


APOSTROPHE_KEY = "background-editor.color.dialog.title"

# Split keys with markup characters by renderer: markup consumers need escapes to parse,
# while plain renderers need raw characters to avoid showing escape sequences.
MARKUP_CONSUMED_KEYS = {
    "settings.performance.header",   # Adw.PreferencesGroup title, markup
    "onboarding.extension.hint",     # Gtk.Label with use_markup
}
PLAIN_CONSUMED_KEYS = {
    "onboarding.productive.details",  # Gtk.Label without use_markup
}

MARKUP_CHARS = ("&", "<", ">")


def test_plain_lookup_preserves_apostrophe() -> None:
    lm = LocaleManager(CSV_PATH)
    lm.set_language("fr_FR")
    value = lm.get(APOSTROPHE_KEY)
    assert "'" in value, f"the French label lost its apostrophe: {value!r}"
    for entity in ("&#x27;", "&#39;", "&apos;", "&quot;", "&amp;"):
        assert entity not in value, (
            f"the plain lookup escaped the label, so a Gtk.Label renders "
            f"{entity} literally: {value!r}"
        )


def test_markup_lookup_escapes_only_what_pango_needs() -> None:
    lm = LocaleManager(CSV_PATH)
    lm.set_language("fr_FR")
    assert lm.get_markup(APOSTROPHE_KEY) == lm.get(APOSTROPHE_KEY), (
        "an apostrophe is legal in Pango markup, so the markup lookup must "
        "leave it alone"
    )
    lm.set_language("en_US")
    plain = lm.get("settings.performance.header")
    markup = lm.get_markup("settings.performance.header")
    assert "&" in plain and "&amp;" not in plain, (
        f"the fixture key must carry a bare ampersand, got {plain!r}"
    )
    assert "&amp;" in markup, (
        f"the markup lookup must escape the ampersand, got {markup!r}"
    )


def test_markup_keys_use_matching_lookup() -> None:
    lm = LocaleManager(CSV_PATH)
    markup_keys = {
        key
        for key, row in lm.locale_data.items()
        for value in row.values()
        if value and any(char in value for char in MARKUP_CHARS)
    }
    assert markup_keys == MARKUP_CONSUMED_KEYS | PLAIN_CONSUMED_KEYS, (
        f"a translation gained or lost a markup character: {markup_keys!r}. Look "
        f"at the call site, pick get or get_markup by the renderer, then list "
        f"the key in the matching set here."
    )
    sources = []
    for directory, _subdirs, names in os.walk(os.path.join(REPO_ROOT, "src")):
        for name in names:
            if name.endswith(".py"):
                with open(os.path.join(directory, name)) as source_file:
                    sources.append(source_file.read())
    blob = "\n".join(sources)
    for key in MARKUP_CONSUMED_KEYS:
        if f'"{key}"' not in blob:
            continue
        assert f'get_markup("{key}")' in blob, (
            f"{key} holds a markup character but its call site uses the plain "
            f"lookup, so the markup parse fails"
        )
    for key in PLAIN_CONSUMED_KEYS:
        assert f'get_markup("{key}")' not in blob, (
            f"{key} reaches a plain renderer, so the escaping lookup would "
            f"put an escape sequence on screen"
        )


def test_markup_consumer_renders_escaped_text() -> None:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    if not Gtk.init_check():
        print("scenario_background_extend_locale: no display, markup render skipped")
        return

    lm = LocaleManager(CSV_PATH)
    lm.set_language("en_US")
    key = "settings.performance.header"
    label = Gtk.Label(label=lm.get_markup(key), use_markup=True)
    assert label.get_text() == lm.get(key), (
        f"the markup label parsed to {label.get_text()!r}, not to the "
        f"translation {lm.get(key)!r}"
    )

    plain = Gtk.Label(label=lm.get(APOSTROPHE_KEY))
    lm.set_language("fr_FR")
    plain.set_label(lm.get(APOSTROPHE_KEY))
    assert "'" in plain.get_text(), (
        f"the plain label shows an escape sequence: {plain.get_text()!r}"
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_background_extend_locale")
    test_key_resolves_to_a_sentence()
    test_every_shipped_locale_is_filled()
    test_background_group_uses_locale_keys()
    test_plain_lookup_preserves_apostrophe()
    test_markup_lookup_escapes_only_what_pango_needs()
    test_markup_keys_use_matching_lookup()
    test_markup_consumer_renders_escaped_text()
    print("scenario_background_extend_locale: PASS")


if __name__ == "__main__":
    main()
