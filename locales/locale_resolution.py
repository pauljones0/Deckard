"""Shared locale-selection policy for CSV and per-language JSON managers.
Choose an exact match, then the first locale with the same primary language, then the fallback."""
import locale
from collections.abc import Iterable


def resolve_best_match(preferred: str, available: Iterable[str], fallback: str) -> str:
    """The available locale that best serves the preferred language."""
    candidates = list(available)
    if preferred in candidates:
        return preferred
    # The primary language code, e.g. en for en_US: a sibling region beats
    # the fallback language.
    primary = preferred.split("_")[0]
    for language in candidates:
        if language.startswith(primary):
            return language
    return fallback


def os_default_language(fallback: str) -> str:
    """The OS locale, or the fallback when the OS reports none."""
    os_locale = locale.getlocale()[0]
    return fallback if os_locale is None else os_locale
