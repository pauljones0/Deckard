
from src.backend.DeckManagement.InputIdentifier import InputIdentifier
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport


class ActionHolderGroup:
    def __init__(self, group_name: str, action_holders: list[ActionHolder]):
        self._group_name: str = group_name
        self._action_holders: set[ActionHolder] = set(action_holders)

    def add_action_holder(self, action_holder: ActionHolder) -> None:
            self._action_holders.add(action_holder)

    def add_action_holders(self, action_holders: list[ActionHolder]) -> None:
        self._action_holders.update(action_holders)

    def remove_action_holder(self, action_holder: ActionHolder) -> None:
        self._action_holders.remove(action_holder)

    def remove_action_holders(self, action_holders: list[ActionHolder]) -> None:
        self._action_holders.difference_update(action_holders)

    def get_group_name(self) -> str:
        return self._group_name

    def get_action_holders(self) -> "set[ActionHolder]":
        return self._action_holders

    def get_min_input_compatibility(self, action_input_support: InputIdentifier) -> ActionInputSupport:
        for action_holder in self._action_holders:
            if action_holder.get_input_compatibility(action_input_support) == ActionInputSupport.UNSUPPORTED:
                return ActionInputSupport.UNSUPPORTED
        
        for action_holder in self._action_holders:
            if action_holder.get_input_compatibility(action_input_support) == ActionInputSupport.UNTESTED:
                return ActionInputSupport.UNTESTED
            
        return ActionInputSupport.SUPPORTED
            
    
    def get_action_holders_with_min_action_input_support(self, identifier: InputIdentifier, action_input_support: ActionInputSupport) -> set[ActionHolder]:
        # Compatibility keys by input type, so an action-id string would make
        # every holder read as UNSUPPORTED.
        action_holders: set[ActionHolder] = set()
        for action_holder in self._action_holders:
            if action_holder.get_input_compatibility(identifier) >= action_input_support:
                action_holders.add(action_holder)

        return action_holders
