"""
Regression test for the store info page overriding inherited widget setters.

InfoPage defined set_name, which GTK already defines on every widget, and
DescriptionRow defined set_title, which Adw.PreferencesRow already defines.
Either override silently changes what the inherited call does.
"""

# The two setters now carry intention-revealing names, so the inherited GTK
# methods stay reachable. The checks read the class dictionaries, so no window
# is realized and no store data is fetched.
import fixtures  # noqa: F401  (isolated --data tempdir; import first)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from src.windows.Store.InfoPage import DescriptionRow, InfoPage  # noqa: E402


def test_inherited_setters_are_not_shadowed() -> None:
    assert "set_name" not in vars(InfoPage), (
        "InfoPage must not define set_name; it shadows Gtk.Widget.set_name"
    )
    assert "set_title" not in vars(DescriptionRow), (
        "DescriptionRow must not define set_title; it shadows the row setter"
    )
    assert InfoPage.set_name is Gtk.Widget.set_name, (
        "the inherited widget name setter must stay reachable"
    )
    assert DescriptionRow.set_title is Adw.PreferencesRow.set_title, (
        "the inherited row title setter must stay reachable"
    )


def test_renamed_setters_exist() -> None:
    assert callable(vars(InfoPage).get("set_pack_name")), (
        "InfoPage must expose the renamed pack-name setter"
    )
    assert callable(vars(DescriptionRow).get("set_description_title")), (
        "DescriptionRow must expose the renamed title setter"
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_info_page_shadowing")
    test_inherited_setters_are_not_shadowed()
    test_renamed_setters_exist()
    print("scenario_store_info_page_shadowing: PASS")


if __name__ == "__main__":
    main()
