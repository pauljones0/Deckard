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
import json
import os
import gi

from collections.abc import Callable
from typing import Any

from src.backend.main_loop import run_in_background
from src.windows.PageManager.Importer.StreamDeckUI.StreamDeckUI import StreamDeckUIImporter
from src.windows.PageManager.Importer.StreamController.StreamController import StreamControllerImporter

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from loguru import logger as log

class Importer(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, window: Gtk.Window) -> None:
        super().__init__(application=app,
                         transient_for=window,
                         modal=True,
                         default_width=400,
                         default_height=120,
                         title="Importing")

        self.build()

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(self.main_box)

        self.header = Adw.HeaderBar(css_classes=["flat"])
        self.main_box.append(self.header)

        self.progess_bar = Gtk.ProgressBar(margin_start=20, margin_end=20, margin_top=20, margin_bottom=20, show_text=True)
        self.main_box.append(self.progess_bar)

    def show_error(self, message: str = "Import failed") -> None:
        GLib.idle_add(self.progess_bar.set_text, message)
        GLib.idle_add(self.progess_bar.set_fraction, 0)
        GLib.timeout_add(3000, self.close)

    def import_pages(self, path: str, app: str, on_finished: Callable[[], Any] | None = None) -> None:
        self.progess_bar.set_text("Importing...")
        self.progess_bar.set_fraction(0)

        if app == "streamdeck-ui":
            run_in_background(self.import_from_streamdeck_ui, path, on_finished)

        if app == "streamcontroller":
            run_in_background(self.import_from_streamcontroller, path, on_finished)
        

    @log.catch
    def import_from_streamdeck_ui(self, path: str, on_finished: Callable[[], Any] | None) -> None:
        if not os.path.exists(path):
            self.show_error("File not found")
            return
        try:
            with open(path) as f:
                json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.show_error("File is not valid JSON")
            return

        ui_importer = StreamDeckUIImporter(path)
        try:
            ui_importer.perform_import()
        except Exception:
            # The @log.catch decorator swallows the exception and returns, so
            # without this branch a failed import leaves the progress bar
            # frozen at 0% and the dialog open forever. Log the failure and
            # then route it to show_error, which marshals the text and the
            # close onto the main thread.
            log.exception("StreamDeck UI import failed")
            self.show_error("Import failed")
            return

        GLib.idle_add(self.progess_bar.set_text, "Imported!")
        GLib.idle_add(self.progess_bar.set_fraction, 1)

        if on_finished:
            on_finished()

        GLib.timeout_add(1500, self.close)

    @log.catch
    def import_from_streamcontroller(self, path: str, on_finished: Callable[[], Any] | None) -> None:
        if not os.path.exists(path):
            self.show_error("File not found")
            return
        try:
            with open(path) as f:
                json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.show_error("File is not valid JSON")
            return

        ui_importer = StreamControllerImporter(path)
        try:
            ui_importer.perform_import()
        except Exception:
            # The @log.catch decorator swallows the exception and returns, so
            # without this branch a failed import leaves the progress bar
            # frozen at 0% and the dialog open forever. Log the failure and
            # then route it to show_error, which marshals the text and the
            # close onto the main thread.
            log.exception("StreamController import failed")
            self.show_error("Import failed")
            return

        GLib.idle_add(self.progess_bar.set_text, "Imported!")
        GLib.idle_add(self.progess_bar.set_fraction, 1)

        if on_finished:
            on_finished()

        GLib.timeout_add(1500, self.close)