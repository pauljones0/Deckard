"""Exercise DBus control methods over a real bus and real controllers.
Success is an empty reply; failure is a user-readable sentence.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from gi.repository import GLib  # noqa: E402

import appinfo  # noqa: E402
import globals as gl  # noqa: E402

import src.api as api  # noqa: E402

# The isolated-daemon harness, shared rather than copied. Importing it only
# defines helpers, because its own legs run under its __main__ guard.
import scenario_api_lifecycle_publish as harness  # noqa: E402

from src.backend.DeckManagement.deck_events import KeyEvent
from src.backend.DeckManagement.InputIdentifier import Input  # noqa: E402

WATCHDOG_SECONDS = 80

SERIAL = "dbus-ctl-1"
OTHER_SERIAL = "dbus-ctl-2"
STATE_KEY = "0x0"
STATE_COUNT = 4

# A generous bound for the target page's inputs to load on the media thread.
STATE_SETTLE_SECONDS = 20.0


def settle_state_change(call, timeout: float = STATE_SETTLE_SECONDS) -> str:
    """Retry ChangeState while media-thread input loading leaves the old state count.
    Return the last rejection when the timeout expires.
    """
    deadline = time.monotonic() + timeout
    reply = call()
    while reply != "" and time.monotonic() < deadline:
        time.sleep(0.05)
        reply = call()
    return reply


def seed_multistate_page(page_name: str, key_ident: str, n_states: int) -> str:
    """Seed a page for checks against the input's real state count."""
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({
            "keys": {key_ident: {"states": {str(i): {} for i in range(n_states)}}},
            "dials": {}, "touchscreens": {},
        }, f)
    return path


def seed_action_page(page_name: str, key_ident: str, action_id: str) -> str:
    """Seed a page for checking the action IDs that ListActions reads."""
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({
            "keys": {key_ident: {"states": {"0": {"actions": [{"id": action_id}]}}}},
            "dials": {}, "touchscreens": {},
        }, f)
    return path


def active_name(controller) -> str | None:
    page = controller.active_page
    return None if page is None else page.get_name()


class Client:
    """Call the DBus methods through their client-facing interface."""

    def __init__(self, observer):
        self._observer = observer

    def change_page(self, serial: str, page: str) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, api.TOP_IFACE, "ChangePage",
            GLib.Variant("(ss)", (serial, page)))
        return reply.unpack()[0]

    def change_state(self, serial: str, page: str, coords: str, state: int) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, api.TOP_IFACE, "ChangeState",
            GLib.Variant("(sssi)", (serial, page, coords, state)))
        return reply.unpack()[0]

    def emulate_input(self, serial: str, page: str, coords: str, event: str) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, api.TOP_IFACE, "EmulateInput",
            GLib.Variant("(ssss)", (serial, page, coords, event)))
        return reply.unpack()[0]

    def query_state(self) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, api.TOP_IFACE, "QueryState", None)
        return reply.unpack()[0]

    def list_actions(self, page: str, coords: str) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, api.TOP_IFACE, "ListActions",
            GLib.Variant("(ss)", (page, coords)))
        return reply.unpack()[0]

    def set_brightness(self, serial: str, value: int) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, api.TOP_IFACE, "SetDeckBrightness",
            GLib.Variant("(si)", (serial, value)))
        return reply.unpack()[0]

    def sleep(self, serial: str) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, api.TOP_IFACE, "Sleep",
            GLib.Variant("(s)", (serial,)))
        return reply.unpack()[0]

    def wake(self, serial: str) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, api.TOP_IFACE, "Wake",
            GLib.Variant("(s)", (serial,)))
        return reply.unpack()[0]

    def rename_page(self, old: str, new: str) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, api.TOP_IFACE, "RenamePage",
            GLib.Variant("(ss)", (old, new)))
        return reply.unpack()[0]

    def duplicate_page(self, source: str, new: str) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, api.TOP_IFACE, "DuplicatePage",
            GLib.Variant("(ss)", (source, new)))
        return reply.unpack()[0]

    def introspect(self) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, "org.freedesktop.DBus.Introspectable",
            "Introspect", None)
        return reply.unpack()[0]


def leg_change_page(client, controller) -> None:
    assert client.change_page(SERIAL, "Alpha") == "", (
        "a switch that worked must answer with nothing at all -- the CLI reads "
        "any text as a failure to print")
    assert active_name(controller) == "Alpha", (
        f"ChangePage answered success without switching the deck: "
        f"{active_name(controller)}")

    assert client.change_page(SERIAL, "Beta") == ""
    assert active_name(controller) == "Beta"
    print("  PASS: ChangePage switches the deck it names")


def leg_already_active_is_success(client, controller) -> None:
    """Require an already active page to succeed without reloading."""
    assert active_name(controller) == "Beta", "this leg starts from a known page"

    original_load_page = controller.load_page
    loads: list = []

    def counting_load_page(page, *args, **kwargs):
        loads.append(page)
        return original_load_page(page, *args, **kwargs)

    controller.load_page = counting_load_page
    try:
        assert client.change_page(SERIAL, "Beta") == "", (
            "the deck already showing the page is a fulfilled request, not an "
            "error to report")
        assert loads == [], f"the already-active page was reloaded: {loads}"
    finally:
        del controller.load_page
    print("  PASS: asking for the active page answers success and loads nothing")


def leg_errors_name_what_exists(client, controller) -> None:
    unknown_deck = client.change_page("not-a-deck", "Alpha")
    assert unknown_deck, "an unknown serial must not answer success"
    assert SERIAL in unknown_deck and OTHER_SERIAL in unknown_deck, (
        f"the failure must name the decks that ARE connected: {unknown_deck!r}")

    unknown_page = client.change_page(SERIAL, "no-such-page")
    assert unknown_page, "an unknown page must not answer success"
    assert "Alpha" in unknown_page and "Beta" in unknown_page, (
        f"the failure must list the pages that exist: {unknown_page!r}")
    assert active_name(controller) == "Beta", (
        f"a rejected request must leave the deck alone: {active_name(controller)}")
    print("  PASS: failures come back as sentences that name what does exist")


def leg_change_state(client, controller) -> None:
    reply = settle_state_change(
        lambda: client.change_state(SERIAL, "States", STATE_KEY.replace("x", ","), 2))
    assert reply == "", (
        f"a state change that worked must answer with nothing: {reply!r}")
    assert active_name(controller) == "States", (
        "ChangeState loads the page whose input it is addressing")

    c_input = controller.get_input(Input.Key(STATE_KEY))
    assert c_input is not None
    assert c_input.state == 2, f"the input is on state {c_input.state}, not 2"
    print("  PASS: ChangeState loads the page and sets the input's state")


def leg_state_errors(client, controller) -> None:
    c_input = controller.get_input(Input.Key(STATE_KEY))

    too_high = client.change_state(SERIAL, "States", "0,0", STATE_COUNT)
    assert too_high, "a state past the end of the input must not answer success"
    assert f"{STATE_COUNT} states" in too_high, too_high
    assert c_input.state == 2, "a rejected state must not have been applied"

    off_device = client.change_state(SERIAL, "States", "99,0", 0)
    assert off_device and "out of bounds" in off_device, off_device

    unparsable = client.change_state(SERIAL, "States", "nope", 0)
    assert unparsable and "x,y" in unparsable, unparsable

    unknown_deck = client.change_state("not-a-deck", "States", "0,0", 0)
    assert unknown_deck and SERIAL in unknown_deck, unknown_deck

    assert active_name(controller) == "States", (
        "a rejected state change must not move the page either")
    print("  PASS: every rejected state change answers with its reason")


def leg_emulate_input(client, controller) -> None:
    """Drive a named press through the deck path as key down and then key up.
    The callbacks must run off the main thread, like hardware input.
    """
    controller.hold_time = 2.0
    seen: list = []
    real_event_callback = controller.event_callback

    def recording_event_callback(ident, *args, **kwargs):
        seen.append((ident.json_identifier, args, threading.current_thread()))
        return real_event_callback(ident, *args, **kwargs)

    controller.event_callback = recording_event_callback
    try:
        reply = client.emulate_input(SERIAL, "States", "0,0", "press")
        assert reply == "", (
            f"a press that was arranged must answer with nothing at all: {reply!r}")
        assert active_name(controller) == "States", (
            "EmulateInput loads the page whose input it is addressing")
        harness.pump_until(lambda: len(seen) >= 2, 10.0,
                           f"the emulated press never released the key: {seen}")
    finally:
        del controller.event_callback

    assert [(ident, args) for (ident, args, _thread) in seen] == [
        (STATE_KEY, (KeyEvent(pressed=True),)),
        (STATE_KEY, (KeyEvent(pressed=False),)),
    ], f"the press reached the deck as {seen}"
    assert all(thread is not threading.main_thread() for (_i, _a, thread) in seen), (
        f"the press ran on the main thread: {[t.name for (_i, _a, t) in seen]} -- "
        f"a hardware press never does, and this method is dispatched there")

    print("  PASS: EmulateInput presses and releases the input it names")


def leg_emulate_errors(client, controller) -> None:
    page_before = active_name(controller)

    unknown_event = client.emulate_input(SERIAL, "States", "0,0", "smash")
    assert unknown_event and "press" in unknown_event, unknown_event
    assert active_name(controller) == page_before, (
        f"a word the app does not know must not move the deck first: "
        f"{active_name(controller)}")

    off_device = client.emulate_input(SERIAL, "States", "99,0", "press")
    assert off_device and "out of bounds" in off_device, off_device

    unparsable = client.emulate_input(SERIAL, "States", "nope", "press")
    assert unparsable and "x,y" in unparsable, unparsable

    unknown_deck = client.emulate_input("not-a-deck", "States", "0,0", "press")
    assert unknown_deck and SERIAL in unknown_deck, unknown_deck

    unknown_page = client.emulate_input(SERIAL, "no-such-page", "0,0", "press")
    assert unknown_page and "States" in unknown_page, unknown_page

    assert active_name(controller) == page_before, (
        f"a rejected press must leave the deck where it was: "
        f"{active_name(controller)}")
    print("  PASS: every rejected press answers with its reason")


def leg_query_state(client, controller, other) -> None:
    """QueryState names every deck with its page and brightness, and the pages."""
    state = json.loads(client.query_state())
    assert "error" not in state, f"a dump names what exists and does not fail: {state}"
    by_serial = {d["serial"]: d for d in state["decks"]}
    assert SERIAL in by_serial and OTHER_SERIAL in by_serial, state
    assert "Main" in state["pages"] and "States" in state["pages"], state["pages"]
    for deck in state["decks"]:
        assert "active_page" in deck and "brightness" in deck, deck
    assert by_serial[SERIAL]["active_page"] == active_name(controller), by_serial[SERIAL]
    print("  PASS: QueryState names every deck, its page and its brightness")


def leg_set_and_get_brightness(client, controller) -> None:
    reply = client.set_brightness(SERIAL, 42)
    assert reply == "", f"a brightness that was set must answer nothing: {reply!r}"
    harness.pump_until(lambda: controller.brightness == 42, 5.0,
                       f"the brightness never reached 42: {controller.brightness}")

    settings = gl.settings_manager.get_deck_settings(SERIAL)
    assert settings.get("brightness", {}).get("value") == 42, (
        f"the brightness was not written to the deck settings: {settings.get('brightness')}")

    deck = next(d for d in json.loads(client.query_state())["decks"]
                if d["serial"] == SERIAL)
    assert deck["brightness"] == 42, f"QueryState did not reflect the set brightness: {deck}"

    unknown = client.set_brightness("not-a-deck", 10)
    assert unknown and SERIAL in unknown, unknown

    # An empty serial is an unknown deck, not a no-op. The CLI plans and
    # forwards it rather than dropping it into an ordinary launch.
    empty = client.sleep("")
    assert empty and SERIAL in empty, f"an empty serial was not refused: {empty!r}"
    print("  PASS: SetDeckBrightness sets the deck, persists it and reports it")


def leg_sleep_and_wake(client, controller) -> None:
    assert not controller.screen_saver.showing, "this leg must start awake"
    page_before = active_name(controller)

    assert client.sleep(SERIAL) == "", "a sleep that worked must answer nothing"
    harness.pump_until(lambda: controller.screen_saver.showing, 5.0,
                       "the deck never showed its screensaver")
    assert controller.allow_interaction, (
        "sleep turned interaction off; a slept deck must still wake on a press")

    assert client.wake(SERIAL) == "", "a wake that worked must answer nothing"
    harness.pump_until(lambda: not controller.screen_saver.showing, 5.0,
                       "the deck never left its screensaver")
    harness.pump_until(lambda: active_name(controller) == page_before, 5.0,
                       f"waking did not restore the page: {active_name(controller)}")

    unknown = client.sleep("not-a-deck")
    assert unknown and SERIAL in unknown, unknown
    print("  PASS: Sleep shows the screensaver and Wake restores the page under it")


def leg_list_actions(client) -> None:
    data = json.loads(client.list_actions("Actioned", ""))
    assert "error" not in data, data
    assert data["page"] == "Actioned", data
    assert data["actions"]["keys"]["0x0"]["0"] == ["demo::Action"], data

    keys = json.loads(client.list_actions("Actioned", "0,0"))["actions"].get("keys", {})
    assert set(keys) == {"0x0"}, f"the coordinate filter kept {set(keys)}"

    bad = json.loads(client.list_actions("no-such-page", ""))
    assert "error" in bad and "no-such-page" in bad["error"], bad

    bad = json.loads(client.list_actions("Actioned", "nope"))
    assert "error" in bad and "x,y" in bad["error"], bad
    print("  PASS: ListActions reads a page's actions, filters by key, names errors")


def leg_rename_and_duplicate_page(client, controller) -> None:
    """Require rename to update the active deck and duplicate to copy content.
    Both operations must use real page files.
    """
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    old_path = os.path.join(pages_dir, "Renamable.json")
    new_path = os.path.join(pages_dir, "Renamed.json")

    assert client.change_page(SERIAL, "Renamable") == ""
    assert active_name(controller) == "Renamable", active_name(controller)

    assert client.rename_page("Renamable", "Renamed") == "", "the rename failed"
    assert not os.path.exists(old_path), "the old page file was left behind"
    assert os.path.exists(new_path), "the renamed page file is missing"
    assert os.path.abspath(controller.active_page.json_path) == os.path.abspath(new_path), (
        f"the deck showing the page did not follow the rename: "
        f"{controller.active_page.json_path}")
    assert active_name(controller) == "Renamed", active_name(controller)

    pages = json.loads(client.query_state())["pages"]
    assert "Renamed" in pages and "Renamable" not in pages, pages

    taken = client.rename_page("Main", "Renamed")
    assert taken and "already exists" in taken, taken
    missing = client.rename_page("no-such-page", "Whatever")
    assert missing and "not found" in missing, missing

    copy_path = os.path.join(pages_dir, "Copy.json")
    assert client.duplicate_page("Renamed", "Copy") == "", "the duplicate failed"
    assert os.path.exists(copy_path), "the duplicated page file is missing"
    with open(new_path) as f:
        source_data = json.load(f)
    with open(copy_path) as f:
        copy_data = json.load(f)
    assert copy_data == source_data, "the duplicate does not match its source"

    taken = client.duplicate_page("Renamed", "Copy")
    assert taken and "already exists" in taken, taken
    print("  PASS: RenamePage moves the file and the deck, DuplicatePage copies it")


def leg_page_containment(client) -> None:
    """Require rename and duplicate to reject sources outside the pages folder.
    Absolute names can otherwise resolve to files outside that folder.
    """
    outside = os.path.join(gl.DATA_PATH, "outside_secret.json")
    with open(outside, "w") as f:
        json.dump({"keys": {}, "dials": {}, "touchscreens": {}}, f)
    pages_dir = os.path.join(gl.DATA_PATH, "pages")

    reply = client.rename_page(outside, "Stolen")
    assert reply and "pages folder" in reply, f"rename did not refuse the path: {reply!r}"
    assert os.path.exists(outside), "rename removed a file outside the pages folder"
    assert not os.path.exists(os.path.join(pages_dir, "Stolen.json")), (
        "rename copied a file from outside the pages folder into it")

    reply = client.duplicate_page(outside, "Stolen2")
    assert reply and "pages folder" in reply, f"duplicate did not refuse the path: {reply!r}"
    assert os.path.exists(outside), "duplicate touched a file outside the pages folder"
    assert not os.path.exists(os.path.join(pages_dir, "Stolen2.json")), (
        "duplicate copied a file from outside the pages folder into it")

    os.remove(outside)
    print("  PASS: a page path outside the pages folder is refused, nothing moves")


def leg_cli_transport_new_verbs(controller) -> None:
    """Drive each CLI variant against the real service and verify its effect.
    A worker runs blocking call_sync while the main thread pumps replies.
    """
    from src.backend import cli_forward

    transport = cli_forward.bus_transport()
    pages_dir = os.path.join(gl.DATA_PATH, "pages")

    def drive(answers: dict) -> None:
        answers["query"] = transport.query_state()
        answers["set_brightness"] = transport.set_brightness(SERIAL, 33)
        answers["brightness_after"] = controller.brightness
        answers["sleep"] = transport.sleep(SERIAL)
        answers["asleep"] = controller.screen_saver.showing
        answers["wake"] = transport.wake(SERIAL)
        answers["awake_showing"] = controller.screen_saver.showing
        answers["list_actions"] = transport.list_actions("Actioned", "")
        answers["rename"] = transport.rename_page("WireRename", "WireRenamed")
        answers["duplicate"] = transport.duplicate_page("WireRenamed", "WireCopy")
        answers["bad_deck"] = transport.set_brightness("not-a-deck", 10)

    answers = drive_on_worker(drive, "cli-transport-new-verbs")

    state = json.loads(answers["query"])
    assert any(d["serial"] == SERIAL for d in state["decks"]), answers["query"]
    assert answers["set_brightness"] == "", answers["set_brightness"]
    assert answers["brightness_after"] == 33, (
        f"set_brightness over the real transport never reached the deck: "
        f"{answers['brightness_after']}")
    assert answers["sleep"] == "" and answers["asleep"] is True, answers
    assert answers["wake"] == "" and answers["awake_showing"] is False, answers
    assert json.loads(answers["list_actions"])["page"] == "Actioned", answers["list_actions"]
    assert answers["rename"] == "", answers["rename"]
    assert os.path.exists(os.path.join(pages_dir, "WireRenamed.json")), (
        "rename over the real transport did not move the file")
    assert answers["duplicate"] == "", answers["duplicate"]
    assert os.path.exists(os.path.join(pages_dir, "WireCopy.json")), (
        "duplicate over the real transport did not create the file")
    assert answers["bad_deck"] and SERIAL in answers["bad_deck"], answers["bad_deck"]
    print("  PASS: the CLI's own transport drives every new verb to the service")


def leg_signatures_match_cli(client) -> None:
    """Require published signatures to match the variants built by the CLI."""
    xml = client.introspect()
    for method in ("ChangePage", "ChangeState", "EmulateInput", "QueryState",
                   "ListActions", "SetDeckBrightness", "Sleep", "Wake",
                   "RenamePage", "DuplicatePage"):
        assert f'<method name="{method}">' in xml, f"{method} is not on the bus"

    def signature(method: str) -> tuple[str, str]:
        block = xml.split(f'<method name="{method}">')[1].split("</method>")[0]
        args = [line for line in block.splitlines() if "<arg" in line]
        in_args = "".join(a.split('type="')[1].split('"')[0] for a in args
                          if 'direction="in"' in a)
        out_args = "".join(a.split('type="')[1].split('"')[0] for a in args
                           if 'direction="out"' in a)
        return in_args, out_args

    assert signature("ChangePage") == ("ss", "s"), signature("ChangePage")
    assert signature("ChangeState") == ("sssi", "s"), signature("ChangeState")
    assert signature("EmulateInput") == ("ssss", "s"), signature("EmulateInput")
    assert signature("QueryState") == ("", "s"), signature("QueryState")
    assert signature("ListActions") == ("ss", "s"), signature("ListActions")
    assert signature("SetDeckBrightness") == ("si", "s"), signature("SetDeckBrightness")
    assert signature("Sleep") == ("s", "s"), signature("Sleep")
    assert signature("Wake") == ("s", "s"), signature("Wake")
    assert signature("RenamePage") == ("ss", "s"), signature("RenamePage")
    assert signature("DuplicatePage") == ("ss", "s"), signature("DuplicatePage")
    print("  PASS: the published signatures are the ones the CLI calls with")


def drive_on_worker(work, name: str, timeout: float = 30.0) -> dict:
    """Run blocking client work on a worker while the main thread pumps replies."""
    answers: dict = {}

    def run() -> None:
        try:
            work(answers)
        except BaseException as e:  # surfaced by the caller's assertions
            answers["error"] = e
        answers["done"] = True

    worker = threading.Thread(target=run, name=name)
    worker.start()
    harness.pump_until(lambda: answers.get("done"), timeout,
                       f"{name} never finished its calls")
    worker.join(timeout=10)
    assert not worker.is_alive(), f"{name} did not return"
    assert "error" not in answers, f"{name} raised: {answers['error']!r}"
    return answers


def leg_cli_transport_reaches_service(controller) -> None:
    """Drive the CLI transport against the app's well-known service name.
    Each request uses this scenario's serial and runs on a worker.
    """
    from src.backend import cli_forward

    assert os.environ.get("DBUS_SESSION_BUS_ADDRESS"), \
        "the isolated bus address is not in the environment this transport reads"

    transport = cli_forward.bus_transport()

    def drive(answers: dict) -> None:
        answers["running"] = transport.has_running_instance()
        answers["page"] = transport.change_page(SERIAL, "Alpha")
        # Read where the deck ended up before the next call moves it. The page
        # was loaded before the reply was sent, so this is settled.
        answers["after_page"] = active_name(controller)
        answers["bad_page"] = transport.change_page(SERIAL, "no-such-page")
        # The media thread settles the States input count after the page switch;
        # settle_state_change waits for that count.
        answers["state"] = settle_state_change(
            lambda: transport.change_state(SERIAL, "States", "0,0", 1))

    answers = drive_on_worker(drive, "cli-transport")

    assert answers["running"] is True, (
        "the app's name is owned on this bus, so the CLI must see an instance "
        "to forward to")
    assert answers["page"] == "", answers["page"]
    assert answers["after_page"] == "Alpha", (
        f"the CLI transport's request never reached the deck: "
        f"{answers['after_page']}")
    assert "no-such-page" in answers["bad_page"], answers["bad_page"]
    assert answers["state"] == "", answers["state"]
    assert active_name(controller) == "States", (
        f"the state request must have loaded its own page: "
        f"{active_name(controller)}")
    assert controller.get_input(Input.Key(STATE_KEY)).state == 1, (
        "the CLI transport's state change never reached the input")
    print("  PASS: the CLI's own transport drives this service end to end")


def leg_instance_never_answers(controller) -> None:
    """Check the CLI reply when the service object or method is absent.
    GDBus reports both states alike while the service name stays owned.
    """
    from src.backend import cli_forward

    # Use the concrete class because phase 2 calls a method outside the public
    # transport surface.
    transport = cli_forward._BusTransport()
    page_before = active_name(controller)

    # Phase 1 removes the object but keeps the name, as during teardown.
    # Mutate registrations on the main context.
    api._bus.unpublish_object(api.DBUS_OBJECT_PATH)

    def drive_missing_object(answers: dict) -> None:
        answers["running"] = transport.has_running_instance()
        # Drive the whole forwarding path, so the assertion covers the sentence
        # a person reads rather than a constant this file names.
        answers["failures"] = cli_forward.forward(
            cli_forward.Plan(page_requests=[(SERIAL, "Alpha")],
                             state_requests=[(SERIAL, "States", "0,0", 1)]),
            transport)

    missing_object_results = drive_on_worker(
        drive_missing_object, "cli-transport-no-objects"
    )

    api._bus.publish_object(api.DBUS_OBJECT_PATH, api._api_instance)

    # Phase 2 keeps the object but removes the method; a separate refusal must
    # remain distinguishable from both missing-service cases.
    def drive_missing_method(answers: dict) -> None:
        try:
            transport._call("NoSuchMethod",
                            GLib.Variant("(ss)", (SERIAL, "Alpha")))
            answers["missing_method"] = None
        except cli_forward.OlderInstance as e:
            answers["missing_method"] = e
        try:
            transport._call("ChangePage", GLib.Variant("(s)", (SERIAL,)))
            answers["refused"] = None
        except cli_forward.TransportError as e:
            answers["refused"] = e

    older = drive_on_worker(drive_missing_method, "cli-transport-missing-method")

    assert missing_object_results["running"] is True, (
        "the name is owned -- an instance with nothing published looks exactly "
        "as running as any other, which is why this state is reachable")
    assert missing_object_results["failures"] == [cli_forward.SKEW_MESSAGE], (
        missing_object_results["failures"]
    )
    assert "older build" in cli_forward.SKEW_MESSAGE, cli_forward.SKEW_MESSAGE
    assert "shutting down" in cli_forward.SKEW_MESSAGE, (
        f"the state this leg just produced is also what tearing down looks "
        f"like from outside, and it is now the only other way to reach it: "
        f"{cli_forward.SKEW_MESSAGE!r}")
    assert "finished starting" not in cli_forward.SKEW_MESSAGE, (
        f"an instance that is starting cannot answer this way any more -- it "
        f"publishes before it takes the name -- so offering that reading sends "
        f"people to wait for something that already happened: "
        f"{cli_forward.SKEW_MESSAGE!r}")
    assert isinstance(older["missing_method"], cli_forward.OlderInstance), (
        f"a method this build does not have must reach the same answer: "
        f"{older['missing_method']!r}")
    assert isinstance(older["refused"], cli_forward.TransportError), (
        f"a call refused for any other reason must arrive as something the CLI "
        f"can print, not as a toolkit error nobody catches: "
        f"{older['refused']!r}")
    assert str(older["refused"]), "the refusal came back with nothing to say"
    assert active_name(controller) == page_before, (
        f"nothing above was applied, so the deck must not have moved: "
        f"{active_name(controller)}")
    print("  PASS: an instance with no methods on the bus is reported for the "
          "two things that now means")


def run_legs(bus_address: str, controller, other) -> None:
    api.start_dbus_service()
    assert api._bus is not None, "the DBus service did not start"
    # Stand in for GApplication so the CLI addresses the well-known app name,
    # not a unique connection name.
    api._bus.register_service(appinfo.APP_ID)
    observer = harness.Observer(bus_address, api._bus.connection.get_unique_name())
    client = Client(observer)
    # Whatever the second deck booted onto, it must still be showing it. Every
    # request below names one deck.
    other_page = other.active_page
    assert other_page is not None, "the second deck never loaded a page"
    try:
        leg_change_page(client, controller)
        leg_already_active_is_success(client, controller)
        leg_errors_name_what_exists(client, controller)
        leg_change_state(client, controller)
        leg_state_errors(client, controller)
        leg_emulate_input(client, controller)
        leg_emulate_errors(client, controller)
        leg_query_state(client, controller, other)
        leg_set_and_get_brightness(client, controller)
        leg_sleep_and_wake(client, controller)
        leg_list_actions(client)
        leg_rename_and_duplicate_page(client, controller)
        leg_page_containment(client)
        leg_signatures_match_cli(client)
        leg_cli_transport_reaches_service(controller)
        leg_cli_transport_new_verbs(controller)
        leg_instance_never_answers(controller)

        assert other.active_page is other_page, (
            f"every request above named one deck; the other one moved anyway: "
            f"{active_name(other)}")
    finally:
        api.stop_dbus_service()
        observer.connection.close_sync(None)


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_dbus_control_methods")
    fixtures._install_integration_globals()
    fixtures.seed_page("Main")
    fixtures.seed_page("Alpha")
    fixtures.seed_page("Beta")
    fixtures.seed_page("Renamable")
    fixtures.seed_page("WireRename")
    seed_multistate_page("States", STATE_KEY, STATE_COUNT)
    seed_action_page("Actioned", "0x0", "demo::Action")

    bus_proc, bus_address = harness.start_private_bus()
    controller = fixtures.make_headless_controller(serial=SERIAL)
    other = fixtures.make_headless_controller(serial=OTHER_SERIAL)
    try:
        run_legs(bus_address, controller, other)
    finally:
        fixtures.teardown(other)
        fixtures.teardown(controller)
        harness.stop_private_bus(bus_proc)

    print("PASS: scenario_dbus_control_methods")


if __name__ == "__main__":
    main()
