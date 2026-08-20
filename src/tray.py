import os
from typing import TYPE_CHECKING, Any
from loguru import logger as log
import appinfo
import globals as gl
from src.backend.trayicon import DBusTrayIcon, DBusMenu

if TYPE_CHECKING:
    from gi.repository import Gio

    from src.windows.mainWindow.mainWindow import MainWindow

class TrayIcon(DBusTrayIcon):
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
        super().__init__(self.menu, self.MenuPath, self.IndicatorPath, self.AppId, "Deckard")
        icon_theme_path = os.path.join(gl.MAIN_PATH, "Assets", "icons")
        self.set_icon(appinfo.APP_ID, path=icon_theme_path)
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
