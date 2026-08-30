"""Run one environment-selected contender for the instance-gate scenario."""
import os
import sys

# Set the parent data directory before globals resolves and creates DATA_PATH
# Refuse a fallback because os._exit modes cannot clean writes to real user data.
_DATA_PATH = os.environ.get("DECKARD_GATE_DATA")
if not _DATA_PATH:
    raise SystemExit(
        "instance_gate_child.py refuses to run without DECKARD_GATE_DATA: "
        "without it `import globals` would resolve the user's real data "
        "directory and create it"
    )
sys.argv = [sys.argv[0], "--data", _DATA_PATH, "--devel",
            "--skip-load-hardware-decks"]

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import time  # noqa: E402

from gi.repository import Gio, GLib  # noqa: E402

import appinfo  # noqa: E402
import globals as gl  # noqa: E402  (imported for its argv-driven side effects)

from src.backend import instance_gate  # noqa: E402

assert gl.DATA_PATH == _DATA_PATH, (
    f"globals resolved {gl.DATA_PATH!r} instead of the parent's "
    f"{_DATA_PATH!r} -- this child would be writing outside the test"
)

MODE = os.environ["DECKARD_GATE_MODE"]
APP_ID = os.environ.get("DECKARD_GATE_APP_ID", appinfo.APP_ID)


def say(line: str) -> None:
    print(line, flush=True)


def wait_for_barrier() -> None:
    """Hold until the shared wall-clock start so children register together."""
    start_at = os.environ.get("DECKARD_GATE_START_AT")
    if not start_at:
        return
    target = float(start_at)
    while time.time() < target:
        time.sleep(0.001)


def run_gate(app: Gio.Application, close_running: bool = False):
    published: list[str] = []
    try:
        decision = instance_gate.establish(
            app, publish=lambda: published.append("published"),
            close_running=close_running)
    except instance_gate.LaunchAborted as e:
        say(f"VERDICT aborted {type(e).__name__}")
        say(f"REASON {e}")
        return None
    say(f"VERDICT {decision.value}")
    say(f"PUBLISHED {len(published)}")
    say(f"NON_UNIQUE {bool(app.get_flags() & Gio.ApplicationFlags.NON_UNIQUE)}")
    return decision


def mode_establish() -> None:
    app = Gio.Application(application_id=APP_ID)
    wait_for_barrier()
    decision = run_gate(app)
    if decision is instance_gate.Decision.PRIMARY:
        # Hold and dispatch the primary name while remote contenders register
        hold = float(os.environ.get("DECKARD_GATE_HOLD", "0"))
        if hold > 0:
            loop = GLib.MainLoop()
            GLib.timeout_add(int(hold * 1000), lambda: (loop.quit(), False)[1])
            loop.run()


def mode_activate() -> None:
    app = Gio.Application(application_id=APP_ID)
    decision = run_gate(app)
    if decision is instance_gate.Decision.REMOTE:
        app.activate()
        say("ACTIVATED")


# Keep the connection and callback alive because GDBus drops collected filters
_WIRE_WATCH: list = []


def watch_wire() -> None:
    """Report each Activate message from the GDBus reader thread.
    The filter observes messages even before the main context starts."""
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def on_message(_connection, message, incoming, *_user_data):
        if incoming and message.get_interface() == "org.gtk.Actions" \
                and message.get_member() == "Activate":
            body = message.get_body()
            say(f"WIRE-ACTIVATE {body.unpack()[0] if body else '?'}")
        return message

    _WIRE_WATCH.append((connection, on_message))
    connection.add_filter(on_message)


def _become_primary_loop(app_id: str, answer_quit: bool,
                         use_gate: bool = True) -> None:
    watch_wire()
    app = Gio.Application(application_id=app_id)
    if use_gate:
        decision = instance_gate.establish(app, publish=lambda: None,
                                           close_running=False)
        if decision is not instance_gate.Decision.PRIMARY:
            say(f"VERDICT {decision.value}")
            raise SystemExit(f"expected to be the primary, got {decision.value}")
    else:
        # Register directly because the old-name gate would probe the name it claims
        app.register(None)
        if app.get_is_remote():
            raise SystemExit(f"{app_id} was already owned")

    if answer_quit:
        delay = float(os.environ.get("DECKARD_GATE_QUIT_DELAY", "0"))

        def on_quit(*_args):
            say("QUIT-RECEIVED")
            if delay > 0:
                # Delay exit so the waiting launch must poll for name release
                GLib.timeout_add(int(delay * 1000), lambda: os._exit(0))
                return
            # Exit inside the handler so the caller observes name release without a reply
            os._exit(0)

        action = Gio.SimpleAction.new("quit", None)
        action.connect("activate", on_quit)
        app.add_action(action)

    say("READY")
    # Model boot after registration but before main-context dispatch
    time.sleep(float(os.environ.get("DECKARD_GATE_DISPATCH_DELAY", "0")))
    # Print before dispatch so the parent can order boot completion against wire events
    say("DISPATCHING")
    GLib.MainLoop().run()


def mode_primary_quit() -> None:
    _become_primary_loop(APP_ID, answer_quit=True)


def mode_primary_deaf() -> None:
    _become_primary_loop(APP_ID, answer_quit=False)


def mode_old_name() -> None:
    _become_primary_loop(appinfo.OLD_APP_ID, answer_quit=True, use_gate=False)


MODES = {
    "establish": mode_establish,
    "activate": mode_activate,
    "primary-quit": mode_primary_quit,
    "primary-deaf": mode_primary_deaf,
    "old-name": mode_old_name,
}

if __name__ == "__main__":
    MODES[MODE]()
