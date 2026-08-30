"""One locale-selection policy for every locale manager.

This module is the single owner of the selection ladder: an exact match
wins, then the first available locale sharing the primary language code,
then the fallback. The managers are storage adapters (CSV and
per-language JSON) and delegate their selection here, so the policy
cannot drift between them.
"""
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
