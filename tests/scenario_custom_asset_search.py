"""Verify custom-asset filtering, ranking, and flow-box hook installation."""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import ast
import importlib
import inspect
import os
import time
import types

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

import globals as gl

from src.windows.AssetManager.CustomAssets.FlowBox import CustomAssetChooserFlowBox
from src.windows.AssetManager.DynamicFlowBox import DynamicFlowBox


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# The asset corpus

# is_video() stats the path, so every asset needs a file on disk. A preview
# decodes the thumbnail, so every thumbnail is a real PNG.
IMAGE_NAMES = ["volume_up", "volume_down", "brightness"]
VIDEO_NAMES = ["clip", "movie"]

ALPHABETICAL = ["brightness", "clip", "movie", "volume_down", "volume_up"]

# Assert rank order rather than raw scores so scoring changes remain visible.


def build_corpus() -> list[dict]:
    """Write the asset files into the isolated data dir and describe them."""
    asset_dir = os.path.join(gl.DATA_PATH, "custom-asset-search")
    assets = []
    for name in IMAGE_NAMES:
        path = fixtures.make_test_png(os.path.join(asset_dir, f"{name}.png"))
        assets.append({"name": name, "internal-path": path, "thumbnail": path})
    for name in VIDEO_NAMES:
        path = os.path.join(asset_dir, f"{name}.mp4")
        with open(path, "wb") as handle:
            handle.write(b"\0")
        thumbnail = fixtures.make_test_png(
            os.path.join(asset_dir, f"{name}-thumb.png"))
        assets.append({"name": name, "internal-path": path,
                       "thumbnail": thumbnail})
    return assets


class FakeToggle:
    def __init__(self, active: bool):
        self.active = active

    def get_active(self) -> bool:
        return self.active


class FakeChooser:
    """The chooser page reduced to what the two hooks read off it."""

    def __init__(self):
        self.text = ""
        self.search_entry = types.SimpleNamespace(get_text=lambda: self.text)
        self.image_button = FakeToggle(True)
        self.video_button = FakeToggle(True)

    def query(self, search: str, images: bool = True, videos: bool = True) -> None:
        self.text = search
        self.image_button.active = images
        self.video_button.active = videos


class StubFlow:
    """Run the real filter and sort hooks without GTK.

    The separate wiring check covers the constructor bindings this stub supplies.
    """

    filter_items = DynamicFlowBox.filter_items
    sort_items = DynamicFlowBox.sort_items
    get_items_to_show = DynamicFlowBox.get_items_to_show

    def __init__(self, asset_chooser: FakeChooser):
        self.asset_chooser = asset_chooser
        self.items: list[dict] = []
        self.filter_func = CustomAssetChooserFlowBox._filter_asset.__get__(self)
        self.sort_func = CustomAssetChooserFlowBox._sort_assets.__get__(self)


def pump_until(condition, timeout: float, what: str) -> None:
    """Service the main context until the recycler satisfies a condition."""
    context = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        while context.iteration(False):
            pass
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout}s: {what}")


def names_of(assets) -> list[str]:
    return [asset["name"] for asset in assets]


def class_def_of(cls: type) -> ast.ClassDef:
    """The class body of cls, read from the file it was defined in."""
    source_file = inspect.getsourcefile(cls)
    assert source_file is not None, f"no source file for {cls.__name__}"
    with open(source_file, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), source_file)
    return next(node for node in ast.walk(tree)
                if isinstance(node, ast.ClassDef) and node.name == cls.__name__)


# 1. The constructor hands every hook to the base. No display needed.

# The base setter, and the slot it writes.
INSTALL_CALLS = {
    "set_filter_func": "filter_func",
    "set_sort_func": "sort_func",
    "set_factory": "factory_func",
}


def check_install_wiring() -> None:
    """Require the constructor to pass each local hook to its base setter."""
    node = class_def_of(CustomAssetChooserFlowBox)
    methods = {child.name for child in node.body
               if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))}
    init = next((child for child in node.body
                 if isinstance(child, ast.FunctionDef) and child.name == "__init__"), None)
    assert init is not None, "CustomAssetChooserFlowBox defines no __init__"

    installed: dict[str, str | None] = {}
    for call in ast.walk(init):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
                and func.attr in INSTALL_CALLS):
            continue
        argument = call.args[0] if call.args else None
        if (isinstance(argument, ast.Attribute)
                and isinstance(argument.value, ast.Name)
                and argument.value.id == "self"):
            installed[func.attr] = argument.attr
        else:
            installed[func.attr] = None

    for setter, slot in sorted(INSTALL_CALLS.items()):
        assert setter in installed, (
            f"the constructor never calls {setter}, so the base keeps None in "
            f"{slot} and the hook never runs")
        handed = installed[setter]
        assert handed is not None, (
            f"{setter} is handed something other than one of this class's own "
            f"methods, so nothing here can say which code the {slot} slot gets")
        assert handed in methods, (
            f"{setter} is handed self.{handed}, which CustomAssetChooserFlowBox "
            f"does not define as a method; the {slot} slot gets whatever the "
            f"base last put in that attribute, which is None")
    print(f"PASS: the constructor hands all {len(INSTALL_CALLS)} hooks to the "
          f"base setters by name")


# 2. The hooks reach the flow box as callables.

def check_hooks_installed(flow: CustomAssetChooserFlowBox) -> None:
    """Require callable local filter, sort, and factory hooks."""
    assert callable(flow.filter_func), (
        "the flow box installed no filter hook (filter_func is "
        f"{flow.filter_func!r}) -- the search entry and the image/video "
        "toggles cannot reach the grid")
    assert callable(flow.sort_func), (
        "the flow box installed no sort hook (sort_func is "
        f"{flow.sort_func!r}) -- the grid keeps backend order")
    assert callable(flow.factory_func), "the flow box installed no factory"

    assert flow.filter_func == flow._filter_asset, (
        f"the filter hook is {flow.filter_func!r}, not this box's "
        "_filter_asset")
    assert flow.sort_func == flow._sort_assets, (
        f"the sort hook is {flow.sort_func!r}, not this box's _sort_assets")
    print("PASS: the flow box installs its own filter and sort hooks")


# 3. The hooks filter and order what the grid asks for.

def check_search_and_kind(flow, chooser: FakeChooser, corpus: list[dict],
                          label: str) -> None:
    """Drive the base's filter and sort through the installed hooks."""
    flow.items = list(corpus)

    chooser.query("")
    got = names_of(flow.get_items_to_show())
    assert got == ALPHABETICAL, (
        f"{label}: an empty query must keep every asset and list it as "
        f"{ALPHABETICAL}, got {got}")

    chooser.query("volume")
    got = names_of(flow.get_items_to_show())
    assert got == ["volume_up", "volume_down"], (
        f"{label}: the search text must keep the two matches and rank the "
        f"closer name first, got {got}")

    chooser.query("clip")
    got = names_of(flow.get_items_to_show())
    assert got == ["clip"], (
        f"{label}: a query matching one name must keep only it, got {got}")

    chooser.query("zzzz")
    got = names_of(flow.get_items_to_show())
    assert got == [], (
        f"{label}: a query matching nothing must empty the grid, got {got}")

    # The kind toggles run through the same filter, on the file extension.
    chooser.query("", images=True, videos=False)
    got = names_of(flow.get_items_to_show())
    assert got == ["brightness", "volume_down", "volume_up"], (
        f"{label}: the video toggle off must leave the images only, got {got}")

    chooser.query("", images=False, videos=True)
    got = names_of(flow.get_items_to_show())
    assert got == ["clip", "movie"], (
        f"{label}: the image toggle off must leave the videos only, got {got}")

    chooser.query("", images=False, videos=False)
    got = names_of(flow.get_items_to_show())
    assert got == [], (
        f"{label}: both toggles off must empty the grid, got {got}")

    # Each half on its own, so a pass here cannot come from one hook doing
    # both jobs.
    chooser.query("volume")
    got = names_of(flow.filter_items(corpus))
    assert got == ["volume_up", "volume_down"], (
        f"{label}: filter_items must keep the two matches, got {got}")
    chooser.query("")
    got = names_of(flow.sort_items(corpus))
    assert got == ALPHABETICAL, (
        f"{label}: sort_items must order the corpus as {ALPHABETICAL}, got "
        f"{got}")
    print(f"PASS: {label} filters on the search text and the asset kind, and "
          f"orders by name and by score")


# 4. The real widget renders a filtered and sorted grid.

def visible_names(flow: CustomAssetChooserFlowBox) -> list[str]:
    """Return visible names and mark recycler children that are not yet bound."""
    names = []
    for index in range(flow.N_ITEMS_PER_PAGE):
        child = flow.flow_box.get_child_at_index(index)
        if child is None:
            break
        if not child.get_visible():
            continue
        asset = getattr(child, "asset", None)
        names.append("<unbound>" if asset is None else asset["name"])
    return names


def check_real_grid(flow: CustomAssetChooserFlowBox, chooser: FakeChooser,
                    corpus: list[dict]) -> None:
    """Require the rendered grid to follow search text and kind toggles."""
    pump_until(lambda: len(visible_names(flow)) == len(corpus), 10,
               "the recycler never rendered the assets")
    got = visible_names(flow)
    assert got == ALPHABETICAL, (
        f"the grid must list the assets as {ALPHABETICAL}, it shows {got}")

    chooser.query("volume")
    flow.refresh()
    pump_until(lambda: visible_names(flow) == ["volume_up", "volume_down"], 10,
               f"the grid must show ['volume_up', 'volume_down'] for the query "
               f"'volume', it shows {visible_names(flow)}")

    chooser.query("volume", images=False, videos=True)
    flow.refresh()
    pump_until(lambda: visible_names(flow) == [], 10,
               f"the grid must empty when the image toggle drops the two "
               f"matches, it shows {visible_names(flow)}")

    chooser.query("")
    flow.refresh()
    pump_until(lambda: visible_names(flow) == ALPHABETICAL, 10,
               f"the grid must return to {ALPHABETICAL} once the search is "
               f"cleared, it shows {visible_names(flow)}")
    print("PASS: the real grid follows the search text and the kind toggles")


# 5. Tripwire: no subclass may supply a name the base keeps a hook in.

# Each entry names a base and only the constructor slots that store hooks.
BASES = (
    ("src.windows.AssetManager.DynamicFlowBox", "DynamicFlowBox",
     frozenset({"filter_func", "sort_func", "factory_func"})),
    # Keep this base covered before its first subclass appears.
    ("GtkHelper.DynamicFlowBox", "DynamicFlowBox",
     frozenset({"filter", "sort", "factory"})),
)

# Directories the scan does not enter. .claude is a symlink back to the repo.
SKIP_DIRS = {".git", ".venv", ".claude", "__pycache__", "tests", "flatpak",
             "locales", "Assets"}

# CustomAssetChooserFlowBox and GenericAssetFlowBox. A drop below this means
# the scan stopped finding the subclasses and now passes over nothing.
MIN_SUBCLASSES_CHECKED = 2


def assigned_attr_names(cls: type) -> set[str]:
    """Return names assigned on self in the class source."""
    names: set[str] = set()
    for child in ast.walk(class_def_of(cls)):
        if isinstance(child, ast.Assign):
            targets: list = list(child.targets)
        elif isinstance(child, ast.AnnAssign):
            targets = [child.target]
        else:
            continue
        for target in targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                names.add(target.attr)
    return names


def base_name_of(expr: ast.expr) -> str | None:
    """The bare name of a base-class expression, generics and dots removed."""
    if isinstance(expr, ast.Subscript):
        expr = expr.value
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def repo_class_defs() -> list[tuple[str, ast.ClassDef]]:
    """Every class in the tree, with the module it lives in."""
    found = []
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
                    found.append((module, node))
    return found


def subclasses_in_tree(root_name: str) -> list[type]:
    """Import source-visible descendants of a named base to a fixed point."""
    class_defs = repo_class_defs()
    sites: set[tuple[str, str]] = set()
    names = {root_name}
    growing = True
    while growing:
        growing = False
        for module, node in class_defs:
            if node.name == root_name or (module, node.name) in sites:
                continue
            if any(base_name_of(base) in names for base in node.bases):
                sites.add((module, node.name))
                names.add(node.name)
                growing = True

    found = []
    for module, name in sorted(sites):
        cls = getattr(importlib.import_module(module), name, None)
        if cls is None:
            print(f"NOTE: {module}.{name} descends from {root_name} in the "
                  f"source but is not a module-level name (nested, or renamed "
                  f"on import); the source half skips it and the runtime half "
                  f"covers it once it is built")
            continue
        found.append(cls)
    return found


def runtime_subclasses(base: type) -> set[type]:
    """Every loaded subclass of base, minus this scenario's own fakes."""
    found: set[type] = set()
    pending = list(base.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__module__ == __name__:
            continue
        found.add(cls)
    return found


def shadowed_names(cls: type, base: type, hooks: frozenset[str]) -> list[str]:
    """Return hook slots supplied between a subclass and its assigning base."""
    from_base = set(base.__mro__)
    found: set[str] = set()
    for klass in cls.__mro__:
        if klass in from_base:
            continue
        found |= {name for name in vars(klass)} & hooks
    return sorted(found)


def check_tripwire_self_test() -> None:
    """Detect planted direct and mixin hooks but allow a non-hook default."""
    hooks = BASES[0][2]

    class _ShadowingFlowBox(DynamicFlowBox):
        # The base assigns self.filter_func, so this never survives
        # construction. runtime_subclasses drops these three by module.
        def filter_func(self, asset: dict) -> bool:
            return True

    class _HookMixin:
        def sort_func(self, a: dict, b: dict) -> int:
            return 0

    class _MixinFlowBox(DynamicFlowBox, _HookMixin):
        pass

    class _DefaultingFlowBox(DynamicFlowBox):
        # show_range reads this default before assigning it; the constructor does not.
        current_start_index = 0

    found = shadowed_names(_ShadowingFlowBox, DynamicFlowBox, hooks)
    assert found == ["filter_func"], (
        f"the check missed a hook planted on the class, it reported {found}")

    found = shadowed_names(_MixinFlowBox, DynamicFlowBox, hooks)
    assert found == ["sort_func"], (
        f"the check missed a hook planted on a mixin, it reported {found}")

    found = shadowed_names(_DefaultingFlowBox, DynamicFlowBox, hooks)
    assert found == [], (
        f"the check refused a class-level default for a name that is not a "
        f"hook slot, it reported {found}")
    assert "current_start_index" in assigned_attr_names(DynamicFlowBox), (
        "the base no longer assigns current_start_index, so the acceptance "
        "above proves nothing")
    print("PASS: the shadowing check reports a planted hook, sees one a mixin "
          "brings in, and accepts a non-hook default")


def check_no_shadowed_hooks() -> None:
    checked = 0
    from_source_total = 0
    offences = []
    for module_path, class_name, hooks in BASES:
        base = getattr(importlib.import_module(module_path), class_name)
        missing = sorted(hooks - assigned_attr_names(base))
        assert not missing, (
            f"{module_path}.{class_name} no longer assigns {missing} onto "
            f"self; the hook slots in the table above have been renamed or "
            f"moved and this check now guards the wrong names")

        from_source = {cls for cls in subclasses_in_tree(class_name)
                       if issubclass(cls, base)}
        from_source_total += len(from_source)
        # Combine source-only classes with alias-inherited and dynamic runtime classes.
        for cls in sorted(from_source | runtime_subclasses(base),
                          key=lambda c: (c.__module__, c.__name__)):
            checked += 1
            names = shadowed_names(cls, base, hooks)
            if names:
                offences.append(
                    f"{cls.__module__}.{cls.__name__} supplies {names}")

    assert not offences, (
        "a flow-box hook is shadowed: the base assigns the same name onto "
        "every instance in its constructor, so the install reads the "
        "attribute and puts None back into the slot, and the hook never "
        "runs: " + "; ".join(offences))
    assert checked >= MIN_SUBCLASSES_CHECKED, (
        f"the shadowing check only looked at {checked} flow-box subclasses, "
        f"expected at least {MIN_SUBCLASSES_CHECKED} -- it has gone vacuous")
    assert from_source_total >= MIN_SUBCLASSES_CHECKED, (
        f"the source scan contributed only {from_source_total} subclasses, so "
        f"the check now rests on whichever modules happen to be imported")
    print(f"PASS: none of the {checked} flow-box subclasses supplies a name "
          f"its base keeps a hook in")


def has_display() -> bool:
    """Require a default GDK display before constructing widgets."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


def main() -> int:
    fixtures.start_watchdog(60, label="scenario_custom_asset_search")

    check_install_wiring()
    check_tripwire_self_test()
    check_no_shadowed_hooks()

    gl.lm = types.SimpleNamespace(get=lambda key, *a, **k: key)
    corpus = build_corpus()
    chooser = FakeChooser()

    if has_display():
        Adw.init()

        class Backend:
            def get_all(self): return list(corpus)

        gl.asset_manager_backend = Backend()
        flow = CustomAssetChooserFlowBox(chooser)
        check_hooks_installed(flow)
        check_real_grid(flow, chooser, corpus)
        check_search_and_kind(flow, chooser, corpus, "the real flow box")
    else:
        print("SKIP(real-widget): no display; the hooks run against a stub and "
              "the wiring check above covers the install lines")
        check_search_and_kind(StubFlow(chooser), chooser, corpus,
                              "the hooks over a stub")

    print("ALL PASS: scenario_custom_asset_search")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
