import os
import glob
from typing import TYPE_CHECKING, Any
from loguru import logger as log
import appinfo
import globals as gl
from src.backend.trayicon import DBusTrayIcon, DBusMenu

if TYPE_CHECKING:
    from gi.repository import Gio

    from src.windows.mainWindow.mainWindow import MainWindow

# The file types a desktop shell loads a tray icon from.
ICON_FILE_SUFFIXES = (".png", ".svg", ".xpm")


def icon_search_roots() -> list[str]:
    """The data dirs a desktop shell reads its icon themes from.

    The user dir comes first, then the system dirs, as the desktop shell
    itself orders them. A dir that is not absolute is one the spec tells
    every reader to drop, and a relative dir would search from wherever the
    app was started, so the defaults take its place.
    """
    home = os.environ.get("XDG_DATA_HOME", "")
    if not os.path.isabs(home):
        home = os.path.join(os.path.expanduser("~"), ".local", "share")
    system = [entry for entry in os.environ.get("XDG_DATA_DIRS", "").split(os.pathsep)
              if os.path.isabs(entry)]
    if not system:
        system = ["/usr/local/share", "/usr/share"]
    return [home, *system]


def host_theme_has_icon(icon_name: str) -> bool:
    """True when an install put icon_name where a desktop shell finds it.

    An application icon belongs in the hicolor fallback theme, and an older
    install puts it in the pixmaps dir, so those two dirs are the whole
    search. A miss costs little: the tray then names the icon dir that ships
    with the app.
    """
    # Only the size dir stays a pattern. A dir or an icon name can hold a
    # character that a pattern reads as a wildcard, and an unescaped one
    # would search for something else or for nothing.
    name = glob.escape(icon_name)
    for root in icon_search_roots():
        for stem in (os.path.join(glob.escape(root), "icons", "hicolor", "*", "apps", name),
                     os.path.join(glob.escape(root), "pixmaps", name)):
            for suffix in ICON_FILE_SUFFIXES:
                if glob.glob(stem + suffix):
                    return True
    return False


def tray_icon_theme_path(icon_name: str, main_path: str) -> str:
    """The icon theme dir to hand the desktop shell, or "" to name none.

    A shell that gets a theme path takes it over the icon theme the user
    picked, so an installed copy hands it nothing and keeps the icon the host
    theme already holds. A flatpak copy carries its icon in the sandbox data
    dir that the search above covers, so it hands nothing over either, and
    never a sandbox path that means nothing to a shell outside. Only a copy
    that runs from a source tree points the shell at the icons that ship with
    the app.
    """
    if host_theme_has_icon(icon_name):
        return ""
    return os.path.join(main_path, "Assets", "icons")


class TrayIcon(DBusTrayIcon):
    # The item and its menu take separate D-Bus paths. The item registers at
    # the item path and announces it to the StatusNotifierWatcher; the menu
    # registers at the menu path, which the item's Menu property carries.
    MenuPath = f"{appinfo.DBUS_OBJECT_PATH}/Menu"
    IndicatorPath = f"/org/ayatana/NotificationItem/{appinfo.DBUS_UNDERSCORE}_TrayIcon"
    AppId = f"{appinfo.APP_ID}.TrayIcon"

    def __init__(self) -> None:
        self.menu = DBusMenu()
        self.menu.add_menu_item(1, "Show Window", callback=self.on_show)
        self.menu.add_menu_item(2, menu_type="separator")
        self.menu.add_menu_item(3, "Settings", callback=self.on_settings)
        self.menu.add_menu_item(4, "Store", callback=self.on_store)
        self.menu.add_menu_item(5, "About", callback=self.on_about)
        self.menu.add_menu_item(6, menu_type="separator")
        self.menu.add_menu_item(7, "Quit", callback=self.on_quit)
        super().__init__(self.menu, path=self.IndicatorPath, menu_path=self.MenuPath,
                         app_id=self.AppId, title="Deckard")
        self.set_icon(appinfo.APP_ID, path=tray_icon_theme_path(appinfo.APP_ID, gl.MAIN_PATH))
        self.set_tooltip("Deckard")
        self.set_label("Deckard")

        # All five are filled once the app has built its window and actions.
        self.main_win: Any = None
        self.show_about_action: "Gio.SimpleAction | None" = None
        self.show_store_action: "Gio.SimpleAction | None" = None
        self.show_settings_action: "Gio.SimpleAction | None" = None
        self.quit_app_action: "Gio.SimpleAction | None" = None
        self.activate_id = -1

    @log.catch
    def initialize(self, main_win: "MainWindow") -> None:
        self.main_win = main_win
        self.show_about_action = main_win.menu_button.open_about_action
        self.show_store_action = main_win.menu_button.open_store_action
        self.show_settings_action = main_win.menu_button.open_settings_action
        self.quit_app_action = main_win.menu_button.quit_action
        show_now = gl.settings_manager.app().tray_icon
        if show_now:
            self.register()

    @log.catch
    def start(self) -> None:
        self.register()

    @log.catch
    def stop(self) -> None:
        self.unregister()

    @log.catch
    def on_show(self) -> None:
        self.main_win.present()

    @log.catch
    def on_settings(self) -> None:
        if self.show_settings_action is None:
            log.warning("Tray settings clicked before the app registered its actions")
            return
        self.show_settings_action.activate()

    @log.catch
    def on_store(self) -> None:
        if self.show_store_action is None:
            log.warning("Tray store clicked before the app registered its actions")
            return
        self.show_store_action.activate()

    @log.catch
    def on_about(self) -> None:
        self.main_win.present()
        if self.show_about_action is None:
            log.warning("Tray about clicked before the app registered its actions")
            return
        self.show_about_action.activate()

    @log.catch
    def on_quit(self) -> None:
        if self.quit_app_action is None:
            log.warning("Tray quit clicked before the app registered its actions")
            return
        self.quit_app_action.activate()
