"""Pins how a chooser page decides when to search, and when to stop.

Gtk.SearchEntry owns the wait for the typing to stop: it holds its
search-changed emission back for its own delay, and the page widens that
delay because one pass scores and sorts a whole icon pack. The page owns two
things only. A pass carries the generation it was queued with, so two
emissions in one turn of the loop cost one pass. And a page that stops showing
invalidates every pass in flight, because the entry can deliver an emission
after the window has gone.

The pure-logic legs drive the base methods over a stand-in page. The legs that
prove the hooks fire build a real page in a real window, which is what a
teardown hook connected to the wrong signal fails.
"""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import ast
import inspect
import os
import textwrap
import time
import types

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk

import globals as gl

gl.lm = types.SimpleNamespace(get=lambda key, *a, **k: key)

from src.windows.AssetManager import asset_search
from src.windows.AssetManager.ChooserPage import SEARCH_DELAY_MS, ChooserPage


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Directories the subclass scan does not enter. .claude is a symlink back to
# the repo.
SKIP_DIRS = {".git", ".venv", ".claude", "__pycache__", "tests", "flatpak",
             "locales", "Assets"}

# The pack pages, the three leaf pages and the custom-asset page. A drop below
# this means the scan stopped finding them and now passes over nothing.
MIN_SUBCLASSES_SCANNED = 7


class FakePage:
    """A chooser page reduced to what the search decision touches.

    It takes the base methods themselves, so a change to either reaches these
    checks. apply_search is the hook a real page overrides.
    """

    on_search_changed = ChooserPage.on_search_changed
    run_search = ChooserPage.run_search
    invalidate_search = ChooserPage.invalidate_search
    _on_map = ChooserPage._on_map
    _search_generation = ChooserPage._search_generation
    _search_showing = ChooserPage._search_showing

    def __init__(self) -> None:
        self.text = ""
        self.applied: list[str] = []
        self.search_entry = types.SimpleNamespace(get_text=lambda: self.text)

    def apply_search(self, query: str) -> None:
        self.applied.append(query)

    def type(self, text: str) -> None:
        """What the entry does once its own delay has run out."""
        self.text = text
        self.on_search_changed(None)


class RecordingPage(ChooserPage):
    """A real page that records the passes it is asked to render."""

    def __init__(self) -> None:
        self.applied: list[str] = []
        super().__init__()

    def apply_search(self, query: str) -> None:
        self.applied.append(query)


def pump(seconds: float = 0.2) -> None:
    """Service the main context for a while."""
    context = GLib.MainContext.default()
    deadline = time.time() + seconds
    while time.time() < deadline:
        while context.iteration(False):
            pass
        time.sleep(0.005)


def pump_until(condition, timeout: float, what: str) -> None:
    context = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        while context.iteration(False):
            pass
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout}s: {what}")


def test_base_holds_the_defaults() -> None:
    """Both must be class attributes.

    ChooserPage._build connects on_search_changed from the constructor, before
    a subclass reaches its own attributes, so an emission can arrive before any
    instance attribute exists.
    """
    for name, expected in (("_search_generation", 0), ("_search_showing", True)):
        assert name in vars(ChooserPage), (
            f"ChooserPage no longer declares {name} on the class; an emission "
            f"during the build then raises AttributeError")
        assert vars(ChooserPage)[name] == expected
    print("PASS: the base declares its search state on the class")


def test_the_entry_owns_the_wait() -> None:
    """The page must not hold a timer of its own beside the entry's.

    Gtk.SearchEntry already waits for the typing to stop. A second wait on top
    of it coalesces nothing and only delays the answer, and it delays a
    cleared entry too, which GTK reports at once.
    """
    page = RecordingPage()
    assert page.search_entry.get_search_delay() == SEARCH_DELAY_MS, (
        f"the entry waits {page.search_entry.get_search_delay()} ms, not the "
        f"{SEARCH_DELAY_MS} ms this page asks for")

    source = inspect.getsource(ChooserPage)
    tree = ast.parse(textwrap.dedent(source))
    timers = [node for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)
              and node.func.attr in ("timeout_add", "timeout_add_seconds")]
    assert timers == [], (
        "the page runs a timer of its own again; the entry's own delay is the "
        "wait, and a second one only adds latency")
    print(f"PASS: the entry waits {SEARCH_DELAY_MS} ms and the page adds no "
          f"timer")


def test_generation_guard_drops_an_overtaken_pass() -> None:
    """Two emissions in one turn of the loop cost one pass.

    A cleared entry reports at once, so it can land in the same turn as a
    delayed emission. Only the newer of the two may render.
    """
    page = FakePage()
    page.type("bat")
    page.type("batt")
    assert page.applied == [], "a pass ran before the loop serviced it"
    pump()
    assert page.applied == ["batt"], (
        f"two emissions in one turn rendered {page.applied}; only the last "
        f"query may reach the grid")

    # A pass dispatched with a generation that has moved on renders nothing.
    stale = page._search_generation
    page.type("battery")
    assert page.run_search(stale) is False, "the pass must be a one-shot idle"
    assert page.applied == ["batt"], f"a stale pass rendered {page.applied}"
    pump()
    assert page.applied == ["batt", "battery"], page.applied
    print("PASS: a pass that a later one overtook renders nothing")


def test_invalidate_stops_passes_and_frees_the_cache() -> None:
    page = FakePage()
    page.type("bat")

    # Build a cached ranker, as a pass over a pack does.
    asset_search.ranker("bat").rank_key("battery_full")
    assert asset_search._cached_ranker is not None

    page.invalidate_search()
    assert page._search_showing is False
    assert asset_search._cached_ranker is None, (
        "the memo of a whole pack survived the page that built it")

    pump()
    assert page.applied == [], (
        f"a pass queued before the page stopped showing rendered "
        f"{page.applied}")

    # An emission that the entry held across the invalidation renders nothing.
    page.type("batt")
    pump()
    assert page.applied == [], (
        f"the entry delivered after the page stopped showing and the page "
        f"rendered {page.applied}")

    # Showing it again searches with what the entry holds now.
    page._on_map()
    pump()
    assert page.applied == ["batt"], (
        f"the page did not search again when it was shown: {page.applied}")
    print("PASS: an invalidated page stops searching, frees the memo and "
          "resumes when shown")


def test_hiding_the_window_invalidates_for_real() -> None:
    """The hook must be on a signal that fires while a pass is pending.

    A destroy-connected hook cannot: a widget that is not a window emits
    destroy from dispose, and anything still holding the page, a queued pass
    among them, keeps dispose from running. Hiding the window unmaps the page,
    which is what this leg drives.
    """
    window = Gtk.Window()
    page = RecordingPage()
    window.set_child(page)
    window.present()
    pump_until(page.get_mapped, 10, "the page never mapped")

    assert page._search_showing is True, "a mapped page is not searching"
    page.applied.clear()

    generation = page._search_generation
    window.set_visible(False)
    pump_until(lambda: not page.get_mapped(), 10, "the page never unmapped")

    assert page._search_showing is False, (
        "hiding the window left the page searching; the teardown hook is on a "
        "signal that does not fire here")
    assert page._search_generation != generation, (
        "hiding the window did not invalidate the passes in flight")

    # The page is alive and undestroyed, which is exactly why a
    # destroy-connected hook would have missed this.
    assert page.get_parent() is window, "the page left its window"

    page.search_entry.set_text("battery")
    pump()
    assert page.applied == [], (
        f"a hidden page rendered {page.applied}")

    # Showing it again picks the entry up.
    window.present()
    pump_until(page.get_mapped, 10, "the page never mapped again")
    pump()
    assert page.applied and page.applied[-1] == "battery", (
        f"the page did not search again when it was shown: {page.applied}")

    window.destroy()
    print("PASS: hiding the window stops the search and showing it resumes")


def test_hooks_are_wired() -> None:
    """The base must connect both hooks itself, or no page has them."""
    source = textwrap.dedent(inspect.getsource(ChooserPage.__init__))
    tree = ast.parse(source)
    connected = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[1], ast.Attribute)):
            connected[node.args[0].value] = node.args[1].attr

    assert connected.get("unmap") == "invalidate_search", (
        f"the page connects {connected.get('unmap')!r} to unmap; a pass that "
        f"outlives the page needs invalidate_search there")
    assert connected.get("map") == "_on_map", (
        f"the page connects {connected.get('map')!r} to map; without it a "
        f"page that was hidden never searches again")

    build = textwrap.dedent(inspect.getsource(ChooserPage._build))
    delays = [node for node in ast.walk(ast.parse(build))
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)
              and node.func.attr == "set_search_delay"]
    assert len(delays) == 1, (
        "the page no longer sets the delay of its entry, so the entry keeps "
        "the GTK default")
    assert isinstance(delays[0].args[0], ast.Name) \
        and delays[0].args[0].id == "SEARCH_DELAY_MS", (
        "the delay is set from something other than SEARCH_DELAY_MS, so the "
        "constant says nothing about what ships")
    print("PASS: the base wires map, unmap and the entry delay")


def subclass_defs() -> list[tuple[str, ast.ClassDef]]:
    """Every class in the tree that names ChooserPage among its bases.

    Source level, and to a fixed point, so a subclass of a subclass counts.
    """
    found: list[tuple[str, ast.ClassDef]] = []
    class_defs: list[tuple[str, ast.ClassDef]] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), path)
            module = os.path.relpath(path, REPO_ROOT)[:-3].replace(os.sep, ".")
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    class_defs.append((module, node))

    names = {"ChooserPage"}
    seen: set[tuple[str, str]] = set()
    growing = True
    while growing:
        growing = False
        for module, node in class_defs:
            if node.name == "ChooserPage" or (module, node.name) in seen:
                continue
            bases = []
            for base in node.bases:
                if isinstance(base, ast.Subscript):
                    base = base.value
                if isinstance(base, ast.Name):
                    bases.append(base.id)
                elif isinstance(base, ast.Attribute):
                    bases.append(base.attr)
            if any(base in names for base in bases):
                seen.add((module, node.name))
                names.add(node.name)
                found.append((module, node))
                growing = True
    return found


def test_no_page_overrides_the_handler() -> None:
    """A page overrides apply_search, never on_search_changed.

    An override of the handler bypasses the staleness guard and the
    invalidation for that page, and nothing else would report it.
    """
    subclasses = subclass_defs()
    assert len(subclasses) >= MIN_SUBCLASSES_SCANNED, (
        f"the scan found {len(subclasses)} ChooserPage subclasses, fewer than "
        f"the {MIN_SUBCLASSES_SCANNED} in the tree; it now passes over nothing")

    offences = []
    for module, node in subclasses:
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and child.name in ("on_search_changed", "run_search"):
                offences.append(f"{module}.{node.name}.{child.name}")
    assert not offences, (
        "these pages take over the search decision and so lose the staleness "
        "guard; override apply_search instead: " + ", ".join(sorted(offences)))
    print(f"PASS: none of the {len(subclasses)} chooser pages takes over the "
          f"search decision")


def main() -> int:
    fixtures.start_watchdog(90, label="scenario_search_debounce")

    test_base_holds_the_defaults()
    test_the_entry_owns_the_wait()
    test_generation_guard_drops_an_overtaken_pass()
    test_invalidate_stops_passes_and_frees_the_cache()
    test_hiding_the_window_invalidates_for_real()
    test_hooks_are_wired()
    test_no_page_overrides_the_handler()

    print("ALL PASS: scenario_search_debounce")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
