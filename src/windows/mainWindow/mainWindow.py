"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
# Import gtk modules
import gi

from src.windows.mainWindow.elements.DeckSettingsButton import DeckSettingsButton


gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Gdk

# Import Python modules
from loguru import logger as log

# Import own modules
from src.windows.mainWindow.elements.KeepRunningDialog import KeepRunningDialog
from src.windows.mainWindow.elements.leftArea import LeftArea
from src.windows.mainWindow.elements.Sidebar.Sidebar import Sidebar
from GtkHelper.GtkHelper import get_deepest_focused_widget_with_attr
from src.windows.mainWindow.elements.NoPagesError import NoPagesError
from src.windows.mainWindow.elements.NoDecksError import NoDecksError
from src.windows.mainWindow.deckSwitcher import DeckSwitcher
from src.windows.mainWindow.elements.HeaderHamburgerMenuButton import HeaderHamburgerMenuButton
from src.backend.DeckManagement.deck_controller.controller import DeckController
from src.backend.PageManagement.Page import Page

from collections.abc import Callable
from typing import TYPE_CHECKING, cast, Any

if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckManager import DeckManager
    from src.windows.mainWindow.elements.DeckStack import DeckStack



# Import globals
from src.backend import services

import globals as gl

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, deck_manager: "DeckManager", **kwargs: Any) -> None:
        services.require_app().main_win = self
        super().__init__(**kwargs)
        self.deck_manager = deck_manager

        # Store copied stuff
        self.key_dict: "dict[Any, Any]" = {}

        # Add tasks to run if build is complete
        self.on_finished: list[Callable[[], object]] = []

        self.build()
        self.init_actions()

        self.set_size_request(800, 700)
        self.set_default_size(1400, 900)
        self.connect("close-request", self.on_close)

        display = Gdk.Display.get_default()
        if display is None:
            raise RuntimeError("there is no default display to take a clipboard from")
        self.key_clipboard: Gdk.Clipboard = display.get_clipboard()

        if gl.argparser.parse_args().devel:
            self.add_css_class("devel")

    def on_close(self, *args: object, **kwargs: object) -> bool:
        keep_running = gl.settings_manager.app().keep_running
        if keep_running is None:
            dialog = KeepRunningDialog(self, self.on_close)
            dialog.present()
        else:
            # self._on_close(keep_running)
            self.hide()
            if not keep_running and gl.app is not None:
                GLib.idle_add(gl.app.on_quit)

        return True

    def _on_close(self, keep_running: bool) -> bool:
        self.hide()
        if not keep_running and gl.app is not None:
            gl.app.on_quit()
        return True

    @log.catch
    def build(self) -> None:
        #TODO: Put the objects in classes
        log.trace("Building main window")
        self.split_view = Adw.NavigationSplitView()
        self.set_content(self.split_view)

        self.content_page = Adw.NavigationPage(title="Deckard")
        self.split_view.set_content(self.content_page)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.content_page.set_child(self.main_box)

        # Add a main stack containing the normal ui and error pages
        self.main_stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.main_box.append(self.main_stack)

        # Add the main stack as the content widget of the split view
        # set_content above binds the page; this selects content when collapsed.
        # Do not pass a page here because the property expects a boolean.
        self.split_view.set_show_content(True)

        # Main toast
        self.toast_overlay = Adw.ToastOverlay()
        self.main_stack.add_titled(self.toast_overlay, "main", "Main")

        # Add a box for the main content (right side)
        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.toast_overlay.set_child(self.content_box)

        self.leftArea = LeftArea(self, deck_manager=self.deck_manager, margin_end=3, width_request=500, margin_bottom=10)
        self.content_box.append(self.leftArea)

        self.sidebar = Sidebar(main_window=self, margin_start=4, width_request=300, margin_end=4)
        # self.mainPaned.set_end_child(self.sidebar)
        self.split_view.set_sidebar(self.sidebar)
        self.split_view.set_sidebar_width_fraction(0.4)
        self.split_view.set_min_sidebar_width(450)
        self.split_view.set_max_sidebar_width(600)

        # Add header
        self.header = Adw.HeaderBar(css_classes=["flat"], show_back_button=False)
        self.main_box.prepend(self.header)

        # Add deck switcher to the header bar
        self.deck_switcher = DeckSwitcher(self)
        self.deck_switcher.switcher.set_stack(self.leftArea.deck_stack)
        self.header.set_title_widget(self.deck_switcher)

        # Add menu button to the header bar
        self.menu_button = HeaderHamburgerMenuButton(main_window=self)
        self.header.pack_end(self.menu_button)

        # Deck settings button
        self.deck_settings_button = DeckSettingsButton(main_window=self)
        self.header.pack_start(self.deck_settings_button)

        # Add sidebar toggle button to the header bar
        self.sidebar_toggle_button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", active=True)
        self.sidebar_toggle_button.connect("toggled", self.on_toggle_sidebar)
        # self.header.pack_start(self.sidebar_toggle_button)


        # Error pages
        self.no_pages_error = NoPagesError()
        self.main_stack.add_titled(self.no_pages_error, "no-pages-error", "No Pages Error")

        self.no_decks_error = NoDecksError()
        self.main_stack.add_titled(self.no_decks_error, "no-decks-error", "No Decks Error")

        self.do_after_build_tasks()
        self.check_for_errors()

        gl.tray_icon.initialize(self)
        

    def on_toggle_sidebar(self, button: Gtk.Button) -> None:
        # The toggle is inert. Collapsing the split view from here fought the
        # width handling that sizes the sidebar.
        return

    def init_actions(self) -> None:
        # Copy paste actions
        self.copy_action = Gio.SimpleAction.new("copy", None)
        self.cut_action = Gio.SimpleAction.new("cut", None)
        self.paste_action = Gio.SimpleAction.new("paste", None)
        # Do not use self.remove_action because Gio.ActionMap already defines that method.
        # Binding over it would make the window method uncallable.
        self.remove_input_action = Gio.SimpleAction.new("remove", None)

        # Connect actions
        self.copy_action.connect("activate", self.on_copy)
        self.cut_action.connect("activate", self.on_cut)
        self.paste_action.connect("activate", self.on_paste)
        self.remove_input_action.connect("activate", self.on_remove)

        # Set accels
        app = services.require_app()
        app.set_accels_for_action("win.copy", ["<Primary>c"])
        app.set_accels_for_action("win.cut", ["<Primary>x"])
        app.set_accels_for_action("win.paste", ["<Primary>v"])
        app.set_accels_for_action("win.remove", ["Delete"])
        self.add_accel_actions()


    def add_accel_actions(self) -> None:
        # Do not install window accelerator actions.
        # KeyButton, Dial, and ScreenBar serve the same keys through their own shortcut controllers.
        return

    def remove_accel_actions(self) -> None:
        return


    def change_ui_to_no_connected_deck(self) -> None:
        if not hasattr(self, "leftArea"):
            self.add_on_finished(self.change_ui_to_no_connected_deck)
            return
        
        self.leftArea.show_no_decks_error()

    def change_ui_to_connected_deck(self) -> None:
        if not hasattr(self, "leftArea"):
            self.add_on_finished(self.change_ui_to_connected_deck)
            return
        
        self.leftArea.hide_no_decks_error()
        self.deck_switcher.set_show_switcher(True)

    def set_main_error(self, error: str | None=None) -> None:
        """
        error: str
            no-decks: Shows the no decks available error
            no-pages: Shows the no pages available error
            None: Goes back to normal mode
        """
        # Safe to call from any thread. One idle for the whole transition, so
        # the window can't be observed halfway between the two modes.
        GLib.idle_add(self._apply_main_error, error)

    def _apply_main_error(self, error: str | None=None) -> None:
        if error is None:
            self.main_stack.set_visible_child(self.toast_overlay)
            self.deck_switcher.set_show_switcher(True)
            self.split_view.set_collapsed(False)
            self.sidebar_toggle_button.set_visible(True)
            self.menu_button.set_optional_actions_state(True)
            self.deck_settings_button.set_visible(True)
            return

        elif error == "no-decks":
            self.main_stack.set_visible_child(self.no_decks_error)
            self.deck_switcher.set_label_text(gl.lm.get("deck-switcher-no-decks"))

        elif error == "no-pages":
            self.main_stack.set_visible_child(self.no_pages_error)
            self.deck_switcher.set_label_text(gl.lm.get("errors.no-page.header"))

        self.deck_switcher.set_show_switcher(False)
        self.sidebar_toggle_button.set_visible(False)
        self.menu_button.set_optional_actions_state(False)
        self.split_view.set_collapsed(True)
        self.deck_settings_button.set_visible(False)

    def check_for_errors(self) -> None:
        if not services.require_deck_manager().deck_controller:
            self.set_main_error("no-decks")

        elif not services.require_page_manager().get_page_names(add_custom_pages=False):
            self.set_main_error("no-pages")

        else:
            self.set_main_error(None)

    def add_on_finished(self, task: Callable[[], object]) -> None:
        if not callable(task):
            # Plugins call this untyped, so the runtime check stays even
            # though the annotation reads it as impossible.
            return
        if task in self.on_finished:
            return
        self.on_finished.append(task)


    def reload_sidebar(self) -> None:
        if not hasattr(self, "sidebar"):
            self.add_on_finished(self.reload_sidebar)
            return
        
        self.sidebar.update()

    def do_after_build_tasks(self) -> None:
        for task in self.on_finished:
            if callable(task):
                task()

    def on_copy(self, *args: object) -> bool:
        child = get_deepest_focused_widget_with_attr(self, "on_copy")
        if child is not None and hasattr(child, "on_copy"):
            cast("Callable[[], object]", child.on_copy)()

        return False

    def on_cut(self, *args: object) -> bool:
        child = get_deepest_focused_widget_with_attr(self, "on_cut")
        if child is not None and hasattr(child, "on_cut"):
            cast("Callable[[], object]", child.on_cut)()

        return False

    def on_paste(self, *args: object) -> bool:
        child = get_deepest_focused_widget_with_attr(self, "on_paste")
        if child is not None and hasattr(child, "on_paste"):
            cast("Callable[[], object]", child.on_paste)()

        return False

    def on_remove(self, *args: object) -> bool:
        child = get_deepest_focused_widget_with_attr(self, "on_remove")
        if child is not None and hasattr(child, "on_remove"):
            cast("Callable[[], object]", child.on_remove)()

        return False

    def show_info_toast(self, text: str) -> None:
        # Safe to call from any thread
        GLib.idle_add(self._add_toast, text, Adw.ToastPriority.NORMAL, 3)

    def show_error_toast(self, text: str) -> None:
        # Safe from any thread; error toasts use high priority and a seven-second timeout.
        # Background plugin and store loads can call this method.
        GLib.idle_add(self._add_toast, text, Adw.ToastPriority.HIGH, 7)

    def _add_toast(self, text: str, priority: Adw.ToastPriority, timeout: int) -> bool:
        toast = Adw.Toast(
            title=text,
            timeout=timeout,
            priority=priority
        )
        self.toast_overlay.add_toast(toast)
        return GLib.SOURCE_REMOVE

    def get_deck_stack(self) -> "DeckStack | None":
        """Return the deck stack, or None while the published window is still building.
        This guard lets early media-thread USB events avoid AttributeError."""
        try:
            return self.leftArea.deck_stack
        except AttributeError:
            return None

    def get_sidebar(self) -> "Sidebar | None":
        """Return the sidebar, or None while the published window is still building.
        A built Sidebar initializes all attributes that callers access."""
        try:
            return self.sidebar
        except AttributeError:
            return None

    def get_active_controller(self) -> DeckController | None:
        deck_stack = self.get_deck_stack()
        if deck_stack is None:
            return None
        visible_child = deck_stack.get_visible_child()
        if visible_child is None:
            return None
        return cast("DeckController | None", visible_child.deck_controller)

    def get_active_page(self) -> Page | None:
        """Return the selected deck's active page, or None.
        None is normal before deck selection or the first page load."""
        controller = self.get_active_controller()
        if controller is None:
            # Report an absent page directly.
            # The page manager has no dummy-page fallback.
            return None
        if hasattr(controller, "active_page"):
            return controller.active_page
        return None
