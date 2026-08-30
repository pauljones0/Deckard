from src.backend.DeckManagement.InputIdentifier import Input, InputEvent
from src.backend.PluginManager.EventAssigner import EventAssigner


class EventManager:
    def __init__(self) -> None:
        self._event_assigners: list[EventAssigner] = []

        self._overrides: dict[str, str | None] = {} # {"key_down": "event_1"}

    def set_overrides(self, overrides: dict[str, str | None]) -> None:
        self._overrides = overrides

    def add_event_assigner(self, event_assigner: EventAssigner) -> None:
        if self.get_event_assigner_by_id(event_assigner.id):
            raise ValueError(f"Event assigner with id '{event_assigner.id}' already exists on this action")
        self._event_assigners.append(event_assigner)

    def clear_event_assigners(self) -> None:
        self._event_assigners.clear()

    def get_all_event_assigners(self) -> list[EventAssigner]:
        return self._event_assigners

    def get_event_assigner_by_id(self, id: str | None) -> EventAssigner | None:
        # A None id matches no assigner, which is what a cleared
        # selection in the event row means.
        for event_assigner in self._event_assigners:
            if event_assigner.id == id:
                return event_assigner
        return None

    def get_event_map(self, ignore_overrides: bool = False) -> dict[InputEvent, EventAssigner | None]:
        # Every known event is a key. The value is None for an event no
        # assigner claims, and for an override that maps an event to nothing.
        event_map: dict[InputEvent, EventAssigner | None] = {}

        all_events = Input.AllEvents()
        for event in all_events:
            event_map[event] = None

        for event_assigner in self._event_assigners:
            for default_event in event_assigner.default_events:
                if default_event is None:
                    # An assigner with no declared event defaults to [None].
                    # Exclude it because map lookups use real InputEvent values.
                    continue
                event_map[default_event] = event_assigner

        if not ignore_overrides:
            for input_event_str, event_id in self._overrides.items():
                input_event = Input.EventFromStringName(input_event_str)
                if input_event is None:
                    # Drop unresolved override keys, including the stored literal "None".
                    # They match no lookup and add an invalid configurator row.
                    continue
                override_assigner = self.get_event_assigner_by_id(event_id) if event_id else None
                event_map[input_event] = override_assigner

        return event_map

    def get_event_assigner_for_event(self, event: InputEvent) -> EventAssigner | None:
        return self.get_event_map().get(event)
