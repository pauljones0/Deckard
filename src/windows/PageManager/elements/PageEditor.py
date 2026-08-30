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
import gi

from GtkHelper.ScaleRow import ScaleRow
from src.backend import services
from src.backend.DeckManagement.ImageHelpers import image2pixbuf
from src.backend.DeckManagement.deck_controller.background_media import background_canvas_size
from src.backend.DeckManagement.deck_controller.viewport import normalize_view, view_as_setting
from src.windows.mainWindow.elements.ViewportDialog import ViewportDialog
from src.windows.MultiDeckSelector.MultiDeckSelectorRow import MultiDeckSelectorRow
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GObject, Gtk, Adw, GLib

# Import typing
from collections.abc import Callable
from typing import Any, TYPE_CHECKING, override
if TYPE_CHECKING:
    from src.windows.PageManager.PageManager import PageManager

# Import globals
import globals as gl

# Import python modules
import os

# Import own modules
from GtkHelper.GtkHelper import BetterExpander
from src.backend.main_loop import run_in_background
from src.backend.WindowGrabber.Window import Window
from src.windows.PageManager.elements.MenuButton import MenuButton

class PageEditor(Adw.NavigationPage):
    def __init__(self, page_manager: "PageManager"):
        super().__init__(title=gl.lm.get("page-manager.page-editor.title"))
        self.page_manager = page_manager
        # None until load_for_page runs. The editor opens on its stack child
        # for no page selected. See delete_active_page.
        self.active_page_path: str | None = None
        self.build()

    def get_page_data(self) -> dict[str, Any]:
        if gl.page_manager is None or self.active_page_path is None:
            return {}
        return gl.page_manager.get_page_data(self.active_page_path, use_backup=False)

    def set_page_data(self, data: dict[str, Any], reload_brightness: bool = True, reload_screensaver: bool = True, reload_background: bool = True, reload_inputs: bool = True) -> None:
        if gl.page_manager is None or self.active_page_path is None:
            return
        gl.page_manager.set_page_data(self.active_page_path, data, reload_brightness=reload_brightness, reload_screensaver=reload_screensaver, reload_background=reload_background, reload_inputs=reload_inputs)

    def build(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.set_child(self.main_box)

        # Header
        self.header = Adw.HeaderBar(show_back_button=False, css_classes=["flat"], show_end_title_buttons=True)
        self.main_box.append(self.header)

        # Menu button
        self.menu_button = MenuButton(self)
        self.header.pack_end(self.menu_button)

        # Main stack - one page for the normal editor and one for the no page info screen
        self.main_stack = Gtk.Stack(hexpand=True, vexpand=True, margin_bottom=20)
        self.main_box.append(self.main_stack)

        # The box for the normal editor
        self.editor_main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.main_stack.add_titled(self.editor_main_box, "editor", "Editor")

        # Scrolled window for  the normal editor
        self.scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.editor_main_box.append(self.scrolled_window)

        # Clamp for the scrolled window
        self.clamp = Adw.Clamp(margin_top=40)
        self.scrolled_window.set_child(self.clamp)

        # Box for all widgets in the editor
        self.editor_main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.clamp.set_child(self.editor_main_box)

        # Name group - Used to rename the page
        self.name_group = NameGroup(page_editor=self)
        self.editor_main_box.append(self.name_group)

        # Default page group - Used to configure default page for decks
        self.default_page_group = DefaultPageGroup(page_editor=self)
        self.editor_main_box.append(self.default_page_group)

        # Auto change group - Used to configure automatic page switching
        self.auto_change_group = AutoChangeGroup(page_editor=self)
        self.editor_main_box.append(self.auto_change_group)

        # Brightness Group
        self.brightness_group = BrightnessGroup(page_editor=self)
        self.editor_main_box.append(self.brightness_group)

        # Background Group
        self.background_group = BackgroundGroup(page_editor=self)
        self.editor_main_box.append(self.background_group)

        # Screensaver
        self.screensaver_group = ScreensaverGroup(page_editor=self)
        self.editor_main_box.append(self.screensaver_group)

        # Every group in one list, so teardown reaches all of them.
        self.groups: list[PageEditorGroup] = [
            self.name_group, self.default_page_group, self.auto_change_group,
            self.brightness_group, self.background_group, self.screensaver_group,
        ]

        # No page page
        self.no_page_box = Gtk.Box(hexpand=True, vexpand=True)
        self.main_stack.add_titled(self.no_page_box, "no-page", "No Page")

        self.no_page_box.append(Gtk.Label(label=gl.lm.get("page-manager.page-editor.no-page-selected"), halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, hexpand=True))

        # Default to the no page info screen
        self.main_stack.set_visible_child_name("no-page")

    def require_active_page_path(self) -> str:
        """Return the active page path or raise before any override write.

        Readers must still accept no path while groups build on the no-page view.
        """
        path = self.active_page_path
        if path is None:
            raise RuntimeError(
                "the page editor has no active page -- load_for_page binds "
                "it, and the editor shows its no-page child until then."
            )
        return path

    def load_for_page(self, page_path: str) -> None:
        self.active_page_path = page_path
        self.name_group.load_for_page(page_path=page_path)
        self.default_page_group.load_for_page(page_path=page_path)
        self.auto_change_group.load_for_page(page_path=page_path)
        self.brightness_group.load_for_page(page_path=page_path)
        self.background_group.load_for_page(page_path=page_path)
        self.screensaver_group.load_for_page(page_path=page_path)

        self.main_stack.set_visible_child_name("editor")
        self.menu_button.set_page_specific_actions_enabled(True)

    def delete_active_page(self) -> None:
        if self.active_page_path is None:
            return

        self.page_manager.remove_page_by_path(self.active_page_path)

    def teardown(self) -> None:
        """Commit pending text, then disconnect row handlers before destruction.

        This order preserves unapplied text and prevents callbacks on dead widgets.
        """
        self.auto_change_group.commit_pending_patterns()
        for group in self.groups:
            group.disconnect_events()

class PageEditorGroup(Adw.PreferencesGroup):
    def __init__(self, page_editor: PageEditor, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.page_editor = page_editor
        # Track one handler per binding key so repeated connect and disconnect
        # operations neither stack callbacks nor disconnect an absent handler.
        self._handlers: dict[str, int] = {}
        self.build()

    def build(self) -> None:
        pass

    def _signal_bindings(self) -> list[tuple[str, GObject.Object, str, Callable[..., Any]]]:
        """Return bindings as (key, widget, signal, callback).

        Each widget must be the object that owns the connected handler.
        """
        return []

    def connect_events(self) -> None:
        for key, widget, signal, callback in self._signal_bindings():
            if self._handlers.get(key) is None:
                self._handlers[key] = widget.connect(signal, callback)

    def disconnect_events(self) -> None:
        for key, widget, _signal, _callback in self._signal_bindings():
            handler = self._handlers.pop(key, None)
            if handler is not None:
                widget.disconnect(handler)

    def load_config_settings(self, page_path: str) -> None:
        pass

    def load_for_page(self, page_path: str) -> None:
        self.disconnect_events()
        try:
            self.load_config_settings(page_path)
        finally:
            # A load that returns early or raises must still leave the rows
            # wired, or every later edit in this group is dropped silently.
            self.connect_events()

class NameGroup(PageEditorGroup):
    def __init__(self, page_editor: PageEditor):
        super().__init__(page_editor)

    @override
    def build(self) -> None:
        self.name_entry = Adw.EntryRow(title=gl.lm.get("page-manager.page-editor.name-group.name"), show_apply_button=True)
        self.add(self.name_entry)

    @override
    def _signal_bindings(self) -> list[tuple[str, GObject.Object, str, Callable[..., Any]]]:
        return [
            ("name-changed", self.name_entry, "changed", self.on_name_changed),
            ("name-apply", self.name_entry, "apply", self.on_name_change_applied),
        ]

    @override
    def load_config_settings(self, page_path: str | None) -> None:
        if page_path is None:
            return

        page_name = os.path.basename(page_path).split(".")[0]
        self.name_entry.set_text(page_name)

        base_path = os.path.dirname(page_path)
        is_user_page = base_path == os.path.join(gl.DATA_PATH, "pages")

        self.set_sensitive(is_user_page)

    def on_name_changed(self, entry: Adw.EntryRow, *args: object) -> None:
        active_page_path = self.page_editor.active_page_path
        if active_page_path is None or gl.page_manager is None:
            return
        original_name = os.path.basename(active_page_path).split(".")[0]
        new_name = entry.get_text()

        all_page_names = gl.page_manager.get_page_names()
        all_page_names.remove(original_name)
        all_page_names.append("")

        if new_name in all_page_names:
            entry.add_css_class("error")
            entry.set_show_apply_button(False)
        else:
            entry.remove_css_class("error")
            entry.set_show_apply_button(True)

    def on_name_change_applied(self, entry: Adw.EntryRow, *args: object) -> None:
        original_path = self.page_editor.active_page_path
        if original_path is None:
            return
        new_path = os.path.join(os.path.dirname(original_path), f"{entry.get_text()}.json")

        if original_path == new_path:
            return

        self.page_editor.page_manager.rename_page_by_path(original_path, new_path)

class DefaultPageGroup(PageEditorGroup):
    def __init__(self, page_editor: PageEditor):
        super().__init__(page_editor, title=gl.lm.get("page-manager.page-editor.default-page.title"))

    @override
    def build(self) -> None:
        self.deck_selector = MultiDeckSelectorRow(
            source_window=self.page_editor.page_manager,
            title=gl.lm.get("page-manager.page-editor.default-page.row.title"),
            subtitle=gl.lm.get("page-manager.page-editor.default-page.row.subtitle"),
            callback=self.on_deck_changed,
            selected_deck_serials=services.require_page_manager().get_serial_numbers_from_page(self.page_editor.active_page_path)
        )
        self.add(self.deck_selector)

    @override
    def load_config_settings(self, page_path: str) -> None:
        if gl.page_manager is None:
            return
        serial_numbers = gl.page_manager.get_serial_numbers_from_page(page_path)

        self.deck_selector.set_label(len(serial_numbers))
        self.deck_selector.set_selected_deck_serials(serial_numbers)

    def on_deck_changed(self, serial_number: str, state: bool) -> None:
        if gl.page_manager is None:
            return
        path: str | None = self.page_editor.active_page_path

        if not state:
            path = None

        # None clears the default page; default-page lookups skip falsy entries.
        gl.page_manager.set_default_page(serial_number, path)

class AutoChangeGroup(PageEditorGroup):
    def __init__(self, page_editor: PageEditor):
        super().__init__(page_editor, title=gl.lm.get("page-manager.page-editor.change-group.title"))

    @override
    def build(self) -> None:
        self.enable_toggle = Adw.SwitchRow(title=gl.lm.get("page-manager.page-editor.change-group.enable"))
        self.add(self.enable_toggle)

        self.stay_on_page_toggle = Adw.SwitchRow(title="Stay on page", subtitle="Stay on the page until another page matches")
        self.add(self.stay_on_page_toggle)

        self.deck_selector = MultiDeckSelectorRow(
            source_window=self.page_editor.page_manager,
            title="Decks",
            subtitle="Decks on which the page should be loaded",
            callback=self.on_deck_changed,
            selected_deck_serials=services.require_page_manager().get_serial_numbers_from_page(self.page_editor.active_page_path)
        )
        self.add(self.deck_selector)

        self.title_entry = Adw.EntryRow(title=gl.lm.get("page-manager.page-editor.change-group.title-regex"), text="", show_apply_button=True)
        self.add(self.title_entry)

        self.wm_class_entry = Adw.EntryRow(title=gl.lm.get("page-manager.page-editor.change-group.wm-class-regex"), text="", show_apply_button=True)
        self.add(self.wm_class_entry)

        # Commit on focus leave too, or text abandoned by a click or window
        # close remains only in the widget.
        self.title_focus = Gtk.EventControllerFocus()
        self.title_entry.add_controller(self.title_focus)

        self.wm_class_focus = Gtk.EventControllerFocus()
        self.wm_class_entry.add_controller(self.wm_class_focus)

        self.matching_window_expander = MatchingWindowExpander(auto_change_group=self)
        self.add(self.matching_window_expander)

    @override
    def _signal_bindings(self) -> list[tuple[str, GObject.Object, str, Callable[..., Any]]]:
        return [
            ("enable", self.enable_toggle, "notify::active", self.on_enable_changed),
            ("stay-on-page", self.stay_on_page_toggle, "notify::active", self.on_stay_on_page_changed),
            ("title-apply", self.title_entry, "apply", self.on_title_entry_applied),
            ("wm-class-apply", self.wm_class_entry, "apply", self.on_wm_class_entry_applied),
            ("title-leave", self.title_focus, "leave", self.on_title_focus_left),
            ("wm-class-leave", self.wm_class_focus, "leave", self.on_wm_class_focus_left),
        ]

    @override
    def load_config_settings(self, page_path: str) -> None:
        active_page_path = self.page_editor.active_page_path
        if gl.page_manager is None or active_page_path is None:
            return
        auto_change = gl.page_manager.get_auto_change_settings(active_page_path)

        self.enable_toggle.set_active(auto_change.get("enable", False))
        self.stay_on_page_toggle.set_active(auto_change.get("stay-on-page", True))
        self.wm_class_entry.set_text(auto_change.get("wm-class", ""))
        self.title_entry.set_text(auto_change.get("title", ""))
        self.deck_selector.set_selected_deck_serials(auto_change.get("decks", []).copy())

    def on_enable_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_auto_change_settings(
            path=self.page_editor.require_active_page_path(),
            enable=self.enable_toggle.get_active()
        )
        self.recheck_active_window()

    def on_stay_on_page_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_auto_change_settings(
            path=self.page_editor.require_active_page_path(),
            stay_on_page=self.stay_on_page_toggle.get_active()
        )
        self.recheck_active_window()

    def on_title_entry_applied(self, *args: object) -> None:
        self.matching_window_expander.update_matching_windows()

        services.require_page_manager().overwrite_auto_change_settings(
            path=self.page_editor.require_active_page_path(),
            regex_title=self.title_entry.get_text()
        )
        self.recheck_active_window()

    def on_wm_class_entry_applied(self, *args: object) -> None:
        self.matching_window_expander.update_matching_windows()

        services.require_page_manager().overwrite_auto_change_settings(
            path=self.page_editor.require_active_page_path(),
            wm_class=self.wm_class_entry.get_text()
        )
        self.recheck_active_window()

    def on_title_focus_left(self, *args: object) -> None:
        if self.is_stored_pattern("title", self.title_entry.get_text()):
            # Recheck even without a write because focus can move to the window
            # that the already-committed rule must now match.
            self.recheck_active_window()
            return
        self.on_title_entry_applied()

    def on_wm_class_focus_left(self, *args: object) -> None:
        if self.is_stored_pattern("wm-class", self.wm_class_entry.get_text()):
            self.recheck_active_window()
            return
        self.on_wm_class_entry_applied()

    def commit_pending_patterns(self) -> None:
        """Write pending entry text before teardown removes its widgets.

        Update only page settings because list refreshes can outlive the widgets.
        """
        page_manager = gl.page_manager
        path = self.page_editor.active_page_path
        if page_manager is None or path is None:
            return

        title = self.title_entry.get_text()
        wm_class = self.wm_class_entry.get_text()
        committed = False

        if not self.is_stored_pattern("title", title):
            page_manager.overwrite_auto_change_settings(path=path, regex_title=title)
            committed = True
        if not self.is_stored_pattern("wm-class", wm_class):
            page_manager.overwrite_auto_change_settings(path=path, wm_class=wm_class)
            committed = True

        if committed:
            self.recheck_active_window()

    def is_stored_pattern(self, key: str, text: str) -> bool:
        """Return whether the page stores this pattern, with absent as empty.

        Return True when no page is loaded to prevent writes without a target.
        """
        page_manager = gl.page_manager
        path = self.page_editor.active_page_path
        if page_manager is None or path is None:
            return True
        return (page_manager.get_auto_change_settings(path).get(key) or "") == text

    def recheck_active_window(self) -> None:
        """Apply edited rules to the current window without waiting for a change.

        The window grabber runs in the background because it can load a page.
        """
        window_grabber = gl.window_grabber
        if window_grabber is None:
            return
        window_grabber.recheck_active_window()

    def on_deck_changed(self, serial_number: str, state: bool) -> None:
        page_manager = gl.page_manager
        path = self.page_editor.active_page_path
        if page_manager is None or path is None:
            return
        info = page_manager.get_auto_change_settings(path)
        decks = info.get("decks", [])

        if state and serial_number not in decks:
            decks.append(serial_number)
        elif not state and serial_number in decks:
            decks.remove(serial_number)
        else:
            return

        page_manager.overwrite_auto_change_settings(path, decks=decks)

class BrightnessGroup(PageEditorGroup):
    def __init__(self, page_editor: PageEditor):
        super().__init__(page_editor, title="Brightness Override")

    @override
    def build(self) -> None:
        self.enable_expander = BetterExpander(
            title="Overwrite Brightness",
            subtitle="Overrides the Deck Brightness",
            expanded=False,
            show_enable_switch=True
        )
        self.add(self.enable_expander)

        self.brightness_scale = ScaleRow(0, 0, 100, digits=0, draw_value=True, draw_side_values=False, title="Brightness")
        self.enable_expander.add_row(self.brightness_scale)

    @override
    def _signal_bindings(self) -> list[tuple[str, GObject.Object, str, Callable[..., Any]]]:
        return [
            ("enable", self.enable_expander, "notify::enable-expansion", self.on_enable_changed),
            ("brightness", self.brightness_scale.scale, "value-changed", self.on_brightness_changed),
        ]

    @override
    def load_config_settings(self, page_path: str) -> None:
        if gl.page_manager is None:
            return
        settings = gl.page_manager.get_brightness_settings(page_path)

        self.enable_expander.set_enable_expansion(settings.get("overwrite", False))
        self.enable_expander.set_expanded(settings.get("overwrite", False))

        self.brightness_scale.set_value(settings.get("value", 75))

    def on_enable_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_brightness_settings(
            path=self.page_editor.require_active_page_path(),
            overwrite=self.enable_expander.get_enable_expansion()
        )
        self.update_brightness()

    def on_brightness_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_brightness_settings(
            path=self.page_editor.require_active_page_path(),
            brightness=self.brightness_scale.get_value()
        )
        self.update_brightness()

    def update_brightness(self) -> None:
        def on_idle() -> None:
            for controller in services.require_deck_manager().deck_controller:
                page = controller.active_page
                # A deck that carries no page must not stop the reload for
                # the decks after it in this list.
                if page is None:
                    continue
                if page.json_path == self.page_editor.active_page_path:
                    controller.load_brightness(page)

        GLib.idle_add(on_idle)

class BackgroundGroup(PageEditorGroup):
    def __init__(self, page_editor: PageEditor):
        super().__init__(page_editor, title="Background Override")

    @override
    def build(self) -> None:
        self.enable_expander = BetterExpander(
            title="Overwrite Background",
            subtitle="Overrides the Deck Background",
            expanded=False,
            show_enable_switch=True
        )
        self.add(self.enable_expander)

        self.media_main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.enable_expander.add_row(self.media_main_box)

        self.media_settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.media_main_box.append(self.media_settings_box)

        self.show_background_toggle = Adw.SwitchRow(title="Show Background")
        self.media_settings_box.append(self.show_background_toggle)

        self.loop_toggle = Adw.SwitchRow(title="Loop")
        self.media_settings_box.append(self.loop_toggle)

        self.fps_spin = Adw.SpinRow.new_with_range(0, 30, 1)
        self.fps_spin.set_title("FPS")
        self.media_settings_box.append(self.fps_spin)

        self.extend_touchscreen_toggle = Adw.SwitchRow(
            title="Extend to Touchscreen",
            subtitle="Continue the background onto the touch strip (Stream Deck +)"
        )
        self.media_settings_box.append(self.extend_touchscreen_toggle)

        self.button_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.media_main_box.append(self.button_box)

        self.media_selector_button = Gtk.Button(
            label="Select",
            css_classes=["page-settings-media-selector"],
            halign=Gtk.Align.CENTER,
        )
        self.button_box.append(self.media_selector_button)

        self.media_selector_image = Gtk.Image()

        # Pan and zoom the visible region of the page's own background media.
        self.adjust_view_button = Gtk.Button(
            label=gl.lm.get("deck.background-group.adjust-view"),
            halign=Gtk.Align.CENTER, margin_top=10,
        )
        self.button_box.append(self.adjust_view_button)

    @override
    def _signal_bindings(self) -> list[tuple[str, GObject.Object, str, Callable[..., Any]]]:
        return [
            ("enable", self.enable_expander, "notify::enable-expansion", self.on_enable_changed),
            ("show", self.show_background_toggle, "notify::active", self.on_show_background_changed),
            ("loop", self.loop_toggle, "notify::active", self.on_loop_changed),
            ("fps", self.fps_spin, "changed", self.on_fps_changed),
            ("extend-touchscreen", self.extend_touchscreen_toggle, "notify::active", self.on_extend_touchscreen_changed),
            ("media-selector", self.media_selector_button, "clicked", self.on_media_selector_click),
            ("adjust-view", self.adjust_view_button, "clicked", self.on_adjust_view),
        ]

    def _controllers_showing_page(self) -> "list[Any]":
        """Every deck controller whose active page is the one being edited."""
        page_path = self.page_editor.active_page_path
        return [
            controller for controller in services.require_deck_manager().deck_controller
            if controller.active_page is not None
            and controller.active_page.json_path == page_path
        ]

    def on_adjust_view(self, *args: object) -> None:
        background_settings = services.require_page_manager().get_background_settings(
            self.page_editor.active_page_path)
        media_path = background_settings.get("media-path")
        if not media_path:
            return
        showing = self._controllers_showing_page()
        # The box needs a deck canvas for its aspect. A deck showing this page
        # is the natural one; with none showing, any connected deck gives the
        # geometry, and with no deck at all there is nothing to aim at.
        controllers = showing or list(services.require_deck_manager().deck_controller)
        if not controllers:
            return
        geometry = controllers[0]
        dialog = ViewportDialog(
            [(media_path, normalize_view(background_settings.get("view")))],
            canvas_size=lambda: background_canvas_size(
                geometry, geometry.background.extend_to_touchscreen),
            on_live=self.on_view_live,
            on_commit=self.on_view_commit,
        )
        dialog.connect("closed", lambda d: d.close_cleanly())
        dialog.present(self)

    def on_view_live(self, path: str, view: "tuple[float, float, float]") -> None:
        """The drag preview, on every deck showing this page whose current
        image is the page's media; a video waits for the commit's reload."""
        for controller in self._controllers_showing_page():
            image = controller.background.image
            if image is not None and image.path == path:
                controller.background.update_view(view)

    def on_view_commit(self, path: str, view: "tuple[float, float, float]") -> None:
        services.require_page_manager().overwrite_background_settings(
            path=self.page_editor.require_active_page_path(),
            view=view_as_setting(view),
        )
        self.update_background()

    @override
    def load_config_settings(self, page_path: str) -> None:
        if gl.page_manager is None:
            return
        background_settings = gl.page_manager.get_background_settings(page_path)

        self.enable_expander.set_enable_expansion(background_settings.get("overwrite", False))
        self.enable_expander.set_expanded(background_settings.get("overwrite", False))

        self.show_background_toggle.set_active(background_settings.get("show", False))
        self.loop_toggle.set_active(background_settings.get("loop", False))
        self.fps_spin.set_value(background_settings.get("fps", 0))
        self.extend_touchscreen_toggle.set_active(background_settings.get("extend-to-touchscreen", False))
        self.set_thumbnail(background_settings.get("media-path", None))

    def on_enable_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_background_settings(
            path=self.page_editor.require_active_page_path(),
            overwrite=self.enable_expander.get_enable_expansion()
        )
        self.update_background()

    def on_show_background_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_background_settings(
            path=self.page_editor.require_active_page_path(),
            show=self.show_background_toggle.get_active()
        )
        self.update_background()

    def on_loop_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_background_settings(
            path=self.page_editor.require_active_page_path(),
            loop=self.loop_toggle.get_active()
        )
        self.update_background()

    def on_fps_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_background_settings(
            path=self.page_editor.require_active_page_path(),
            fps=int(self.fps_spin.get_value())
        )
        self.update_background()

    def on_extend_touchscreen_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_background_settings(
            path=self.page_editor.require_active_page_path(),
            extend_to_touchscreen=self.extend_touchscreen_toggle.get_active()
        )
        self.update_background()

    def on_media_selector_click(self, *args: object) -> None:
        background_settings = services.require_page_manager().get_background_settings(self.page_editor.active_page_path)

        services.require_app().let_user_select_asset(default_path=background_settings.get("media-path", ""), callback_func=self.update_image)

    def set_thumbnail(self, file_path: str | None) -> None:
        if not file_path:
            self.media_selector_image.set_from_pixbuf(None)
            return

        image = gl.media_manager.get_thumbnail(file_path)
        pixbuf = image2pixbuf(image)

        self.media_selector_image.set_from_pixbuf(pixbuf)
        self.media_selector_button.set_child(self.media_selector_image)

        image.close()

    def update_image(self, file_path: str | None) -> None:
        self.set_thumbnail(file_path)

        # A view belongs to the image it was framed on; a new file starts
        # from the default crop instead of inheriting the previous zoom.
        services.require_page_manager().overwrite_background_settings(
            path=self.page_editor.require_active_page_path(),
            media_path=file_path,
            view=None,
        )

        self.update_background()

    def update_background(self) -> None:
        def on_idle() -> None:
            for controller in services.require_deck_manager().deck_controller:
                page = controller.active_page
                # A deck that carries no page must not stop the reload for
                # the decks after it in this list.
                if page is None:
                    continue
                if page.json_path == self.page_editor.active_page_path:
                    controller.load_background(page)

        GLib.idle_add(on_idle)

class ScreensaverGroup(PageEditorGroup):
    def __init__(self, page_editor: PageEditor):
        super().__init__(page_editor, title="Screensaver Overwrite")

    @override
    def build(self) -> None:
        self.overwrite_expander = BetterExpander(
            title="Overwrite Screensaver",
            subtitle="Overrides the Deck Screensaver",
            expanded=False,
            show_enable_switch=True
        )
        self.add(self.overwrite_expander)

        self.media_main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.overwrite_expander.add_row(self.media_main_box)

        self.media_settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.media_main_box.append(self.media_settings_box)

        self.enable_screensaver_toggle = Adw.SwitchRow(title="Enable Screensaver")
        self.media_settings_box.append(self.enable_screensaver_toggle)

        self.delay_spin = Adw.SpinRow.new_with_range(1, 60, 1)
        self.delay_spin.set_title("Delay (min)")
        self.media_settings_box.append(self.delay_spin)

        self.loop_toggle = Adw.SwitchRow(title="Loop")
        self.media_settings_box.append(self.loop_toggle)

        self.fps_spin = Adw.SpinRow.new_with_range(0, 30, 1)
        self.fps_spin.set_title("FPS")
        self.media_settings_box.append(self.fps_spin)

        self.brightness_scale = ScaleRow(
            0, 0, 100,draw_side_values=False, draw_value=True, digits=0, title="Brightness"
        )
        self.media_settings_box.append(self.brightness_scale)

        self.button_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.media_main_box.append(self.button_box)

        self.media_selector_button = Gtk.Button(
            label="Select",
            css_classes=["page-settings-media-selector"],
            halign=Gtk.Align.CENTER,
        )
        self.button_box.append(self.media_selector_button)

        self.media_selector_image = Gtk.Image()

    @override
    def _signal_bindings(self) -> list[tuple[str, GObject.Object, str, Callable[..., Any]]]:
        return [
            ("overwrite", self.overwrite_expander, "notify::enable-expansion", self.on_overwrite_changed),
            ("enable", self.enable_screensaver_toggle, "notify::active", self.on_enable_changed),
            ("delay", self.delay_spin, "changed", self.on_delay_changed),
            ("loop", self.loop_toggle, "notify::active", self.on_loop_changed),
            ("fps", self.fps_spin, "changed", self.on_fps_changed),
            ("brightness", self.brightness_scale.scale, "value-changed", self.on_brightness_changed),
            ("media-selector", self.media_selector_button, "clicked", self.on_media_selector_click),
        ]

    @override
    def load_config_settings(self, page_path: str) -> None:
        if gl.page_manager is None:
            return
        screensaver_settings = gl.page_manager.get_screensaver_settings(page_path)

        self.overwrite_expander.set_enable_expansion(screensaver_settings.get("overwrite", False))
        self.overwrite_expander.set_expanded(screensaver_settings.get("overwrite", False))

        self.enable_screensaver_toggle.set_active(screensaver_settings.get("enable", False))
        self.delay_spin.set_value(screensaver_settings.get("time-delay", 5))
        self.loop_toggle.set_active(screensaver_settings.get("loop", True))  # loop is on by default
        self.fps_spin.set_value(screensaver_settings.get("fps", 30))
        # 30 is the value that the deck dims to when nothing is stored. 75 is
        # the running brightness, which no screensaver applies.
        self.brightness_scale.set_value(screensaver_settings.get("brightness", 30))

        self.set_thumbnail(screensaver_settings.get("media-path", None))

    def on_overwrite_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_screensaver_settings(
            path=self.page_editor.require_active_page_path(),
            overwrite=self.overwrite_expander.get_enable_expansion()
        )
        self.update_screensaver()

    def on_enable_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_screensaver_settings(
            path=self.page_editor.require_active_page_path(),
            enable=self.enable_screensaver_toggle.get_active()
        )
        self.update_screensaver()

    def on_delay_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_screensaver_settings(
            path=self.page_editor.require_active_page_path(),
            time_delay=int(self.delay_spin.get_value())
        )
        self.update_screensaver()

    def on_loop_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_screensaver_settings(
            path=self.page_editor.require_active_page_path(),
            loop=self.loop_toggle.get_active()
        )
        self.update_screensaver()

    def on_fps_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_screensaver_settings(
            path=self.page_editor.require_active_page_path(),
            fps=int(self.fps_spin.get_value())
        )
        self.update_screensaver()

    def on_brightness_changed(self, *args: object) -> None:
        services.require_page_manager().overwrite_screensaver_settings(
            path=self.page_editor.require_active_page_path(),
            brightness=self.brightness_scale.get_value()
        )
        self.update_screensaver()

    def on_media_selector_click(self, *args: object) -> None:
        screensaver_settings = services.require_page_manager().get_screensaver_settings(self.page_editor.active_page_path)

        services.require_app().let_user_select_asset(default_path=screensaver_settings.get("media-path", ""), callback_func=self.update_image)

    def set_thumbnail(self, file_path: str | None) -> None:
        if not file_path:
            self.media_selector_image.set_from_pixbuf(None)
            return

        image = gl.media_manager.get_thumbnail(file_path)
        pixbuf = image2pixbuf(image)

        self.media_selector_image.set_from_pixbuf(pixbuf)
        self.media_selector_button.set_child(self.media_selector_image)

        image.close()

    def update_image(self, file_path: str | None) -> None:
        self.set_thumbnail(file_path)

        services.require_page_manager().overwrite_screensaver_settings(
            path=self.page_editor.require_active_page_path(),
            media_path=file_path
        )

        self.update_screensaver()

    def update_screensaver(self) -> None:
        def on_idle() -> None:
            for controller in services.require_deck_manager().deck_controller:
                page = controller.active_page
                # A deck that carries no page must not stop the reload for
                # the decks after it in this list.
                if page is None:
                    continue
                if page.json_path == self.page_editor.active_page_path:
                    controller.load_screensaver(page)

        GLib.idle_add(on_idle)

class MatchingWindowExpander(BetterExpander):
    def __init__(self, auto_change_group: AutoChangeGroup):
        super().__init__(
            title=gl.lm.get("page-manager.page-editor.matching-windows.title"),
            subtitle=gl.lm.get("page-manager.page-editor.matching-windows.subtitle"),
            expanded=False
        )

        self.auto_change_group = auto_change_group

        # Stamps each query so a slow earlier one cannot overwrite the list
        # a later one produced. Only ever touched on the main thread.
        self._query_generation = 0

        self.update_button = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                                        css_classes=["flat"])
        self.update_button.connect("clicked", self.update_matching_windows)
        self.add_suffix(self.update_button)

    def load_windows(self, windows: list[Window]) -> None:
        self.clear()
        for window in windows:
            self.add_row(Adw.ActionRow(title=window.title, subtitle=window.wm_class, use_markup=False))

    def update_matching_windows(self, *args: object) -> None:
        # Read widget regexes on the main thread, but run window discovery in
        # the background because its subprocess and integration probe can stall.
        class_regex = self.auto_change_group.wm_class_entry.get_text()
        title_regex = self.auto_change_group.title_entry.get_text()

        # Stamp each query so out-of-order results cannot replace a newer list.
        self._query_generation += 1
        run_in_background(self._load_matching_windows, class_regex, title_regex,
                          self._query_generation)

    def _load_matching_windows(self, class_regex: str, title_regex: str, generation: int) -> None:
        window_grabber = gl.window_grabber
        if window_grabber is None:
            return
        matching_windows = window_grabber.get_all_matching_windows(class_regex=class_regex, title_regex=title_regex)
        GLib.idle_add(self._show_matching_windows, matching_windows, generation)

    def _show_matching_windows(self, windows: list[Window], generation: int) -> None:
        if generation != self._query_generation:
            # A newer query started, so this list is out of date, and a show
            # here undoes the newer answer.
            return
        self.load_windows(windows=windows)
