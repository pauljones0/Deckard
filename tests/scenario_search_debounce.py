"""Pins the debounce and the generation guard of the chooser search boxes.

Every page of the AssetManager carries a search entry, and the base defers the
work until the typing stops. Without the deferral each keystroke scores and
sorts a whole icon pack, which is thousands of names, and typing stutters.

The checks drive the base methods over a stand-in page, so no widget and no
display is needed. The main context still dispatches the timeouts, which is
what the deferral rides on.
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
from gi.repository import GLib

from src.windows.AssetManager.ChooserPage import SEARCH_DEBOUNCE_MS, ChooserPage


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Directories the subclass scan does not enter. .claude is a symlink back to
# the repo.
SKIP_DIRS = {".git", ".venv", ".claude", "__pycache__", "tests", "flatpak",
             "locales", "Assets"}

# The pack pages, the three leaf pages and the custom-asset page. A drop below
# this means the scan stopped finding them and now passes over nothing.
MIN_SUBCLASSES_SCANNED = 7


class FakePage:
    """A chooser page reduced to what the deferral touches.

    It takes the base methods themselves, so a change to either reaches these
    checks. apply_search is the hook a real page overrides.
    """

    on_search_changed = ChooserPage.on_search_changed
    _run_deferred_search = ChooserPage._run_deferred_search
    _search_generation = ChooserPage._search_generation
    _search_timeout_id = ChooserPage._search_timeout_id

    def __init__(self) -> None:
        self.text = ""
        self.applied: list[str] = []
        self.search_entry = types.SimpleNamespace(get_text=lambda: self.text)

    def apply_search(self, query: str) -> None:
        self.applied.append(query)

    def type(self, text: str) -> None:
        """One keystroke: the entry holds the new text and emits."""
        self.text = text
        self.on_search_changed(None)


def pump_until(condition, timeout: float, what: str) -> None:
    """Iterate the default main context until condition() holds.

    The deferral runs from a timeout, so it only fires while this thread
    services the main context.
    """
    context = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        while context.iteration(False):
            pass
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout}s: {what}")


def drain(page: FakePage) -> None:
    """Wait for the pending search of page, if it has one."""
    if page._search_timeout_id:
        pump_until(lambda: page._search_timeout_id == 0, 10,
                   "the deferred search never ran")


def test_base_holds_the_defaults() -> None:
    """Both counters must be class attributes.

    ChooserPage._build connects on_search_changed from the constructor, before
    a subclass reaches its own attributes, so a keystroke can arrive before
    any instance attribute exists.
    """
    for name in ("_search_generation", "_search_timeout_id"):
        assert name in vars(ChooserPage), (
            f"ChooserPage no longer declares {name} on the class; a keystroke "
            f"during the build then raises AttributeError")
        assert vars(ChooserPage)[name] == 0
    print("PASS: the base declares both search counters on the class")


def test_nothing_runs_before_the_loop() -> None:
    page = FakePage()
    page.type("b")
    # No iteration of the main context has happened, and a timeout dispatches
    # from there only.
    assert page.applied == [], (
        f"the search ran inside the keystroke: {page.applied}")
    assert page._search_timeout_id != 0, "the keystroke queued no search"
    drain(page)
    assert page.applied == ["b"], f"the deferred search did not run: {page.applied}"
    assert page._search_timeout_id == 0, "the finished timeout kept its source id"
    print("PASS: a keystroke queues the search and the main loop runs it")


def test_a_burst_searches_once() -> None:
    """Three keystrokes with no loop between them cost one search."""
    page = FakePage()
    page.type("b")
    page.type("ba")
    page.type("bat")
    drain(page)
    assert page.applied == ["bat"], (
        f"a burst of three keystrokes searched {page.applied}; it must search "
        f"once, for the text typed last")

    # The next burst searches again, so the deferral does not swallow it.
    page.type("batt")
    drain(page)
    assert page.applied == ["bat", "batt"], page.applied
    print("PASS: a burst of keystrokes costs one search, for the last text")


def test_generation_guard_drops_a_stale_pass() -> None:
    """A callback that a later keystroke overtook must render nothing.

    Removing the source is not enough on its own: a timeout that already fired
    sits in the queue with its own generation, and it would render a query the
    user has replaced.
    """
    page = FakePage()
    page.type("x")
    stale = page._search_generation
    page.type("xy")
    assert page._search_generation != stale, "the keystroke did not move the generation"

    # The stale callback, dispatched by hand as the main loop would.
    kept = page._search_timeout_id
    assert page._run_deferred_search(stale) is False, (
        "the deferred search must be a one-shot timeout")
    assert page.applied == [], f"a stale pass rendered {page.applied}"

    # It cleared the id of the live pass, so restore what the loop holds and
    # let the live one land.
    page._search_timeout_id = kept
    drain(page)
    assert page.applied == ["xy"], f"the live pass rendered {page.applied}"
    print("PASS: a pass that a later keystroke overtook renders nothing")


def test_debounce_interval() -> None:
    assert SEARCH_DEBOUNCE_MS == 150, "the search debounce interval moved"
    source = textwrap.dedent(inspect.getsource(ChooserPage.on_search_changed))
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "timeout_add"]
    assert len(calls) == 1, (
        f"on_search_changed makes {len(calls)} timeout_add calls; the "
        f"deferral is one timeout")
    first = calls[0].args[0]
    assert isinstance(first, ast.Name) and first.id == "SEARCH_DEBOUNCE_MS", (
        "the deferral no longer waits SEARCH_DEBOUNCE_MS, so the constant "
        "above says nothing about what ships")
    print(f"PASS: the search waits {SEARCH_DEBOUNCE_MS} ms after the last keystroke")


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

    An override of the handler bypasses the deferral and the generation guard
    for that page, and nothing else would report it.
    """
    subclasses = subclass_defs()
    assert len(subclasses) >= MIN_SUBCLASSES_SCANNED, (
        f"the scan found {len(subclasses)} ChooserPage subclasses, fewer than "
        f"the {MIN_SUBCLASSES_SCANNED} in the tree; it now passes over nothing")

    offences = []
    for module, node in subclasses:
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and child.name == "on_search_changed":
                offences.append(f"{module}.{node.name}")
    assert not offences, (
        "these pages override on_search_changed and so lose the debounce and "
        "the generation guard; override apply_search instead: "
        + ", ".join(sorted(offences)))
    print(f"PASS: none of the {len(subclasses)} chooser pages overrides the "
          f"search handler")


def main() -> int:
    fixtures.start_watchdog(60, label="scenario_search_debounce")

    test_base_holds_the_defaults()
    test_nothing_runs_before_the_loop()
    test_a_burst_searches_once()
    test_generation_guard_drops_a_stale_pass()
    test_debounce_interval()
    test_no_page_overrides_the_handler()

    print("ALL PASS: scenario_search_debounce")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
