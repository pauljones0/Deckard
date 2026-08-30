"""
Author: G4PLS
Year: 2024
"""

from collections.abc import Callable
from typing import Any

from loguru import logger as log

from src.backend.PluginManager import event_dispatch
from src.Signals.weak_callbacks import CallbackRegistry

class Observer:
    def __init__(self, label: str | None = None):
        # Weak bound-method storage prevents subscribers that omit unsubscribe()
        # from retaining themselves through this registry.
        self.observers = CallbackRegistry()
        # Subscribers run one at a time in subscription order; a block stalls
        # only this asset stream, and the watchdog names the lane by label.
        self._lane = event_dispatch.Lane(label=label)

    def subscribe(self, observer: Callable[..., Any]) -> None:
        self.observers.add(observer)

    def unsubscribe(self, observer: Callable[..., Any]) -> None:
        self.observers.remove(observer)

    def notify(self, *args: Any, **kwargs: Any) -> None:
        """Queue a subscriber snapshot on this notifier's lane and return.
        Callbacks run later, one at a time in subscription order; cross-lane order is undefined.
        """
        try:
            self._lane.dispatch(self.observers.snapshot(), args, kwargs)
        except event_dispatch.DispatchShutdown:
            # Drop notifications that race dispatcher shutdown because callers
            # do not handle teardown failures; other dispatch errors propagate.
            log.debug("Asset notification after dispatch shutdown; dropped")
