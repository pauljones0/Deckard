"""Confirm untrusted session-bus actions before they download or run content.
Validate targets, serialize work through completion, and quiet repeated refusals."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from gi.repository import Gio, GLib

from loguru import logger as log

INSTALL_ACTION = "install-plugin"
UPDATE_ACTION = "update-all-assets"

# Arm quiet mode after two refusals, which still permits one immediate user retry.
REFUSALS_BEFORE_QUIET = 2
QUIET_PERIOD_S = 60.0


def is_store_id(value: str) -> bool:
    """Check an action target with the installer id gate before showing it."""
    # Defer the backend import to avoid loading the store layer during app construction.
    from src.backend.Store.StoreBackend import StoreBackend

    return StoreBackend.is_safe_asset_id(value)


class ConfirmedActionGate:
    """Serialize one exported action behind a blocking confirmation and worker.
    Both callbacks run on the gate thread, so UI confirmation must marshal to the main loop."""

    def __init__(self, action_name: str,
                 worker: Callable[[str], None],
                 confirm: Callable[[str], bool], *,
                 target_type: "str | None" = None,
                 validate: "Callable[[str], bool] | None" = None) -> None:
        self._action_name = action_name
        self._worker = worker
        self._confirm = confirm
        self._target_type = target_type
        self._validate = validate
        self._lock = threading.Lock()
        self._pending: str | None = None
        self._refusals = 0
        self._quiet_until = 0.0

    def add_to(self, application: Gio.Application) -> Gio.SimpleAction:
        """Create, connect, and export the action."""
        parameter = GLib.VariantType(self._target_type) if self._target_type else None
        action = Gio.SimpleAction.new(self._action_name, parameter)
        action.connect("activate", self.on_activate)
        application.add_action(action)
        return action

    def on_activate(self, action: "Gio.SimpleAction | None",
                    target: "GLib.Variant | None") -> None:
        self.request(target.unpack() if target is not None else None)

    def request(self, subject: object) -> bool:
        """Request confirmation for an untrusted activation.
        Return whether confirmation started, not whether the worker ran."""
        if self._target_type is None:
            subject = self._action_name
        if not isinstance(subject, str) or not subject.strip():
            log.warning(f"Ignoring a {self._action_name} activation with the target {subject!r}")
            return False
        subject = subject.strip()
        if self._validate is not None and not self._validate(subject):
            log.warning(f"Ignoring a {self._action_name} activation: the target "
                        f"{subject[:64]!r} is not a store id")
            return False

        now = time.monotonic()
        with self._lock:
            if self._pending is not None:
                log.warning(
                    f"Ignoring the {self._action_name} activation for {subject}: "
                    f"the request for {self._pending} has not finished")
                return False
            if self._quiet_until:
                if now < self._quiet_until:
                    log.warning(
                        f"Ignoring the {self._action_name} activation for {subject}: "
                        f"{self._refusals} refusals in a row, so it stays quiet for "
                        f"{self._quiet_until - now:.0f}s more")
                    return False
                self._quiet_until = 0.0
                self._refusals = 0
            self._pending = subject
        # Let app quit proceed while confirmation waits for a user response.
        threading.Thread(target=self._confirm_then_run, args=(subject,),
                         name=f"{self._action_name}_request", daemon=True).start()
        return True

    def _confirm_then_run(self, subject: str) -> None:
        try:
            try:
                agreed = self._confirm(subject)
            except Exception as e:
                # Treat confirmation failure as refusal so repeated failures also quiet the action.
                log.error(f"The confirmation for {self._action_name} ({subject}) failed: {e!r}")
                agreed = False
            if not agreed:
                self._note_refusal()
                log.info(f"Not running {self._action_name} for {subject}: it was not confirmed")
                return
            with self._lock:
                self._refusals = 0
            self._worker(subject)
        except Exception as e:
            log.error(f"The confirmed {self._action_name} for {subject} failed: {e!r}")
        finally:
            # Hold the slot through worker completion to prevent overlapping jobs.
            with self._lock:
                self._pending = None

    def _note_refusal(self) -> None:
        with self._lock:
            self._refusals += 1
            if self._refusals >= REFUSALS_BEFORE_QUIET:
                self._quiet_until = time.monotonic() + QUIET_PERIOD_S
