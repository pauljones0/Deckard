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
import contextlib

# Import gtk modules
import gi

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.mainWindow.elements.PageSettingsPage import PageSettingsPage
    from src.backend.DeckManagement.InputIdentifier import InputIdentifier
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from PIL import Image
    from src.backend.DeckManagement.deck_controller.inputs import ControllerKey
    from src.windows.mainWindow.DeckPlus.ScreenBar import ScreenBar
from src.backend.DeckManagement.InputIdentifier import Input


gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Gio

# Import Python modules 
from loguru import logger as log

# Imort globals
from src.backend import services

import globals as gl

# Import own modules
from src.backend.DeckManagement.ImageHelpers import image2pixbuf
from src.backend.DeckManagement.HelperMethods import recursive_hasattr
from src.windows.ui_adapter import mark_dirty

from typing import Protocol, cast, Any


class _ImageSink(Protocol):
    """A widget that shows one PIL frame: the key button and the screen-bar
    image both answer it."""

    def set_image(self, image: "Image.Image") -> None: ...

class KeyGrid(Gtk.Grid):
    """
    Child of PageSettingsPage
    Key grid for the button config
    """
    def __init__(self, deck_controller: "DeckController", page_settings_page: "PageSettingsPage", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.deck_controller = deck_controller
        self.page_settings_page = page_settings_page

        self.selected_key: "KeyButton | None" = None # The selected key, indicated by a blue frame around it

        [y, x] = self.deck_controller.deck.key_layout()
        # build() fills every slot. The Nones live between this line and it.
        self.buttons: list[list["KeyButton | None"]] = [[None] * y for i in range(x)]


        self.build()

        self.connect("map", self.on_map)
        self.connect("unmap", self.on_unmap)

        self.load_from_changes()

        GLib.idle_add(self.select_key, 0, 0)

    def regenerate_buttons(self) -> None:
        [y, x] = self.deck_controller.deck.key_layout()
        self.buttons = [[None] * y for i in range(x)]
    
    def build(self) -> None:
        self.clear()

        layout = self.deck_controller.deck.key_layout()
        for x in range(layout[1]):
            for y in range(layout[0]):
                button = KeyButton(self, (x, y))
                self.attach(button, x, y, 1, 1)
                button._set_visible(False) # Hide buttons per default - they will be shown when the the grid is mapped to prevent large grids to resize every child
                self.buttons[x][y] = button

    def load_from_changes(self) -> None:
        # Apply the changes that arrived before this widget existed, or while
        # the window was hidden. Each entry is a dirty marker and not a stored
        # PIL image, so this composites the current frame for each dirty
        # identifier and pushes it through the set-image path that a live
        # update uses.
        if not hasattr(self.deck_controller, "ui_image_changes_while_hidden"):
            return
        tasks = self.deck_controller.ui_image_changes_while_hidden
        for identifier in list(tasks.keys()):
            if isinstance(identifier, Input.Key):
                x, y = identifier.coords
                button = self.buttons[x][y]
                if button is None:
                    # Mid-rebuild the slot is empty; the rebuild composites
                    # this frame itself, so there is nothing to push here.
                    continue
                self._push_current_image(identifier, button)
                with contextlib.suppress(KeyError):
                    tasks.pop(identifier)
            elif isinstance(identifier, Input.Touchscreen):
                # ScreenBar.load_from_changes normally consumes this entry,
                # because it owns the widget that shows it. When the map
                # handler of that widget has not run, or never runs, consume
                # the entry here instead of leaking it. The tasks.pop() call
                # carries the same guard as the Key branch above, so the first
                # widget wins and the second does nothing.
                screenbar = self._find_screenbar()
                if screenbar is not None:
                    self._push_current_image(identifier, screenbar.image)
                    with contextlib.suppress(KeyError):
                        tasks.pop(identifier)

    def _find_screenbar(self) -> "ScreenBar | None":
        """The sibling screenbar, found by a walk up the widget tree.

        The lookup is duck-typed, because an import of DeckStackChild or
        DeckConfig here is a cycle, and the engine caches no child to read.
        The parent chain is a plain Gtk.Widget with no static screenbar
        attribute, so this stays a string existence check where the other UI
        reachability guards became typed accessors.
        """
        # The lookup may fail. During __init__ this grid is not in the widget
        # tree, because DeckConfig.build appends the grid before the screenbar
        # exists, so the touchscreen replay waits for
        # ScreenBar.load_from_changes.
        widget = self.get_parent()
        while widget is not None:
            if recursive_hasattr(widget, "screenbar.image"):
                # The walk is duck-typed; the guard above is what proves the
                # widget is the deck config that owns the screenbar.
                return cast("ScreenBar", getattr(widget, "screenbar"))
            widget = widget.get_parent()
        return None

    def _push_current_image(self, identifier: "InputIdentifier", widget: "_ImageSink") -> None:
        controller_input = self.deck_controller.get_input(identifier)
        if controller_input is None:
            return
        try:
            image = controller_input.get_current_image()
        except Exception:
            log.exception(f"Failed to recomposite {identifier} on map")
            return
        widget.set_image(image)
        
    def select_key(self, x: int, y: int) -> None:
        button = self.buttons[x][y]
        if button is None:
            return
        button.on_focus_in()
        button.image.grab_focus()

    def on_map(self, widget: Gtk.Widget) -> None:
        self.load_from_changes()

        # Only show buttons when the grid is mapped to prevent large grids to resize every child
        self.set_buttons_visible(True)

    def on_unmap(self, widget: Gtk.Widget) -> None:
        # Only show buttons when the grid is mapped to prevent large grids to resize every child
        self.set_buttons_visible(False)

    def clear(self) -> None:
        child = self.get_first_child()
        while child is not None:
            self.remove(child)
            child = self.get_first_child()

    def set_buttons_visible(self, visible: bool) -> None:
        for row in self.buttons:
            for button in row:
                if button is not None:
                    button._set_visible(visible)


class KeyButton(Gtk.Frame):
    def __init__(self, key_grid:KeyGrid, coords: tuple[int, int], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.set_css_classes(["key-button-frame-hidden"])
        self.coords = coords
        self.identifier = Input.Key(f"{coords[0]}x{coords[1]}")

        self.key_grid = key_grid

        self.pixbuf: GdkPixbuf.Pixbuf | None = None

        # self.button = Gtk.Button(hexpand=True, vexpand=True, css_classes=["key-button"])
        # self.set_child(self.button)

        self.image = Gtk.Image(hexpand=True, vexpand=True, css_classes=["key-image", "key-button"])
        self.image.set_overflow(Gtk.Overflow.HIDDEN)
        self.image.set_size_request(75, 75)
        self.image.set_pixel_size(75)
        self.set_child(self.image)

        # self.button.connect("clicked", self.on_click)

        focus_controller = Gtk.EventControllerFocus()
        self.add_controller(focus_controller)
        focus_controller.connect("enter", self.on_focus_in)

        # Click ctrl
        self.right_click_ctrl = Gtk.GestureClick().new()
        self.right_click_ctrl.connect("pressed", self.on_click)
        self.right_click_ctrl.set_button(0)
        self.image.add_controller(self.right_click_ctrl)

        # Make image focusable
        self.set_focus_child(self.image)
        self.image.set_focusable(True)

        self.init_actions()

        self.init_dnd()

        self.init_shortcuts()

    @property
    def state(self) -> int:
        key = self.get_key()
        if key is None:
            # The controller holds no input under this identifier, which a
            # stale identifier or a changed deck model both produce. State 0
            # exists on every input, so the caller reads the default one.
            log.warning(f"No input {self.identifier} on deck {self._deck_name()}; reading state 0")
            return 0
        return key.state

    def init_dnd(self) -> None:
        self.drag_source = Gtk.DragSource()
        self.drag_source.connect("prepare", self.on_drag_prepare)
        self.drag_source.connect("drag-begin", self.on_drag_begin)
        # self.drag_source.connect("drag-end", self.on_drag_end)
        self.add_controller(self.drag_source)

        self.button_dnd_target = Gtk.DropTarget.new(KeyButton, Gdk.DragAction.COPY)
        self.button_dnd_target.set_gtypes([KeyButton, Gdk.FileList])
        self.button_dnd_target.connect("accept", self.on_button_accept)
        self.button_dnd_target.connect("drop", self.on_button_drop)
        self.add_controller(self.button_dnd_target)

    def on_button_accept(self, drop: Gtk.DropTarget, user_data: Gdk.Drop) -> bool:
        return True

    # GTK4 passes the dropped value to the drop signal, not the content
    # provider. Here that value is a KeyButton or a Gdk.FileList. See
    # set_gtypes above.
    def on_button_drop(self, drop: Gtk.DropTarget, value: "KeyButton | Gdk.FileList", x: float, y: float) -> "bool | None":
        # value IS drop.get_value(): GTK passes the dropped value to the
        # signal, so the narrowing reads the parameter it forwards.
        if isinstance(value, KeyButton):
            self.handle_key_button_drop(drop, value, x, y)

        elif isinstance(value, Gdk.FileList):
            self.handle_file_drop(drop, value, x, y)

        else:
            # The gtype pin above makes this unreachable for the checker,
            # and the reject stays for whatever GTK marshals anyway.
            drop.reject()
            return False
        return None
        
    def handle_key_button_drop(self, drop: Gtk.DropTarget, value: "KeyButton", x: float, y: float) -> None:
        active_page = self.key_grid.deck_controller.active_page
        if active_page is None:
            return

        dropped_button = drop.get_value()
        if dropped_button is None:
            return
        dropped_identifier = dropped_button.identifier

        # The swap is one edit of the page, and not two assignments with a
        # save around each. Both halves are read and written inside the block,
        # so no writer sees a page where one key holds the content of the
        # other and its own content is nowhere. A deferred write between two
        # assignments puts that broken state on disk. Both sections also read
        # under the same lock, so a half-stale copy cannot overwrite a page
        # edit from a plugin thread. Nothing here touches the file, because
        # the reloads below run outside the block, without the lock.
        with active_page.edit() as page_dict:
            own_section = page_dict.setdefault(self.identifier.input_type, {})
            dropped_section = page_dict.setdefault(dropped_identifier.input_type, {})
            own_content = own_section.get(self.identifier.json_identifier, {})
            dropped_content = dropped_section.get(dropped_identifier.json_identifier, {})

            own_section[self.identifier.json_identifier] = dropped_content
            dropped_section[dropped_identifier.json_identifier] = own_content

        active_page.switch_actions_of_inputs(self.identifier, dropped_identifier)

        # Both keys repaint, one identifier at a time. Each reload writes the
        # page out before it reads the page again, so the swap is on disk when
        # these calls return. A save after them writes back only the content
        # that the last reload read from the file.
        active_page.reload_similar_pages(self.identifier, reload_self=True)
        active_page.reload_similar_pages(dropped_identifier, reload_self=True)

        # Reload sidebar
        if gl.app is not None:
            gl.app.main_win.sidebar.update()

    # value is the dropped Gdk.FileList and not the ContentProvider.
    # on_button_drop routes here only when drop.get_value() is a Gdk.FileList,
    # because GTK passes the value to the drop signal, not the provider.
    def handle_file_drop(self, drop: Gtk.DropTarget, value: Gdk.FileList, x: float, y: float) -> "bool | None":
        files = value.get_files()
        if len(files) > 1:
            drop.reject()
            return False
        
        file = files[0]
        url = file.get_uri()
        # A remote drop carries a uri and no local path. The importer owns
        # that case, validates the extension and tells the user, so this code
        # must not return early.
        path = file.get_path()

        internal_path = gl.asset_manager_backend.add_custom_media_set_by_ui(url=url, path=path)
        # Any result that is not a path means a refusal. The import can answer
        # a rejected url with -1, which passes an is None test and reaches the
        # media path of the key.
        if not isinstance(internal_path, str) or internal_path == "":
            return False

        # Set media to key
        active_page = self.key_grid.deck_controller.active_page
        if active_page is None:
            # The page can clear while the drop is in flight.
            return None

        state_dict = self.identifier.ensure_state_dict(active_page, self.state)
        state_dict.setdefault("media", {
            "path": None,
            "loop": True,
            "fps": 30
        })
        state_dict["media"]["path"] = internal_path
        # Save page
        active_page.save()
        self.key_grid.deck_controller.load_input_from_identifier(self.identifier, page=active_page)

        # Update icon selector if current key is selected
        if gl.app is None:
            return None
        active_identifier = gl.app.main_win.sidebar.active_identifier
        if active_identifier == self.identifier:
            gl.app.main_win.sidebar.key_editor.icon_selector.load_for_identifier(self.identifier, self.state)
        return None

        
    def on_drag_begin(self, drag_source: Gtk.DragSource, data: Gdk.Drag) -> None:
        content = data.get_content()

    def on_drag_prepare(self, drag_source: Gtk.DragSource, x: float, y: float) -> Gdk.ContentProvider:
        drag_source.set_icon(self.image.get_paintable(), self.get_width() // 2, self.get_height() // 2)
        content = Gdk.ContentProvider.new_for_value(self)
        return content

    def on_dnd_accept(self, drop: Gtk.DropTarget, user_data: Gdk.Drop) -> bool:
        return True

        

    def set_image(self, image: "Image.Image") -> None:
        # Callable from any thread. This is the map-time replay path. A live
        # frame arrives through the UI adapter, which calls the same two
        # halves and coalesces the paints into one per input. The idle takes
        # the default priority, because a high-priority pixbuf update on every
        # frame starves the layout and draw of the main loop.
        GLib.idle_add(self.paint_mirror_frame, self.prepare_mirror_frame(image))
        # image.close()
        # image = None
        # del image

    def prepare_mirror_frame(self, image: "Image.Image") -> "GdkPixbuf.Pixbuf | None":
        """The paint-ready payload for paint_mirror_frame.

        Any thread may call it. image2pixbuf uses only PIL and GdkPixbuf, so
        the conversion runs on the caller. The caller is the media thread for
        a live frame. Only the widget change needs the loop.
        """
        # This carries no staleness stamp, unlike the screenbar. One slot
        # coalesces the live frames of a key, so they cannot queue out of
        # order, and the only other producer is the map-time replay, which
        # dispatches in attach order. An inversion between the two costs one
        # stale frame, and the next repaint corrects it.
        return image2pixbuf(image.convert("RGBA"), force_transparency=True)

    def paint_mirror_frame(self, pixbuf: "GdkPixbuf.Pixbuf | None") -> bool:
        # Main loop only. It returns False, because a GLib idle callback that
        # returns a true value re-arms.
        self.pixbuf = pixbuf
        # update righthand side key preview if possible - before the paint
        # below, which bails out when this button is unmapped
        self.set_icon_selector_previews(pixbuf)
        # Skip when the button unmapped between the queue and this callback,
        # because a paint on a disposed widget crashes GTK.
        try:
            if not self.get_mapped():
                # This is a late failure. push_input_image already returned
                # True for this frame, so the engine did not dirty-mark it.
                # Record the drop here, or load_from_changes has nothing to
                # replay on the remap and the preview goes stale.
                self._mark_dropped()
                return False
            self.image.set_from_pixbuf(self.pixbuf)
        except Exception as e:
            log.debug(f"Key mirror paint skipped: {e}")
            self._mark_dropped()
        return False

    def _mark_dropped(self) -> None:
        controller = getattr(self.key_grid, "deck_controller", None)
        if controller is not None:
            mark_dirty(controller, self.identifier)

    def set_icon_selector_previews(self, pixbuf: "GdkPixbuf.Pixbuf | None") -> None:
        # Main loop only, because the gating below reads widget state.
        sidebar = services.sidebar()
        if sidebar is None:
            return
        if pixbuf is None:
            return
        if sidebar.key_editor.label_editor.label_group.expander.active_identifier != self.identifier:
            return
        deck_stack = services.deck_stack()
        if deck_stack is None:
            return
        child = deck_stack.get_visible_child()
        if child is None:
            return
        if child.deck_controller != self.key_grid.deck_controller:
            return
        # Update icon selector on the top of the right are
        sidebar.key_editor.icon_selector.set_pixbuf_and_del(pixbuf)
        # Update icon selector in margin editor
        # sidebar.key_editor.image_editor.image_group.expander.margin_row.icon_selector.image.set_from_pixbuf(pixbuf)

    def on_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        if gesture.get_current_button() == 1 and n_press == 1:
            # Single left click
            # Select key
            self.image.grab_focus()

        elif gesture.get_current_button() == 1 and n_press == 2:
            # Double left click
            # Simulate key press
            self.simulate_press()
            
        elif gesture.get_current_button() == 3 and n_press == 1:
            # Single right click
            # Open context menu
            popover = KeyButtonContextMenu(self)
            popover.on_open()
            popover.popup()

    def simulate_press(self) -> None:
        ## Check if double click to emulate is turned on in the settings
        if not gl.settings_manager.app().emulate_at_double_click:
            return
        
        self.key_grid.deck_controller.event_callback(self.identifier, True)
        # Release key after 100ms
        GLib.timeout_add(100, self.key_grid.deck_controller.event_callback, self.identifier, False)

    def set_border_active(self, visible: bool) -> None:
        if visible:
            # Hide other frames
            if self.key_grid.page_settings_page.deck_config.active_widget not in [self, None]:
                # self.key_grid.selected_key.set_css_classes(["key-button-frame-hidden"])
                self.key_grid.page_settings_page.deck_config.active_widget.set_border_active(False)
            self.key_grid.page_settings_page.deck_config.active_widget = self
            self.set_css_classes(["key-button-frame"])
            self.key_grid.selected_key = self
        else:
            self.set_css_classes(["key-button-frame-hidden"])
            self.key_grid.page_settings_page.deck_config.active_widget = None

    def on_focus_in(self, *args: Any) -> None:
        # Update settings on the righthand side of the screen
        self.update_sidebar()
        # Update preview
        if self.pixbuf is not None:
            self.set_icon_selector_previews(self.pixbuf)
        # self.set_css_classes(["key-button-frame"])
        # self.button.set_css_classes(["key-button-new-small"])
        self.set_border_active(True)

    def update_sidebar(self) -> None:
        sidebar = services.sidebar()
        if sidebar is None:
            return
        # Check if already loaded for this coords
        if sidebar.active_identifier == self.identifier:
            if not self.get_mapped():
                return
            
        sidebar.load_for_identifier(self.identifier, self.state)

    # Modifier
    def on_copy(self, *args: Any) -> bool:
        active_page = self.key_grid.deck_controller.active_page
        if active_page is None:
            return False
        key_dict = active_page.dict.get(self.identifier.input_type, {}).get(self.identifier.json_identifier, {})
        services.require_main_window().key_dict = key_dict
        content = Gdk.ContentProvider.new_for_value(key_dict)
        services.require_main_window().key_clipboard.set_content(content)
        return False

    def on_cut(self, *args: Any) -> bool:
        self.on_copy()
        self.on_remove()
        return False

    def on_paste(self, *args: Any) -> bool:
        # No is_local() check on the clipboard. Refusing a clipboard this
        # instance does not own breaks copy and paste on KDE under Wayland,
        # where ownership reads as foreign. Telling the two apart needs the
        # value itself, through read_value_async.

        # Remove the old action objects. Several actions can share one action
        # base, and nothing else tells those actions apart.
        self.on_remove()
        
        active_page = self.key_grid.deck_controller.active_page
        if active_page is None:
            return False
        active_page.dict.setdefault(self.identifier.input_type, {})
        active_page.dict[self.identifier.input_type].setdefault(self.identifier.json_identifier, {})
        active_page.dict[self.identifier.input_type][self.identifier.json_identifier] = services.require_main_window().key_dict
        active_page.reload_similar_pages(self.identifier, reload_self=True)

        # Reload ui
        services.require_main_window().sidebar.load_for_identifier(self.identifier, self.state)
        return False

    def on_remove(self, *args: Any) -> bool:
        active_page = self.key_grid.deck_controller.active_page
        if active_page is None:
            return False
        x, y = self.coords
        
        if f"{x}x{y}" not in active_page.dict.get("keys", {}):
            return False
        del active_page.dict["keys"][f"{x}x{y}"]
        active_page.save()
        active_page.load()

        # Remove media from key
        active_page.reload_similar_pages(self.identifier, reload_self=True)

        # Reload ui
        services.require_main_window().sidebar.load_for_identifier(self.identifier, self.state)
        return False

    def _deck_name(self) -> str:
        """The deck this widget draws, for a log line. Reads the cached serial
        rather than the device, so a log on a missing input cannot itself
        reach hardware."""
        controller = self.key_grid.deck_controller
        return str(getattr(controller, "_serial_number", None) or "unknown")

    def get_key(self) -> "ControllerKey | None":
        """The controller input this widget stands for, or None when the
        controller carries no input under the identifier. get_input answers
        None for a stale identifier and for a deck model that has no such
        input, so the None is a real answer, not an error."""
        controller = self.key_grid.deck_controller
        return controller.get_input(self.identifier)

    def remove_media(self) -> None:
        key = self.get_key()
        if key is None:
            log.warning(f"No input {self.identifier} on deck {self._deck_name()}; nothing to remove media from")
            return
        state = key.get_active_state()

        state.remove_media()

    def _set_visible(self, visible: bool) -> None:
        self.set_visible(visible)
        self.image.set_visible(visible)

    def init_actions(self) -> None:
        self.action_group = Gio.SimpleActionGroup()
        self.insert_action_group("key", self.action_group)

        self.copy_action = Gio.SimpleAction.new("copy", None)
        self.cut_action = Gio.SimpleAction.new("cut", None)
        self.paste_action = Gio.SimpleAction.new("paste", None)
        self.remove_action = Gio.SimpleAction.new("remove", None)

        self.copy_action.connect("activate", self.on_copy)
        self.cut_action.connect("activate", self.on_cut)
        self.paste_action.connect("activate", self.on_paste)
        self.remove_action.connect("activate", self.on_remove)

        self.action_group.add_action(self.copy_action)
        self.action_group.add_action(self.cut_action)
        self.action_group.add_action(self.paste_action)
        self.action_group.add_action(self.remove_action)


    def init_shortcuts(self) -> None:
        self.shortcut_controller = Gtk.ShortcutController()

        self.copy_shortcut_action = Gtk.CallbackAction.new(self.on_copy)
        self.cut_shortcut_action = Gtk.CallbackAction.new(self.on_cut)
        self.paste_shortcut_action = Gtk.CallbackAction.new(self.on_paste)
        self.remove_shortcut_action = Gtk.CallbackAction.new(self.on_remove)

        self.copy_shortcut = Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Primary>c"), self.copy_shortcut_action)
        self.cut_shortcut = Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Primary>x"), self.cut_shortcut_action)
        self.paste_shortcut = Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Primary>v"), self.paste_shortcut_action)
        self.remove_shortcut = Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Delete"), self.remove_shortcut_action)

        self.shortcut_controller.add_shortcut(self.copy_shortcut)
        self.shortcut_controller.add_shortcut(self.cut_shortcut)
        self.shortcut_controller.add_shortcut(self.paste_shortcut)
        self.shortcut_controller.add_shortcut(self.remove_shortcut)

        self.add_controller(self.shortcut_controller)

class KeyButtonContextMenu(Gtk.PopoverMenu):
    def __init__(self, key_button:KeyButton, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.key_button = key_button
        self._unparenting = False
        self.build()

        self.connect("closed", self.on_close)

        # gl.app.set_accels_for_action("context.test", ["<Primary>t"])

    def build(self) -> None:
        self.set_parent(self.key_button)
        self.set_has_arrow(False)

        self.main_menu = Gio.Menu.new()

        self.copy_paste_menu = Gio.Menu.new()
        self.remove_menu = Gio.Menu.new()

        # Add actions to menus
        self.copy_paste_menu.append("Copy", "key.copy")
        self.copy_paste_menu.append("Cut", "key.cut")
        self.copy_paste_menu.append("Paste", "key.paste")
        self.remove_menu.append("Remove", "key.remove")
        self.remove_menu.append("Update", "key.update")

        # Add sections to menu
        self.main_menu.append_section(None, self.copy_paste_menu)
        self.main_menu.append_section(None, self.remove_menu)

        self.set_menu_model(self.main_menu)

    def on_close(self, popover: Gtk.PopoverMenu) -> None:
        # Unparent on an idle, not here. This code runs inside the closed
        # signal emission, and an unparent of the popover during that emission
        # can dispose the emitter under GTK. The idle also lets fast repeated
        # right-clicks each schedule their own unparent without a race, and
        # the guard queues one per menu.
        if self._unparenting:
            return
        self._unparenting = True

        def _do_unparent() -> bool:
            if self.get_parent() is not None:
                self.unparent()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_do_unparent)

    def on_open(self) -> None:
        # Inert. MainWindow.add_accel_actions is inert too, and each
        # KeyButton serves these keys through a Gtk.ShortcutController of
        # its own, added in init_shortcuts.
        return