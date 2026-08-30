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
# Import gtk modules
import gi

from GtkHelper.GtkHelper import RevertButton
from src.backend.DeckManagement.InputIdentifier import InputIdentifier, Input

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from gi.repository import GdkPixbuf
    from src.windows.mainWindow.elements.Sidebar.Sidebar import Sidebar
    from src.backend.PageManagement.Page import Page
from src.backend.DeckManagement.HelperMethods import is_video
from src.backend.DeckManagement.ImageHelpers import image2pixbuf
from src.backend.DeckManagement.media_loop import MEDIA_LOOP_FPS

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GLib

# Import Python modules
from loguru import logger as log

# Import globals
from src.backend import services

import globals as gl
from typing import Any, Protocol


def build_preview_pixbuf(image_path: str | None) -> "GdkPixbuf.Pixbuf | None":
    """Pixbuf for paths Gtk.Picture cannot render directly (videos, via their
    thumbnail); None means set_filename can handle the path itself."""
    if image_path and is_video(image_path):
        try:
            return image2pixbuf(gl.media_manager.get_thumbnail(image_path))
        except Exception as e:
            log.error(f"Could not build video preview thumbnail for {image_path}: {e}")
    return None


class _InputBoundRow(Protocol):
    """What _page_and_input needs of a row: the input it currently edits."""

    active_identifier: InputIdentifier | None
    active_state: int | None


def _page_and_input(row: _InputBoundRow) -> "tuple[Page, InputIdentifier, int] | None":
    """Return the page, identifier, and state that a row writes, or None.
    All three must be bound because Page setters use them to select the dict path."""
    page = services.require_main_window().get_active_page()
    identifier = row.active_identifier
    state = row.active_state
    if page is None or identifier is None or state is None:
        return None
    return page, identifier, state


class BackgroundEditor(Gtk.Box):
    def __init__(self, sidebar: "Sidebar", **kwargs: Any) -> None:
        self.sidebar = sidebar
        super().__init__(**kwargs)
        self.build()

    def build(self) -> None:
        self.clamp = Adw.Clamp()
        self.append(self.clamp)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.clamp.set_child(self.main_box)

        self.background_group = BackgroundGroup(self.sidebar)
        self.main_box.append(self.background_group)

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.background_group.load_for_identifier(identifier, state)


class BackgroundGroup(Adw.PreferencesGroup):
    def __init__(self, sidebar: "Sidebar", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sidebar = sidebar

        self.build()

    def build(self) -> None:
        self.expander = BackgroundExpanderRow(self)
        self.add(self.expander)

        return

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.expander.load_for_identifier(identifier, state)

class BackgroundExpanderRow(Adw.ExpanderRow):
    def __init__(self, label_group: BackgroundGroup) -> None:
        super().__init__(title=gl.lm.get("background-editor.header"), subtitle=gl.lm.get("background-editor-expander.subtitle"))
        self.label_group = label_group
        # Unset until load_for_identifier binds a row to an input.
        self.active_identifier: InputIdentifier | None = None
        self.active_state: int | None = None
        self.build()

    def build(self) -> None:
        self.color_row = ColorRow(sidebar=self.label_group.sidebar, expander=self)
        self.add_row(self.color_row)

        self.image_row = ImageRow(sidebar=self.label_group.sidebar, expander=self)
        self.add_row(self.image_row)

        self.video_loop_row = VideoLoopRow(sidebar=self.label_group.sidebar, expander=self)
        self.add_row(self.video_loop_row)

        self.video_fps_row = VideoFpsRow(sidebar=self.label_group.sidebar, expander=self)
        self.add_row(self.video_fps_row)

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.active_identifier = identifier
        self.active_state = state

        self.color_row.load_for_identifier(identifier, state)

        # Only show image row for touchscreens
        is_touchscreen = isinstance(identifier, Input.Touchscreen)
        self.image_row.set_visible(is_touchscreen)
        if is_touchscreen:
            self.image_row.load_for_identifier(identifier, state)

        self.update_video_rows()

    def update_video_rows(self) -> bool:
        # Show loop and FPS for touchscreen background video, but only FPS for key or dial media.
        # Key GIFs also use FPS because the row caps reads of their delay timeline.
        show_loop = False
        show_fps = False
        active_page = services.require_main_window().get_active_page()
        if active_page is None:
            return False
        identifier = self.active_identifier
        state = self.active_state
        # With no state selected neither branch runs and both rows hide, which
        # is what the isinstance chain did before the state was checked here.
        if state is not None and identifier is not None:
            if isinstance(identifier, Input.Touchscreen):
                path = active_page.get_background_image(identifier=identifier, state=state)
                show_loop = show_fps = bool(path and is_video(path))
            elif isinstance(identifier, (Input.Key, Input.Dial)):
                path = active_page.get_media_path(identifier=identifier, state=state)
                show_fps = bool(path and is_video(path))
                if isinstance(identifier, Input.Dial) and str(path).lower().endswith(".gif"):
                    # Keep GIF FPS unavailable for dials because their page load cannot build GIFs.
                    # Do not offer a rate for media that cannot reach the dial.
                    show_fps = False
        self.video_loop_row.set_visible(show_loop)
        self.video_fps_row.set_visible(show_fps)
        # Neither flag can be true unless the block above ran, so the two
        # extra checks only tell the checker what the flags already carry.
        if show_loop and identifier is not None and state is not None:
            self.video_loop_row.load_for_identifier(identifier, state)
        if show_fps and identifier is not None and state is not None:
            self.video_fps_row.load_for_identifier(identifier, state)
        return False  # usable directly as a GLib.idle_add callback

class ColorRow(Adw.PreferencesRow):
    def __init__(self, sidebar: "Sidebar", expander: BackgroundExpanderRow, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.expander = expander
        # Unset until load_for_identifier binds a row to an input.
        self.active_identifier: InputIdentifier | None = None
        self.active_state: int | None = None
        # Colour-handler ID, or None while disconnected.
        # Tracking keeps connect and disconnect idempotent.
        self._color_handler: int | None = None
        self.build()

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label=gl.lm.get("background-editor.color.label"), xalign=0, hexpand=True)
        self.main_box.append(self.label)
        self.button = ColorButton(self)
        self.main_box.append(self.button)

        self.color_dialog = Gtk.ColorDialog(title=gl.lm.get("background-editor.color.dialog.title"))

        self.button.button.set_dialog(self.color_dialog)

        # The revert click stays wired for the life of the row. Only the colour
        # handler toggles, so it alone is disconnected while values are set.
        self.button.revert_button.connect("clicked", self.on_revert)
        self.connect_signals()

    def connect_signals(self) -> None:
        if self._color_handler is None:
            self._color_handler = self.button.button.connect("notify::rgba", self.on_change_color)

    def disconnect_signals(self) -> None:
        if self._color_handler is not None:
            self.button.button.disconnect(self._color_handler)
            self._color_handler = None

    def set_color(self, color_values: list[int]) -> None:
        if len(color_values) == 3:
            color_values.append(255)
        color = Gdk.RGBA()
        color.parse(f"rgba({color_values[0]}, {color_values[1]}, {color_values[2]}, {color_values[3]/255})")
        self.button.button.set_rgba(color)

    def on_change_color(self, *args: object) -> None:
        target = _page_and_input(self)
        if target is None:
            return
        active_page, identifier, state = target
        color = self.button.button.get_rgba()
        green = round(color.green * 255)
        blue = round(color.blue * 255)
        red = round(color.red * 255)
        alpha = round(color.alpha * 255)

        active_page.set_background_color(identifier=identifier, state=state, color=[red, green, blue, alpha], update_ui=False)

        self.button.revert_button.set_visible(True)

    def on_revert(self, *args: object) -> None:
        # Ask before disconnecting. A return between the disconnect and the
        # reconnect leaves the button silently unwired.
        target = _page_and_input(self)
        if target is None:
            return
        active_page, identifier, state = target
        self.disconnect_signals()
        active_page.set_background_color(identifier=identifier, state=state, color=None, update_ui=True)
        self.button.revert_button.set_visible(False)
        self.connect_signals()

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.disconnect_signals()
        try:
            if gl.app is None:
                return
            self.active_identifier = identifier
            self.active_state = state

            active_page = gl.app.main_win.get_active_page()
            if active_page is None:
                return

            c_input = active_page.deck_controller.get_input(identifier)
            if c_input is None:
                log.error("Input not found")
                return

            c_state = c_input.states.get(state)
            if c_state is None:
                log.error("State not found")
                return

            self.set_color(c_state.background_manager.get_composed_color())

            self.button.revert_button.set_visible(c_state.background_manager.get_use_page_background())
        finally:
            # A lookup that returns early must still leave the button wired, or
            # every later colour change is dropped silently.
            self.connect_signals()

class ColorButton(Gtk.Box):
    def __init__(self, color_row: ColorRow, **kwargs: Any) -> None:
        super().__init__(css_classes=["linked"], **kwargs)
        
        self.button = Gtk.ColorDialogButton()
        self.revert_button = RevertButton()

        self.append(self.button)
        self.append(self.revert_button)

class VideoLoopRow(Adw.PreferencesRow):
    def __init__(self, sidebar: "Sidebar", expander: BackgroundExpanderRow, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.expander = expander
        # Unset until load_for_identifier binds a row to an input.
        self.active_identifier: InputIdentifier | None = None
        self.active_state: int | None = None
        # The toggle handler id, or None while it is disconnected. A tracked id
        # keeps connect and disconnect idempotent across early-return loads.
        self._toggle_handler: int | None = None
        self.build()

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label="Loop", xalign=0, hexpand=True)
        self.main_box.append(self.label)

        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.main_box.append(self.switch)

        self.connect_signals()

    def connect_signals(self) -> None:
        if self._toggle_handler is None:
            self._toggle_handler = self.switch.connect("notify::active", self.on_toggle)

    def disconnect_signals(self) -> None:
        if self._toggle_handler is not None:
            self.switch.disconnect(self._toggle_handler)
            self._toggle_handler = None

    def on_toggle(self, *args: object) -> None:
        target = _page_and_input(self)
        if target is None:
            return
        active_page, identifier, state = target
        active_page.set_background_loop(identifier=identifier, state=state,
                                        loop=self.switch.get_active(), update=True)

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.disconnect_signals()
        try:
            self.active_identifier = identifier
            self.active_state = state
            if gl.app is None:
                return
            active_page = gl.app.main_win.get_active_page()
            if active_page is None:
                return
            self.switch.set_active(active_page.get_background_loop(identifier=identifier, state=state))
        finally:
            # A lookup that returns early must still leave the switch wired.
            self.connect_signals()


class VideoFpsRow(Adw.PreferencesRow):
    # Use the common FPS range with the media-loop ceiling as its top.
    # A value at the ceiling means no cap, like a page with no FPS key.
    MIN_FPS = 1
    MAX_FPS = MEDIA_LOOP_FPS

    # Delay revert by 250 ms so rapid spinner steps do not flicker or shift it.
    # This outlasts an input burst but still appears tied to the edit.
    REVEAL_DELAY_MS = 250

    def __init__(self, sidebar: "Sidebar", expander: BackgroundExpanderRow, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.expander = expander
        # Unset until load_for_identifier binds a row to an input.
        self.active_identifier: InputIdentifier | None = None
        self.active_state: int | None = None
        # The change handler id, or None while it is disconnected. A tracked id
        # keeps connect and disconnect idempotent across early-return loads.
        self._change_handler: int | None = None
        # Pending reveal source, or None; all access occurs on the GTK main thread.
        # Spinner, revert, sidebar-load, and idle paths therefore need no lock.
        self._reveal_source: int | None = None
        self.build()

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label="FPS", xalign=0, hexpand=True)
        self.main_box.append(self.label)

        self.button_box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        self.main_box.append(self.button_box)

        self.spinner = Gtk.SpinButton.new_with_range(self.MIN_FPS, self.MAX_FPS, 1)
        self.button_box.append(self.spinner)

        self.revert_button = RevertButton()
        self.revert_button.set_tooltip_text(gl.lm.get("background-editor.fps.reset"))
        self.revert_button.set_visible(False)
        self.button_box.append(self.revert_button)

        # Keep the revert click connected for the row's lifetime.
        # Disconnect only the spinner handler while a load writes widget values.
        self.revert_button.connect("clicked", self.on_revert)
        self.connect_signals()

    def connect_signals(self) -> None:
        if self._change_handler is None:
            self._change_handler = self.spinner.connect("value-changed", self.on_change)

    def disconnect_signals(self) -> None:
        if self._change_handler is not None:
            self.spinner.disconnect(self._change_handler)
            self._change_handler = None

    def cancel_reveal(self) -> None:
        """Cancel a pending reveal and clear its source ID.
        A fired source must not leave an ID that can make a later cancel target the wrong source."""
        if self._reveal_source is not None:
            GLib.source_remove(self._reveal_source)
            self._reveal_source = None

    def _reveal_revert(self) -> bool:
        """Reveal revert only if the row's currently bound input still has an FPS override.
        Re-read the page because a hidden row can remain bound to an earlier input."""
        self._reveal_source = None
        target = _page_and_input(self)
        if target is not None:
            self.revert_button.set_visible(self._has_override(*target))
        return GLib.SOURCE_REMOVE

    def _request_revert(self, show: bool) -> None:
        """Hide revert immediately when no override exists; reveal it after the rate settles.
        Keep visible controls in place; restart only pending reveals on each step."""
        self.cancel_reveal()
        if not show:
            self.revert_button.set_visible(False)
            return
        if self.revert_button.get_visible():
            return
        self._reveal_source = GLib.timeout_add(self.REVEAL_DELAY_MS, self._reveal_revert)

    def _uses_media_fps(self) -> bool:
        # A key or a dial caps its media video, and the touchscreen caps its
        # background video.
        return isinstance(self.active_identifier, (Input.Key, Input.Dial))

    def _write_fps(self, active_page: "Page", identifier: InputIdentifier, state: int,
                   fps: int | None) -> None:
        """Store a cap for this row's target, or clear it when fps is None."""
        if self._uses_media_fps():
            active_page.set_media_fps(identifier=identifier, state=state,
                                      fps=fps, update=True)
        else:
            active_page.set_background_fps(identifier=identifier, state=state,
                                           fps=fps, update=True)

    def _stored_fps(self, active_page: "Page", identifier: InputIdentifier, state: int) -> int:
        """The cap the page stores for this row's target, or the ceiling when
        it stores none."""
        if self._uses_media_fps():
            return active_page.get_media_fps(identifier=identifier, state=state)
        return active_page.get_background_fps(identifier=identifier, state=state)

    def _has_override(self, active_page: "Page", identifier: InputIdentifier, state: int) -> bool:
        """Return whether the page stores an effective FPS cap.
        Treat a value at the render ceiling as uncapped, including older stored values."""
        if self._uses_media_fps():
            stored = active_page.has_media_fps(identifier=identifier, state=state)
        else:
            stored = active_page.has_background_fps(identifier=identifier, state=state)
        return stored and self._stored_fps(active_page, identifier, state) < self.MAX_FPS

    def _displayed_fps(self, active_page: "Page", identifier: InputIdentifier, state: int) -> int:
        """Return the effective cap, or the media's native FPS within the spinner range.
        Use the ceiling when an uncapped pipeline reports no native rate."""
        if self._has_override(active_page, identifier, state):
            return self._stored_fps(active_page, identifier, state)
        if self._uses_media_fps():
            native = active_page.get_media_native_fps(identifier=identifier, state=state)
            if native is not None:
                return max(self.MIN_FPS, min(self.MAX_FPS, round(native)))
        return self.MAX_FPS

    def on_change(self, *args: object) -> None:
        target = _page_and_input(self)
        if target is None:
            return
        active_page, identifier, state = target
        fps = int(self.spinner.get_value())
        # Store no key for the uncapped top of the range.
        # Choosing it has the same meaning as revert and avoids ineffective caps.
        stored = None if fps >= self.MAX_FPS else fps
        self._write_fps(active_page, identifier, state, stored)
        self._request_revert(stored is not None)

    def on_revert(self, *args: object) -> None:
        # Ask before disconnecting. A return between the disconnect and the
        # reconnect leaves the spinner silently unwired.
        target = _page_and_input(self)
        if target is None:
            return
        active_page, identifier, state = target
        self.disconnect_signals()
        try:
            self._write_fps(active_page, identifier, state, None)
            # Read the rate back after the clear, so the row shows what the
            # media now runs at rather than the cap that was just dropped.
            self.spinner.set_value(self._displayed_fps(active_page, identifier, state))
            # No cancel here. A reveal is armed only while the arrow is off
            # screen, and an arrow that is off screen cannot be clicked.
            self.revert_button.set_visible(False)
        finally:
            # An exception in between must still leave the spinner wired, or
            # every later change is dropped silently.
            self.connect_signals()

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.disconnect_signals()
        # Cancel an earlier reveal before binding and before every possible early return.
        # The old source must not change the arrow after this load settles it.
        self.cancel_reveal()
        try:
            self.active_identifier = identifier
            self.active_state = state
            if gl.app is None:
                return
            active_page = gl.app.main_win.get_active_page()
            if active_page is None:
                return
            self.spinner.set_value(self._displayed_fps(active_page, identifier, state))
            self.revert_button.set_visible(self._has_override(active_page, identifier, state))
        finally:
            # A lookup that returns early must still leave the spinner wired.
            self.connect_signals()


class ImageRow(Adw.PreferencesRow):
    def __init__(self, sidebar: "Sidebar", expander: BackgroundExpanderRow, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.expander = expander
        # Unset until load_for_identifier binds a row to an input.
        self.active_identifier: InputIdentifier | None = None
        self.active_state: int | None = None
        self.build()

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15,
                                spacing=10)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label="Background", xalign=0, hexpand=True)
        self.main_box.append(self.label)
        
        # Image preview with constrained size
        self.preview_frame = Gtk.Frame(css_classes=["card"])
        self.preview_frame.set_size_request(48, 48)
        self.preview = Gtk.Picture(
            content_fit=Gtk.ContentFit.COVER,
            overflow=Gtk.Overflow.HIDDEN,
            width_request=48,
            height_request=48,
        )
        self.preview_frame.set_child(self.preview)
        self.preview_frame.set_visible(False)
        self.main_box.append(self.preview_frame)
        
        self.button_box = Gtk.Box(css_classes=["linked"])
        self.main_box.append(self.button_box)
        
        self.select_button = Gtk.Button(icon_name="folder-open-symbolic")
        self.select_button.connect("clicked", self.on_select_image)
        self.button_box.append(self.select_button)
        
        self.clear_button = Gtk.Button(icon_name="edit-clear-symbolic")
        self.clear_button.connect("clicked", self.on_clear_image)
        self.clear_button.set_visible(False)
        self.button_box.append(self.clear_button)

    def on_select_image(self, button: Gtk.Button) -> None:
        target = _page_and_input(self)
        if target is None:
            return
        active_page, identifier, state = target
        current_path = active_page.get_background_image(identifier=identifier, state=state)
        services.require_app().let_user_select_asset(default_path=current_path, callback_func=self.set_background_image)

    def set_background_image(self, file_path: str) -> None:
        if not file_path or gl.app is None:
            return
        target = _page_and_input(self)
        if target is None:
            return
        active_page, identifier, state = target
        active_page.set_background_image(identifier=identifier, state=state, path=file_path, update=True)
        # The custom-assets chooser can call from a worker thread.
        # Marshal widget changes onto the GTK main loop.
        GLib.idle_add(self.clear_button.set_visible, True)
        GLib.idle_add(self.expander.update_video_rows)
        self.update_preview(file_path)

    def on_clear_image(self, button: Gtk.Button) -> None:
        target = _page_and_input(self)
        if target is None:
            return
        active_page, identifier, state = target
        active_page.set_background_image(identifier=identifier, state=state, path=None, update=True)
        self.clear_button.set_visible(False)
        self.expander.update_video_rows()
        self.update_preview(None)

    def update_preview(self, image_path: str | None) -> None:
        # Safe from any thread; decode the thumbnail on the caller.
        # Marshal widget updates onto the GTK main loop.
        GLib.idle_add(self._apply_preview, image_path, build_preview_pixbuf(image_path))

    def _apply_preview(self, image_path: str | None, pixbuf: "GdkPixbuf.Pixbuf | None") -> bool:
        if image_path:
            if pixbuf is not None:
                self.preview.set_pixbuf(pixbuf)
            else:
                self.preview.set_filename(image_path)
            self.preview_frame.set_visible(True)
        else:
            self.preview.set_filename(None)
            self.preview_frame.set_visible(False)
        return False

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.active_identifier = identifier
        self.active_state = state

        if gl.app is None:
            return
        active_page = gl.app.main_win.get_active_page()
        if active_page is None:
            return
        image_path = active_page.get_background_image(identifier=identifier, state=state)
        self.clear_button.set_visible(image_path is not None)
        self.update_preview(image_path)
