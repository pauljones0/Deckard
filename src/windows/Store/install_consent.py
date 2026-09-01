"""Collect install-script, dependency-set, and exported-action consent.

Dialogs run on-main while the install worker waits for a boolean answer.
"""
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from loguru import logger as log

if TYPE_CHECKING:
    from src.backend.Store.dependencies import Plan


# Bound lost dialogs so they decline instead of blocking the install worker
_ANSWER_TIMEOUT_S = 300


def _ask(what: str, build: "Callable[[Callable[[bool], None]], None]") -> bool:
    """Show a main-loop dialog and block this worker until answer or timeout."""
    # Reject main-thread callers because the blocking wait would freeze the dialog
    assert threading.current_thread() is not threading.main_thread(), (
        "an install prompt must run off the main thread")
    answered = threading.Event()
    box = {"agreed": False}

    def record(agreed: bool) -> None:
        box["agreed"] = agreed
        answered.set()

    def present() -> bool:
        build(record)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(present)
    if not answered.wait(timeout=_ANSWER_TIMEOUT_S):
        log.warning(f"No answer to the {what} prompt; taking it as a refusal")
        return False
    return box["agreed"]


def _dialog(parent: "Gtk.Window | None", title: str, heading: str, body: str,
            agree_label: str, refuse_label: str,
            record: "Callable[[bool], None]") -> None:
    """Show a two-answer dialog that defaults all dismissal to refusal.

    Without a parent, keep it nonmodal so tray-only startup does not hide it.
    """
    dialog = Adw.MessageDialog(transient_for=parent, modal=parent is not None,
                               title=title, heading=heading, body=body)
    dialog.add_response("refuse", refuse_label)
    dialog.add_response("agree", agree_label)
    dialog.set_response_appearance("agree", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("refuse")
    dialog.set_close_response("refuse")
    dialog.connect("response", lambda _dialog, response: record(response == "agree"))
    dialog.present()


def make_install_script_consent(parent: "Gtk.Window | None") -> Callable[[str], bool]:
    """Return parent-bound install-script consent that defaults to decline."""
    def ask(display_name: str) -> bool:
        return _ask(f"install-script prompt for {display_name}", lambda record: _dialog(
            parent,
            title="Run install steps?",
            heading=f"Run {display_name}'s install steps?",
            body=(f"{display_name} may run a setup step on this computer to "
                  "finish installing, such as building a helper environment. "
                  "Run it only if you trust the plugin. Skip installs the "
                  "plugin without the step, which some plugins need to work. "
                  "Either way the plugin's own code still runs once it loads."),
            agree_label="Run",
            refuse_label="Skip",
            record=record,
        ))

    return ask


def make_dependency_consent(parent: "Gtk.Window | None") -> Callable[[str, "Plan"], bool]:
    """Return consent for the complete dependency plan before any download."""
    def ask(root_name: str, plan: "Plan") -> bool:
        listed = "\n".join(f"• {name}" for name in plan.names())
        body = (f"Installing {root_name} also installs the items it names, in "
                f"this order:\n\n{listed}\n\nThey come from the store catalogs "
                "this app is set to use. Removing an item later leaves the "
                "others installed.")
        if plan.unknown:
            named = ", ".join(plan.unknown)
            body += (f"\n\n{root_name} also asks for items that no catalog "
                     f"lists, so they cannot be installed: {named}. It may not "
                     "work without them.")
        if plan.truncated:
            body += ("\n\nThe items named go deeper than this app follows, so "
                     "the list above may be short of what they ask for.")
        return _ask(f"dependency prompt for {root_name}", lambda record: _dialog(
            parent,
            title="Install these store items?",
            heading=f"{root_name} needs other store items",
            body=body,
            agree_label="Install all",
            refuse_label="Cancel",
            record=record,
        ))

    return ask


def make_update_confirm(parent: "Gtk.Window | None") -> Callable[[], bool]:
    """Confirm exported updates before any session-bus peer can reinstall assets."""
    def ask() -> bool:
        return _ask("update request", lambda record: _dialog(
            parent,
            title="Update all store assets?",
            heading="Update every installed store asset?",
            body=("Something outside this window asked for this update. That "
                  "is the Update All button of a notification from this app, "
                  "and it is also any other program on your desktop session. "
                  "Updating reinstalls every plugin, icon pack and wallpaper "
                  "pack that is out of date, and a plugin may run its own "
                  "setup step while it installs."),
            agree_label="Update all",
            refuse_label="Cancel",
            record=record,
        ))

    return ask


def make_install_confirm(parent: "Gtk.Window | None") -> Callable[[str], bool]:
    """Confirm an anonymous exported install before reading the store catalog."""
    def ask(plugin_id: str) -> bool:
        return _ask(f"install request for {plugin_id}", lambda record: _dialog(
            parent,
            title="Install this plugin?",
            heading=f"Install the plugin {plugin_id}?",
            body=("Something outside this window asked for this install. That "
                  "is the Install button of a notification from this app, and "
                  "it is also any other program on your desktop session. "
                  "Install it only if you asked for it just now. The plugin's "
                  "own code runs once it is installed."),
            agree_label="Install",
            refuse_label="Cancel",
            record=record,
        ))

    return ask
