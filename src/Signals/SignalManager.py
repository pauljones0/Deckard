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

import threading
from collections.abc import Callable
from typing import Any, Literal, overload

from loguru import logger as log

from src.Signals.Signals import Signal
from src.Signals.weak_callbacks import CallbackRegistry, describe_callback

from gi.repository import GLib


def _invoke_signal_callback(callback: Callable[..., Any], args: tuple[Any, ...],
                            kwargs: dict[str, Any]) -> bool:
    """Invoke a signal callback with keywords and stop the GLib idle source.
    Exceptions reach the main-loop hooks, and GLib removes the source."""
    callback(*args, **kwargs)
    return False


def _safe_describe(callback: Callable[..., Any]) -> str:
    """Name a callback without remote attribute failures after shutdown.
    The fallbacks do not access callback attributes."""
    try:
        return describe_callback(callback)
    except BaseException:
        try:
            return object.__repr__(callback)
        except BaseException:
            return "<unnameable callback>"


class SignalManager:
    def __init__(self) -> None:
        # Registries hold methods weakly and lock mutation and snapshots.
        # Their iterable interface preserves direct connected_signals readers.
        self.connected_signals: dict[type[Signal], CallbackRegistry] = {}
        # Guards registry creation; each registry locks its own contents.
        self._registries_lock = threading.Lock()

    # create=True returns a registry and creates one on miss.
    # Only create=False can return None.
    @overload
    def _get_registry(self, signal: type[Signal], create: Literal[True]) -> CallbackRegistry: ...
    @overload
    def _get_registry(self, signal: type[Signal], create: bool) -> CallbackRegistry | None: ...

    def _get_registry(self, signal: type[Signal], create: bool) -> CallbackRegistry | None:
        registry = self.connected_signals.get(signal)
        if registry is not None or not create:
            return registry
        with self._registries_lock:
            registry = self.connected_signals.get(signal)
            if registry is None:
                registry = CallbackRegistry()
                self.connected_signals[signal] = registry
            return registry

    def connect_signal(self, signal: type[Signal], callback: Callable[..., Any]) -> None:
        if not issubclass(signal, Signal):
            raise TypeError("signal_name must be of type Signal")

        if not callable(callback):
            raise TypeError("callback must be callable")

        self._get_registry(signal, create=True).add(callback)

    def disconnect_signal(self, signal: type[Signal], callback: Callable[..., Any]) -> None:
        if not issubclass(signal, Signal):
            raise TypeError("signal_name must be of type Signal")

        registry = self._get_registry(signal, create=False)
        if registry is not None:
            registry.remove(callback)

    def trigger_signal(self, signal: type[Signal], *args: Any, **kwargs: Any) -> None:
        """Queue each observer on the GTK main loop from any thread.
        Return before observers run; use trigger_signal_sync to wait."""
        if not issubclass(signal, Signal):
            raise TypeError("signal must be of type Signal")

        registry = self._get_registry(signal, create=False)
        if registry is None:
            return

        # The locked snapshot permits concurrent connection changes.
        for callback in registry.snapshot():
            GLib.idle_add(_invoke_signal_callback, callback, args, kwargs)

    def trigger_signal_sync(self, signal: type[Signal], *args: Any, **kwargs: Any) -> None:
        """Run all observers on the calling thread before return.
        This does not marshal GTK work; AppQuit uses it before os._exit."""
        if not issubclass(signal, Signal):
            raise TypeError("signal must be of type Signal")

        registry = self._get_registry(signal, create=False)
        if registry is None:
            return

        for callback in registry.snapshot():
            # Continue after failures, including plugin sys.exit calls.
            # AppQuit follows this fan-out with os._exit.
            try:
                callback(*args, **kwargs)
            except BaseException:
                log.opt(exception=True).warning(
                    f"{signal.__name__} handler {_safe_describe(callback)} "
                    f"failed; continuing with the remaining handlers"
                )
