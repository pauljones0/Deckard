"""Static and GTK icon-theme checks for the tray scenario."""

import configparser
import os
import re

import gi

import appinfo

gi.require_version("Gtk", "4.0")

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BUNDLED_ICON_DIR = os.path.join(REPO_ROOT, "Assets", "icons")
ICON_THEME_DIR = os.path.join(BUNDLED_ICON_DIR, "hicolor")
FLATPAK_MANIFEST = os.path.join(REPO_ROOT, f"{appinfo.APP_ID}.yml")
ICON_SUFFIXES = (".png", ".svg", ".xpm")


def shipped_icon_dirs() -> set[str]:
    found = set()
    for dirpath, _dirnames, filenames in os.walk(ICON_THEME_DIR):
        if any(name.endswith(ICON_SUFFIXES) for name in filenames):
            found.add(os.path.relpath(dirpath, ICON_THEME_DIR))
    return found


def check_shipped_icon_theme_resolves() -> None:
    from gi.repository import Gtk

    index_path = os.path.join(ICON_THEME_DIR, "index.theme")
    assert os.path.isfile(index_path), f"{index_path} is missing"

    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(index_path, encoding="utf-8")

    assert parser.has_section("Icon Theme"), (
        f"{index_path} has no [Icon Theme] section"
    )
    assert parser.get("Icon Theme", "Name", fallback="") != "", (
        f"{index_path} has no Name"
    )

    listed = [
        entry.strip()
        for entry in parser.get(
            "Icon Theme", "Directories", fallback=""
        ).split(",")
        if entry.strip() != ""
    ]
    assert listed, f"{index_path} lists no Directories"

    shipped = shipped_icon_dirs()
    missing = sorted(shipped - set(listed))
    assert not missing, f"{index_path} does not list {missing}"

    for entry in listed:
        entry_path = os.path.join(ICON_THEME_DIR, *entry.split("/"))
        assert os.path.isdir(entry_path), (
            f"{index_path} lists {entry!r}, which is not a directory"
        )
        assert parser.has_section(entry), (
            f"{index_path} lists {entry!r} with no [{entry}] group"
        )
        for key in ("Size", "Type", "Context"):
            assert parser.get(entry, key, fallback="") != "", (
                f"[{entry}] in {index_path} has no {key}"
            )

    theme = Gtk.IconTheme.new()
    theme.set_search_path([BUNDLED_ICON_DIR])
    theme.set_theme_name("hicolor")
    assert theme.has_icon(appinfo.APP_ID), (
        f"{appinfo.APP_ID} does not resolve under {BUNDLED_ICON_DIR}"
    )
    print(f"PASS: the shipped icon theme resolves {appinfo.APP_ID} and lists "
          f"all {len(shipped)} icon dirs")


def check_flatpak_icon_install() -> None:
    with open(FLATPAK_MANIFEST, encoding="utf-8") as manifest_file:
        manifest = manifest_file.read()
    pattern = (
        r"/app/share/icons/hicolor/[^/\s]+/apps/"
        + re.escape(appinfo.APP_ID)
        + r"\.(?:png|svg|xpm)\b"
    )
    assert re.search(pattern, manifest), (
        f"{FLATPAK_MANIFEST} installs no {appinfo.APP_ID} icon under "
        "/app/share/icons/hicolor/<size>/apps/"
    )
    print("PASS: the flatpak manifest installs the app icon where the icon "
          "search reads it")
