"""Select exact, language-sibling, then fallback locales, including empty availability.
OS absence falls back; both managers delegate matches, while only legacy set resolves."""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import json
import os
from types import SimpleNamespace

from locales import locale_resolution
from locales.LegacyLocaleManager import LegacyLocaleManager
from locales.LocaleManager import LocaleManager
from locales.locale_resolution import os_default_language, resolve_best_match


def check_policy_ladder() -> None:
    available = ["en_US", "de_DE", "fr_FR"]
    assert resolve_best_match("de_DE", available, "en_US") == "de_DE", (
        "an exact match must win")
    assert resolve_best_match("de_AT", available, "en_US") == "de_DE", (
        "a sibling of the primary language code must beat the fallback")
    assert resolve_best_match("pt_BR", available, "en_US") == "en_US", (
        "an unmatched language must fall back")
    assert resolve_best_match("de_DE", [], "en_US") == "en_US", (
        "an empty availability list must fall back")
    print("PASS: the policy ladder holds")


def check_os_default() -> None:
    real = locale_resolution.locale
    try:
        locale_resolution.locale = SimpleNamespace(getlocale=lambda: (None, None))
        assert os_default_language("en_US") == "en_US", (
            "no OS locale must select the fallback")
        locale_resolution.locale = SimpleNamespace(getlocale=lambda: ("de_DE", "UTF-8"))
        assert os_default_language("en_US") == "de_DE", (
            "a reported OS locale must be selected")
    finally:
        locale_resolution.locale = real
    print("PASS: the OS-default path selects fallback or the OS locale")


def _write_csv(path: str) -> None:
    with open(path, "w", newline="") as f:
        f.write("key;en_US;de_DE\n")
        f.write('greeting;hello;hallo\n')


def _write_legacy(dir_path: str) -> None:
    os.makedirs(dir_path, exist_ok=True)
    for lang, word in (("en_US", "hello"), ("de_DE", "hallo")):
        with open(os.path.join(dir_path, f"{lang}.json"), "w") as f:
            json.dump({"greeting": word}, f)


def check_managers_delegate() -> None:
    import locales.LegacyLocaleManager as legacy_module
    import locales.LocaleManager as modern_module

    csv_path = os.path.join(fixtures.DATA_DIR, "locales_test.csv")
    _write_csv(csv_path)
    modern = LocaleManager(csv_path)

    # Pin real delegation, not just matching outcomes: each manager binds
    # the resolver by name into its own module, so the spy goes there.
    calls: list[tuple] = []

    def spying(preferred, available, fallback):
        calls.append((preferred, fallback))
        return resolve_best_match(preferred, available, fallback)

    modern_real = modern_module.resolve_best_match
    modern_module.resolve_best_match = spying
    try:
        assert modern.get_best_match("de_AT") == "de_DE", (
            "the CSV manager must resolve a sibling locale through the policy")
        assert calls == [("de_AT", "en_US")], (
            "the CSV manager answered without calling the shared resolver")
        assert modern.get_best_match("pt_BR") == "en_US", (
            "the CSV manager must fall back through the policy")
    finally:
        modern_module.resolve_best_match = modern_real

    # The modern manager's set_language deliberately does not resolve.
    modern.set_language("de_AT")
    assert modern.language == "de_AT", (
        "modern set_language must assign straight through; resolution on "
        "set is the legacy manager's contract only")

    legacy_dir = os.path.join(fixtures.DATA_DIR, "legacy_locales_test")
    _write_legacy(legacy_dir)
    legacy = LegacyLocaleManager(legacy_dir)
    calls.clear()
    legacy_real = legacy_module.resolve_best_match
    legacy_module.resolve_best_match = spying
    try:
        legacy.set_language("de_AT")
        assert calls == [("de_AT", "en_US")], (
            "legacy set_language answered without calling the shared resolver")
    finally:
        legacy_module.resolve_best_match = legacy_real
    assert legacy.locales == "de_DE", (
        "legacy set_language must keep applying best-match resolution")
    assert legacy.get("greeting") == "hallo", (
        "the resolved legacy language must serve its translations")
    print("PASS: both managers delegate to the shared policy")


fixtures.start_watchdog(60, "locale resolver")
check_policy_ladder()
check_os_default()
check_managers_delegate()
print("SCENARIO PASS")
