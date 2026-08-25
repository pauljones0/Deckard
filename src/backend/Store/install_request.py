"""The gate in front of the exported actions that spend the user's computer.

GApplication publishes the whole application action group on the session
bus, at the object path the application id names, so an action added to
the application is not a private control. Any peer of the session bus can
call org.gtk.Actions.Activate, name the action and pass its target, and
the activation that arrives carries nothing that says who sent it. The
desktop shell relaying the button of a notification is such a peer too,
so no property of the message separates the two.

Two exported actions reach code that downloads and runs things: the one
that installs a named plugin, and the one that updates every installed
asset. The second one is the wider of the two, because an update
reinstalls each out-of-date asset, and a plugin reinstall may run that
plugin's install step.

The app's own paths do not use either action. The store window, the
onboarding page and the missing-action row each hold a store backend and
call it directly. An in-process token would attest nothing here: the only
activations these actions still serve are notification buttons, and those
arrive from outside this process like every other activation.

What is left is to make an activation say out loud what it wants.
ConfirmedActionGate answers every activation with a confirmation that
names the subject, and starts the work only once a person agrees. The
confirmation runs before the store catalog is read, so an activation
nobody wanted costs no network request either. One request is handled at
a time and the slot is held until the work ends, so a peer cannot stack
dialogs or run two jobs over each other. A target that is not a store id
is dropped before it can reach a dialog label, and a run of refusals
makes the action quiet for a while, so a looping peer cannot keep raising
a modal for as long as the user keeps cancelling.

Flatpak narrows who can reach these actions, and the grant is not this
app's to make. A sandboxed peer reaches this app's bus name only if that
peer's own manifest carries a talk-name permission for it, which is a
decision made when the peer is packaged and installed. Nothing in this
app's manifest widens or narrows that. A program running unsandboxed on
the session bus, which is every ordinary desktop program, needs no grant
at all, so the confirmation and not the sandbox is what stands in front
of the work.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from gi.repository import Gio, GLib

from loguru import logger as log

# The exported action names this module gates.
INSTALL_ACTION = "install-plugin"
UPDATE_ACTION = "update-all-assets"

# How many refusals in a row make an action quiet, and for how long. One
# mistaken Cancel must not lock the user out of their own retry, so the
# quiet period arms on the second refusal and not the first. It bounds a
# looping peer to two dialogs per period.
REFUSALS_BEFORE_QUIET = 2
QUIET_PERIOD_S = 60.0


def is_store_id(value: str) -> bool:
    """Whether an activation target is shaped like a store asset id.

    This is the check the installer itself applies to a manifest id, so a
    target that passes here is one the install would accept. It also keeps
    a hostile target out of a dialog label: the shape allows no newline, no
    control character and no unbounded length, so a peer cannot write its
    own instruction lines above the fixed text of a prompt, and cannot hand
    the window a string long enough to hang it.
    """
    # Deferred, because this module is imported while the application is
    # built and the store backend pulls in the whole store layer.
    from src.backend.Store.StoreBackend import StoreBackend

    return StoreBackend.is_safe_asset_id(value)


class ConfirmedActionGate:
    """One exported action, and the confirmation in front of it.

    worker does the work the action names and blocks while it does, so it
    runs on a thread of this gate's own. confirm names the subject to the
    user and answers whether the work may start; it runs on the same
    thread, and blocks there, so it must marshal its own dialog to the main
    loop the way src/windows/Store/install_consent.py does.

    Both take the activation's subject. For an action with a string target
    that is the target, such as a plugin id. For an action with no target
    the subject is the action's own name, so a log line still says what was
    asked for.
    """

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
        """Create the action, wire it to this gate, and export it with the
        application. Returns the action, which the caller keeps alive."""
        parameter = GLib.VariantType(self._target_type) if self._target_type else None
        action = Gio.SimpleAction.new(self._action_name, parameter)
        action.connect("activate", self.on_activate)
        application.add_action(action)
        return action

    def on_activate(self, action: "Gio.SimpleAction | None",
                    target: "GLib.Variant | None") -> None:
        # A new name after unpack: the value is a str, and no longer a
        # Variant. A target of the wrong type cannot arrive through the
        # action's own signature, and an activation with none can.
        self.request(target.unpack() if target is not None else None)

    def request(self, subject: object) -> bool:
        """Ask to run this action's work, from an activation that proves
        nothing about its sender. Returns whether the request reached a
        confirmation, and never whether the work ran."""
        if self._target_type is None:
            # No target carries a subject, so the action names itself.
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
                # The quiet period elapsed, so the run of refusals ended.
                self._quiet_until = 0.0
                self._refusals = 0
            self._pending = subject
        # A daemon thread, because the confirmation waits minutes for an
        # answer and a quit must not wait with it.
        threading.Thread(target=self._confirm_then_run, args=(subject,),
                         name=f"{self._action_name}_request", daemon=True).start()
        return True

    def _confirm_then_run(self, subject: str) -> None:
        try:
            try:
                agreed = self._confirm(subject)
            except Exception as e:
                # A confirmation that failed is not an agreement, and it
                # counts against a peer that makes the dialog fail on purpose.
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
            # Held until the work ends, and not only until the dialog closes,
            # so the next request cannot start a second job beside this one.
            # Two installs of one id would swap the same directory under each
            # other.
            with self._lock:
                self._pending = None

    def _note_refusal(self) -> None:
        with self._lock:
            self._refusals += 1
            if self._refusals >= REFUSALS_BEFORE_QUIET:
                self._quiet_until = time.monotonic() + QUIET_PERIOD_S
