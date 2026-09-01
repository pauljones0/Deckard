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

from gi.repository import Gtk

from src.backend.DeckManagement.deck_controller.inputs import ControllerInput, StateT
from src.backend.DeckManagement.InputIdentifier import InputIdentifier
from src.backend import services

import globals as gl

from collections.abc import Callable
from typing import Any

class StateSwitcher(Gtk.ScrolledWindow):
    def __init__(self, type: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.type = type

        # A switch callback takes no arguments. An add-new callback takes the
        # index of the state that this switcher appended.
        self.switch_callbacks: list[Callable[[], object]] = []
        self.add_new_callbacks: list[Callable[[int], object]] = []

        # Visible-child-name handler ID, or None while disconnected.
        # Tracking keeps connect and disconnect idempotent.
        self._switch_handler_id: int | None = None

        self.build()

    def build(self) -> None:
        self.stack = Gtk.Stack()

        self.main_box = Gtk.Box(overflow=Gtk.Overflow.HIDDEN, css_classes=["state-switcher-box", "linked"], valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.set_child(self.main_box)

        self.switcher = Gtk.StackSwitcher(stack=self.stack, css_classes=["state-switcher"])
        self.main_box.append(self.switcher)

        self.add_button = Gtk.Button(icon_name="list-add-symbolic")
        self.add_button.connect("clicked", self.on_add_click)
        self.main_box.append(self.add_button)

    def clear_stack(self) -> None:
        child = self.stack.get_first_child()
        while child is not None:
            self.stack.remove(child)
            child = self.stack.get_first_child()

    def get_n_states(self) -> int:
        n = 0
        child = self.stack.get_first_child()
        while child is not None:
            n += 1
            child = child.get_next_sibling()
        return n

    def set_n_states(self, n: int) -> None:
        self._disconnect_signal()
        try:
            self.clear_stack()

            for i in range(n):
                self.stack.add_titled(Gtk.Box(), str(i+1), f"State {i+1}")
        finally:
            # An update that returns early or raises must still leave the stack
            # wired, or every later state switch is dropped silently.
            self._connect_signal()

    def on_add_click(self, button: Gtk.Button) -> None:
        main_win = services.require_main_window()
        controller = main_win.get_active_controller()
        if controller is None:
            return
        c_input = controller.get_input(main_win.sidebar.active_identifier)

        if c_input is None:
            return

        # The input owns the state list and tells the sidebar to reload.
        # Do not add a stack child or fire the add callback here.
        c_input.add_new_state()

    def get_selected_state(self) -> int:
        name = self.stack.get_visible_child_name()
        if name is None:
            raise RuntimeError(
                "the state switcher holds no state -- set_n_states fills the "
                "stack, and nothing selects a state before it runs."
            )
        return int(name) - 1
    
    def select_state(self, state: int) -> None:
        if state >= self.get_n_states():
            return
        self._disconnect_signal()
        try:
            self.stack.set_visible_child_name(str(state + 1))
        finally:
            # An update that returns early or raises must still leave the stack
            # wired, or every later state switch is dropped silently.
            self._connect_signal()

    def _connect_signal(self) -> None:
        if self._switch_handler_id is None:
            self._switch_handler_id = self.stack.connect("notify::visible-child-name", self.on_state_switch)

    def _disconnect_signal(self) -> None:
        if self._switch_handler_id is not None:
            self.stack.disconnect(self._switch_handler_id)
            self._switch_handler_id = None

    def add_switch_callback(self, callback: Callable[[], object]) -> None:
        self.switch_callbacks.append(callback)

    def add_add_new_callback(self, callback: Callable[[int], object]) -> None:
        self.add_new_callbacks.append(callback)

    def on_state_switch(self, *args: object) -> None:
        for callback in self.switch_callbacks:
            if callable(callback):
                callback()

    def load_for_identifier(self, identifier: InputIdentifier, state: int) -> None:
        if gl.app is None:
            return
        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return
        c_input = controller.get_input(identifier)
        if c_input is None:
            return

        self.load_for_input(c_input, state)

    def load_for_input(self, c_input: ControllerInput[StateT], state: int | None = None) -> None:
        self.set_n_states(len(c_input.states.keys()))
        self.select_state(state or c_input.state)

        self.set_visible(c_input.enable_states)
