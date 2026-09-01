"""Verify that store information rows do not shadow inherited widget members."""

# Class dictionaries and isolated rows verify the names without realizing a window.
import fixtures  # noqa: F401  (isolated --data tempdir; import first)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from GtkHelper.GtkHelper import AttributeRow  # noqa: E402
from src.windows.Store.InfoPage import DescriptionRow, InfoPage  # noqa: E402


def test_inherited_setters_are_not_shadowed() -> None:
    assert "set_name" not in vars(InfoPage), (
        "InfoPage must not define set_name; it shadows Gtk.Widget.set_name"
    )
    assert "set_title" not in vars(DescriptionRow), (
        "DescriptionRow must not define set_title; it shadows the row setter"
    )
    assert "set_title" not in vars(AttributeRow), (
        "AttributeRow must not define set_title; it shadows the row setter"
    )
    assert InfoPage.set_name is Gtk.Widget.set_name, (
        "the inherited widget name setter must stay reachable"
    )
    assert DescriptionRow.set_title is Adw.PreferencesRow.set_title, (
        "the inherited row title setter must stay reachable"
    )
    assert AttributeRow.set_title is Adw.PreferencesRow.set_title, (
        "the inherited row title setter must stay reachable"
    )


def test_renamed_setters_exist() -> None:
    assert callable(vars(InfoPage).get("set_asset_name")), (
        "InfoPage must expose the renamed pack-name setter"
    )
    assert callable(vars(DescriptionRow).get("set_description_title")), (
        "DescriptionRow must expose the renamed title setter"
    )
    assert callable(vars(AttributeRow).get("set_attribute_title")), (
        "AttributeRow must expose the renamed title setter"
    )
    assert callable(vars(AttributeRow).get("set_attribute")), (
        "AttributeRow must expose the renamed value setter"
    )


def test_rows_store_caption_in_distinct_attribute() -> None:
    attribute_row = AttributeRow(title="Name:", attr="Error")
    assert attribute_row.title_label.get_label() == "Name:"
    assert attribute_row.attribute_label.get_label() == "Error"
    assert "title" not in vars(attribute_row), (
        "AttributeRow must not hold its caption in title; the row inherits that name"
    )
    assert vars(attribute_row).get("title_str") == "Name:"
    assert attribute_row.get_title() == "", (
        "the inherited title must stay empty until a caller sets it"
    )

    description_row = DescriptionRow(title="Description:", desc="N/A")
    assert description_row.title_label.get_label() == "Description:"
    assert description_row.description_label.get_label() == "N/A"
    assert "title" not in vars(description_row), (
        "DescriptionRow must not hold its caption in title; the row inherits that name"
    )
    assert vars(description_row).get("title_str") == "Description:"
    assert "desc" not in vars(description_row), (
        "DescriptionRow must hold its body text in desc_str"
    )
    assert description_row.get_title() == "", (
        "the inherited title must stay empty until a caller sets it"
    )


def test_renamed_setters_update_existing_labels() -> None:
    attribute_row = AttributeRow(title="Name:", attr="Error")
    attribute_row.set_attribute_title("Author:")
    attribute_row.set_attribute("core447")
    assert attribute_row.title_label.get_label() == "Author:"
    assert attribute_row.attribute_label.get_label() == "core447"
    attribute_row.set_attribute(None)
    assert attribute_row.attribute_label.get_label() == "N/A"

    # The inherited setter must reach the GObject property again, and must
    # leave the drawn labels alone.
    attribute_row.set_title("inherited")
    assert attribute_row.get_title() == "inherited"
    assert attribute_row.title_label.get_label() == "Author:"

    description_row = DescriptionRow(title="Description:", desc="N/A")
    description_row.set_description_title("License Description:")
    description_row.set_description("GPL-3.0")
    assert description_row.title_label.get_label() == "License Description:"
    assert description_row.description_label.get_label() == "GPL-3.0"
    description_row.set_description(None)
    assert description_row.description_label.get_label() == "N/A"

    description_row.set_title("inherited")
    assert description_row.get_title() == "inherited"
    assert description_row.title_label.get_label() == "License Description:"


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_info_page_shadowing")
    test_inherited_setters_are_not_shadowed()
    test_renamed_setters_exist()
    test_rows_store_caption_in_distinct_attribute()
    test_renamed_setters_update_existing_labels()
    print("scenario_store_info_page_shadowing: PASS")


if __name__ == "__main__":
    main()
