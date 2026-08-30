"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import os
import csv
import html
from loguru import logger as log

from locales.locale_resolution import os_default_language, resolve_best_match

class LocaleManager:
    def __init__(self, csv_path: str) -> None:
        self.csv_path = csv_path
        self.language = "en_US"
        self.FALLBACK_LOCALE = "en_US"

        self.available_locales: list[str] = []
        # A locale value can be absent for a key, which the CSV loader stores
        # as None, so the fallback path below is reachable.
        self.locale_data: dict[str, dict[str, str | None]] = {}

        self.load_csv()

    def load_csv(self) -> None:
        if not os.path.exists(self.csv_path):
            log.error(f"No locales found at {self.csv_path}")
            return

        with open(self.csv_path, newline='') as csvfile:
            reader = csv.reader(csvfile, delimiter=';', quotechar='"', skipinitialspace=True)
            self.available_locales = next(reader)[1:]

            for row in reader:
                if not row:
                    continue

                translations = [value.replace('\\n', '\n') for value in row[1:]]
                self.locale_data[row[0]] = dict(zip(self.available_locales, translations))

    def set_language(self, language: str) -> None:
        self.language = language

    def set_fallback_language(self, language: str) -> None:
        self.FALLBACK_LOCALE = language

    def set_to_os_default(self) -> None:
        self.set_language(os_default_language(self.FALLBACK_LOCALE))

    def get_best_match(self, preferred_language: str) -> str:
        return resolve_best_match(
            preferred_language, self.available_locales, self.FALLBACK_LOCALE)

    def get_custom_translation(self, locale_json: dict[str, str] | None) -> str | None:
        if locale_json is None:
            return ""
        result = locale_json.get(self.language)
        if result in [None, ""]:
            return locale_json.get(self.FALLBACK_LOCALE)
        return result

    def get(self, key: str, fallback: str | None = None) -> str:
        key_dict = self.locale_data.get(key, {})

        if fallback is None:
            fallback = key

        result = key_dict.get(self.language)

        if result in [None, ""]:
            result = key_dict.get(self.FALLBACK_LOCALE, key)

        if result is None:
            result = fallback

        return result

    def get_markup(self, key: str, fallback: str | None = None) -> str:
        """Return the translation escaped for a Pango markup consumer.

        get() returns plain text, which is what a Gtk.Label, a title property
        or a tooltip renders verbatim. A caller that feeds the value into a
        markup-parsed property must escape the three markup-significant
        characters first, or a translation holding "&" makes the parse fail.
        Quotes stay literal: Pango needs them escaped only inside a tag
        attribute, and escaping them puts "&#x27;" on screen wherever the
        value reaches a plain renderer.
        """
        return html.escape(self.get(key, fallback), quote=False)
