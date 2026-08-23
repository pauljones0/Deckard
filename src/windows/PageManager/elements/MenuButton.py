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
# Import gi
from datetime import datetime
import gi

from GtkHelper.GtkHelper import EntryDialog

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, GLib

import globals as gl
import json
import os

from src.backend import services
from src.backend.atomic_json import atomic_write_json
from src.backend.PageManagement import page_flush

from loguru import logger as log 
from src.windows.PageManager.Importer.Importer import Importer
# Import typing
from collections.abc import Callable
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.PageManager.elements.PageEditor import PageEditor

# Import signals
from src.Signals import Signals

class MenuButton(Gtk.MenuButton):
    def __init__(self, pageEditor: "PageEditor"):
        super().__init__()
        self.pageEditor = pageEditor
        self.set_icon_name("open-menu-symbolic")

        # The Gio.File the import dialog handed back; held only between
        # import_page_callback and import_page_name_selected_callback.
        self.selected_file: Gio.File | None = None

        self.init_actions()
        self.set_page_specific_actions_enabled(False)
        self.build()

    def init_actions(self) -> None:
        self.action_group = Gio.SimpleActionGroup()
        self.insert_action_group("pm", self.action_group)

        self.import_streamdeck_ui_action = Gio.SimpleAction.new("streamdeck-ui", None)
        self.duplicate_page_action = Gio.SimpleAction.new("duplicate-page", None)
        self.export_page_action = Gio.SimpleAction.new("export-page", None)
        self.import_page_action = Gio.SimpleAction.new("import-page", None)
        self.export_all_pages_action = Gio.SimpleAction.new("export-all-pages", None)
        self.import_streamcontroller = Gio.SimpleAction.new("import-streamcontroller", None)

        self.duplicate_page_action.connect("activate", self.on_duplicate_page)
        self.import_streamdeck_ui_action.connect("activate", self.on_import_streamdeck_ui)
        self.export_page_action.connect("activate", self.on_export_page)
        self.import_page_action.connect("activate", self.on_import_page)
        self.export_all_pages_action.connect("activate", self.on_export_all_pages)
        self.import_streamcontroller.connect("activate", self.on_import_streamcontroller)

        self.action_group.add_action(self.duplicate_page_action)
        self.action_group.add_action(self.import_streamdeck_ui_action)
        self.action_group.add_action(self.export_page_action)
        self.action_group.add_action(self.import_page_action)
        self.action_group.add_action(self.export_all_pages_action)
        self.action_group.add_action(self.import_streamcontroller)

    def set_page_specific_actions_enabled(self, enabled: bool) -> None:
        self.duplicate_page_action.set_enabled(enabled)
        self.export_page_action.set_enabled(enabled)

    def build(self) -> None:
        self.menu = Gio.Menu.new()
        self.menu.append(gl.lm.get("page-manager.duplicate"), "pm.duplicate-page")
        self.menu.append(gl.lm.get("page-manager.export-page"), "pm.export-page")
        self.menu.append("Export All", "pm.export-all-pages")

        self.import_menu = Gio.Menu.new()
        self.menu.append_submenu(gl.lm.get("page-manager.import"), self.import_menu)

        self.import_menu.append(gl.lm.get("page-manager.import.page"), "pm.import-page")
        self.import_menu.append("StreamController", "pm.import-streamcontroller")
        self.import_menu.append("StreamDeck UI", "pm.streamdeck-ui")


        # Popover
        self.popover = Gtk.PopoverMenu()
        self.popover.set_menu_model(self.menu)
        self.set_popover(self.popover)

    def on_import_streamdeck_ui(self, action: Gio.SimpleAction, parameter: "GLib.Variant | None") -> None:
        ChooseImportFileDialog(self, self.streamdeck_ui_callback)

    def streamdeck_ui_callback(self, selected_file: Gio.File) -> None:
        path = selected_file.get_path()
        if not path:
            # A location with no local path, such as a remote GVfs mount,
            # cannot be read or written here.
            return
        importer = Importer(services.require_app(), self.pageEditor.page_manager)
        # GLib.idle_add(importer.present)
        importer.present()
        importer.import_pages(path, "streamdeck-ui")

    def on_export_page(self, action: Gio.SimpleAction, parameter: "GLib.Variant | None") -> None:
        path = self.pageEditor.active_page_path
        if path in [None, ""]:
            return
        
        initial_name = os.path.basename(path)
        ChooseExportFileDialog(self, self.export_page_callback, initial_name=initial_name)

    def export_page_callback(self, selected_file: Gio.File) -> None:
        export_path = selected_file.get_path()
        if not export_path:
            # A location with no local path, such as a remote GVfs mount,
            # cannot be read or written here.
            return
        page_json = {}
        # Read the path once, so the flush and the open reach the same file
        # when the editor selection changes during this call.
        page_path = self.pageEditor.active_page_path
        if not page_path:
            # The editor cleared its selection while the file chooser was up.
            # Same reading as on_export_page, which checks before opening it.
            return
        # A read barrier. The export reads the file directly, so an edit that
        # is still in flight would be absent from what the user exports.
        page_flush.get().flush_path(page_path)
        with open(page_path, "r") as f:
            page_json = json.load(f)

        atomic_write_json(export_path, page_json)

    def on_import_page(self, action: Gio.SimpleAction, parameter: "GLib.Variant | None") -> None:
        ChooseImportFileDialog(self, self.import_page_callback)

    def import_page_callback(self, selected_file: Gio.File) -> None:
        path = selected_file.get_path()
        if not path:
            # A location with no local path, such as a remote GVfs mount,
            # cannot be read or written here.
            return
        page_name = os.path.splitext(os.path.basename(path))[0]
        self.selected_file = selected_file
        if page_name in services.require_page_manager().get_page_names():
            dial = EntryDialog(parent_window=self.pageEditor.page_manager,
                           dialog_title=gl.lm.get("page-manager.page-selector.add-dialog.title"),
                           placeholder=gl.lm.get("page-manager.page-selector.add-dialog.placeholder"),
                           confirm_label=gl.lm.get("page-manager.page-selector.add-dialog.confirm"),
                           cancel_label=gl.lm.get("page-manager.page-selector.add-dialog.cancel"),
                           empty_warning=gl.lm.get("page-manager.page-selector.add-dialog.empty-warning"),
                           already_exists_warning=gl.lm.get("page-manager.page-selector.add-dialog.already-exists-warning"),
                           forbid_answers=services.require_page_manager().get_page_names(),
                           default_text=page_name)
        
            dial.show(callback_func=self.import_page_name_selected_callback)
        else:
            self.import_page_name_selected_callback(page_name)


    def import_page_name_selected_callback(self, name: str) -> None:
        import_dict = {}
        selected_file = self.selected_file
        source_path = selected_file.get_path() if selected_file is not None else None
        if not source_path:
            # No file left to read: either the chooser handed back a location
            # with no local path, or a second callback arrived after the first
            # cleared the slot below.
            log.error("Page import has no source file to read")
            return
        # A read barrier. On the duplicate path this file is a live page, so
        # its pending edits must reach the disk before the copy reads it. A
        # duplicate must match what the screen shows. This does nothing for a
        # real import, whose source is not a page of this app.
        page_flush.get().flush_path(source_path)
        with open(source_path, "r") as f:
            import_dict = json.load(f)

        self.selected_file = None

        # Both callers already pass the page name. The direct path passes the
        # basename without the extension, and the rename dialog passes the
        # text that the user typed. Use it unchanged. A splitext call here
        # truncates a typed name at its first dot, so "backup.v2" becomes
        # "backup". add_page appends the .json extension itself.
        page_name = name
        try:
            page_path = services.require_page_manager().add_page(page_name, import_dict)
        except (FileExistsError, ValueError):
            # ValueError: a name that resolves outside the pages directory.
            # Fail closed rather than let it escape this GTK callback.
            return

        self.pageEditor.page_manager.page_selector.add_row_by_path(page_path)

        # Emit signal
        gl.signal_manager.trigger_signal(Signals.PageAdd, page_path)

    def on_duplicate_page(self, action: Gio.SimpleAction, parameter: "GLib.Variant | None") -> None:
        active_page_path = self.pageEditor.active_page_path
        if active_page_path in [None, ""]:
            return
        
        file =Gio.File.new_for_path(active_page_path)
        self.import_page_callback(file)

    def on_export_all_pages(self, action: Gio.SimpleAction, parameter: "GLib.Variant | None") -> None:
        initial_name = f"Deckard_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.json"
        ChooseExportFileDialog(self, self.export_all_pages_callback, initial_name=initial_name)

    def export_all_pages_callback(self, selected_file: Gio.File) -> None:
        selected_path = selected_file.get_path()
        if not selected_path:
            # A location with no local path, such as a remote GVfs mount,
            # cannot be read or written here.
            return

        pages = {}

        page_manager = services.require_page_manager()
        for path in page_manager.get_pages(add_custom_pages=False):
            js = page_manager.get_page_data(path)
            pages[os.path.basename(path)] = js

        atomic_write_json(selected_path, pages)

    def on_import_streamcontroller(self, action: Gio.SimpleAction, parameter: "GLib.Variant | None") -> None:
        ChooseImportFileDialog(self, self.import_streamcontroller_callback)

    def import_streamcontroller_callback(self, selected_file: Gio.File) -> None:
        path = selected_file.get_path()
        if not path:
            # A location with no local path, such as a remote GVfs mount,
            # cannot be read or written here.
            return
        importer = Importer(services.require_app(), self.pageEditor.page_manager)
        importer.present()
        importer.import_pages(path, "streamcontroller")
        

class ChooseImportFileDialog(Gtk.FileDialog):
    def __init__(self, menu_button: MenuButton, callback: "Callable[[Gio.File], None] | None" = None):
        super().__init__(title=gl.lm.get("asset-chooser.custom.browse-files.dialog.title"),
                         accept_label=gl.lm.get("asset-chooser.custom.browse-files.dialog.select-button"))
        self.menu_button = menu_button
        self.original_callback = callback
        self.open(callback=self.callback)

    def callback(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            selected_file = self.open_finish(result)
        except GLib.Error as err:
            log.error(err)
            return
        
        if self.original_callback is not None:
            self.original_callback(selected_file)

class ChooseExportFileDialog(Gtk.FileDialog):
    def __init__(self, menu_button: MenuButton, callback: "Callable[[Gio.File], None] | None" = None, initial_name: str | None = None):
        super().__init__(title=gl.lm.get("asset-chooser.custom.browse-files.dialog.title"),
                         accept_label=gl.lm.get("asset-chooser.custom.browse-files.dialog.select-button"),
                         initial_name=initial_name)
        self.menu_button = menu_button
        self.original_callback = callback
        self.save(callback=self.callback)

    def callback(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            selected_file = self.save_finish(result)
        except GLib.Error as err:
            log.error(err)
            return
        
        if self.original_callback is not None:
            self.original_callback(selected_file)