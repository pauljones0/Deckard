"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import os
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from PIL import Image
from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.HelperMethods import is_image
from src.backend.DeckManagement.ImageHelpers import image2pixbuf
from src.backend.DeckManagement.deck_controller.viewport import (
    DEFAULT_VIEW, MAX_SCALE, MIN_SCALE, viewport_rect,
)

from typing import Any, Callable

# The live-preview rate cap during a drag, in seconds between pushes.
LIVE_PUSH_INTERVAL_S = 0.1

# How long after the last scale spin before the change commits, in
# milliseconds. A drag commits on release instead.
SCALE_COMMIT_DELAY_MS = 400

# The largest preview surface, in logical pixels. The image letterboxes
# inside it at its own aspect.
PREVIEW_W = 560
PREVIEW_H = 340

ViewTuple = tuple[float, float, float]


class ViewportDialog(Adw.Dialog):
    """Pan and zoom the visible region of a background image.

    The dialog shows the media with a white, deck-canvas-aspect box over it:
    the region the deck shows. Dragging the box pans; the zoom factor
    scales it. entries is a list of (path, view) pairs; more than one entry
    (a slideshow) adds a picker for which image is being adjusted.

    on_live(path, view) fires rate-capped while a drag is in flight, for
    the deck-side preview. on_commit(path, view) fires on drag release, on
    a settled zoom change and on reset; the caller persists it there.
    canvas_size() answers the current deck canvas, read per draw, so an
    extend-to-touchscreen flip mid-dialog reshapes the box on the next
    frame.
    """

    def __init__(self, entries: "list[tuple[str, ViewTuple]]",
                 canvas_size: "Callable[[], tuple[int, int] | None]",
                 on_live: "Callable[[str, ViewTuple], None]",
                 on_commit: "Callable[[str, ViewTuple], None]") -> None:
        super().__init__(title=gl.lm.get("viewport-dialog.title"))
        self.entries = entries
        self.canvas_size = canvas_size
        self.on_live = on_live
        self.on_commit = on_commit

        self.index = 0
        self.view: ViewTuple = entries[0][1]
        self._source: Image.Image | None = None
        self._pixbuf: Any = None  # GdkPixbuf.Pixbuf of _source, built once per entry
        self._last_live = 0.0
        self._drag_anchor: "tuple[float, float] | None" = None
        self._scale_commit_handle: int | None = None
        # The scale spinner is written back programmatically on entry switch
        # and reset; this flag mutes its value-changed handler there.
        self._loading = False

        self.build()
        self._load_entry(0)

    def build(self) -> None:
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())
        self.set_child(toolbar_view)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=10, margin_bottom=15, margin_start=15, margin_end=15)
        toolbar_view.set_content(box)

        if len(self.entries) > 1:
            picker_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.append(picker_box)
            picker_box.append(Gtk.Label(label=gl.lm.get("viewport-dialog.image"),
                                        hexpand=True, xalign=0))
            names = Gtk.StringList.new([os.path.basename(p) for p, _v in self.entries])
            self.entry_picker = Gtk.DropDown(model=names)
            self.entry_picker.connect("notify::selected", self.on_entry_picked)
            picker_box.append(self.entry_picker)

        self.preview = Gtk.DrawingArea(content_width=PREVIEW_W, content_height=PREVIEW_H,
                                       halign=Gtk.Align.CENTER)
        self.preview.set_draw_func(self.on_draw)
        box.append(self.preview)

        self.drag = Gtk.GestureDrag()
        self.drag.connect("drag-begin", self.on_drag_begin)
        self.drag.connect("drag-update", self.on_drag_update)
        self.drag.connect("drag-end", self.on_drag_end)
        self.preview.add_controller(self.drag)

        box.append(Gtk.Label(label=gl.lm.get("viewport-dialog.hint"),
                             css_classes=["dim-label"], halign=Gtk.Align.CENTER))

        scale_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.append(scale_box)
        scale_box.append(Gtk.Label(label=gl.lm.get("viewport-dialog.scale"),
                                   hexpand=True, xalign=0))
        self.scale_spinner = Gtk.SpinButton.new_with_range(MIN_SCALE, MAX_SCALE, 0.05)
        self.scale_spinner.set_digits(2)
        self.scale_spinner.connect("value-changed", self.on_scale_changed)
        scale_box.append(self.scale_spinner)

        self.reset_button = Gtk.Button(label=gl.lm.get("viewport-dialog.reset"),
                                       halign=Gtk.Align.END)
        self.reset_button.connect("clicked", self.on_reset)
        box.append(self.reset_button)

    # -- entry handling ------------------------------------------------------

    def _load_entry(self, index: int) -> None:
        self.index = index
        path, view = self.entries[index]
        self.view = view
        self._source = None
        self._pixbuf = None
        try:
            if is_image(path):
                with Image.open(path) as img:
                    self._source = img.convert("RGBA")
            else:
                # A video or GIF previews through its thumbnail frame; the
                # crop math only needs the source aspect, which the frame
                # shares with the stream.
                self._source = gl.media_manager.get_thumbnail(path).convert("RGBA")
        except Exception:
            log.opt(exception=True).warning(f"Viewport preview failed to load {path}")
        if self._source is not None:
            self._pixbuf = image2pixbuf(self._source)
        self._loading = True
        try:
            self.scale_spinner.set_value(view[2])
        finally:
            self._loading = False
        self.preview.queue_draw()

    def on_entry_picked(self, dropdown: Gtk.DropDown, _pspec: Any) -> None:
        selected = int(dropdown.get_selected())
        if 0 <= selected < len(self.entries) and selected != self.index:
            self._cancel_scale_commit(commit_now=True)
            self._load_entry(selected)

    def current_path(self) -> str:
        return self.entries[self.index][0]

    # -- geometry ------------------------------------------------------------

    def _display_transform(self, widget_w: int, widget_h: int) -> "tuple[float, float, float] | None":
        """(scale, x offset, y offset) that maps source pixels onto the
        widget, containing the image centered at its own aspect."""
        if self._source is None or self._source.width == 0 or self._source.height == 0:
            return None
        scale = min(widget_w / self._source.width, widget_h / self._source.height)
        off_x = (widget_w - self._source.width * scale) / 2
        off_y = (widget_h - self._source.height * scale) / 2
        return (scale, off_x, off_y)

    def _box_rect(self, widget_w: int, widget_h: int) -> "tuple[float, float, float, float] | None":
        transform = self._display_transform(widget_w, widget_h)
        canvas = self.canvas_size()
        if transform is None or canvas is None or self._source is None:
            return None
        scale, off_x, off_y = transform
        left, top, right, bottom = viewport_rect(
            (self._source.width, self._source.height), canvas, self.view)
        return (off_x + left * scale, off_y + top * scale,
                off_x + right * scale, off_y + bottom * scale)

    # -- drawing -------------------------------------------------------------

    def on_draw(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        transform = self._display_transform(width, height)
        if transform is None or self._pixbuf is None or self._source is None:
            return
        scale, off_x, off_y = transform

        cr.save()
        cr.translate(off_x, off_y)
        cr.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(cr, self._pixbuf, 0, 0)
        cr.paint()
        cr.restore()

        box = self._box_rect(width, height)
        if box is None:
            return
        left, top, right, bottom = box

        # Dim everything outside the box, so the visible region reads at a
        # glance: four rectangles around it, clipped to the widget.
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.rectangle(0, 0, width, max(0.0, top))
        cr.rectangle(0, bottom, width, max(0.0, height - bottom))
        cr.rectangle(0, max(0.0, top), max(0.0, left), bottom - top)
        cr.rectangle(right, max(0.0, top), max(0.0, width - right), bottom - top)
        cr.fill()

        cr.set_source_rgba(1, 1, 1, 1)
        cr.set_line_width(2)
        cr.rectangle(left, top, right - left, bottom - top)
        cr.stroke()

    # -- interaction ---------------------------------------------------------

    def _apply_view(self, view: ViewTuple, live: bool) -> None:
        self.view = view
        self.entries[self.index] = (self.current_path(), view)
        self.preview.queue_draw()
        if live:
            now = time.monotonic()
            if now - self._last_live >= LIVE_PUSH_INTERVAL_S:
                self._last_live = now
                self.on_live(self.current_path(), view)
        else:
            self.on_live(self.current_path(), view)
            self.on_commit(self.current_path(), view)

    def on_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self._drag_anchor = (self.view[0], self.view[1])

    def on_drag_update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._drag_anchor is None or self._source is None:
            return
        transform = self._display_transform(self.preview.get_width(), self.preview.get_height())
        if transform is None:
            return
        display_scale = transform[0]
        anchor_x, anchor_y = self._drag_anchor
        new_x = anchor_x + (dx / display_scale) / self._source.width
        new_y = anchor_y + (dy / display_scale) / self._source.height
        new_x = min(max(new_x, 0.0), 1.0)
        new_y = min(max(new_y, 0.0), 1.0)
        self._apply_view((new_x, new_y, self.view[2]), live=True)

    def on_drag_end(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._drag_anchor is None:
            return
        self._drag_anchor = None
        self._apply_view(self.view, live=False)

    def on_scale_changed(self, spinner: Gtk.SpinButton) -> None:
        if self._loading:
            return
        view = (self.view[0], self.view[1], float(spinner.get_value()))
        self.view = view
        self.entries[self.index] = (self.current_path(), view)
        self.preview.queue_draw()
        self.on_live(self.current_path(), view)
        # Commit once the spinner settles, not per click of a held button.
        if self._scale_commit_handle is not None:
            GLib.source_remove(self._scale_commit_handle)
        self._scale_commit_handle = GLib.timeout_add(
            SCALE_COMMIT_DELAY_MS, self._commit_scale)

    def _commit_scale(self) -> bool:
        self._scale_commit_handle = None
        self.on_commit(self.current_path(), self.view)
        return False

    def _cancel_scale_commit(self, commit_now: bool) -> None:
        if self._scale_commit_handle is not None:
            GLib.source_remove(self._scale_commit_handle)
            self._scale_commit_handle = None
            if commit_now:
                self.on_commit(self.current_path(), self.view)

    def on_reset(self, button: Gtk.Button) -> None:
        self._cancel_scale_commit(commit_now=False)
        self._loading = True
        try:
            self.scale_spinner.set_value(DEFAULT_VIEW[2])
        finally:
            self._loading = False
        self._apply_view(DEFAULT_VIEW, live=False)

    def close_cleanly(self) -> None:
        """Flush a pending scale commit before the dialog goes away."""
        self._cancel_scale_commit(commit_now=True)
