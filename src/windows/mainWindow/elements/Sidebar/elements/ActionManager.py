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

from src.backend.DeckManagement.InputIdentifier import InputIdentifier
from src.windows.Settings.PluginSettingsWindow.PluginSettingsWindow import PluginSettingsWindow

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GLib, Pango

# Import Python modules
from loguru import logger as log

# Import globals
from src.backend import services

import globals as gl

# Import own modules
from src.backend.PluginManager.ActionCore import ActionCore
from GtkHelper.GtkHelper import BetterExpander
from src.backend.PageManagement import action_order
from src.backend.PageManagement.Page import NoActionHolderFound, ActionOutdated
from src.windows.mainWindow.elements.Sidebar.elements.ActionMissing.MisingActionButtonRow import MissingActionButtonRow
from src.windows.mainWindow.elements.Sidebar.elements.ActionMissing.OutdatedActionRow import OutdatedActionRow

from collections.abc import Collection
from typing import Any, TYPE_CHECKING

# The CSS classes that mark where a dragged action row lands.
DROP_ABOVE_CLASS = "action-row-drop-above"
DROP_BELOW_CLASS = "action-row-drop-below"
DRAGGED_CLASS = "action-row-dragged"

if TYPE_CHECKING:
    from src.backend.PluginManager.ActionHolder import ActionHolder
    from src.windows.mainWindow.elements.Sidebar.Sidebar import Sidebar

class ActionManager(Gtk.Box):
    def __init__(self, sidebar: "Sidebar", **kwargs: Any) -> None:
        self.sidebar = sidebar
        super().__init__(**kwargs)
        self.build()

    def build(self) -> None:
        self.clamp = Adw.Clamp()
        self.append(self.clamp)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.clamp.set_child(self.main_box)

        self.action_group = ActionGroup(self.sidebar)
        self.main_box.append(self.action_group)

        self.main_box.set_margin_bottom(50)

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.action_group.load_for_identifier(identifier, state)

class ActionGroup(Adw.PreferencesGroup):
    def __init__(self, sidebar: "Sidebar", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.active_identifier: "InputIdentifier | None" = None

        self.actions: "list[ActionCore | NoActionHolderFound | ActionOutdated]" = []

        self.build()

    def build(self) -> None:
        self.expander = ActionExpanderRow(self)
        self.add(self.expander)

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        self.active_identifier = identifier
        self.expander.load_for_identifier(identifier, state)


class ActionExpanderRow(BetterExpander):
    def __init__(self, action_group: ActionGroup) -> None:
        super().__init__(title=gl.lm.get("action-editor-header"), subtitle=gl.lm.get("action-editor-expander-subtitle"))
        self.set_expanded(True)
        self.active_identifier: "InputIdentifier | None" = None
        self.action_group = action_group
        self.active_state: "int | None" = None

        # The row a drag holds, between drag-begin and drag-end. The drop
        # target reads it to say where the row lands before it is dropped.
        self.dragged_row: "ActionRow | None" = None

        self.build()

    def build(self) -> None:
        self.add_action_button = AddActionButtonRow(self).button
        self.add_row(self.add_action_button)

    def add_action_row(self, action_name: str, action_id: str, action_category: "str | None", action_object: "ActionCore", comment: str | None, index: int, total_rows: int, controls_image: bool = False, controls_labels: list[bool] | None = None, controls_background: bool = False) -> None:
        if controls_labels is None:
            controls_labels = [False, False, False]

        action_row = ActionRow(action_name, action_id, action_category, action_object, self.action_group.sidebar, comment, index, controls_image, controls_labels, controls_background, total_rows, self)
        self.add_row(action_row)

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        if not isinstance(identifier, InputIdentifier):
            raise ValueError("Invalid identifier given to load_for_identifier")
        self.active_state = state
        self.active_identifier = identifier

        self.clear_actions(keep_add_button=True)

        if gl.app is None:
            return
        controller = services.require_main_window().get_active_controller()
        if controller is None or controller.active_page is None:
            return

        actions = controller.active_page.action_objects.get(identifier.input_type, {}).get(identifier.json_identifier, {}).get(state, {})
        self.load_for_actions(actions.values())

    def load_for_actions(self, actions: "Collection[ActionCore | NoActionHolderFound | ActionOutdated | None]") -> None:
        # Ignore empty slots that match none of the action variants.
        # Missing and outdated rows require the state bound by load_for_identifier.
        active_state = self.active_state
        number_of_actions = len(actions)
        for i, action in enumerate(actions):
            if isinstance(action, ActionCore):
                # Get action comment. The page holds the comments, and an
                # action the teardown detached has none to show.
                action_page = action.page
                comment = "" if action_page is None else action_page.get_action_comment(
                    index=i, state=action.state, identifier=action.input_ident)

                controls_image = action.has_image_control()
                controls_background = action.has_background_control()
                controls_labels = action.has_label_controls()

                self.add_action_row(action.action_name, action.action_id, action.plugin_base.plugin_name, action, controls_image=controls_image, controls_labels=controls_labels, controls_background=controls_background, comment=comment, index=i, total_rows=number_of_actions)
            elif isinstance(action, NoActionHolderFound):
                if active_state is None:
                    continue
                missing_button_row = MissingActionButtonRow(action.id, action.identifier, active_state, i)
                self.add_row(missing_button_row)
            elif isinstance(action, ActionOutdated):
                # No plugin installed for this action
                if active_state is None:
                    continue
                outdated_row = OutdatedActionRow(action.id, action.identifier, active_state, i)
                self.add_row(outdated_row)

        # Place add button at the end
        if len(self.get_rows()) > 0:
            self.reorder_child_after(self.add_action_button, self.get_rows()[-1])

    def clear_actions(self, keep_add_button: bool = False) -> None:
        for child in self.get_rows():
            if hasattr(child, "action_object"):
                child.action_object = None
        self.clear()
        if keep_add_button:
            self.add_row(self.add_action_button)

    def update_indices(self) -> None:
        for i, row in enumerate(self.get_rows()):
            row.index = i

    def action_rows(self) -> list[Any]:
        """Return action rows in display order, excluding the add button.
        Missing and outdated rows retain their positions as page action indexes."""
        rows = self.get_rows() or []
        return [row for row in rows if row is not self.add_action_button]

    def plan_drop(self, source_row: "ActionRow", target_row: "ActionRow", drop_below: bool) -> "tuple[int, int] | None":
        """Answer the source and destination index of a drop, or None for a drop that moves nothing."""
        rows = self.action_rows()
        if source_row not in rows or target_row not in rows:
            return None

        source_index = rows.index(source_row)
        dest_index = action_order.resolve_drop_index(source_index, rows.index(target_row), drop_below, len(rows))
        if dest_index is None:
            return None
        return source_index, dest_index

    def show_drop_indicator(self, row: "ActionRow", drop_below: bool) -> None:
        """Mark the edge of row that the dragged row lands on."""
        self.clear_drop_indicators()
        row.add_css_class(DROP_BELOW_CLASS if drop_below else DROP_ABOVE_CLASS)

    def clear_drop_indicators(self) -> None:
        for row in self.get_rows() or []:
            row.remove_css_class(DROP_ABOVE_CLASS)
            row.remove_css_class(DROP_BELOW_CLASS)

    def apply_drop(self, source_index: int, dest_index: int,
                   identifier: "InputIdentifier | None", state: "int | None") -> bool:
        """Run a queued drop only if its input and state are still active.
        The sidebar can load another target before this idle runs."""
        if identifier != self.active_identifier or state != self.active_state:
            return GLib.SOURCE_REMOVE

        rows = self.action_rows()
        if 0 <= source_index < len(rows):
            self.move_row(rows[source_index], dest_index)
        return GLib.SOURCE_REMOVE

    def move_row_by(self, row: "ActionRow", offset: int) -> None:
        """Move one action row offset places down the list, or up for a negative offset."""
        rows = self.action_rows()
        if row not in rows:
            return
        self.move_row(row, rows.index(row) + offset)

    def move_row(self, row: "ActionRow", dest_index: int) -> None:
        """Move one action row and apply the order to the page and deck.
        Ignore destinations outside the action rows, including moves beyond either end."""
        rows = self.action_rows()
        if row not in rows:
            return
        source_index = rows.index(row)
        if not 0 <= dest_index < len(rows):
            return
        if dest_index == source_index:
            return

        # The page first. A row that moves ahead of a write that does not
        # happen shows an order the page does not hold, until the next rebuild.
        if not self.reorder_actions(source_index, dest_index):
            return

        # reorder_child_after places the row on the far side of its neighbour.
        # The row at dest_index is the correct neighbour in both directions.
        self.reorder_child_after(row, rows[dest_index])

        # Update row indexes before the idle-priority sidebar rebuild.
        # A second move can otherwise read a stale index.
        self.update_indices()

    def reorder_actions(self, source_index: int, dest_index: int) -> bool:
        """Write the new order, load the page onto the deck, and report a change."""
        controller = services.require_main_window().get_active_controller()
        if controller is None:
            return False
        identifier = self.active_identifier
        state = self.active_state
        page = controller.active_page
        if identifier is None or state is None or page is None:
            return False

        if not action_order.move_action(page, identifier, state, source_index, dest_index):
            # The page is untouched, so there is nothing to load.
            return False

        controller.load_page(page)
        return True

class ActionRowLabelToggle(Gtk.Button):
    def __init__(self, action_row: "ActionRow"):
        self.action_row = action_row
        super().__init__(tooltip_text="Control which labels are controlled by this action")

        # Toggled-handler ID by config button index, absent while disconnected.
        # Tracking keeps connect and disconnect idempotent.
        self._label_handler_ids: dict[int, int] = {}

        self.build()

    def build(self) -> None:
        self.set_css_classes(["blue-toggle-button"])

        self.main_box = Gtk.Box()
        self.set_child(self.main_box)

        self.main_box.append(Gtk.Image(icon_name="format-text-italic-symbolic"))

        self.indicator_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, valign=Gtk.Align.CENTER, margin_start=5)
        self.main_box.append(self.indicator_box)


        self.indicators: list[Gtk.Box] = []
        for i in range(3):
            indicator = Gtk.Box(css_classes=["action-row-label-toggle-inactive"])
            self.indicator_box.append(indicator)
            self.indicators.append(indicator)

        self.config_buttons: list[Gtk.CheckButton] = []
        self.config_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        label_names = ["Top", "Center", "Bottom"]
        for i, name in enumerate(label_names):
            check = Gtk.CheckButton(label=name, name=str(i))
            self.config_buttons.append(check)
            if "action-row-label-toggle-active" in self.indicators[i].get_css_classes():
                check.set_active(True)
            self.config_box.append(check)

        self.connect_signals()


        self.popover = Gtk.Popover(child=self.config_box)
        self.main_box.append(self.popover)


        self.connect("clicked", self.on_click)

    def on_click(self, button: Gtk.Button) -> None:
        self.popover.popup()

    def on_label_toggled(self, button: Gtk.CheckButton) -> None:
        i = int(button.get_name())

        indicator = self.indicators[i]

        if button.get_active():
            indicator.set_css_classes(["action-row-label-toggle-active"])
        else:
            indicator.set_css_classes(["action-row-label-toggle-inactive"])

        self.action_row.label_toggled(i, button.get_active())

    def connect_signals(self) -> None:
        for i, button in enumerate(self.config_buttons):
            if i not in self._label_handler_ids:
                self._label_handler_ids[i] = button.connect("toggled", self.on_label_toggled)

    def disconnect_signals(self) -> None:
        for i, button in enumerate(self.config_buttons):
            handler_id = self._label_handler_ids.pop(i, None)
            if handler_id is not None:
                button.disconnect(handler_id)

    def set_active(self, values: list[bool]) -> None:
        self.disconnect_signals()
        try:
            for i, value in enumerate(values):
                indicator = self.indicators[i]
                if value:
                    indicator.set_css_classes(["action-row-label-toggle-active"])
                else:
                    indicator.set_css_classes(["action-row-label-toggle-inactive"])

                self.config_buttons[i].set_active(value)
        finally:
            # An update that returns early or raises must still leave every
            # button wired, or the label controls stop reporting clicks.
            self.connect_signals()

    def get_active(self) -> list[bool]:
        return [indicator.get_css_classes() == ["action-row-label-toggle-active"] for indicator in self.indicators]


class ActionRow(Adw.ActionRow):
    def __init__(self, action_name: str, action_id: str, action_category: "str | None", action_object: "ActionCore", sidebar: "Sidebar", comment: str | None, index: int, controls_image: bool, controls_labels: list[bool], controls_background: bool, total_rows: int, expander: ActionExpanderRow, **kwargs: Any) -> None:
        super().__init__(**kwargs, css_classes=["no-padding"])
        self.action_name = action_name
        self.action_id = action_id
        self.action_category = action_category
        self.sidebar: "Sidebar" = sidebar
        self.action_object: "ActionCore" = action_object
        self.comment = comment
        self.index = index
        self.controls_image = controls_image
        self.controls_labels = controls_labels
        self.controls_background = controls_background
        self.active_type = None
        self.active_identifier = None
        self.total_rows = total_rows
        self.expander = expander
        # Toggled-handler IDs, or None while disconnected.
        # Tracking keeps connect and disconnect idempotent.
        self._image_handler_id: int | None = None
        self._background_handler_id: int | None = None
        self.build()
        self.update_allow_box_visibility()
        self.init_dnd()

    def build(self) -> None:
        # self.overlay = Gtk.Overlay()
        # self.set_child(self.overlay)

        # self.button = Gtk.Button(hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN, css_classes=["no-margin", "invisible", "action-row-button"])
        # self.button.connect("clicked", self.on_click)
        # self.overlay.set_child(self.button)

        self.connect("activated", self.on_click)

        self.set_activatable(True)


        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.allow_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, css_classes=["linked"], margin_end=15)
        self.main_box.append(self.allow_box)

        self.allow_image_toggle = Gtk.ToggleButton(css_classes=["blue-toggle-button"], icon_name="image-x-generic-symbolic", active=self.controls_image,
                                                   tooltip_text="Allow action to control the media")
        self.connect_image_signal()
        self.allow_box.append(self.allow_image_toggle)

        self.allow_background_toggle = Gtk.ToggleButton(css_classes=["blue-toggle-button"], icon_name="color-select-symbolic", active=self.controls_background,
                                                        tooltip_text="Allow action to control the background color")
        self.connect_background_signal()
        self.allow_box.append(self.allow_background_toggle)

        self.allow_label_toggle = ActionRowLabelToggle(self)
        self.allow_label_toggle.set_active(self.controls_labels)
        self.allow_box.append(self.allow_label_toggle)
        
        self.left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.main_box.append(self.left_box)

        self.left_top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        self.left_box.append(self.left_top_box)

        self.left_bottom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        self.left_box.append(self.left_bottom_box)

        self.label = Gtk.Label(label=f"<b>{self.action_name}</b> <span color=\"#979797\">({self.action_category})</span>", use_markup=True, xalign=0, hexpand=False, margin_end=5,
                               wrap_mode=Pango.WrapMode.WORD_CHAR, wrap=True)
        self.left_top_box.append(self.label)

        self.comment_label = Gtk.Label(label=self.comment or "", xalign=0, sensitive=False, ellipsize=Pango.EllipsizeMode.END, margin_end=60)
        self.left_bottom_box.append(self.comment_label)

        if self.comment in ["", None]:
            self.left_bottom_box.set_visible(False)
            # self.left_top_box.set_

        ## Edit buttons
        self.button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.END, valign=Gtk.Align.CENTER, css_classes=["linked"])
        self.main_box.append(self.button_box)
        # self.overlay.add_overlay(self.button_box)

        self.up_button = Gtk.Button(icon_name="go-up-symbolic")
        self.up_button.connect("clicked", self.on_click_up)
        self.button_box.append(self.up_button)

        self.down_button = Gtk.Button(icon_name="go-down-symbolic")
        self.down_button.connect("clicked", self.on_click_down)
        self.button_box.append(self.down_button)

    def update_allow_box_visibility(self) -> None:
        self.allow_box.set_visible(True) #TODO
        return

    def _control_index_for_toggle(self, active: bool) -> "tuple[bool, int | None]":
        """Return whether to write a control permission and its filtered action index.
        Write None when off; skip -1 or None indexes from screensaver or absent actions."""
        if not active:
            return True, None
        own = self.action_object.get_own_action_index()
        if own is None or own < 0:
            return False, None
        return True, own

    def on_allow_image_toggled(self, button: Gtk.ToggleButton) -> None:
        for child in self.expander.get_rows():
            if child is self:
                continue
            if not isinstance(child, ActionRow):
                continue
            child.set_image_toggled(False)


        controller = services.require_main_window().get_active_controller()
        if controller is None:
            return
        page = controller.active_page

        controller_input = self.action_object.get_input()
        state = self.expander.active_state
        if controller_input is None or state is None:
            log.error("Input or state not found")
            return
        input_state = controller_input.states.get(state)
        if input_state is None:
            log.error("Input state not found")
            return
        
        write, new_value = self._control_index_for_toggle(button.get_active())
        if not write:
            return
        input_state.action_permission_manager.set_image_control_index(new_value, True, True)

        if page is not None:
            page.reload_similar_pages(identifier=self.action_object.input_ident, reload_self=True)

    def on_allow_background_toggled(self, button: Gtk.ToggleButton) -> None:
        for child in self.expander.get_rows():
            if child is self:
                continue
            if not isinstance(child, ActionRow):
                continue
            child.set_background_toggled(False)

        controller = services.require_main_window().get_active_controller()
        if controller is None:
            return
        page = controller.active_page

        controller_input = self.action_object.get_input()
        state = self.expander.active_state
        if controller_input is None or state is None:
            log.error("Input or state not found")
            return
        input_state = controller_input.states.get(state)
        if input_state is None:
            log.error("Input state not found")
            return
        
        write, new_value = self._control_index_for_toggle(button.get_active())
        if not write:
            return
        input_state.action_permission_manager.set_background_control_index(new_value, True, True)

        if page is not None:
            page.reload_similar_pages(identifier=self.action_object.input_ident, reload_self=True)

    def label_toggled(self, i: int, value: bool) -> None:
        for child in self.expander.get_rows():
            if child is self:
                continue
            if not isinstance(child, ActionRow):
                continue
            active = child.allow_label_toggle.get_active()
            active[i] = False
            child.allow_label_toggle.set_active(active)

        controller = services.require_main_window().get_active_controller()
        if controller is None:
            return
       
        controller_input = self.action_object.get_input()
        state = self.expander.active_state
        if controller_input is None or state is None:
            log.error("Input or state not found")
            return
        input_state = controller_input.states.get(state)
        if input_state is None:
            log.error("Input state not found")
            return
        
        # Same guard as the image and background toggles: a -1 or None own
        # index (screensaver showing, or the action absent) must not be stored.
        write, index_value = self._control_index_for_toggle(value)
        if not write:
            return
        input_state.action_permission_manager.set_label_control_index(i, index_value, True, True)

    def connect_image_signal(self) -> None:
        if self._image_handler_id is None:
            self._image_handler_id = self.allow_image_toggle.connect("toggled", self.on_allow_image_toggled)

    def disconnect_image_signal(self) -> None:
        if self._image_handler_id is not None:
            self.allow_image_toggle.disconnect(self._image_handler_id)
            self._image_handler_id = None

    def connect_background_signal(self) -> None:
        if self._background_handler_id is None:
            self._background_handler_id = self.allow_background_toggle.connect("toggled", self.on_allow_background_toggled)

    def disconnect_background_signal(self) -> None:
        if self._background_handler_id is not None:
            self.allow_background_toggle.disconnect(self._background_handler_id)
            self._background_handler_id = None

    def set_image_toggled(self, value: bool) -> None:
        self.disconnect_image_signal()
        try:
            self.allow_image_toggle.set_active(value)
        finally:
            # An update that returns early or raises must still leave the toggle
            # wired, or the button stops reporting every later click.
            self.connect_image_signal()

    def set_background_toggled(self, value: bool) -> None:
        self.disconnect_background_signal()
        try:
            self.allow_background_toggle.set_active(value)
        finally:
            # An update that returns early or raises must still leave the toggle
            # wired, or the button stops reporting every later click.
            self.connect_background_signal()

    def on_click_up(self, button: Gtk.Button) -> None:
        self.expander.move_row_by(self, -1)

    def on_click_down(self, button: Gtk.Button) -> None:
        self.expander.move_row_by(self, 1)

    def init_dnd(self) -> None:
        """Enable drag reordering between loaded action rows.
        Missing or outdated rows reject drag; buttons preserve non-drag access to list ends."""
        dnd_source = Gtk.DragSource()
        dnd_source.set_actions(Gdk.DragAction.MOVE)
        dnd_source.connect("prepare", self.on_dnd_prepare)
        dnd_source.connect("drag-begin", self.on_dnd_begin)
        dnd_source.connect("drag-end", self.on_dnd_end)
        self.add_controller(dnd_source)

        dnd_target = Gtk.DropTarget.new(ActionRow, Gdk.DragAction.MOVE)
        dnd_target.connect("drop", self.on_dnd_drop)
        dnd_target.connect("motion", self.on_dnd_motion)
        dnd_target.connect("leave", self.on_dnd_leave)
        self.add_controller(dnd_target)

    def on_dnd_prepare(self, drag_source: Gtk.DragSource, x: float, y: float) -> Gdk.ContentProvider:
        drag_source.set_icon(
            Gtk.WidgetPaintable.new(self),
            # The paintable hotspot takes ints; the halves round down.
            self.get_width() // 2, self.get_height() // 2
        )
        return Gdk.ContentProvider.new_for_value(self)

    def on_dnd_begin(self, drag_source: Gtk.DragSource, data: Gdk.Drag) -> None:
        # The drop target reads this to tell a real move from a drop that
        # changes nothing, before the row is dropped.
        self.expander.dragged_row = self
        self.add_css_class(DRAGGED_CLASS)

    def on_dnd_end(self, drag_source: Gtk.DragSource, data: Gdk.Drag, flag: bool) -> None:
        self.expander.dragged_row = None
        self.remove_css_class(DRAGGED_CLASS)
        # A drop clears the indicator itself. This covers the drag that ends
        # anywhere else, where no drop handler runs.
        self.expander.clear_drop_indicators()

    def on_dnd_motion(self, drop_target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        source_row = self.expander.dragged_row
        drop_below = y > self.get_height() / 2
        if source_row is None or self.expander.plan_drop(source_row, self, drop_below) is None:
            # Nothing to show, and nothing to accept. The pointer sits on the
            # dragged row itself, or on the edge it already occupies.
            self.expander.clear_drop_indicators()
            return Gdk.DragAction(0)

        self.expander.show_drop_indicator(self, drop_below)
        return Gdk.DragAction.MOVE

    def on_dnd_leave(self, drop_target: Gtk.DropTarget) -> None:
        self.remove_css_class(DROP_ABOVE_CLASS)
        self.remove_css_class(DROP_BELOW_CLASS)

    def on_dnd_drop(self, drop_target: Gtk.DropTarget, value: Any, x: float, y: float) -> bool:
        self.expander.clear_drop_indicators()
        if not isinstance(value, ActionRow):
            return False

        plan = self.expander.plan_drop(value, self, y > self.get_height() / 2)
        if plan is None:
            return False
        source_index, dest_index = plan

        # Move on idle because rebuilding rows inside the drop removes this handler's widget.
        # Carry input and state because the sidebar can load another target first.
        GLib.idle_add(self.expander.apply_drop, source_index, dest_index,
                      self.expander.active_identifier, self.expander.active_state)
        return True

    def on_click(self, button: Gtk.Button) -> None:
        self.sidebar.action_configurator.load_for_action(self.action_object, self.index)
        self.sidebar.show_action_configurator()

    def update_comment(self, comment: str | None) -> None:
        self.comment = comment
        # Update ui
        if comment is None:
            comment = ""
            self.left_bottom_box.set_visible(False)
        else:
            self.left_bottom_box.set_visible(True)

        # build() stores the comment widget as comment_label.
        self.comment_label.set_label(comment)

class AddActionButtonRow:
    def __init__(self, expander: ActionExpanderRow) -> None:
        self.expander: ActionExpanderRow = expander
        self.button = Adw.ButtonRow(title=gl.lm.get("action-editor-add-new-action"), css_classes=["suggested-action", "add-action-button"])
        # self.button = Gtk.Button(hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN,
        #                          css_classes=["no-margin", "suggested-action"],
        #                          label=gl.lm.get("action-editor-add-new-action"),
        #                          margin_bottom=5, margin_top=5)
        self.button.connect("activated", self.on_click)
        self.action_name = "Add Action"
        # self.set_child(self.button)

    def on_click(self, button: Gtk.Button) -> None:
        identifier = self.expander.active_identifier
        if identifier is None:
            # The expander loads its identifier before the sidebar shows the
            # configurator that hosts this button.
            return
        self.expander.action_group.sidebar.let_user_select_action(callback_function=self.add_action, identifier=identifier)

    def add_action(self, action_class: "ActionHolder") -> None:
        log.trace(f"Adding action: {action_class}")

        # Gather data
        # action_string = gl.plugin_manager.get_action_string_from_action(action_class)
        active_page = services.require_main_window().get_active_page()
        if active_page is None:
            return
        
        identifier = self.expander.active_identifier
        state = self.expander.active_state
        if identifier is None or state is None:
            return
        state_dict = identifier.ensure_state_dict(active_page, state)
        state_dict.setdefault("actions", [])

        # Add action
        state_dict["actions"].append({
            "id": action_class.action_id,
            "settings": {}
        })

        if len(state_dict["actions"]) == 1:
            state_dict.setdefault("image-control-action", 0)
            state_dict.setdefault("label-control-actions", [0, 0, 0])
            state_dict.setdefault("background-control-action", 0)

        # Save page
        active_page.save()
        # Reload page to add an object to the new action
        active_page.load()
        # Reload the key on all decks
        active_page.reload_similar_pages(identifier=identifier, reload_self=True)

        # Reload ui
        self.expander.load_for_identifier(identifier, state)

        rows = self.expander.get_rows()
        if len(rows) < 2:
            return

        last_row = rows[-2]  # -1 is the add button
        action = last_row.action_object

        # Open Action Config Screen
        if gl.settings_manager.app().auto_open_action_config:
            if action and action.has_configuration:
                services.require_main_window().sidebar.action_configurator.load_for_action(last_row.action_object, last_row.index)
                services.require_main_window().sidebar.show_action_configurator()

        # Open Plugin Settings Window
        if action and action.plugin_base.has_plugin_settings and action.plugin_base.first_setup:
            settings_window = PluginSettingsWindow(action.plugin_base)
            settings_window.present(services.require_app().get_active_window())

            settings = action.plugin_base.get_settings()
            settings["first-setup"] = False
            action.plugin_base.set_settings(settings)
            action.plugin_base.first_setup = False
