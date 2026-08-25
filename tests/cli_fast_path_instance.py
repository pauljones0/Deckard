"""A stand-in for a running Deckard, for the CLI fast-path scenario.

It owns the application name on the session bus it is pointed at and answers
the control methods the CLI forwards, so a real `main.py --change-page` has
something to talk to without an application, a display or a deck. Every call
it takes is appended to the record file as one JSON line, which is what proves
a forward arrived rather than merely returned.

Environment: DECKARD_STUB_APP_ID, DECKARD_STUB_RECORD, and a session bus
address. DECKARD_STUB_REFUSE, when set, is the sentence every method answers
with instead of success, which is how a failing instance is driven without a
second stand-in. It prints READY on stdout once it owns the name.
"""
import json
import os

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

APP_ID = os.environ["DECKARD_STUB_APP_ID"]
OBJECT_PATH = "/" + APP_ID.replace(".", "/")
RECORD_PATH = os.environ["DECKARD_STUB_RECORD"]
REFUSE = os.environ.get("DECKARD_STUB_REFUSE", "")

# The methods src/backend/cli_forward.py calls, with the signatures src/api.py
# exports. A signature that drifts from the app's own makes the reply
# unreadable to the CLI, which is a failure this stand-in should show rather
# than paper over.
INTROSPECTION = f"""
<node>
  <interface name="{APP_ID}">
    <method name="ChangePage">
      <arg type="s" name="serial" direction="in"/>
      <arg type="s" name="page" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="ChangeState">
      <arg type="s" name="serial" direction="in"/>
      <arg type="s" name="page" direction="in"/>
      <arg type="s" name="coords" direction="in"/>
      <arg type="i" name="state" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="EmulateInput">
      <arg type="s" name="serial" direction="in"/>
      <arg type="s" name="page" direction="in"/>
      <arg type="s" name="coords" direction="in"/>
      <arg type="s" name="event" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
  </interface>
</node>
"""


def record(entry: dict) -> None:
    """Append one call to the record. Opened per call and flushed, so the
    parent reads a complete line even though this process is killed."""
    with open(RECORD_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())


def on_call(connection, sender, path, interface, method, params, invocation) -> None:
    record({"method": method, "args": list(params.unpack())})
    invocation.return_value(GLib.Variant("(s)", (REFUSE,)))


def main() -> None:
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
    connection.register_object(OBJECT_PATH, node.interfaces[0], on_call, None, None)

    def acquired(_connection, _name) -> None:
        # The parent waits for this line before it runs the CLI. Without it
        # the CLI can probe an unowned name and read a fall-through as a
        # broken fast path.
        print("READY", flush=True)

    def lost(_connection, _name) -> None:
        print("LOST", flush=True)

    Gio.bus_own_name_on_connection(
        connection, APP_ID, Gio.BusNameOwnerFlags.NONE, acquired, lost)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
