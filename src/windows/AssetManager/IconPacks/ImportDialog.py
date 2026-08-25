"""The dialog that turns a zip archive or a folder of pictures into a pack.

It collects a name, an optional description, an optional banner and the
archive or folder to read, and then hands all four to pack_import on a worker
thread. The file dialogs and every widget here run on the main loop, and the
scan, the copy and the unpack do not, because they read and write the disk.
"""
# Import gtk modules
import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gio, Gtk

from loguru import logger as log

# Import own modules
from src.backend.PackManagement import pack_import

# Import globals
import globals as gl

# Import typing
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.windows.AssetManager.IconPacks.PackChooser import IconPackChooser


class ImportPackDialog(Adw.MessageDialog):
    """Ask for the pack, then build it off the main loop."""

    def __init__(self, pack_chooser: "IconPackChooser") -> None:
        super().__init__(
            transient_for=pack_chooser.asset_manager,
            modal=True,
            heading=gl.lm.get("asset-chooser.icon-packs.import.heading"),
            body=gl.lm.get("asset-chooser.icon-packs.import.body"),
        )
        self.pack_chooser = pack_chooser
        #: The archive or folder to read. None until the user picks one.
        self.source_path: str | None = None
        #: The picture the chooser shows for the pack, or None for the first
        #: icon of the import.
        self.banner_path: str | None = None

        self.build()

        self.add_response("cancel", gl.lm.get("asset-chooser.icon-packs.import.cancel"))
        self.add_response("import", gl.lm.get("asset-chooser.icon-packs.import.confirm"))
        self.set_response_appearance("import", Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response("cancel")
        self.set_close_response("cancel")
        self.set_response_enabled("import", False)

        self.connect("response", self.on_response)

    def build(self) -> None:
        self.group = Adw.PreferencesGroup(margin_top=10)
        self.set_extra_child(self.group)

        self.name_row = Adw.EntryRow(title=gl.lm.get("asset-chooser.icon-packs.import.name"))
        self.name_row.connect("changed", self.on_changed)
        self.group.add(self.name_row)

        self.description_row = Adw.EntryRow(
            title=gl.lm.get("asset-chooser.icon-packs.import.description"))
        self.group.add(self.description_row)

        self.source_row = Adw.ActionRow(
            title=gl.lm.get("asset-chooser.icon-packs.import.source"),
            subtitle=gl.lm.get("asset-chooser.icon-packs.import.source-none"))
        self.archive_button = Gtk.Button(
            label=gl.lm.get("asset-chooser.icon-packs.import.choose-archive"),
            valign=Gtk.Align.CENTER)
        self.archive_button.connect("clicked", self.on_choose_archive)
        self.folder_button = Gtk.Button(
            label=gl.lm.get("asset-chooser.icon-packs.import.choose-folder"),
            valign=Gtk.Align.CENTER)
        self.folder_button.connect("clicked", self.on_choose_folder)
        self.source_row.add_suffix(self.archive_button)
        self.source_row.add_suffix(self.folder_button)
        self.group.add(self.source_row)

        self.banner_row = Adw.ActionRow(
            title=gl.lm.get("asset-chooser.icon-packs.import.banner"),
            subtitle=gl.lm.get("asset-chooser.icon-packs.import.banner-none"))
        self.banner_button = Gtk.Button(
            label=gl.lm.get("asset-chooser.icon-packs.import.choose-banner"),
            valign=Gtk.Align.CENTER)
        self.banner_button.connect("clicked", self.on_choose_banner)
        self.banner_row.add_suffix(self.banner_button)
        self.group.add(self.banner_row)

    # Collecting the answers

    def on_changed(self, *_args: Any) -> None:
        """A pack needs a name and something to read. Until both are there,
        the import response stays off rather than fail behind the dialog."""
        ready = bool(self.name_row.get_text().strip()) and self.source_path is not None
        self.set_response_enabled("import", ready)

    def set_source(self, path: str) -> None:
        self.source_path = path
        self.source_row.set_subtitle(path)
        # A pack with no name of its own takes the name of what it came from,
        # which is right more often than an empty row.
        if not self.name_row.get_text().strip():
            self.name_row.set_text(os.path.splitext(os.path.basename(path.rstrip(os.sep)))[0])
        self.on_changed()

    def on_choose_archive(self, _button: Gtk.Button) -> None:
        archives = Gtk.FileFilter(name=gl.lm.get("asset-chooser.icon-packs.import.filter-archive"))
        archives.add_pattern("*.zip")
        _FileChoice(self, self.set_source, archives).open_file()

    def on_choose_folder(self, _button: Gtk.Button) -> None:
        _FileChoice(self, self.set_source).select_source_folder()

    def on_choose_banner(self, _button: Gtk.Button) -> None:
        pictures = Gtk.FileFilter(name=gl.lm.get("asset-chooser.icon-packs.import.filter-picture"))
        for extension in sorted(pack_import.importable_extensions()):
            pictures.add_pattern(f"*.{extension}")
        _FileChoice(self, self.set_banner, pictures).open_file()

    def set_banner(self, path: str) -> None:
        self.banner_path = path
        self.banner_row.set_subtitle(path)

    # Doing the work

    def on_response(self, _dialog: Adw.MessageDialog, response: str) -> None:
        if response != "import":
            return
        source = self.source_path
        if source is None:
            return
        if pack_import.import_is_running():
            # A second import while one runs would race the first over the
            # pack folders and the grid reload. The button that opens this
            # dialog is guarded too; this is the guard for the dialog that is
            # already open.
            return
        name = self.name_row.get_text().strip()
        description = self.description_row.get_text().strip()
        banner = self.banner_path

        pack_import.set_import_running(True)
        asset_manager = self.pack_chooser.asset_manager
        asset_manager.set_cursor_from_name("wait")

        threading.Thread(
            target=self._run_import, args=(source, name, description, banner),
            name="import_icon_pack", daemon=True,
        ).start()

    def _run_import(self, source: str, name: str, description: str,
                    banner: str | None) -> None:
        """Runs on a worker thread. Every widget call from here marshals."""
        failure: str | None = None
        try:
            pack_import.import_icon_pack(source, name, description, banner)
        except pack_import.PackImportError as error:
            failure = str(error)
        except Exception as error:
            log.opt(exception=True).error(f"Importing an icon pack from {source!r} failed: {error}")
            failure = gl.lm.get("asset-chooser.icon-packs.import.failed-body")
        GLib.idle_add(self._finish_import, failure)

    def _finish_import(self, failure: "str | None") -> bool:
        """Runs on the main loop, whether the import worked or not."""
        pack_import.set_import_running(False)

        asset_manager = self.pack_chooser.asset_manager
        if gl.asset_manager is not asset_manager:
            # The asset manager window closed while the import ran, so GTK is
            # disposing its widgets. Touch none of them: a reload of a
            # destroyed grid, a cursor on a gone window and a dialog transient
            # for it all raise or warn. The pack is on disk, and the next open
            # of the window reads it.
            return GLib.SOURCE_REMOVE

        asset_manager.set_cursor_from_name("default")
        if failure is None:
            self.pack_chooser.reload()
            return GLib.SOURCE_REMOVE

        dialog = Adw.MessageDialog(
            transient_for=asset_manager, modal=True,
            heading=gl.lm.get("asset-chooser.icon-packs.import.failed"),
            body=failure,
        )
        dialog.add_response("close", gl.lm.get("asset-chooser.icon-packs.import.close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present()
        return GLib.SOURCE_REMOVE



class _FileChoice(Gtk.FileDialog):
    """One file-dialog round trip that hands a path back to a callback.

    Gtk.FileDialog answers on the main loop, and its finish call raises when
    the user cancels, so each of the two forms has its own small handler. It
    follows ChooseFileDialog of the custom-asset chooser, which is the file
    dialog this window already uses.
    """

    def __init__(self, dialog: ImportPackDialog, on_path: Callable[[str], None],
                 file_filter: "Gtk.FileFilter | None" = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # The import dialog stays alive through its own presentation, and this
        # reference keeps this chooser alive until its callback lands.
        self.dialog = dialog
        self.on_path = on_path
        if file_filter is not None:
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(file_filter)
            self.set_filters(filters)
            self.set_default_filter(file_filter)

    def open_file(self) -> None:
        self.open(callback=self.on_open_finished)

    def select_source_folder(self) -> None:
        self.select_folder(callback=self.on_select_folder_finished)

    def on_open_finished(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            chosen = self.open_finish(result)
        except GLib.Error:
            # Cancelled, or the portal refused. Neither is worth a message.
            return
        self._deliver(chosen)

    def on_select_folder_finished(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            chosen = self.select_folder_finish(result)
        except GLib.Error:
            return
        self._deliver(chosen)

    def _deliver(self, chosen: "Gio.File | None") -> None:
        path = chosen.get_path() if chosen is not None else None
        if path is None:
            # A location with no local path, such as a remote mount that the
            # portal did not download. There is nothing here to read.
            return
        self.on_path(path)
