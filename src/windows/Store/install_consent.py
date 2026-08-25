"""The prompts a plugin install has to get past, and the one wait behind
them.

Three questions reach the user from an install worker thread. Whether a
plugin's install script may run, which the install gate asks before it
destroys a working install. Whether a whole set of store items may be
installed, which dependency resolution asks before the first download.
And whether an install that arrived on the exported action may start at
all, which is the only thing standing in front of a session-bus peer.

Every one of them is built and shown on the GTK main loop while the
worker blocks on an event, so each answer comes back to the worker as a
plain bool off the main thread.
"""
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from loguru import logger as log

if TYPE_CHECKING:
    from src.backend.Store.dependencies import Plan


# A dialog that never returns, because the window closed under it or the
# idle callback never ran, must not wedge the install worker forever. The
# safe answer on a lost dialog is to decline, which skips the script.
_ANSWER_TIMEOUT_S = 300


def _ask(what: str, build: "Callable[[Callable[[bool], None]], None]") -> bool:
    """Show one dialog on the main loop and block this thread for its answer.

    build gets the callback that records the answer, and constructs and
    presents the dialog with it. A lost dialog answers False; see the
    timeout above. what names the question in the log line.
    """
    # The caller (an install worker) blocks below. On the main thread that
    # block would freeze the loop the dialog needs, so the invariant is
    # enforced rather than deadlocked.
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
    """One two-answer dialog. Refusing is the default and the close answer,
    so a dialog dismissed any other way installs nothing.

    With no parent window, which the tray-only autostart has until a window
    is opened, the dialog stands on its own and is not modal. A modal
    dialog with nothing to be modal to can come up behind everything, and
    then the only answer is the timeout above.
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


def make_consent(parent: "Gtk.Window | None") -> Callable[[str], bool]:
    """A consent callable for install_script.decide_install_scripts, bound
    to a parent window. It presents a modal dialog on the main loop and
    returns the user's choice, defaulting to decline if no answer arrives."""
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


def make_set_consent(parent: "Gtk.Window | None") -> Callable[[str, "Plan"], bool]:
    """A consent callable for an install that carries dependencies. It names
    every item, and everything the resolution could not deliver, before
    anything downloads, and a refusal refuses the whole set."""
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
    """A confirmation for an update of every installed asset that arrived on
    the exported action.

    Any peer of the session bus can activate that action, and an update
    reinstalls every out-of-date asset, which for a plugin can run that
    plugin's install step. The answer here is what decides whether any of
    that starts.
    """
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
    """A confirmation for an install that arrived on the exported
    install-plugin action.

    Any peer of the session bus can activate that action, and nothing in the
    activation says who sent it, so the answer here is what decides whether
    an install starts. It is asked before the store catalog is read, so a
    refusal costs no request either.
    """
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
