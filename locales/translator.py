"""The translator surface the application reads through a locale manager.

LocaleManager (CSV storage) and LegacyLocaleManager (per-language JSON)
both satisfy it. Code that only translates annotates against this
protocol instead of naming the storage classes, so a plugin's manager can
be either without a union type at every use site.
"""
from typing import Protocol


class Translator(Protocol):
    def get(self, key: str, fallback: str | None = None) -> str: ...

    def set_language(self, language: str) -> None: ...

    def set_fallback_language(self, language: str) -> None: ...

    def set_to_os_default(self) -> None: ...

    def get_best_match(self, preferred_language: str) -> str: ...
