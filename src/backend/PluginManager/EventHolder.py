import itertools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loguru import logger as log

from src.backend.PluginManager import event_dispatch
from src.Signals.weak_callbacks import CallbackRegistry

if TYPE_CHECKING:
    from src.backend.PluginManager.PluginBase import PluginBase

# Gives each holder a unique backend-hold key.
# count() is atomic across threads through its C implementation.
_hold_serial = itertools.count()

class EventHolder:
    """Holds the event callbacks of one event id."""
    def __init__(self, plugin_base: "PluginBase | None",
                 event_id: str | None = None,
                 event_id_suffix: str | None = None):
        if event_id in ["", None] and event_id_suffix in ["", None]:
            raise ValueError("Please specify a signal id")

        # A holder without a plugin is a valid event source: only the suffix
        # form needs a plugin, to build the full event id from its id.
        self.plugin_base = plugin_base
        if event_id:
            self.event_id = event_id
        elif plugin_base is None:
            raise ValueError("An event id suffix needs a plugin to build the event id")
        else:
            self.event_id = f"{plugin_base.get_plugin_id()}::{event_id_suffix}"
        # Weak bound-method observers disappear with their action or plugin.
        # This limits leaks when teardown omits remove_listener().
        self.observers = CallbackRegistry()
        # Include a nonrepeating holder serial because holders can share an event id.
        # Unlike id(), the serial cannot repeat after collection.
        self._hold_key = f"{next(_hold_serial)}::{self.event_id}"
        # Each holder owns an ordered lane that isolates its blocking observers.
        # The lane dies with the holder; holders can share an event id.
        self._lane = event_dispatch.Lane(label=self.event_id)

    def add_listener(self, callback: Callable[..., Any]) -> None:
        if not self.observers.add(callback):
            # The eager default evaluates repr before getattr checks __name__.
            # A raising repr escapes this duplicate-listener warning.
            name = getattr(callback, "__name__", repr(callback))
            log.warning(f"Callback {name} is already subscribed to: {self.event_id}")

    def remove_listener(self, callback: Callable[..., Any]) -> None:
        self.observers.remove(callback)

    def trigger_event(self, *args: Any, **kwargs: Any) -> None:
        """Queue observers in registration order and return; lanes run later, isolated, unordered.
        Hold unobserved events until backend registration; dispatch observed events immediately."""
        # Prepend event_id as the observers' first positional argument.
        payload = (self.event_id, *args)

        def dispatch(from_hold: bool) -> None:
            # The observers are read here and not at the trigger, so an action
            # that subscribes while the hold keeps this event still gets it.
            observers = self.observers.snapshot()
            if from_hold and not observers:
                # Backend registration can precede listener on_ready.
                # Report a held event that still has no observer.
                log.info(f"Event {self.event_id} was held while the backend connected and "
                         f"still reached no observer")
            try:
                self._lane.dispatch(observers, payload, kwargs, label=self.event_id)
            except event_dispatch.DispatchShutdown:
                # Plugin event sources can run after dispatch shutdown until os._exit.
                # Drop only DispatchShutdown; other RuntimeError instances propagate.
                log.debug(f"Event {self.event_id} triggered after dispatch shutdown; dropped")

        plugin_base = self.plugin_base
        hold = plugin_base.backend_event_hold if plugin_base is not None else None
        if hold is not None:
            if not self.observers and hold.submit(self._hold_key, lambda: dispatch(True)):
                return
            # Drop the older held value before this event reaches observers.
            hold.drop(self._hold_key)
        dispatch(False)
