"""Session-bus stand-in for CLI forwarding scenarios.
Records calls and uses DECKARD_STUB_* variables to control replies."""
import json
import os

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

APP_ID = os.environ["DECKARD_STUB_APP_ID"]
OBJECT_PATH = "/" + APP_ID.replace(".", "/")
RECORD_PATH = os.environ["DECKARD_STUB_RECORD"]
REFUSE = os.environ.get("DECKARD_STUB_REFUSE", "")
# Query result used to test read verbs without a live deck
QUERY_JSON = os.environ.get("DECKARD_STUB_QUERY_JSON", "{}")

# Keep these signatures equal to src/api.py so interface drift fails the scenario.
# Read methods return JSON; other methods return an empty or refusal reason.
_QUERY_METHODS = {"QueryState", "ListActions"}

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
    <method name="QueryState">
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="ListActions">
      <arg type="s" name="page" direction="in"/>
      <arg type="s" name="coords" direction="in"/>
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="SetDeckBrightness">
      <arg type="s" name="serial" direction="in"/>
      <arg type="i" name="value" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="Sleep">
      <arg type="s" name="serial" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="Wake">
      <arg type="s" name="serial" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="RenamePage">
      <arg type="s" name="old" direction="in"/>
      <arg type="s" name="new" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="DuplicatePage">
      <arg type="s" name="source" direction="in"/>
      <arg type="s" name="new" direction="in"/>
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
    if method in _QUERY_METHODS:
        invocation.return_value(GLib.Variant("(s)", (QUERY_JSON,)))
    else:
        invocation.return_value(GLib.Variant("(s)", (REFUSE,)))


def main() -> None:
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
    connection.register_object(OBJECT_PATH, node.interfaces[0], on_call, None, None)

    def acquired(_connection, _name) -> None:
        # Signal ownership before the parent probes the CLI fast path
        print("READY", flush=True)

    def lost(_connection, _name) -> None:
        print("LOST", flush=True)

    Gio.bus_own_name_on_connection(
        connection, APP_ID, Gio.BusNameOwnerFlags.NONE, acquired, lost)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
