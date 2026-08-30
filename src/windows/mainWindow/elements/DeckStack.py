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


gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

# Import Python modules 
from loguru import logger as log

# Import globals
import globals as gl

# Import own modules
from src.backend import ui_port
from src.windows.mainWindow.elements.DeckStackChild import DeckStackChild as DeckStackChild

# Import typing
from typing import Any, TYPE_CHECKING, cast, override
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckManager import DeckManager
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.windows.mainWindow.elements.leftArea import LeftArea
    from src.windows.mainWindow.mainWindow import MainWindow

class DeckStack(Gtk.Stack):
    """
    A deck with childs for each connected deck
    """
    def __init__(self, main_window: "MainWindow", left_area: "LeftArea", deck_manager: "DeckManager", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.deck_manager = deck_manager
        self.main_window = main_window
        self.left_area = left_area

        self.deck_names: list[str] = []
        self.deck_numbers: list[str] = []

        self.deck_attributes: dict["DeckController", tuple[str, str]] = {}

    def on_switch(self, widget: Gtk.Widget, *args: Any) -> None:
        # Update page selector
        self.main_window.sidebar.page_selector.update_selected()
        self.main_window.deck_settings_button.refresh_state()

        # Every child of this stack is a DeckStackChild.
        child = cast(DeckStackChild, self.get_visible_child())

    def build(self) -> None:
        self.connect("notify::visible-child-name", self.on_switch)

    def add_pages(self) -> None:
        for deck_controller in self.deck_manager.deck_controller:
            self.add_page(deck_controller)

        if len(self.deck_manager.deck_controller) == 0:
            self.main_window.change_ui_to_no_connected_deck()

    def add_page(self, deck_controller: "DeckController") -> None:
        attr = self.get_page_attributes(deck_controller)
        if attr is None:
            return
        deck_number, deck_type = attr

        # Unbind before child construction to keep hidden-image replay off stale widgets.
        # Duck typing keeps wrapper ports compatible with the bind/unbind contract.
        adapter = ui_port.get()
        unbind = getattr(adapter, "unbind", None)
        if callable(unbind):
            unbind(deck_controller)
        page = DeckStackChild(self, deck_controller)
        self.add_titled(page, deck_number, deck_type)
        # Bind by reference after insertion to avoid another device serial read.
        # This prevents construction failure from binding a child absent from the stack.
        bind = getattr(adapter, "bind", None)
        if callable(bind):
            bind(deck_controller, page)

        page.page_settings.deck_config.grid.select_key(0, 0)

        self.main_window.change_ui_to_connected_deck()

        self.main_window.reload_sidebar()
            
    def base_title(self, deck_controller: "DeckController", serial_number: str) -> str:
        """Return the configured deck name before any duplicate suffix.
        Use the device model name when no custom name exists and always return a string."""
        try:
            model_name = deck_controller.deck.deck_type()
        except Exception as e:
            # deck_type needs no device lock but can raise after the handle is gone.
            # An unplugged deck then falls back through its settings and serial.
            log.error(e)
            model_name = None
        return gl.settings_manager.deck(
            self._settings_serial(deck_controller, serial_number)).display_name(model_name)

    def _settings_serial(self, deck_controller: "DeckController", fallback: str) -> str:
        """Return the current device serial used for deck settings.
        Fall back to the memoized stack serial only when the fresh read fails."""
        try:
            return deck_controller.deck.get_serial_number()
        except Exception as e:
            log.error(e)
            return fallback

    def unique_title(self, base_title: str) -> str:
        """Return and reserve base_title with a "(n)" suffix when already shown.
        Append the suffix to the complete title so names such as "Stream Deck MK.2" stay intact."""
        title = base_title
        suffix = 2
        while title in self.deck_names:
            title = f"{base_title} ({suffix})"
            suffix += 1
        self.deck_names.append(title)
        return title

    def get_page_attributes(self, deck_controller: "DeckController") -> tuple[Any, ...] | None:
        if deck_controller in self.deck_attributes:
            return self.deck_attributes[deck_controller]

        try:
            # Use the cached serial for the stack-child name.
            # Every reader must keep one value even if a later device read differs.
            serial_number = deck_controller.serial_number()
        except Exception as e:
            log.error(e)
            return None
        self.deck_numbers.append(serial_number)
        deck_number = str(serial_number)

        title = self.unique_title(self.base_title(deck_controller, deck_number))
        self.deck_attributes[deck_controller] = deck_number, title

        return deck_number, title

    def refresh_page_title(self, deck_controller: "DeckController") -> None:
        """Retitle this live stack child from its settings on the main thread.
        Release its old title first; other duplicate suffixes remain until the stack rebuilds."""
        attr = self.deck_attributes.get(deck_controller)
        if attr is None:
            return
        deck_number, old_title = attr
        if old_title in self.deck_names:
            self.deck_names.remove(old_title)

        title = self.unique_title(self.base_title(deck_controller, deck_number))
        self.deck_attributes[deck_controller] = deck_number, title

        child = self.get_child_by_name(deck_number)
        if child is None:
            # The child is not in the stack. The recorded attributes hold the
            # new title, and an add of the child reads them.
            return
        page = self.get_page(child)
        page.set_title(title)

    def remove_page(self, deck_controller: "DeckController") -> None:
        adapter = ui_port.get()
        unbind = getattr(adapter, "unbind", None)
        if callable(unbind):
            unbind(deck_controller)

        was_visible: bool = False
        for i, page in enumerate(self.get_pages()):  # ty: ignore[invalid-argument-type]  # gi stub: PyGObject's Gio.ListModel override makes the returned SelectionModel iterable/sized/indexable; gi-stubs declares none of that
            if page.get_child().deck_controller == deck_controller:
                if self.get_visible_child() == page.get_child():
                    was_visible = True
                # Remove from deck_names
                self.deck_names.remove(page.get_title())
                # Remove page from stack
                self.remove(page.get_child())
                break

        # Drop cached attributes even when the controller was not visible.
        # A retained object key would keep the dead controller reachable across replugs.
        attr = self.deck_attributes.pop(deck_controller, None)
        if attr is not None:
            deck_number, _deck_type = attr
            if deck_number in self.deck_numbers:
                self.deck_numbers.remove(deck_number)

        if not was_visible:
            return
        
        # Reload righ area
        self.main_window.reload_sidebar()
            
        pages = self.get_pages()
        # Show message if no decks are connected
        if len(pages) == 0:  # ty: ignore[invalid-argument-type]  # gi stub: PyGObject's Gio.ListModel override makes the returned SelectionModel iterable/sized/indexable; gi-stubs declares none of that
            self.main_window.change_ui_to_no_connected_deck()
            return

        self.set_visible_child(pages[0].get_child())  # ty: ignore[not-subscriptable]  # gi stub: PyGObject's Gio.ListModel override makes the returned SelectionModel iterable/sized/indexable; gi-stubs declares none of that

    def focus_controller(self, deck_controller: "DeckController") -> None:
        for page in self.get_pages():  # ty: ignore[not-iterable]  # gi stub: PyGObject's Gio.ListModel override makes the returned SelectionModel iterable/sized/indexable; gi-stubs declares none of that
            if page.get_child().deck_controller == deck_controller:
                self.set_visible_child(page.get_child())
                return
            
    @override
    def get_visible_child(self) -> DeckStackChild | None:
        # None while the stack is empty (no deck connected yet).
        return super().get_visible_child()  # ty: ignore[invalid-return-type]  # gi stub: Gtk.Stack.get_visible_child is typed Gtk.Widget | None; every child of this stack is a DeckStackChild
