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
import os
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GdkPixbuf, GLib, Pango

from loguru import logger as log

import enum

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.windows.AssetManager.thumbnail_loader import ThumbnailLoader


class _PixbufUnset(enum.Enum):
    # Distinguish an omitted pixbuf from an explicit failed-decode None
    TOKEN = enum.auto()


_PIXBUF_UNSET = _PixbufUnset.TOKEN


class Preview(Gtk.FlowBoxChild):
    # Pooled cards use an off-main loader; standalone cards decode inline
    _thumbnail_loader: "ThumbnailLoader | None" = None

    def __init__(self, image_path: str | os.PathLike[str] | None = None, text:str | None = None, can_be_deleted: bool = False,
                 pixbuf: "GdkPixbuf.Pixbuf | None | _PixbufUnset" = _PIXBUF_UNSET):
        super().__init__()
        self.set_css_classes(["asset-preview"])
        self.set_margin_start(5)
        self.set_margin_end(5)
        self.set_margin_top(5)
        self.set_margin_bottom(5)

        # None while no image is bound. A failed decode shows the
        # broken-image icon instead. See show_broken_image.
        self.pixbuf: GdkPixbuf.Pixbuf | None = None
        self.can_be_deleted = can_be_deleted

        self._build()

        if pixbuf is not _PIXBUF_UNSET:
            # The caller decoded this already, off the main thread, for the
            # pack grids. See GenericPackChooserPage.build.
            self.set_pixbuf(pixbuf)
        elif image_path is not None:
            self.set_image(image_path)
        if text is not None:
            self.set_text(text)

    def _build(self) -> None:
        self.overlay = Gtk.Overlay()
        self.set_child(self.overlay)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=250, height_request=180)
        self.overlay.set_child(self.main_box)

        self.picture = Gtk.Picture(width_request=250, height_request=180, overflow=Gtk.Overflow.HIDDEN, content_fit=Gtk.ContentFit.COVER,
                                   hexpand=False, vexpand=False, keep_aspect_ratio=True)
        
        self.picture.set_pixbuf(self.pixbuf)
        self.main_box.append(self.picture)

        # Shows instead of the picture when the decode of the file fails. It
        # starts hidden, and set_image toggles it, so a recycled cell recovers.
        self.broken_icon = Gtk.Image(icon_name="image-missing-symbolic", pixel_size=48,
                                     halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                     visible=False, tooltip_text="Could not load this file")
        self.overlay.add_overlay(self.broken_icon)

        self.label = Gtk.Label(xalign=0.5, hexpand=False, ellipsize=Pango.EllipsizeMode.END, max_width_chars=20,
                               margin_start=20, margin_end=20)
        self.main_box.append(self.label)

        # Optional second line identifies each pack in cross-pack results
        self.subtitle = Gtk.Label(xalign=0.5, hexpand=False, ellipsize=Pango.EllipsizeMode.END, max_width_chars=20,
                                  margin_start=20, margin_end=20, visible=False,
                                  css_classes=["dim-label", "caption"])
        self.main_box.append(self.subtitle)

        self.info_button = Gtk.Button(icon_name="help-about-symbolic", halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=5, margin_bottom=5)
        self.info_button.connect("clicked", self.on_click_info)
        self.overlay.add_overlay(self.info_button)

        self.remove_button = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.END, halign=Gtk.Align.END, margin_end=5, margin_bottom=5, visible=self.can_be_deleted)
        self.remove_button.connect("clicked", self.on_click_remove)
        self.overlay.add_overlay(self.remove_button)

    @staticmethod
    def decode_pixbuf(path: str | os.PathLike[str] | None) -> GdkPixbuf.Pixbuf | None:
        """Decode at preview size; safe off-main, returning None for unreadable files."""
        # Check None before fspath and catch corrupt-file errors to clear recycled cells
        if path is None:
            return None

        try:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(os.fspath(path),
                                                           width=250,
                                                           height=180,
                                                           preserve_aspect_ratio=True)
        except GLib.Error as e:
            # A corrupt or unreadable file reaches here. The message says why.
            log.warning(f"Could not load asset preview for {path}: {e}")
            return None
        except Exception as e:
            # An unexpected error, which means a defect and not a bad file.
            # Keep the traceback, so the logs separate the two.
            log.opt(exception=True).warning(f"Unexpected error loading asset preview for {path}: {e}")
            return None

    def set_image(self, path: str | os.PathLike[str]) -> None:
        """Decode through the grid loader when present, else inline."""
        loader = self._thumbnail_loader
        if loader is None:
            self.set_pixbuf(self.decode_pixbuf(path))
            return
        loader.request(os.fspath(path) if path is not None else None, self)

    def set_pixbuf(self, pixbuf: GdkPixbuf.Pixbuf | None) -> None:
        """Shows an already-decoded pixbuf. None means the decode failed, so
        the broken-image icon shows instead."""
        if pixbuf is None:
            self.show_broken_image()
            return

        self.pixbuf = pixbuf
        self.picture.set_pixbuf(self.pixbuf)
        self.broken_icon.set_visible(False)

    def show_broken_image(self) -> None:
        """Clear any recycled pixbuf and show the missing-image icon."""
        self.pixbuf = None
        self.picture.set_pixbuf(None)
        self.broken_icon.set_visible(True)

    def set_text(self, text:str) -> None:
        self.label.set_text(text)

    def set_subtitle(self, text: str | None) -> None:
        """Replace a recycled card's subtitle, or hide it for None."""
        self.subtitle.set_text(text or "")
        self.subtitle.set_visible(text is not None and text != "")

    def on_click_info(self, button: Gtk.Button) -> None:
        pass

    def on_click_remove(self, button: Gtk.Button) -> None:
        pass
