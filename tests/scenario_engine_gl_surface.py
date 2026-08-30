"""Measure the render engine's closed globals surface at runtime and statically.
Required-absent UI slots prevent allow-list expansion from hiding a violation.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import ast  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from contextlib import contextmanager  # noqa: E402

import globals as gl  # noqa: E402

from src.backend.DeckManagement.InputIdentifier import Input  # noqa: E402

# Import the real DeckManager so the static sweep includes its four globals
# reads despite the runtime stub.
import src.backend.DeckManagement.DeckManager  # noqa: E402,F401

WATCHDOG_SECONDS = 60

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Roots whose files are the engine when they appear as a caller or as a loaded
# module. Directories match by prefix; the two bare modules match by name.
_ENGINE_DIRS = ("src", "GtkHelper")
_ENGINE_FILES = ("globals.py", "cli_args.py")

SERIAL = "gl-surface-1"

# Tag each measured engine slot with its reader; the allow list must match both
# runtime and static measurements exactly enough to remain useful.
ALLOWED_ENGINE_GL_SURFACE = frozenset({
    "DATA_PATH",                   # paths
    "STATIC_SETTINGS_FILE_PATH",   # settings
    "api_page_requests",           # cli
    "api_state_requests",          # cli
    "argparser",                   # flags
    "deck_manager",                # decks
    "fallback_font",               # labels
    "icon_pack_manager",           # icons
    "image_extensions",            # media
    "media_manager",               # thumbnails
    "page_manager",                # pages
    "plugin_manager",              # plugins
    "presence_monitor",            # screensaver
    "screen_locked",               # lockscreen
    "settings_manager",            # settings
    "signal_manager",              # signals
    "svg_extensions",              # media
    "threads_running",             # shutdown
    "top_level_dir",               # assets
    "video_extensions",            # media
    "window_grabber",              # autoswitch
})

# Exempt only startup_queue's app deferral slots, which share a module with the
# engine's CLI paths but are not read by the engine workload.
LEG_A_HOST_EXEMPTION = {
    "src/backend/startup_queue.py": frozenset({"app", "app_loading_finished_tasks"}),
}

# Name UI slots that must remain absent independently of the extensible allow
# list.
REQUIRED_ABSENT = frozenset({
    "lm",                          # locale
    "app",                         # application
    "notify",                      # notifications
    "store_backend",               # store
    "app_loading_finished_tasks",  # boot deferral
})

# Require unavoidable workload reads so a missing recorder or drive cannot
# satisfy the surface checks with an empty set.
REQUIRED_OBSERVED = frozenset({
    "page_manager",
    "settings_manager",
    "api_page_requests",
    "api_state_requests",
})


_READS: set = set()


class _RecordingGlobals(types.ModuleType):
    """The globals module, with every non-dunder attribute read written down."""

    def __getattribute__(self, name: str):
        if not (name.startswith("__") and name.endswith("__")):
            _READS.add((name, sys._getframe(1).f_code.co_filename))
        return super().__getattribute__(name)


@contextmanager
def _recording():
    """Route reads of gl through the recorder for the duration."""
    original = type(gl)
    gl.__class__ = _RecordingGlobals
    try:
        yield
    finally:
        gl.__class__ = original


def _repo_relative(path: str) -> str | None:
    absolute = os.path.abspath(path)
    if not absolute.startswith(_REPO_ROOT + os.sep):
        return None
    return os.path.relpath(absolute, _REPO_ROOT).replace(os.sep, "/")


def _is_engine_file(path: str) -> bool:
    relative = _repo_relative(path)
    if relative is None:
        return False
    if relative in _ENGINE_FILES:
        return True
    return relative.split("/")[0] in _ENGINE_DIRS


def engine_runtime_reads() -> set:
    """Slot and repo-relative file for every read made by engine code."""
    return {
        (slot, _repo_relative(path))
        for slot, path in _READS
        if _is_engine_file(path)
    }


def _any_action_ready(page) -> bool:
    for by_ident in page.action_objects.values():
        for by_state in by_ident.values():
            for by_index in by_state.values():
                for action in by_index.values():
                    if getattr(action, "on_ready_called", False):
                        return True
    return False


def drive_engine_workload() -> None:
    """Drive pages, inputs, media, teardown, and both parked CLI request types."""
    from src.backend import startup_queue

    queue = startup_queue.get()

    red = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "surface_red.png"), color=(220, 20, 20))
    green = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "surface_green.png"), color=(20, 220, 20))
    page_a_path = fixtures.seed_page_with_background("SurfaceA", red)

    # Before the controller. load_default_page() runs at the end of
    # DeckController.__init__ and would hit an unset plugin_manager.
    fixtures.install_stub_plugin_manager(fixtures.make_latch_action_class(), green)

    # Leg B parks before the deck exists, the only state the CLI can park in.
    # The load_default_page of DeckController.__init__ is the claim.
    fixtures.seed_page("Parked")
    queue.park_page_request(SERIAL, "Parked")

    controller = fixtures.make_headless_controller(serial=SERIAL)
    try:
        assert SERIAL not in gl.api_page_requests, (
            "the controller's first load_default_page did not claim the parked "
            "page request -- leg B's engine-side read went unexercised"
        )

        deck = fixtures.raw_deck(controller)

        page_a = gl.page_manager.get_page(page_a_path, controller)
        controller.load_page(page_a, allow_reload=True)
        assert fixtures.wait_until(
            lambda: deck.ops_by_name("set_key_image"), timeout=10), (
            "page A never reached the device"
        )

        # Input while page A is up, so the real event callbacks, hold timers
        # and action dispatch all run.
        deck.fire_key_event(0, True)
        deck.fire_key_event(0, False)
        if controller.inputs.get(Input.Dial):
            from StreamDeck.Devices.StreamDeck import DialEventType

            deck.fire_dial_event(0, DialEventType.PUSH, True)
            deck.fire_dial_event(0, DialEventType.PUSH, False)

        key_ident = controller.inputs[Input.Key][0].identifier.json_identifier
        page_b_path = fixtures.seed_action_page("SurfaceB", key_ident)

        before_switch = deck.current_seq()
        page_b = gl.page_manager.get_page(page_b_path, controller)
        controller.load_page(page_b, allow_reload=True)
        assert fixtures.wait_until(
            lambda: any(e[2] == "set_key_image" for e in deck.ops_after(before_switch)),
            timeout=10), "page B never reached the device"
        assert fixtures.wait_until(lambda: _any_action_ready(page_b), timeout=10), (
            "page B's action never reached on_ready -- ActionCore init / "
            "initialize_actions / on_ready would be outside this scenario's "
            "read surface"
        )
        deck.fire_key_event(0, True)
        deck.fire_key_event(0, False)

        # Leg C is peeked, applied and resolved inside load_default_page.
        queue.park_state_request(SERIAL, {
            "page_name": "SurfaceA", "coords": "0,0", "state": 0,
        })
        controller.load_default_page()
        assert SERIAL not in gl.api_state_requests, (
            "the parked state request was never resolved -- leg C's "
            "engine-side reads went unexercised"
        )
    finally:
        fixtures.teardown(controller)


def check_recorder_records_and_restores() -> None:
    """Require the recorder to capture reads and restore the module class on exit."""
    baseline = type(gl)
    probe = ("DATA_PATH", __file__)

    _READS.discard(probe)
    with _recording():
        assert isinstance(gl, _RecordingGlobals), (
            "the __class__ swap did not take -- the recorder is not installed "
            "and every read below would go unrecorded"
        )
        assert gl.DATA_PATH, "fixture sanity: the isolated data dir is unset"
    assert probe in _READS, (
        f"the recorder did not record a read it was installed for: {_READS}"
    )
    assert type(gl) is baseline, (
        f"the recorder survived a clean exit from its context: {type(gl)}"
    )

    boom = RuntimeError("deliberate failure inside the recorded region")
    try:
        with _recording():
            assert isinstance(gl, _RecordingGlobals)
            raise boom
    except RuntimeError as raised:
        assert raised is boom, f"an unrelated error masked the restore check: {raised}"
    else:
        raise AssertionError("the deliberate failure did not propagate")
    assert type(gl) is baseline, (
        f"the recorder survived a FAILING exit from its context -- the finally "
        f"restore is broken: {type(gl)}"
    )

    _READS.discard(probe)
    print("PASS: the recorder records reads and restores globals.__class__ on failure")


def check_recorder_saw_engine(runtime_reads: set) -> None:
    """Require mandatory runtime reads so later subset checks are not vacuous."""
    assert runtime_reads, (
        "the recorder captured no engine reads at all -- it did not install, "
        "or nothing engine-side ran, and every assertion in this scenario "
        "would be vacuous"
    )
    slots = {slot for slot, _ in runtime_reads}
    missing = sorted(REQUIRED_OBSERVED - slots)
    assert not missing, (
        f"the drive did not exercise {missing} -- these are read on every page "
        f"load and on both CLI-request legs, so their absence means the "
        f"workload did not run, not that the engine stopped reading them. "
        f"Recorded: {sorted(slots)}"
    )
    controller_page_manager = [
        path for slot, path in runtime_reads
        if slot == "page_manager" and path.endswith("/controller.py")
    ]
    assert controller_page_manager, (
        "no gl.page_manager read was attributed to the deck controller -- the "
        "known-allowed read this guard is calibrated against never happened"
    )
    print(f"PASS: the recorder captured {len(runtime_reads)} engine reads of "
          f"{len(slots)} distinct slots")


def check_runtime_surface(runtime_reads: set) -> None:
    """Layer one. What the engine actually read."""
    unlisted = sorted({
        (slot, path) for slot, path in runtime_reads
        if slot not in ALLOWED_ENGINE_GL_SURFACE
    })
    assert not unlisted, (
        f"engine code read gl slots outside the allowed surface: {unlisted}. "
        f"Either the read belongs somewhere else, or "
        f"ALLOWED_ENGINE_GL_SURFACE gains an entry -- and that edit is the "
        f"decision this guard exists to force."
    )
    print(f"PASS: every engine read is inside the allowed surface "
          f"({len(ALLOWED_ENGINE_GL_SURFACE)} slots)")


def check_required_absent(runtime_reads: set) -> None:
    """Require every named UI slot to remain absent from runtime engine reads."""
    present = sorted({
        (slot, path) for slot, path in runtime_reads if slot in REQUIRED_ABSENT
    })
    assert not present, (
        f"the engine read a UI-side slot: {present}. The render path is "
        f"supposed to run with no locale manager, no App, no notification "
        f"facade and no store -- that is the daemon/client seam, and this is "
        f"where it stops being true."
    )
    print(f"PASS: none of {sorted(REQUIRED_ABSENT)} is read by engine code")


def _globals_aliases(tree: ast.Module, relative: str) -> set:
    """Find globals-module aliases and reject direct imports the sweep cannot follow."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name == "globals":
                    aliases.add(name.asname or "globals")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "globals" and node.level == 0:
                raise AssertionError(
                    f"{relative} imports slots out of `globals` directly, which "
                    f"this sweep cannot follow -- use `import globals as gl`"
                )
    return aliases


def static_engine_references() -> dict:
    """Map loaded engine modules to every globals attribute they load or store."""
    references: dict = {}
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path or not _is_engine_file(path):
            continue
        relative = _repo_relative(path)
        if relative in references or not path.endswith(".py"):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source, filename=path)
        aliases = _globals_aliases(tree, relative)
        if not aliases:
            continue
        slots = {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        }
        if slots:
            references[relative] = slots
    return references


def check_static_surface(references: dict) -> None:
    """Reject static globals references that an exercised runtime branch can miss."""
    assert references, (
        "no engine module referenced `globals` at all -- the sweep found "
        "nothing to check and would pass vacuously"
    )
    unlisted = []
    for relative, slots in sorted(references.items()):
        exempt = LEG_A_HOST_EXEMPTION.get(relative, frozenset())
        for slot in sorted(slots):
            if slot not in ALLOWED_ENGINE_GL_SURFACE and slot not in exempt:
                unlisted.append(f"{relative}: gl.{slot}")
    assert not unlisted, (
        f"engine sources mention gl slots outside the allowed surface: "
        f"{unlisted}. The runtime drive may never reach them; that is what "
        f"this layer is for."
    )
    print(f"PASS: {len(references)} engine modules mention only allowed slots")


def check_static_absent(references: dict) -> None:
    """Require UI slots statically absent except for exact file-and-slot exemptions."""
    for relative, slots in sorted(references.items()):
        exempt = LEG_A_HOST_EXEMPTION.get(relative, frozenset())
        offending = sorted((slots & REQUIRED_ABSENT) - exempt)
        assert not offending, (
            f"{relative} mentions UI-side slots {offending}. Engine code does "
            f"not get a locale manager, an App, a notification facade or a "
            f"store -- if this file grew a legitimate need for one, it is not "
            f"engine code any more."
        )
    hosts = sorted(LEG_A_HOST_EXEMPTION)
    for host in hosts:
        assert host in references, (
            f"{host} is exempted from the absent-slot check but is not a loaded "
            f"engine module any more -- a stale exemption is a hole"
        )
        stale = sorted(LEG_A_HOST_EXEMPTION[host] - references[host])
        assert not stale, (
            f"{host} no longer mentions {stale}; drop it from the exemption "
            f"rather than leaving the slot permitted there"
        )
    print(f"PASS: no engine source names a UI-side slot (exempt: {hosts})")


def report_measurement(runtime_reads: set, references: dict) -> None:
    """Print runtime and static surfaces on every run for allow-list comparison."""
    print("--- measured engine gl surface ---")
    print(f"  runtime: {sorted({slot for slot, _ in runtime_reads})}")
    static = sorted(set().union(*references.values())) if references else []
    print(f"  static:  {static}")
    print(f"  modules: {len(references)} engine sources reference globals")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_engine_gl_surface")

    check_recorder_records_and_restores()

    with _recording():
        drive_engine_workload()
    assert type(gl) is types.ModuleType, (
        f"the recorder outlived the drive: {type(gl)}"
    )

    runtime_reads = engine_runtime_reads()
    references = static_engine_references()
    report_measurement(runtime_reads, references)

    check_recorder_saw_engine(runtime_reads)
    check_runtime_surface(runtime_reads)
    check_required_absent(runtime_reads)
    check_static_surface(references)
    check_static_absent(references)

    print("ALL PASS: scenario_engine_gl_surface")


if __name__ == "__main__":
    main()
