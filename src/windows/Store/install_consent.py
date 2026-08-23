"""The consent prompt before a new plugin's install script runs.

A plugin install runs on a download worker thread, and the install gate
asks this before it runs an __install__.py. The prompt is built and
shown on the GTK main loop, and the worker blocks on an event until the
user answers, so the gate gets a plain bool back off the main thread.
"""
import threading
from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from loguru import logger as log


# A dialog that never returns, because the window closed under it or the
# idle callback never ran, must not wedge the install worker forever. The
# safe answer on a lost dialog is to decline, which skips the script.
_ANSWER_TIMEOUT_S = 300


def make_consent(parent: "Gtk.Window | None") -> Callable[[str], bool]:
    """A consent callable for install_script.decide_install_scripts, bound
    to a parent window. It presents a modal dialog on the main loop and
    returns the user's choice, defaulting to decline if no answer arrives."""
    def ask(display_name: str) -> bool:
        # The caller (a store download worker) blocks on the dialog. On the
        # main thread that block would freeze the loop the dialog needs, so
        # the invariant is enforced rather than deadlocked.
        assert threading.current_thread() is not threading.main_thread(), (
            "the install-script consent prompt must run off the main thread")
        answered = threading.Event()
        box = {"run": False}

        def present() -> bool:
            dialog = Adw.MessageDialog(
                transient_for=parent,
                modal=True,
                title="Run install steps?",
                heading=f"Run {display_name}'s install steps?",
                body=(f"{display_name} may run a setup step on this computer to "
                      "finish installing, such as building a helper environment. "
                      "Run it only if you trust the plugin. Skip installs the "
                      "plugin without the step, which some plugins need to work. "
                      "Either way the plugin's own code still runs once it loads."),
            )
            dialog.add_response("skip", "Skip")
            dialog.add_response("run", "Run")
            dialog.set_response_appearance("run", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("skip")
            dialog.set_close_response("skip")

            def on_response(_dialog: Adw.MessageDialog, response: str) -> None:
                box["run"] = response == "run"
                answered.set()

            dialog.connect("response", on_response)
            dialog.present()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(present)
        if not answered.wait(timeout=_ANSWER_TIMEOUT_S):
            log.warning(f"No answer to the install-script prompt for {display_name}; skipping the script")
            return False
        return box["run"]

    return ask
