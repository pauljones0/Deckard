
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
import contextlib
import threading
from loguru import logger as log
import subprocess
import os
from PIL import Image

from src.backend.DeckManagement.media_loop import MEDIA_LOOP_FPS
from src.backend.PluginManager.EventManager import EventManager
from src.backend.PluginManager.EventAssigner import EventAssigner

from gi.repository import GLib

import rpyc
from rpyc.utils.server import ThreadedServer
from rpyc.core.protocol import Connection
from rpyc.core import netref

from src.backend.DeckManagement.HelperMethods import is_image, is_svg, is_video
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout
from src.backend.DeckManagement.Subclasses.render_enums import LabelPosition
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent, InputIdentifier
from src.Signals.Signals import Signal

import globals as gl

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypedDict, cast, override
# One label keyed by position; set_label writes every declared field.
# Functional TypedDict syntax supports the hyphenated field names.
ActionLabel = TypedDict("ActionLabel", {
    "text": "str | None",
    "color": "list[int] | None",
    "font-family": "str | None",
    "font-size": "float | None",
    "outline_width": "int | None",
    "outline_color": "list[int] | None",
    "font-weight": "int | None",
    "font-style": "str | None",
})


from src.backend.PluginManager.PluginSettings.Asset import Color,Icon

if TYPE_CHECKING:
    # Type-only. Nothing here runs gi.require_version, and the annotations
    # that use Adw and Gtk are strings.
    from gi.repository import Adw
    from gi.repository import Gtk
    # GenerativeUI imports Gtk at module scope, so keep it type-only here and
    # import it lazily for the runtime check.
    from src.backend.PageManagement.Page import ActionOutdated, NoActionHolderFound
    from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI
    from src.backend.PluginManager.PluginBase import PluginBase
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput, ControllerInputState, ControllerKey
    from src.backend.PageManagement.Page import Page

class ActionCore(rpyc.Service):
    # Change to match your action
    def __init__(self, action_id: str, action_name: str,
                 deck_controller: "DeckController", page: "Page", plugin_base: "PluginBase", state: int,
                 input_ident: "InputIdentifier"):
        self.backend_connection: "Connection | None" = None
        self.backend: "netref.BaseNetref | None" = None
        self.server: "ThreadedServer | None" = None
        self.backend_process: subprocess.Popen[bytes] | None = None
        # register_backend relaxes its port-ownership check for a terminal
        # launch, where the backend is not a child of the Popen handle.
        self._backend_via_terminal: bool = False
        # The backend's rpyc service thread sets this and wakes
        # wait_for_backend on the launching thread.
        self._backend_ready = threading.Event()

        # The (signal, callback) pairs of this action, disconnected on teardown.
        self._connected_signals: "list[tuple[type[Signal], Callable[..., Any]]]" = []

        # Eviction, USB, media, and rpyc threads can call clean_up(); the lock
        # makes the state transition idempotent.
        self._cleaned_up = False
        self._cleanup_lock = threading.Lock()

        self.deck_controller = deck_controller
        # Construction requires a page, but Page.clear_action_objects detaches
        # it at teardown; every reader must support both states.
        self.page: "Page | None" = page
        self.state = state
        self.input_ident = input_ident
        self.action_id = action_id
        self.action_name = action_name
        self.plugin_base = plugin_base
        self.generative_ui_objects: list["GenerativeUI[Any]"] = []

        self.on_ready_called = False
        # Set after on_ready() returns or raises; ticks and external updates
        # use it because on_ready_called is true while on_ready still runs.
        self.on_ready_finished = False

        self.has_configuration = False
        self.allow_event_configuration: bool = True

        self.put_custom_config_rows_below_gen_ui: bool = False

        self.labels: "dict[str, ActionLabel]" = {}

        self.event_manager = EventManager()

        log.info(f"Loaded action {self.action_name} with id {self.action_id}")

    def clear_event_assigners(self) -> None:
        self.event_manager.clear_event_assigners()

    def load_event_overrides(self) -> None:
        self.event_manager.set_overrides(self.get_event_assignments())
        
    def set_deck_controller(self, deck_controller: "DeckController") -> None:
        """Internal function. Do not call it manually."""
        self.deck_controller = deck_controller
 
    def set_page(self, page: "Page") -> None:
        """Internal function. Do not call it manually."""
        self.page = page

    def get_input(self) -> "ControllerInput[Any] | None":
        # None when the identifier names an input this deck does not have.
        # DeckController.get_input then falls off the end of its search loop.
        return self.deck_controller.get_input(self.input_ident)

    def get_state(self) -> "ControllerInputState | None":
        i = self.get_input()
        if i is None: return None
        return cast("ControllerInputState | None", i.states.get(self.state))
    
    def add_event_assigner(self, event_assigner: EventAssigner) -> None:
        self.event_manager.add_event_assigner(event_assigner)

    def _raw_event_callback(self, event: InputEvent, data: dict[str, Any] | None = None) -> None:
        event_assigner = self.event_manager.get_event_assigner_for_event(event)
        if event_assigner:
            event_assigner.call(data)

    def event_callback(self, event: InputEvent, data: dict[str, Any] | None = None) -> None:
        pass

    def on_trigger(self) -> None:
        pass

    def on_tick(self) -> None:
        pass

    def on_ready(self) -> None:
        """The app calls this when the page can process action requests.

        Set the default image here rather than in the constructor. This hook
        runs off the GTK main thread, so do not touch a raw GTK object here.
        Use the GenerativeUI layer or GtkHelper.GtkHelper.run_on_main.
        """
        pass

    def on_update(self) -> None:
        """The app calls this when the action must redraw itself."""
        # Skip compatibility on_ready to avoid duplicate resources; this invocation returns.
        # The ready sequence supplies the update after the initial call finishes.
        if not self.on_ready_finished:
            log.debug(f"{self.action_id}: on_update compat on_ready skipped, on_ready has not finished")
            return
        self.on_ready() # backward compatibility

    def set_media(self, image: "Image.Image | None" = None, media_path: "str | None" = None, size: float | None = None, valign: float | None = None, halign: float | None = None, fps: int = MEDIA_LOOP_FPS, loop: bool = True, update: bool = True) -> None:
        self.raise_error_if_not_ready()

        if type(self.input_ident) not in [Input.Key, Input.Dial, Input.Touchscreen]:
            return
        # Touchscreens share the image, video, layout, and permission path with
        # keys and dials. Only keys use KeyGIF; touchscreen GIFs use InputVideo.

        if not self.get_is_present(): return
        if self.has_custom_user_asset(): return
        if not self.has_image_control(): return #TODO
        
        input_state = self.get_state()

        if input_state is None:
            return
        if input_state.state != self.state:
            return

        # Set a reopen path only for images loaded here; plugin-supplied images
        # have no known source file and must upscale in memory.
        path_for_reopen = None
        if media_path is not None and is_image(media_path) and image is None:
            with Image.open(media_path) as img:
                image = img.copy()
            path_for_reopen = media_path

        if media_path is not None and is_svg(media_path) and image is None:
            image = gl.media_manager.generate_svg_thumbnail(media_path)

        controller_input = self.get_input()
        if controller_input is None:
            return

        # Re-resolve the state under its lock because a page load can replace
        # all states; keep image decoding outside the lock.
        with controller_input._states_lock:
            input_state = controller_input.states.get(self.state)
            if input_state is None:
                return
            if input_state.state != self.state:
                return

            if image is not None:
                input_state.set_image(InputImage(
                    controller_input=controller_input,
                    image=image,
                    path=path_for_reopen,
                ), update=False)
                self._stamp_media_owner(input_state)

            elif media_path is not None and is_video(media_path):
                # Keep these imports local because deck_controller/inputs.py
                # imports ActionCore at module scope.
                from src.backend.DeckManagement.deck_controller.gif_pipeline import KeyGIF
                from src.backend.DeckManagement.deck_controller.inputs import ControllerKey
                key_gif = None
                if os.path.splitext(media_path)[1].lower() == ".gif" and isinstance(controller_input, ControllerKey):
                    # Keys use KeyGIF to preserve RGBA alpha and frame delays;
                    # dials, touchscreens, and bad KeyGIF input use InputVideo.
                    try:
                        key_gif = KeyGIF(
                            controller_key=controller_input,
                            gif_path=media_path,
                            fps=fps,
                            loop=loop
                        )
                    except Exception:
                        log.opt(exception=True).warning(
                            f"GIF decode failed in set_media, falling back to the opaque cv2 path: {media_path}")
                if key_gif is not None:
                    input_state.set_video(key_gif)
                else:
                    input_state.set_video(InputVideo(
                        controller_input=controller_input,
                        video_path=media_path,
                        fps=fps,
                        loop=loop
                    ))
                self._stamp_media_owner(input_state)

            else:
                input_state.set_image(None, update=False)

            # valign, halign and size are optional here, and ImageLayout
            # stores each one unchanged.
            input_state.layout_manager.set_action_layout(ImageLayout(
                valign=valign,
                halign=halign,
                size=size
            ), update=False)

        if update:
            controller_input.update()

    def _stamp_media_owner(self, input_state: "ControllerInputState") -> None:
        # Mark key and dial media ownership so load_from_input_dict can restore
        # it after create_n_states replaces the state objects.
        from src.backend.DeckManagement.deck_controller.inputs import (
            ControllerDialState,
            ControllerKeyState,
        )
        if isinstance(input_state, (ControllerKeyState, ControllerDialState)):
            input_state.media_owner_action = self

    def set_background_color(self, color: list[int] | None = None, update: bool = True) -> None:
        if color is None:
            color = [0, 0, 0, 0]

        self.raise_error_if_not_ready()

        if not self.get_is_present(): return

        if not self.has_background_control(): return

        if not self.on_ready_called:
            update = False

        state = self.get_state()
        if state is None or state.state != self.state: return

        state.background_manager.set_action_color(color)
        if update:
            controller_input = self.get_input()
            if controller_input is not None:
                controller_input.update()

    def show_error(self, duration: int = -1) -> None:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        if self.get_is_multi_action(): return
        state = self.get_state()
        if state is None:
            return
        try:
            state.show_error(duration=duration)
        except AttributeError as e:
            log.error(e)

    def hide_error(self) -> None:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        if self.get_is_multi_action(): return
        state = self.get_state()
        if state is None:
            return
        with contextlib.suppress(AttributeError):
            state.hide_error()

    def show_overlay(self, image: Image.Image, duration: int = -1) -> None:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        if self.get_is_multi_action(): return
        state = self.get_state()
        if state is None:
            return
        with contextlib.suppress(AttributeError):
            state.show_overlay(image, duration=duration)

    def hide_overlay(self) -> None:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        if self.get_is_multi_action(): return
        state = self.get_state()
        if state is None:
            return
        with contextlib.suppress(AttributeError):
            state.hide_overlay()

    def set_label(self, text: str | None, position: str = "bottom", color: list[int] | None=None,
                  font_family: str | None=None, font_size: "float | None" = None, outline_width: int | None = None, outline_color: list[int] | None = None,
                  font_weight: int | None = None, font_style: str | None = None,
                  update: bool=True) -> None:
        self.raise_error_if_not_ready()

        if type(self.input_ident) not in [Input.Key, Input.Dial]:
            return
        
        state = self.get_state()
        if state is None:
            log.error(f"Could not find state, action: {self.action_id}, state: {self.state}")
            return

        if not self.get_is_present():
            return
        if not self.on_ready_called:
            update = False
            update = True #FIXME

        if font_style not in ["normal", "italic", "oblique", None]:
            raise ValueError("font_style must be one of ['normal', 'italic', 'oblique', None]")

        # Plugin values are untyped; unknown positions use the bottom slot and
        # emit a warning.
        match position:
            case LabelPosition.TOP:
                label_index = 0
            case LabelPosition.CENTER:
                label_index = 1
            case LabelPosition.BOTTOM:
                label_index = 2
            case _:
                log.warning(f"Unknown label position {position!r}; using the bottom slot")
                label_index = 2

        if not self.has_label_control(label_index):
            return
        
        if text is None:
            text = ""

        text = str(text)

        # Every field below is optional on KeyLabel. An unset field takes the
        # page or font default at compose time, and does not read as empty.
        key_label = KeyLabel(
            controller_input=state.controller_input,
            text=text,
            font_size=font_size,
            font_name=font_family,
            color=color,
            outline_width=outline_width,
            outline_color=outline_color,
            font_weight=font_weight,
            style=font_style
        )

        self.labels[position] = {
            "text": key_label.text,
            "color": key_label.color,
            "font-family": key_label.font_name,
            "font-size": key_label.font_size,
            "outline_width": key_label.outline_width,
            "outline_color": key_label.outline_color,
            "font-weight": key_label.font_weight,
            "font-style": key_label.style
        }

        state.label_manager.set_action_label(label=key_label, position=position, update=update)

    def set_top_label(self, text: str, color: list[int] | None = None,
                      font_family: str | None = None, font_size: "float | None" = None, outline_width: int | None = None, outline_color: list[int] | None = None,
                      font_weight: int | None = None, font_style: str | None = None,
                      update: bool = True) -> None:
        self.set_label(text=text, position="top", color=color, font_family=font_family, font_size=font_size,
                       outline_width=outline_width, outline_color=outline_color,
                       font_weight=font_weight, font_style=font_style, update=update)

    def set_center_label(self, text: str, color: list[int] | None = None,
                      font_family: str | None = None, font_size: "float | None" = None, outline_width: int | None = None, outline_color: list[int] | None = None,
                      font_weight: int | None = None, font_style: str | None = None,
                      update: bool = True) -> None:
        self.set_label(text=text, position="center", color=color, font_family=font_family, font_size=font_size,
                       outline_width=outline_width, outline_color=outline_color,
                       font_weight=font_weight, font_style=font_style, update=update)

    def set_bottom_label(self, text: str, color: list[int] | None = None,
                      font_family: str | None = None, font_size: "float | None" = None, outline_width: int | None = None, outline_color: list[int] | None = None,
                      font_weight: int | None = None, font_style: str | None = None,
                      update: bool = True) -> None:
        self.set_label(text=text, position="bottom", color=color, font_family=font_family, font_size=font_size,
                       outline_width=outline_width, outline_color=outline_color,
                       font_weight=font_weight, font_style=font_style, update=update)

    def on_labels_changed_in_ui(self) -> None:
        # TODO
        pass

    def get_config_rows(self) -> "list[Adw.PreferencesRow]":
        return []
    
    def get_custom_config_area(self) -> "Gtk.Widget | None":
        return None
    
    def get_settings(self) -> dict[str, Any]:
        # self.page.load()
        if self.page is None:
            # Untyped rpyc callers can drive a detached action, so keep the
            # runtime check despite the construction contract.
            return {}
        return cast(dict[str, Any], self.page.get_action_settings(action_object=self))
    
    def set_settings(self, settings: dict[str, Any]) -> None:
        if self.page is None:
            # Untyped rpyc callers can drive a detached action, so keep the
            # runtime check despite the construction contract.
            return
        self.page.set_action_settings(action_object=self, settings=settings)

    def connect(self, signal: type[Signal], callback: Callable[..., Any]) -> None:
        gl.signal_manager.connect_signal(signal = signal, callback = callback)
        # Tracked, so the teardown can disconnect it. See clean_up.
        self._connected_signals.append((signal, callback))

    def get_own_key(self) -> "ControllerKey | None":
        if not isinstance(self.input_ident, Input.Key):
            return None
        # The isinstance guard selects get_input()'s Key overload, so this
        # already reads as ControllerKey | None.
        return self.deck_controller.get_input(self.input_ident)
    
    def get_is_multi_action(self) -> bool:
        self.raise_error_if_not_ready()

        page = self.page
        if page is None or not self.get_is_present(): return False
        # Query this state directly; action_objects at the identifier level
        # contains a state map, not an action list.
        actions = page.get_all_actions_for_input(self.input_ident, self.state)
        return len(actions) > 1

    def get_asset_path(self, asset_name: str, subdirs: list[str] | None = None, asset_folder: str = "assets") -> str:
        """Return the path to a plugin asset.

        Args:
            asset_name (str): Name of the asset file
            subdirs (list[str], optional): Subdirectories. Defaults to [].
            asset_folder (str, optional): The asset folder. Defaults to "assets".

        Returns:
            str: The full path to the asset
        """

        if not subdirs:
            return os.path.join(self.plugin_base.PATH, asset_folder, asset_name)

        subdir = os.path.join(*subdirs)
        if subdir != "":
            return os.path.join(self.plugin_base.PATH, asset_folder, subdir, asset_name)
        return ""

    def get_icon(self, key: str, skip_override: bool = False) -> Icon | None:
        return self.plugin_base.asset_manager.icons.get_asset(key, skip_override)

    def get_color(self, key: str, skip_override: bool = False) -> Color | None:
        return self.plugin_base.asset_manager.colors.get_asset(key, skip_override)

    def get_translation(self, key: str, fallback: str | None = None) -> str:
        return self.plugin_base.locale_manager.get(key, fallback)
    
    def has_label_controls(self) -> "list[bool]":
        state = self.get_state()
        if state is None:
            return []
        own_action_index = self.get_own_action_index()
        return [own_action_index == i for i in state.action_permission_manager.get_label_control_indices()]
    
    def has_label_control(self, label_index: int) -> bool:
        #TODO: Might require performance improvements
        state = self.get_state()
        if state is None:
            return False
        return state.action_permission_manager.get_label_control_index(label_index) == self.get_own_action_index()

    def has_image_control(self) -> bool:
        #TODO: Might require performance improvements
        state = self.get_state()
        if state is None:
            return False
        image_control_index = state.action_permission_manager.get_image_control_index()
        return image_control_index == self.get_own_action_index()

    
    def has_background_control(self) -> bool:
        #TODO: Might require performance improvements
        state = self.get_state()
        if state is None:
            return False
        background_control_index = state.action_permission_manager.get_background_control_index()
        return background_control_index == self.get_own_action_index()
    
    def get_is_present(self) -> bool:
        # Untyped rpyc callers can drive a detached action, so keep the runtime
        # check despite the construction contract.
        if self.page is None: return False
        if self.page.deck_controller.active_page is not self.page: return False
        if self.page.deck_controller.screen_saver.showing: return False
        # if self.state != self.get_state().state: return False #TODO: Check for touchscreen and dial states
        return self in self.page.get_all_actions()
    
    def has_custom_user_asset(self) -> bool:
        page = self.page
        if page is None or not self.get_is_present(): return False
        media = self.input_ident.get_state_dict(page, self.state).get("media", {})
        return media.get("path", None) is not None
    
    def get_own_action_index(self) -> int | None:
        # Return -1 if detached, inactive, hidden by the screen saver, or absent from page actions.
        # Return None if absent from this input state; permission getters need the unset value.
        page = self.page
        if page is None or not self.get_is_present(): return -1
        actions = page.get_all_actions_for_input(self.input_ident, self.state)
        if self not in actions:
            return None
        return cast(int | None, actions.index(self))

    # None maps an event to no assigner; every event key remains present so
    # callers can iterate the complete map and skip None values.
    def get_page_event_assignments(self) -> dict[InputEvent, InputEvent | None]:
        assignment: dict[InputEvent, InputEvent | None] = {}

        # A detached action has no stored overrides, so use the same empty map
        # as a page without assignments.
        page = self.page
        page_assignment_dict = ({} if page is None
                                else page.get_action_event_assignments(action_object=self))

        all_events = Input.AllEvents()
        for event in all_events:
            if event.string_name in page_assignment_dict:
                assignment[event] = Input.EventFromStringName(page_assignment_dict[event.string_name])
            else:
                assignment[event] = event

        return assignment
    
    def set_all_events_to_null(self) -> None:
        for input_type in self.event_manager.get_event_map().keys():
            self.set_event_assignment(input_type, None)

    
    def get_event_assignments(self) -> dict[str, str | None]:
        # Teardown can reload overrides after detachment; an empty map keeps
        # that path valid without a page.
        page = self.page
        if page is None:
            return {}
        return page.get_action_event_assignments(
            action_object=self
        )

    def set_event_assignment(self, input_event: InputEvent | None, event_assigner: EventAssigner | None) -> None:
        page = self.page
        if page is None:
            # A detached action has nowhere to store the assignment; warn so a
            # lost UI edit is visible.
            log.warning(f"Action {self.action_id} has no page, so its event assignment was not stored")
            return
        page.set_action_event_assigment(
            event_assigner=event_assigner,
            input_event=input_event,
            action_object=self
        )

        self.load_event_overrides()
    
    def raise_error_if_not_ready(self) -> None:
        if self.on_ready_called:
            return
        raise Warning("Seems like you're calling this method before the action is ready")
    
    def get_generative_ui_objects(self) -> list["GenerativeUI[Any]"]:
        from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI

        objects = []
        for attr in dir(self):
            if isinstance(getattr(self, attr), GenerativeUI):
                objects.append(getattr(self, attr))

        return objects

    def add_generative_ui_object(self, generative_ui_object: "GenerativeUI[Any]") -> None:
        self.generative_ui_objects.append(generative_ui_object)

    def remove_generative_ui_object(self, generative_ui_object: "GenerativeUI[Any]") -> None:
        """Unregister a GenerativeUI element, such as a rebuilt config row.

        The action then stops retaining it for its own lifetime."""
        with contextlib.suppress(ValueError):
            self.generative_ui_objects.remove(generative_ui_object)

    def get_generative_ui(self) -> "list[GenerativeUI[Any]]":
        return self.generative_ui_objects

    def get_generative_ui_widgets(self) -> "list[Gtk.Widget]":
        widgets = []

        for generative_object in self.generative_ui_objects:
            widget = generative_object.widget

            if widget is None:
                continue

            widgets.append(widget)
        return cast("list[Gtk.Widget]", widgets)

    def load_initial_generative_ui(self) -> None:
        GLib.idle_add(self._do_load_initial_generative_ui)

    def _do_load_initial_generative_ui(self) -> None:
        # Reconcile only built widgets; reading .widget here would eagerly
        # build every config row, while unbuilt rows already read persisted values.
        for generative_object in self.generative_ui_objects:
            if generative_object.is_built:
                generative_object.load_initial_ui()
    
    def start_server(self) -> None:
        if self.server is not None:
            log.warning("Server already running, skipping...")
            return
        from src.backend.PluginManager.PluginManager import frontend_authenticator

        self.server = ThreadedServer(self, hostname="localhost", port=0, protocol_config={"allow_public_attrs": True},
                                     authenticator=frontend_authenticator)
        threading.Thread(target=self.server.start, name="server_start", daemon=True).start()

    @override
    def on_disconnect(self, conn: "Connection | None" = None) -> None:
        # The rpyc disconnect hook. A dropped connection with a live process
        # orphans the backend, so the full teardown runs here too.
        self._release_backend_resources()
    
    def launch_backend(self, backend_path: str, venv_path: str | None = None, open_in_terminal: bool = False) -> None:
        """Start the rpyc server, validate paths, and launch this action backend.
        Raise RuntimeError if no server starts, or ValueError for invalid paths before Popen."""
        from src.backend.PluginManager.PluginManager import (
            backend_guard_env,
            build_backend_launch_command,
            attempt_backend_venv_repair,
            inject_backend_guard,
        )

        self.start_server()
        if self.server is None:
            # start_server() sets self.server. An override that does not set
            # it would launch a backend with no port to register on.
            raise RuntimeError("the rpyc server is not running, so the backend has no port to register on")
        port = self.server.port

        # Validate the venv before command construction; the owning plugin must
        # rebuild an environment stranded by a Python upgrade.
        if venv_path is not None:
            attempt_backend_venv_repair(venv_path, self.plugin_base.PATH, self.action_id)

        command = build_backend_launch_command(backend_path, venv_path, port, open_in_terminal)

        # The guard binds the child server to loopback; the venv copy survives
        # terminal launch, while PYTHONPATH covers a venv-less child.
        if venv_path is not None:
            inject_backend_guard(venv_path)
        elif open_in_terminal:
            log.warning("Terminal backend launch without a venv: no loopback guard reaches the child")

        log.info(f"Launching backend: {command}")
        self._backend_via_terminal = open_in_terminal
        # Clear after validation and before spawn so relaunch waits for the new
        # backend registration instead of the previous one.
        self._backend_ready.clear()
        self.backend_process = subprocess.Popen(command, start_new_session=True, env=backend_guard_env())
        if gl.plugin_manager is not None:
            gl.plugin_manager.backend_processes.append(self.backend_process)

        self.wait_for_backend()

    def wait_for_backend(self, tries: int = 3) -> None:
        """Block until registration or a timeout of tries * 0.1 seconds.
        Registration wakes the call immediately, so tries is a timeout budget."""
        self._backend_ready.wait(timeout=tries * 0.1)

    def register_backend(self, port: int) -> None:
        """Internal method. Do not call it manually."""
        from src.backend.PluginManager.PluginManager import terminate_refused_backend, verify_backend_port

        # Verify that the launched child owns a loopback port before exposing
        # this process through netref, then connect to the verified address.
        try:
            host = verify_backend_port(port, self.backend_process, self._backend_via_terminal, self.action_id)
        except RuntimeError:
            # The backend is exposed or unverifiable. Terminate it, so a
            # LAN-reachable port does not outlive the refused registration.
            terminate_refused_backend(self.backend_process, self.action_id)
            raise
        self.backend_connection = rpyc.connect(host, port, config={"allow_public_attrs": True})
        self.backend = self.backend_connection.root
        if gl.plugin_manager is not None:
            gl.plugin_manager.backends.append(self.backend_connection)
        # Only after the connection attributes hold their values. The caller
        # that wait_for_backend wakes reads self.backend at once.
        self._backend_ready.set()
        self.on_backend_ready()

    def on_backend_ready(self) -> None:
        pass

    def ping(self) -> bool:
        return True
    
    def on_removed_from_cache(self) -> None:
        """Notify a plugin that an action left a live page or cache.
        The framework always calls clean_up(), even if this override raises."""
        pass

    def on_remove(self) -> None:
        """Notify a plugin of removal through the action configurator.
        The framework always calls clean_up(), as for on_removed_from_cache()."""
        pass

    @staticmethod
    def teardown(action: "ActionCore | NoActionHolderFound | ActionOutdated | None", hook_name: str = "on_removed_from_cache") -> None:
        """Run the named removal hook and always clean up an ActionCore.
        Ignore placeholders and other values that are not ActionCore instances."""
        if not isinstance(action, ActionCore):
            return
        try:
            getattr(action, hook_name)()
        except Exception:
            log.opt(exception=True).error(
                f"{hook_name} failed for {getattr(action, 'action_id', action)}"
            )
        action.clean_up()

    def clean_up(self) -> None:
        """Tear down after reload, uninstall, sidebar or config removal, or cache eviction.
        Any thread can call this; do not call run_on_main() here or from synchronous teardown."""
        # The lock makes cleanup idempotent across main, USB, media, and rpyc threads.
        # Queued work can run later; use get_is_present() and expect empty settings when detached.
        with self._cleanup_lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True

        # Disconnect callbacks so SignalManager stops retaining this action.
        for signal, callback in self._connected_signals:
            try:
                gl.signal_manager.disconnect_signal(signal, callback)
            except Exception as e:
                log.error(f"Failed to disconnect signal {signal}: {e}")
        self._connected_signals.clear()

        # Clear the list synchronously, then queue the GTK widget teardown on
        # the main loop.
        gen_ui_snapshot = list(self.generative_ui_objects)
        self.generative_ui_objects.clear()
        if gen_ui_snapshot:
            GLib.idle_add(self._destroy_gen_ui_batch, gen_ui_snapshot)

        self._release_backend_resources()

    @staticmethod
    def _destroy_gen_ui_batch(snapshot: list["GenerativeUI[Any]"]) -> None:
        """Destroy a GenerativeUI teardown snapshot on the GTK main loop.
        GenerativeUI.destroy() then runs inline and cannot re-queue or deadlock."""
        for generative_ui in snapshot:
            try:
                owner = generative_ui.action_core
                if owner is not None and generative_ui in owner.generative_ui_objects:
                    # A live action re-registered this object after the snapshot;
                    # its current owner must retain it.
                    continue
                if getattr(generative_ui, "_widget", None) is None:
                    # It built no widget, so there is nothing to unparent, and
                    # it left generative_ui_objects already.
                    continue
                generative_ui.destroy()
            except Exception:
                log.opt(exception=True).error(
                    f"Failed to destroy GenerativeUI object {generative_ui!r}"
                )

    def _release_backend_resources(self) -> None:
        """Detach and tear down the rpyc server, connection, and process.
        Concurrent cleanup and disconnect calls are safe and idempotent."""
        if self.backend_connection is None and self.server is None and self.backend_process is None:
            return

        # Snapshot and detach the backend resources, then close them
        # off-thread.
        server, connection, process = self.server, self.backend_connection, self.backend_process
        self.server = None
        self.backend_connection = None
        self.backend_process = None
        self.backend = None

        if connection is not None and gl.plugin_manager is not None:
            with contextlib.suppress(ValueError):
                gl.plugin_manager.backends.remove(connection)
        if process is not None and gl.plugin_manager is not None:
            with contextlib.suppress(ValueError):
                gl.plugin_manager.backend_processes.remove(process)

        threading.Thread(
            target=self._teardown_backend_resources,
            args=(server, connection, process),
            name="action_backend_teardown",
            daemon=True,
        ).start()

    @staticmethod
    def _teardown_backend_resources(server: "ThreadedServer | None", connection: "Connection | None", process: "subprocess.Popen[bytes] | None") -> None:
        # This runs on a worker thread. See clean_up. Each close and terminate
        # tolerates a failure, because a hung backend must not stop the app.
        if connection is not None:
            try:
                connection.close()
            except Exception as e:
                log.error(f"Failed to close backend connection: {e}")
        if server is not None:
            try:
                server.close()
            except Exception as e:
                log.error(f"Failed to close backend server: {e}")
        if process is not None:
            from src.backend.PluginManager.PluginManager import terminate_backend_process
            terminate_backend_process(process)
