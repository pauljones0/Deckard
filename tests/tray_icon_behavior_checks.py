"""Tray object-path and host icon-search checks."""

import os
import tempfile

import appinfo
from src.tray import TrayIcon, icon_search_roots, tray_icon_theme_path

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def check_item_and_menu_take_their_own_paths() -> None:
    tray = TrayIcon()
    sni = tray.sni_service

    assert TrayIcon.IndicatorPath != TrayIcon.MenuPath
    assert sni.dbus_path == TrayIcon.IndicatorPath, (
        f"the tray item took {sni.dbus_path!r}, not {TrayIcon.IndicatorPath!r}"
    )
    assert sni.object_path == TrayIcon.IndicatorPath, (
        f"the tray item registered at {sni.object_path!r}"
    )
    assert sni._menu.dbus_path == TrayIcon.MenuPath, (
        f"the tray menu took {sni._menu.dbus_path!r}, not {TrayIcon.MenuPath!r}"
    )
    assert sni._menu.object_path == TrayIcon.MenuPath, (
        f"the tray menu registered at {sni._menu.object_path!r}"
    )
    assert sni.Menu == TrayIcon.MenuPath, (
        f"the Menu property points at {sni.Menu!r}, not {TrayIcon.MenuPath!r}"
    )
    assert sni.Id == TrayIcon.AppId, (
        f"the item id is {sni.Id!r}, not {TrayIcon.AppId!r}"
    )
    assert sni.Title == "Deckard", f"the item title is {sni.Title!r}"
    print("PASS: the tray item and its menu take their own D-Bus paths")


def check_icon_theme_path_only_when_the_host_lacks_the_icon() -> None:
    bundled = os.path.join(REPO_ROOT, "Assets", "icons")
    saved = {
        name: os.environ.get(name)
        for name in ("XDG_DATA_HOME", "XDG_DATA_DIRS")
    }

    def restore() -> None:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    try:
        os.environ.pop("XDG_DATA_HOME", None)
        os.environ.pop("XDG_DATA_DIRS", None)
        assert "/usr/share" in icon_search_roots(), icon_search_roots()

        os.environ["XDG_DATA_DIRS"] = os.pathsep.join(("share", "", "/usr/share"))
        assert icon_search_roots()[1:] == ["/usr/share"], icon_search_roots()
        os.environ.pop("XDG_DATA_DIRS", None)

        with tempfile.TemporaryDirectory(prefix="sc_tray_xdg_") as root:
            data_home = os.path.join(root, "home")
            data_dir = os.path.join(root, "system")
            os.makedirs(data_home)
            os.makedirs(data_dir)
            os.environ["XDG_DATA_HOME"] = data_home
            os.environ["XDG_DATA_DIRS"] = data_dir

            assert tray_icon_theme_path(appinfo.APP_ID, REPO_ROOT) == bundled
            tray = TrayIcon()
            assert tray.sni_service.IconThemePath == bundled
            assert tray.sni_service.IconName == appinfo.APP_ID

            installed = os.path.join(
                data_dir, "icons", "hicolor", "256x256", "apps"
            )
            os.makedirs(installed)
            icon_file = os.path.join(installed, f"{appinfo.APP_ID}.png")
            with open(icon_file, "wb") as file:
                file.write(b"")
            assert tray_icon_theme_path(appinfo.APP_ID, REPO_ROOT) == ""
            tray = TrayIcon()
            assert tray.sni_service.IconThemePath == ""
            os.remove(icon_file)

            for relative in (
                ("icons", "hicolor", "scalable", "apps", f"{appinfo.APP_ID}.svg"),
                ("pixmaps", f"{appinfo.APP_ID}.png"),
            ):
                path = os.path.join(data_home, *relative)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as file:
                    file.write(b"")
                assert tray_icon_theme_path(appinfo.APP_ID, REPO_ROOT) == ""
                os.remove(path)

            odd_home = os.path.join(root, "we[i]rd")
            odd_apps = os.path.join(
                odd_home, "icons", "hicolor", "48x48", "apps"
            )
            os.makedirs(odd_apps)
            odd_icon = os.path.join(odd_apps, f"{appinfo.APP_ID}.png")
            with open(odd_icon, "wb") as file:
                file.write(b"")
            os.environ["XDG_DATA_HOME"] = odd_home
            assert tray_icon_theme_path(appinfo.APP_ID, REPO_ROOT) == ""
            odd_name = "we[i]rd-icon"
            with open(os.path.join(odd_apps, f"{odd_name}.png"), "wb") as file:
                file.write(b"")
            assert tray_icon_theme_path(odd_name, REPO_ROOT) == ""
            os.environ["XDG_DATA_HOME"] = data_home

            other = os.path.join(installed, "some.other.App.png")
            os.makedirs(installed, exist_ok=True)
            with open(other, "wb") as file:
                file.write(b"")
            assert tray_icon_theme_path(appinfo.APP_ID, REPO_ROOT) == bundled
    finally:
        restore()
    print("PASS: the tray names an icon theme path only when the host theme "
          "lacks the app icon")
