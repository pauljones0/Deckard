"""The gate in front of the install-plugin action the application exports.

GApplication publishes its whole action group on the session bus, at the
object path the application id names, so "install-plugin" is not a private
control. Any peer of the session bus can call org.gtk.Actions.Activate,
name that action and pass a plugin id, and the activation that arrives
carries nothing that says who sent it. The desktop shell relaying the
Install button of a notification is such a peer too, so no property of
the message separates the two.

The app's own install paths therefore do not use the action at all. The
store window, the onboarding page and the missing-action row each hold a
store backend and call install_plugin on it directly. An in-process token
would attest nothing here: the one activation the action still serves is
that notification button, and it arrives from outside this process like
every other activation.

What is left is to make an activation say out loud what it wants.
InstallActionGate answers every activation with a confirmation that names
the plugin, and starts the install only once a person agrees. The
confirmation runs before the catalog is read, so an activation nobody
wanted costs no request either, and one request stands at a time, so a
peer cannot stack dialogs or run two installs over each other.

Under flatpak the surface is narrower and still open. A sandboxed peer
needs a --talk-name grant for this app's bus name before it can address
the action at all, and this app's manifest grants that name to nobody.
Anything running unsandboxed on the session bus, which is every ordinary
desktop program, reaches the action with no grant at all, so the
confirmation and not the sandbox is what stands in front of an install.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from gi.repository import Gio, GLib

from loguru import logger as log

# The action name the application exports, and the one this module gates.
ACTION_NAME = "install-plugin"


class InstallActionGate:
    """The exported install-plugin action, and the confirmation in front of
    it.

    worker installs one plugin by id and blocks while it does, so it runs on
    a thread of this gate's own. confirm names the plugin to the user and
    answers whether the install may start; it runs on the same thread, and
    blocks there, so it must marshal its own dialog to the main loop the way
    src/windows/Store/install_consent.py does.
    """

    def __init__(self, worker: Callable[[str], None],
                 confirm: Callable[[str], bool]) -> None:
        self._worker = worker
        self._confirm = confirm
        # One request at a time, so a peer that activates in a loop gets one
        # dialog and not a stack of them, and two installs never overlap.
        self._lock = threading.Lock()
        self._pending: str | None = None

    def add_to(self, application: Gio.Application) -> Gio.SimpleAction:
        """Create the action, wire it to this gate, and export it with the
        application. Returns the action, which the caller keeps alive."""
        action = Gio.SimpleAction.new(ACTION_NAME, GLib.VariantType("s"))
        action.connect("activate", self.on_activate)
        application.add_action(action)
        return action

    def on_activate(self, action: Gio.SimpleAction,
                    target: "GLib.Variant | None") -> None:
        # A new name after unpack: the value is a str, and no longer a
        # Variant. A target of the wrong type cannot arrive through the
        # action's own signature, and an activation with none can.
        plugin_id = target.unpack() if target is not None else None
        self.request(plugin_id)

    def request(self, plugin_id: object) -> bool:
        """Ask to install one plugin, from an activation that proves nothing
        about its sender. Returns whether the request reached a confirmation,
        and never whether anything installed."""
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            log.warning(f"Ignoring an install-plugin activation with the target {plugin_id!r}")
            return False
        with self._lock:
            if self._pending is not None:
                log.warning(
                    f"Ignoring the install-plugin activation for {plugin_id}: "
                    f"the request for {self._pending} is still waiting for an answer")
                return False
            self._pending = plugin_id
        # A daemon thread, because the confirmation waits minutes for an
        # answer and a quit must not wait with it.
        threading.Thread(target=self._confirm_then_install, args=(plugin_id,),
                         name="install_plugin_request", daemon=True).start()
        return True

    def _confirm_then_install(self, plugin_id: str) -> None:
        try:
            try:
                agreed = self._confirm(plugin_id)
            except Exception as e:
                # A confirmation that failed is not an agreement.
                log.error(f"The install confirmation for {plugin_id} failed: {e!r}")
                return
            if not agreed:
                log.info(f"Not installing {plugin_id}: the install was not confirmed")
                return
            self._worker(plugin_id)
        except Exception as e:
            log.error(f"The confirmed install of {plugin_id} failed: {e!r}")
        finally:
            # Held until the install ends, and not only until the dialog
            # closes, so the next request cannot start a second install
            # beside this one.
            with self._lock:
                self._pending = None
