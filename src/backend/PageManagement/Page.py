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
import os
import threading
import time

import globals as gl

from loguru import logger as log
from contextlib import contextmanager

from src.backend.PluginManager.EventAssigner import EventAssigner
from src.backend.PageManagement import page_document, page_flush
import globals as gl

from src.backend.PluginManager.ActionCore import ActionCore
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent, InputIdentifier
from src.backend.DeckManagement.media_loop import MEDIA_LOOP_FPS
from typing import cast, Any, Iterator, TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput, ControllerInputState
    from src.backend.DeckManagement.deck_controller.label_engine import LabelManager
    from src.backend.PluginManager.ActionHolder import ActionHolder


# Inside the body of Page the name dict is the page-content property, so an
# annotation there reaches the builtin through this alias.
_Dict = dict

# A missing fps key uses the media-loop ceiling, which applies no lower cap.
# MediaConfig.from_dict uses the same default.
DEFAULT_MEDIA_FPS = MEDIA_LOOP_FPS

# Registry path: input type -> JSON identifier -> state -> index.
# A leaf is an action, an unresolved or outdated placeholder, or an empty slot.
ActionObjects = _Dict[str, _Dict[str, _Dict[int, _Dict[int, "ActionCore | NoActionHolderFound | ActionOutdated | None"]]]]


class Page:
    def __init__(self, json_path: str, deck_controller: "DeckController", *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self.json_path = json_path
        self.deck_controller = deck_controller

        # Share one page document for this path and bind it before load() reads it.
        self._document = page_document.document_for(json_path)

        # The action objects, kept so a reload can reuse them. Keyed
        # input_type -> json_identifier -> state -> index -> action.
        self.action_objects: ActionObjects = {}

        # Serialize the ready claim because initialize_actions runs outside _load_page_lock.
        # Concurrent page loads must not submit on_ready twice.
        self._ready_claim_lock = threading.Lock()

        self.load(load_from_file=True)

    @property
    def dict(self) -> _Dict[str, Any]:
        """Return the shared content of this page's document.
        Mutate this dict, or refresh the document to replace all content."""
        return self._document.data

    def rebind_document(self, document: "page_document.PageDocument") -> None:
        """Read through document after a page rename.
        This keeps Pages created during the rename on the shared content."""
        self._document = document

    def get_name(self) -> str:
        return os.path.splitext(os.path.basename(self.json_path))[0]

    def update_dict(self) -> None:
        """Refresh shared content from disk without changing action objects.
        Call only before action-object changes; this replaces every Page's unsaved content."""
        self._document.refresh_from_disk()

    def load(self, load_from_file: bool = False) -> None:
        start = time.time()
        if load_from_file:
            self.update_dict()
        self.load_action_objects()

        end = time.time()
        log.debug(f"Loaded page {self.get_name()} in {end - start:.2f} seconds")

    def save(self) -> None:
        # Mark the shared content dirty and let the per-path flush seam coalesce writes.
        # Page switches, deck close, quit, and file reads flush pending data.
        page_flush.get().mark_dirty(self)

    @contextmanager
    def edit(self) -> Iterator[_Dict[str, Any]]:
        """Apply one atomic page edit and mark it for writing.
        The block must not read a page file, reload a deck, or marshal to GTK."""
        # The document lock prevents writes from observing a partial compound edit.
        # It is a leaf lock; perform reads, reloads, and main-thread marshaling after release.
        with self._document.edit() as data:
            yield data

    def flush(self) -> str:
        """Write pending edits now and return the path written.
        Flush boundaries are page leave, deck close, app quit, and page-file read."""
        page_flush.get().flush_path(self.json_path)
        return self.json_path

    def make_backup(self, json_path: str | None = None) -> None:
        # Use the flush path because a rename can change json_path during an old-path write.
        # The backup must copy the file that the flush will overwrite.
        page_document.back_up_page_file(json_path if json_path is not None else self.json_path)

    def move_key_to_end(self, dictionary: _Dict[str, Any], key: str) -> None:
        # Reorder the flush snapshot, not live content that can change during a save.
        page_document.move_key_to_end(dictionary, key)

    def load_action_objects(self) -> None:
        # The import is function-scoped, because deck_controller/controller.py
        # imports this module and a module-level import here closes a cycle.
        from src.backend.DeckManagement.deck_controller.controller import CONTROLLER_CLASSES

        new_action_objects: ActionObjects = {}

        for input_type in Input.All:
            input_class = CONTROLLER_CLASSES[input_type]
            input_type_name = input_type.input_type
            for key in input_class.Available_Identifiers(self.deck_controller.deck):
                input_ident = Input.FromTypeIdentifier(input_type_name, key)
                for state in input_ident.get_states(self):
                    try:
                        # Registry states use integers, while page JSON uses numeric strings.
                        # Ignore a state key that cannot map between them.
                        state_int = int(state)
                    except ValueError:
                        continue
                    for i, action in enumerate(input_ident.get_actions(self, state_int)):
                        if action.get("id") is None:
                            continue

                        action_object = self.get_new_action_object(
                            # loaded_action_objects=self.action_objects,
                            loaded_action_objects=self.action_objects,
                            action_id=action["id"],
                            state=state_int,
                            i=i,
                            input_ident=input_ident,
                        )
                        # input_action_objects[state][i] = action_object
                        new_action_objects.setdefault(input_type_name, {})
                        new_action_objects[input_type_name].setdefault(key, {})
                        new_action_objects[input_type_name][key].setdefault(state_int, {})
                        # new_action_objects[input_type][key][state].setdefault(i, {})
                        new_action_objects[input_type_name][key][state_int][i] = action_object

        old_actions = self.get_all_actions(self.action_objects)
        new_actions = self.get_all_actions(new_action_objects)

        for old_action in old_actions:
            if old_action not in new_actions:
                # Framework teardown notifies first and always calls clean_up.
                # A plugin override that omits super() cannot leak the dropped action.
                ActionCore.teardown(old_action)

        self.action_objects = new_action_objects

        if self.deck_controller.active_page == self:
            # The page is loaded already, so this covers new actions only.
            self.initialize_actions()

    # def load_action_object_sector(self, loaded_action_objects, dict_key: str, state)

    def get_new_action_object(self, loaded_action_objects: ActionObjects, action_id: str, state: int, i: int, input_ident: InputIdentifier) -> "ActionCore | NoActionHolderFound | ActionOutdated | None":
        
        plugin_manager = gl.plugin_manager
        if plugin_manager is None:
            # Only before create_global_objects(), where no holder resolves.
            return NoActionHolderFound(id=action_id, identifier=input_ident, state=state)

        action_holder = plugin_manager.get_action_holder_from_id(action_id)

        ## No action holder found
        if action_holder is None:
            plugin_id = plugin_manager.get_plugin_id_from_action_id(action_id)
            if plugin_id is not None and plugin_manager.get_is_plugin_out_of_date(plugin_id):
                return ActionOutdated(id=action_id, identifier=input_ident, state=state)
            return NoActionHolderFound(id=action_id, identifier=input_ident, state=state)

        ## Keep old object if it exists
        old_action = loaded_action_objects.get(input_ident.input_type, {}).get(input_ident.json_identifier, {}).get(state, {}).get(i)
        if old_action is not None:
            # A holder without an action class cannot vouch for the old
            # object; the isinstance below needs a real class either way.
            action_core_class = action_holder.action_core
            if action_core_class is not None and isinstance(old_action, action_core_class):
                return old_action
            
        ## Create new action object            
        action_object = action_holder.init_and_get_action(
            deck_controller=self.deck_controller,
            page=self,
            state=state,
            input_ident=input_ident,
        )
        return action_object

    def switch_actions_of_inputs(self, input_1: InputIdentifier, input_2: InputIdentifier) -> None:
        input_1_dict = self.action_objects.get(input_1.input_type, {}).get(input_1.json_identifier, {})
        input_2_dict = self.action_objects.get(input_2.input_type, {}).get(input_2.json_identifier, {})

        # Only real actions carry input_ident; placeholders and empty slots do not.
        for state in input_1_dict:
            for action in input_1_dict[state].values():
                if isinstance(action, ActionCore):
                    action.input_ident = input_2

        for state in input_2_dict:
            for action in input_2_dict[state].values():
                if isinstance(action, ActionCore):
                    action.input_ident = input_1

        # Change in action_objects
        self.action_objects.setdefault(input_1.input_type, {})
        self.action_objects.setdefault(input_2.input_type, {})
        self.action_objects[input_1.input_type][input_1.json_identifier] = input_2_dict
        self.action_objects[input_2.input_type][input_2.json_identifier] = input_1_dict


    @log.catch
    def add_action_object_from_holder(self, action_holder: "ActionHolder", input_ident: "InputIdentifier", state: int, i: int) -> None:
        action_object = action_holder.init_and_get_action(deck_controller=self.deck_controller, page=self, input_ident=input_ident, state=state)
        if action_object is None:
            return
        self.action_objects.setdefault(input_ident.input_type, {})
        self.action_objects[input_ident.input_type].setdefault(input_ident.json_identifier, {})
        self.action_objects[input_ident.input_type][input_ident.json_identifier].setdefault(int(state), {})
        self.action_objects[input_ident.input_type][input_ident.json_identifier][int(state)][i] = action_object

    def remove_plugin_action_objects(self, plugin_id: str) -> bool:
        plugin_manager = gl.plugin_manager
        if plugin_manager is None:
            return False

        plugin = plugin_manager.get_plugin_by_id(plugin_id)
        if plugin is None:
            return False

        # Collect before deletion, then call framework teardown for every removed action.
        # Deleting a local reference alone does not call clean_up.
        to_remove: list[tuple[str, str, int, int, ActionCore]] = []
        for type in list(self.action_objects.keys()):
            for key in list(self.action_objects[type].keys()):
                for state in list(self.action_objects[type][key].keys()):
                    for index in list(self.action_objects[type][key][state].keys()):
                        action = self.action_objects[type][key][state][index]
                        if not isinstance(action, ActionCore):
                            continue
                        if action.plugin_base == plugin:
                            to_remove.append((type, key, state, index, action))

        for type, key, state, index, action in to_remove:
            del self.action_objects[type][key][state][index]
            ActionCore.teardown(action)

        return True
    
    def update_inputs_with_actions_from_plugin(self, plugin_id: str) -> None:
        # plugin_obj = gl.plugin_manager.get_plugin_by_id(plugin_id)
        plugin_manager = gl.plugin_manager
        if plugin_manager is None:
            return

        for input_type in list(self.action_objects.keys()):
            for json_identifier in list(self.action_objects[input_type].keys()):
                for state in list(self.action_objects[input_type][json_identifier].keys()):
                    for index in list(self.action_objects[input_type][json_identifier][state].keys()):
                        action_core = self.action_objects[input_type][json_identifier][state][index]
                        if action_core is None:
                            continue
                        action_id = action_core.action_id

                        if plugin_manager.get_plugin_id_from_action_id(action_id) == plugin_id:
                            identifier = Input.FromTypeIdentifier(input_type, json_identifier)

                            c_input = self.deck_controller.get_input(identifier)
                            if c_input is None:
                                # The input can detach between the page walk
                                # and this read, on a deck mid-teardown.
                                continue
                            if c_input.state == int(state):
                                c_input.update()
    
#    def get_keys_with_plugin(self, plugin_id: str):
#        plugin_obj = gl.plugin_manager.get_plugin_by_id(plugin_id)
#        if plugin_obj is None:
#            return []
#        
#        keys = []
#        for type in self.action_objects.values():
#            for key in self.action_objects[type]:
#                for state in self.action_objects[type][state]:
#                    for action in self.action_objects[type][state][key].values():
#                        if not isinstance(action, ActionCore):
#                            continue
#                        if action.plugin_base == plugin_obj:
#                            keys.append(key)
#
#        return keys

    def remove_plugin_actions_from_json(self, plugin_id: str) -> None:
        for type in Input.KeyTypes:
            # A page json can lack an input type. A non-Plus deck has no
            # touchscreens section.
            for key in self.dict.get(type, {}):
                for state in self.dict[type][key].get("states", {}):
                    actions = self.dict[type][key]["states"][state].get("actions", [])
                    # Collect the indices first. A delete from actions during
                    # the enumerate() walk skips the next entry.
                    to_remove = [
                        i for i, action in enumerate(actions)
                    # Actions here are raw JSON dicts, not action objects.
                    # The fallback also handles an explicit null id.
                        if (action.get("id") or "").split("::")[0] == plugin_id
                    ]
                    for i in reversed(to_remove):
                        del actions[i]

        self.save()

    def get_without_action_objects(self) -> _Dict[str, Any]:
        # The document owns the content and its file shape. The flush writes
        # pages that no deck shows, and those have no Page to ask.
        return self._document.get_without_action_objects()

    def get_all_actions(self, action_dict: ActionObjects | None = None) -> list[ActionCore]:
        if action_dict is None:
            action_dict = self.action_objects
        actions = []
        for input_type in action_dict:
            for key in action_dict[input_type]:
                for state in action_dict[input_type][key]:
                    for action in action_dict[input_type][key][state].values():
                        if action is None:
                            continue
                        if not isinstance(action, ActionCore):
                            continue
                        actions.append(action)
        return actions
    
    def get_all_actions_for_type(self, ident: InputIdentifier, only_action_cores: bool = False) -> "list[ActionCore | NoActionHolderFound | ActionOutdated]":
        actions = []
        input_type = ident.input_type
        input_identifier = ident.json_identifier
        if input_identifier in self.action_objects.get(input_type, {}):
            for state in self.action_objects[input_type].get(input_identifier, {}):
                for action in self.action_objects[input_type][input_identifier].get(state, {}).values():
                    if action is None or not action:
                        continue
                    if only_action_cores and not isinstance(action, ActionCore):
                        continue
                    actions.append(action)
        return actions
    
    def get_all_actions_for_input(self, ident: InputIdentifier, state: int, only_action_cores: bool = False) -> "list[ActionCore | NoActionHolderFound | ActionOutdated]":
        actions = []
        input_type = ident.input_type
        json_identifier = ident.json_identifier
        if json_identifier in self.action_objects.get(input_type, {}):
            if state in self.action_objects[input_type].get(json_identifier, {}):
                for action in self.action_objects[input_type][json_identifier].get(state, {}).values():
                    if action is None or not action:
                        continue
                    if only_action_cores and not isinstance(action, ActionCore):
                        continue
                    actions.append(action)
        return actions
    
    def get_action(self, identifier: InputIdentifier | None = None, state: int | None = None, index: int | None = None) -> "ActionCore | NoActionHolderFound | ActionOutdated | None":
        if identifier is None or state is None or index is None:
            # A missing coordinate keys nothing; the untyped read answered
            # None here and this keeps that.
            return None
        return self.action_objects.get(identifier.input_type, {}).get(identifier.json_identifier, {}).get(state, {}).get(index)
    
    def get_action_dict(self, action_object: "ActionCore | None" = None, identifier: InputIdentifier | None = None, state: int | None = None, index: int | None = None) -> _Dict[str, Any]:
        # Arg validation
        if action_object is None:
            if None in (identifier, state, index):
                raise ValueError("Please pass an identifier, state and index or an action object")
            
        if action_object is None:
            found = self.get_action(identifier, state, index)
            # Only a real action owns a dict to look up; a placeholder or an
            # empty slot has nothing, and the raise below reports it.
            action_object = found if isinstance(found, ActionCore) else None

        if action_object is None:
            raise ValueError("Could not find action object")
        
        ident = action_object.input_ident
        # get_states keys by the state's string spelling; the loop name must
        # not rebind the state parameter above.
        for state_key in ident.get_states(self):
            try:
                state_int = int(state_key)
            except (TypeError, ValueError):
                # Skip non-integer state keys so the ready handshake can reach valid states.
                # Raising here would leave the action unready for the page lifetime.
                continue
            for i, action_dict in enumerate(ident.get_actions(self, state_key)):
                if self.get_action(ident, state_int, i) is action_object:
                    # The list holds the page JSON, so the element is a dict.
                    return cast("_Dict[str, Any]", action_dict)

        return {}
                
    def set_action_dict(self, action_object: "ActionCore | None" = None, identifier: InputIdentifier | None = None, state: int | None = None, index: int | None = None, action_dict: _Dict[str, Any] | None = None) -> None:
        # Arg validation
        if action_object is None:
            if None in (identifier, state, index):
                raise ValueError("Please pass an identifier, state and index or an action object")
            
        if action_object is None:
            found = self.get_action(identifier, state, index)
            action_object = found if isinstance(found, ActionCore) else None

        if action_object is None:
            raise ValueError("Could not find action object")
        
        # Do not name the loop variable action_dict. It shadows the parameter
        # and makes the assignment below a self-assignment.
        ident = action_object.input_ident
        for state_key in ident.get_states(self):
            try:
                state_int = int(state_key)
            except (TypeError, ValueError):
                # Skip non-integer state keys so settings and event-assignment writes reach valid states.
                continue
            actions = ident.get_actions(self, state_key)
            for i, _existing_dict in enumerate(actions):
                if self.get_action(ident, state_int, i) is action_object:
                    actions[i] = action_dict
                    break

        self.save()
    
    def get_action_settings(self, action_object: "ActionCore | None" = None, identifier: InputIdentifier | None = None, state: int | None = None, index: int | None = None) -> Any:
        action_dict = self.get_action_dict(action_object, identifier, state, index)
        return action_dict.get("settings", {})


    def set_action_settings(self, action_object: "ActionCore | None" = None, identifier: InputIdentifier | None = None, state: int | None = None, index: int | None = None, settings: _Dict[str, Any] | None = None) -> None:
        action_dict = self.get_action_dict(action_object, identifier, state, index)
        action_dict["settings"] = settings
        self.set_action_dict(action_object, identifier, state, index, action_dict)

    def get_action_event_assignments(self, action_object: "ActionCore | None" = None, identifier: InputIdentifier | None = None, state: int | None = None, index: int | None = None) -> _Dict[str, "str | None"]:
        action_dict = self.get_action_dict(action_object, identifier, state, index)

        # backwards compat
        assignments: _Dict[str, "str | None"] = action_dict.get("event-assignments", {})
        for key, value in assignments.items():
            if value == "None":
                assignments[key] = None

        return assignments
    
    
    def set_action_event_assigment(self, event_assigner: EventAssigner | None, input_event: InputEvent | None, action_object: "ActionCore | None" = None, identifier: InputIdentifier | None = None, state: int | None = None, index: int | None = None) -> None:
        action_dict = self.get_action_dict(action_object, identifier, state, index)
        action_dict.setdefault("event-assignments", {})
        action_dict["event-assignments"][str(input_event)] = event_assigner.id if event_assigner else None
        self.set_action_dict(action_object, identifier, state, index, action_dict)


    def has_key_an_image_controlling_action(self, identifier: InputIdentifier, state: int) -> bool:
        input_type = identifier.input_type
        json_identifier = identifier.json_identifier
        if input_type not in self.action_objects or json_identifier not in self.action_objects[input_type]:
            return False
        for action in self.action_objects[input_type][json_identifier][state].values():
            # CONTROLS_KEY_IMAGE is optional and absent from the base class.
            # The default covers flagless actions, placeholders, and empty slots.
            if getattr(action, "CONTROLS_KEY_IMAGE", False):
                return True
        return False

    @log.catch
    def initialize_actions(self) -> None:
        for action in self.get_all_actions():
            # Claim readiness atomically so concurrent page loads cannot submit on_ready twice.
            # Hold the lock only for the claim.
            with self._ready_claim_lock:
                if action.on_ready_called:
                    continue
                action.on_ready_called = True
            action.load_event_overrides()
            action.load_initial_generative_ui()
            # A plugin callback can block without end, so it runs on the deck's
            # action pool and never on the caller's thread, which is often GTK.
            self._submit_ready_callbacks(action)

    def _submit_ready_callbacks(self, action: ActionCore) -> None:
        executor = getattr(self.deck_controller, "action_executor", None)
        if executor is None:
            # The deck is in teardown, so drop the call.
            return
        try:
            # A shut-down pool drops the call, as does a missing pool.
            # Only close cancels queued callbacks, when the page dies with the deck.
            executor.submit(self._run_ready_callbacks, action)
        except RuntimeError as error:
            # A live pool can refuse when the process has no available threads.
            # The action then stays unready until the page loads again.
            log.warning(
                f"The action pool refused the ready callback for "
                f"{getattr(action, 'action_id', action)}: {error!r}. That "
                f"action stays unready until the page loads again.")

    @log.catch
    def _run_ready_callbacks(self, action: ActionCore) -> None:
        try:
            action.on_ready()
        except Exception:
            log.opt(exception=True).error(
                f"on_ready failed for action {getattr(action, 'action_id', action)}"
            )
        finally:
            # Always open the tick and update gates and redraw after on_ready.
            # Keep on_update in finally so even an uncaught BaseException cannot skip it.
            action.on_ready_finished = True
            action.on_update()

    def clear_action_objects(self) -> None:
        for input_type in self.action_objects:
            for input_identifier in self.action_objects[input_type]:
                for state in self.action_objects[input_type][input_identifier]:
                    state_dict = self.action_objects[input_type][input_identifier][state]
                    for action in list(state_dict.values()):
                        # Notify before detach because plugin cleanup can still need action.page.
                        # Teardown always calls clean_up and accepts placeholders.
                        ActionCore.teardown(action)
                        if isinstance(action, ActionCore):
                            # The action is torn down; page describes the
                            # live phase, so the detach steps outside it.
                            action.page = None
                    state_dict.clear()
            self.action_objects[input_type] = {}

    def get_pages_with_same_json(self, get_self: bool = False) -> "list[Page]":
        pages: list[Page]= []
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            # Snapshot active_page because connect, disconnect, or close can clear it concurrently.
            # Re-reading after a non-None check can dereference None.
            active_page = controller.active_page
            if active_page is None:
                continue
            if active_page == self and not get_self:
                continue
            if active_page.json_path == self.json_path:
                pages.append(active_page)
        return pages
    
    def reload_similar_pages(self, identifier: InputIdentifier | None = None, reload_self: bool = False,
                             load_brightness: bool = True, load_screensaver: bool = True, load_background: bool = True, load_inputs: bool = True,
                             load_dials: bool = True, load_touchscreens: bool = True) -> None:

        # Save direct dict edits before reload, including pasted keys and dials.
        # The reload's read barrier then writes and re-reads the edit instead of erasing it.
        self.save()
        for page in self.get_pages_with_same_json(get_self=reload_self):
            page.load(load_from_file=True)
            # page.deck_controller.update_input(identifier)
            if identifier is not None:
                page.deck_controller.load_input_from_identifier(identifier, page)
            else:
                # Each controller gets its own Page object. A self here loads
                # this controller's Page onto the other decks.
                page.deck_controller.load_page(page)

    def get_action_comment(self, index: int, state: int, identifier: InputIdentifier) -> str | None:
        try:
            return cast(str | None, self.dict[identifier.input_type][identifier.json_identifier]["states"][str(state)]["actions"][index].get("comment"))
        except KeyError:
            return ""

    def set_action_comment(self, index: int, comment: str, state: int, identifier: InputIdentifier) -> None:
        if identifier.json_identifier in self.action_objects[identifier.input_type] and index in self.action_objects[identifier.input_type][identifier.json_identifier][state]:
            self.dict[identifier.input_type][identifier.json_identifier]["states"][str(state)]["actions"][index]["comment"] = comment
            self.save()

    def fix_action_objects_order(self, identifier: InputIdentifier) -> None:
        """
        #TODO: Switch to list instead of dict to avoid this
        """
        if identifier.json_identifier not in self.action_objects.get(identifier.input_type, {}):
            return
        
        actions = list(self.action_objects[identifier.input_type][identifier.json_identifier].values())

        self.action_objects[identifier.input_type][identifier.json_identifier] = {}
        for i, action in enumerate(actions):
            self.action_objects[identifier.input_type][identifier.json_identifier][i] = action
    
    # Configuration
    def _get_dict_value(self, keys: list[str]) -> Any:
        # The walk can leave the page mapping and reach any leaf type.
        value: Any = self.dict
        for i, key in enumerate(keys):
            fallback: dict[str, Any] | None = {}
            if i == len(keys) - 1:
                fallback = None

            try:
                value = value.get(key, fallback)
            except AttributeError:
                # A path shorter than keys ended on a non-dict.
                return
        return value
    
    def _set_dict_value(self, keys: list[str], value: Any) -> None:
        d = self.dict
        for i, key in enumerate(keys):
            if i == len(keys) - 1:
                d[key] = value
            else:
                d = d.setdefault(key, {})

        self.save()

    def _del_dict_value(self, keys: list[str]) -> None:
        """Remove one override leaf without pruning parents so future defaults can apply.
        Missing leaves and non-dict branches are no-ops; revert can leave empty parents."""
        # Any, because the walk descends out of the page's mapping into
        # whatever the branch holds, exactly as _get_dict_value does.
        d: Any = self.dict
        for key in keys[:-1]:
            branch = d.get(key)
            if not isinstance(branch, dict):
                return
            d = branch
        if keys[-1] not in d:
            return
        del d[keys[-1]]

        self.save()

    def update_key_image(self, coords: str | tuple[int, int], state: int) -> None:
        #TODO: Move to DeckController
        #TODO: Make input specific
        coords = self.get_tuple_coords(coords)
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            # active_page is None while a controller connects, disconnects or
            # closes. Skip it instead of raising AttributeError.
            active_page = controller.active_page
            if active_page is None or active_page.json_path != self.json_path:
                continue
            key_index = controller.coords_to_index(coords)
            if key_index > len(controller.inputs[Input.Key]) - 1:
                continue
            key = controller.inputs[Input.Key][key_index]
            if key.state == state:
                key.update()

    def update_input(self, identifier: InputIdentifier, state: int, wake: bool = True) -> None:
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            if wake:
                if controller.screen_saver.showing:
                    controller.screen_saver.hide()

            # active_page is None while a controller connects, disconnects or
            # closes. Skip it instead of raising AttributeError.
            active_page = controller.active_page
            if active_page is None or active_page.json_path != self.json_path:
                continue
            c_input = controller.get_input(identifier)
            if c_input is None:
                continue
            if c_input.state != state:
                continue
            c_input.update()

    def get_controller_inputs(self, identifier: InputIdentifier) -> list["ControllerInput[Any]"]:
        inputs: list["ControllerInput[Any]"] = []

        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            # Include only controllers that show this page to prevent cross-page writes.
            # This matches update_input's repaint scope.
            active_page = controller.active_page
            if active_page is None or active_page.json_path != self.json_path:
                continue
            for c_input in controller.get_inputs(identifier):
                if c_input.identifier == identifier:
                    inputs.append(c_input)

        return inputs

    # ControllerInput declares base ControllerInputState values, whose members suffice here.
    def get_controller_input_states(self, identifier: InputIdentifier, state: int) -> list["ControllerInputState"]:
        matching_states: list["ControllerInputState"] = []

        for controller_input in self.get_controller_inputs(identifier):
            for input_state in controller_input.states.values():
                if input_state.state == state:
                    matching_states.append(input_state)

        return matching_states

    def get_page_coords(self, coords: str | tuple[int, int]) -> str:
        if isinstance(coords, tuple):
            return f"{coords[0]}x{coords[1]}"
        return coords
    
    def get_tuple_coords(self, coords: str | tuple[int, int]) -> tuple[int, int]:
        if isinstance(coords, str):
            x, y = coords.split("x")
            return int(x), int(y)
        return coords
    
    # Get/set methods

    def get_label_manager(self, identifier: InputIdentifier, state: int) -> "LabelManager | None":
        c_input = self.deck_controller.get_input(identifier)
        if c_input is None:
            return None
        input_state = c_input.states.get(state)
        if input_state is None:
            return None

        return cast("LabelManager | None", input_state.label_manager)
        

    def get_label_text(self, identifier: InputIdentifier, state: int, label_position: str) -> str | None:
        return cast(str | None, self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "text"]))

    def set_label_text(self, identifier: InputIdentifier, state: int, label_position: str, text: str | None, update: bool = True) -> None:
        for input_state in self.get_controller_input_states(identifier, state):
            input_state.label_manager.page_labels[label_position].text = text
            # In-place changes bypass set_page_label, so invalidate scroll caches here.
            # Otherwise shortened labels keep scrolling and lengthened labels do not start.
            input_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "text"], text)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].text = text
            label_manager.invalidate_scroll_caches()

        if update:
            self.update_input(identifier, state)

    def get_label_font_family(self, identifier: InputIdentifier, state: int, label_position: str) -> str | None:
        return cast(str | None, self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-family"]))

    def set_label_font_family(self, identifier: InputIdentifier, state: int, label_position: str, font_family: str | None, update: bool = True) -> None:
        for input_state in self.get_controller_input_states(identifier, state):
            input_state.label_manager.page_labels[label_position].font_name = font_family
            input_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-family"], font_family)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].font_name = font_family
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def get_label_font_size(self, identifier: InputIdentifier, state: int, label_position: str) -> int | None:
        return cast(int | None, self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-size"]))
    
    def get_label_font_style(self, identifier: InputIdentifier, state: int, label_position: str) -> int | None:
        return cast(int | None, self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-style"]))
    
    def get_label_font_weight(self, identifier: InputIdentifier, state: int, label_position: str) -> int | None:
        return cast(int | None, self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-weight"]))

    def set_label_font_size(self, identifier: InputIdentifier, state: int, label_position: str, font_size: float | None, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].font_size = font_size
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-size"], font_size)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].font_size = font_size
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_font_weight(self, identifier: InputIdentifier, state: int, label_position: str, font_weight: int, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].font_weight = font_weight
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-weight"], font_weight)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].font_weight = font_weight
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_font_color(self, identifier: InputIdentifier, state: int, label_position: str, font_color: list[int] | None, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].color = font_color
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "color"], font_color)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].color = font_color
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    # outline_width is a scalar stroke width; None restores the font default.
    def set_label_outline_width(self, identifier: InputIdentifier, state: int, label_position: str, outline_width: int | None, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].outline_width = outline_width
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "outline_width"], outline_width)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].outline_width = outline_width
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_outline_color(self, identifier: InputIdentifier, state: int, label_position: str, outline_color: list[int] | None, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].outline_color = outline_color
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "outline_color"], outline_color)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].outline_color = outline_color
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_font_style(self, identifier: InputIdentifier, state: int, label_position: str, font_style: str, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].style = font_style
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "style"], font_style)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].style = font_style
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_alignment(self, identifier: InputIdentifier, state: int, label_position: str, alignment: str | None, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].alignment = alignment
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "alignment"], alignment)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].alignment = alignment
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def get_media_size(self, identifier: InputIdentifier, state: int) -> float | None:
        return cast(float | None, self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "size"]))

    def set_media_size(self, identifier: InputIdentifier, state: int, size: float | None, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.layout_manager.page_layout.size = size

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "size"], size)

        if update:
            self.update_input(identifier, state)

    def get_media_valign(self, identifier: InputIdentifier, state: int) -> float | None:
        return cast(float | None, self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "valign"]))

    def set_media_valign(self, identifier: InputIdentifier, state: int, valign: float, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.layout_manager.page_layout.valign = valign

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "valign"], valign)

        if update:
            self.update_input(identifier, state)

    def get_media_halign(self, identifier: InputIdentifier, state: int) -> float | None:
        return cast(float | None, self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "halign"]))

    def set_media_halign(self, identifier: InputIdentifier, state: int, halign: float, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.layout_manager.page_layout.halign = halign

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "halign"], halign)

        if update:
            self.update_input(identifier, state)

    def get_media_path(self, identifier: InputIdentifier, state: int) -> str | None:
        return cast(str | None, self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "path"]))

    def set_media_path(self, identifier: InputIdentifier, state: int, path: "str | None", update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.layout_manager.page_layout.path = path  # ty: ignore[unresolved-attribute]  # ImageLayout (Subclasses/KeyLayout.py) declares no `path` field; nothing reads this write

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "path"], path)

        if update:
            self.update_input(identifier, state)

    def _media_fps_keys(self, identifier: InputIdentifier, state: int) -> list[str]:
        """The dict path of a state's media frame-rate cap. Three callers walk
        it, and a path spelled once cannot disagree with itself."""
        return [identifier.input_type, identifier.json_identifier, "states", str(state), "media", "fps"]

    def get_media_fps(self, identifier: InputIdentifier, state: int) -> int:
        """The frame-rate cap this state's media renders under. A page that
        carries no cap reports the loop ceiling, which caps nothing."""
        value = self._get_dict_value(self._media_fps_keys(identifier, state))
        return DEFAULT_MEDIA_FPS if value is None else int(value)

    def has_media_fps(self, identifier: InputIdentifier, state: int) -> bool:
        """Return whether this state's media has an explicit frame-rate cap.
        The sidebar shows its revert control only when a cap exists."""
        return self._get_dict_value(self._media_fps_keys(identifier, state)) is not None

    def get_media_native_fps(self, identifier: InputIdentifier, state: int) -> float | None:
        """Return the video container rate or GIF frame-count-over-delay rate.
        Return None when no matching media is loaded or no usable rate exists."""
        for input_state in self.get_controller_input_states(identifier, state):
            video = getattr(input_state, "key_video", None) or getattr(input_state, "video", None)
            native = getattr(video, "native_fps", None)
            if not callable(native):
                continue
            rate = native()
            if rate:
                return float(rate)
        return None

    def set_media_fps(self, identifier: InputIdentifier, state: int, fps: int | None, update: bool = True) -> None:
        """Set this state's media cap, or remove it when fps is None.
        Apply the change to playing video and GIF media without a page reload."""
        # A cleared cap must reach playing media as the value a fresh page
        # load would give it, or the media keeps the old cap until a reload.
        applied = DEFAULT_MEDIA_FPS if fps is None else fps
        # get_controller_inputs excludes controllers that show a different page.
        for input_state in self.get_controller_input_states(identifier, state):
            video = getattr(input_state, "key_video", None) or getattr(input_state, "video", None)
            if video is not None and hasattr(video, "set_playback"):
                video.set_playback(fps=applied, loop=video.loop)

        keys = self._media_fps_keys(identifier, state)
        if fps is None:
            self._del_dict_value(keys)
        else:
            self._set_dict_value(keys, fps)

        if update:
            self.update_input(identifier, state)

    def get_background_color(self, identifier: InputIdentifier, state: int) -> list[int]:
        return cast(list[int], self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "color"]))

    def set_background_color(self, identifier: InputIdentifier, state: int, color: list[int] | None, update: bool = True, update_ui: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.background_manager.set_page_color(color, update=update, update_ui=update_ui)

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "color"], color)

    def get_background_image(self, identifier: InputIdentifier, state: int) -> str | None:
        return cast(str | None, self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "image"]))

    def set_background_image(self, identifier: InputIdentifier, state: int, path: str | None, update: bool = True) -> None:
        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "image"], path)
        if update:
            self.update_input(identifier, state)

    def get_background_loop(self, identifier: InputIdentifier, state: int) -> bool:
        value = self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "loop"])
        return True if value is None else bool(value)

    def set_background_loop(self, identifier: InputIdentifier, state: int, loop: bool, update: bool = True) -> None:
        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "loop"], loop)
        if update:
            self.update_input(identifier, state)

    def _background_fps_keys(self, identifier: InputIdentifier, state: int) -> list[str]:
        """The dict path of a state's background frame-rate cap."""
        return [identifier.input_type, identifier.json_identifier, "states", str(state), "background", "fps"]

    def get_background_fps(self, identifier: InputIdentifier, state: int) -> int:
        """Return this state's background-video frame-rate cap.
        A missing cap returns the media-loop ceiling, which imposes no lower limit."""
        value = self._get_dict_value(self._background_fps_keys(identifier, state))
        return DEFAULT_MEDIA_FPS if value is None else int(value)

    def has_background_fps(self, identifier: InputIdentifier, state: int) -> bool:
        """Does the page carry an explicit cap for this state's background?"""
        return self._get_dict_value(self._background_fps_keys(identifier, state)) is not None

    def set_background_fps(self, identifier: InputIdentifier, state: int, fps: int | None, update: bool = True) -> None:
        """Set or clear the frame-rate cap for this state's background video.
        None removes the key, as set_media_fps does."""
        keys = self._background_fps_keys(identifier, state)
        if fps is None:
            self._del_dict_value(keys)
        else:
            self._set_dict_value(keys, fps)
        if update:
            self.update_input(identifier, state)


class NoActionHolderFound:
    def __init__(self, id: str, state: int, identifier: InputIdentifier | None = None):
        self.id = id
        self.action_id = id
        self.type = type
        self.identifier = identifier
        self.state = state


class ActionOutdated:
    def __init__(self, id: str, state: int, identifier: InputIdentifier | None = None):
        self.id = id
        self.action_id = id
        self.type = type
        self.identifier = identifier
        self.state = state
