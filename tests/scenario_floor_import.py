"""Check that engine modules import on Python 3.13 with evaluated annotations."""
import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

import os
import subprocess
import sys

WATCHDOG_SECONDS = 60

FLOOR_PYTHON = "/usr/bin/python3.13"
FLOOR_VERSION = (3, 13)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(_REPO_ROOT, "src", "backend", "DeckManagement", "deck_controller")
COMPAT_SHIM = os.path.join(_REPO_ROOT, "src", "backend", "DeckManagement", "DeckController.py")

# Modules outside the package with engine-closure or distinct floor-path contracts
EXTRA_MODULES = (
    # The app-ready deferral protocol imports globals and stdlib only.
    os.path.join(_REPO_ROOT, "src", "backend", "startup_queue.py"),
    # Control plane imported by the deck controller
    os.path.join(_REPO_ROOT, "src", "backend", "control_plane.py"),
    # CLI forwarding is a distinct deployment-floor path, not part of the engine closure.
    os.path.join(_REPO_ROOT, "src", "backend", "cli_forward.py"),
    # CLI fast path is distinct; main imports some modules before reaching it.
    os.path.join(_REPO_ROOT, "src", "backend", "cli_fast_path.py"),
    # Typed global accessors with TYPE_CHECKING-only application types
    os.path.join(_REPO_ROOT, "src", "backend", "services.py"),
    # Page flush seam imported by Page
    os.path.join(_REPO_ROOT, "src", "backend", "PageManagement", "page_flush.py"),
    # Page document used for all page content reads
    os.path.join(_REPO_ROOT, "src", "backend", "PageManagement", "page_document.py"),
    # Settings store reached through the engine's settings manager
    os.path.join(_REPO_ROOT, "src", "backend", "settings_store.py"),
    # Settings views imported by the store as compatibility exports
    os.path.join(_REPO_ROOT, "src", "backend", "settings_views.py"),
    # Page-cache pins reached by deck-controller tick and key paths
    os.path.join(_REPO_ROOT, "src", "backend", "PageManagement", "page_pins.py"),
)

# Child program that imports each requested module under stubs
_CHILD = r'''
import ast, importlib.abc, importlib.machinery, os, sys, traceback, types


class _AnyMeta(type):
    """Metaclass so stubs answer attribute access, subscripting, `|` unions and
    decoration -- everything a module body does to an imported name."""

    def __getattr__(cls, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        sub = _AnyMeta(f"{cls.__name__}.{name}", (_Any,), {})
        setattr(cls, name, sub)
        return sub

    def __getitem__(cls, item):
        return cls

    def __call__(cls, *a, **k):
        # Usable bare or called: @log.catch and @log.catch(...) both work.
        if len(a) == 1 and not k and callable(a[0]) and not isinstance(a[0], _AnyMeta):
            return a[0]
        return super().__call__(*a, **k)

    def __or__(cls, other):
        return cls

    def __ror__(cls, other):
        return cls

    def __iter__(cls):
        return iter(())


class _Any(metaclass=_AnyMeta):
    def __init__(self, *a, **k):
        # Constructed with whatever the module body passes: several modules
        # build real objects at import (a measuring canvas, budget constants).
        pass

    def __getattr__(self, name):
        return _Any

    def __call__(self, *a, **k):
        if len(a) == 1 and not k and callable(a[0]):
            return a[0]
        return _Any()


class _Stub(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        v = _AnyMeta(name, (_Any,), {"__module__": self.__name__})
        setattr(self, name, v)
        return v


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, roots):
        self.roots = roots

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.roots:
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        return _Stub(spec.name)

    def exec_module(self, module):
        module.__path__ = []          # every stub is a package, so submodules resolve
        module.TYPE_CHECKING = False


def _import_roots(path):
    """Top-level import roots of one module that are not stdlib -- i.e. exactly
    what has to be stubbed for its body to execute here."""
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return {r for r in roots if r not in sys.stdlib_module_names}


paths = sys.argv[1:]
roots = set()
for p in paths:
    roots |= _import_roots(p)
sys.meta_path.insert(0, _Finder(roots))

print("FLOOR %d.%d" % sys.version_info[:2])
print("STUBBED " + ",".join(sorted(roots)))

for p in paths:
    stem = os.path.splitext(os.path.basename(p))[0]
    mod = types.ModuleType("floorcheck_" + stem)
    mod.__file__ = p
    # @dataclass resolves the defining module through sys.modules, so the
    # module has to be registered there before its body runs.
    sys.modules[mod.__name__] = mod
    try:
        # optimize=1 strips `assert` statements. A module body that asserts
        # over real structure (the controller's registry-completeness check)
        # cannot hold when every value it compares is a stub, and that is a
        # limitation of the stubbing, not a floor defect. Those asserts run
        # for real on every ordinary import; what is being exercised here is
        # the definitions, not the module's self-checks.
        exec(compile(open(p, encoding="utf-8").read(), p, "exec", optimize=1), mod.__dict__)
    except BaseException as e:
        frame = traceback.extract_tb(sys.exc_info()[2])[-1]
        print("FAIL %s %s: %s @ %s:%d"
              % (stem, type(e).__name__, e, os.path.basename(frame.filename), frame.lineno))
    else:
        # A re-export shim's own failure mode: __all__ drifts away from the
        # import block above it and promises a name nothing binds.
        missing = [n for n in getattr(mod, "__all__", ()) if not hasattr(mod, n)]
        if missing:
            print("FAIL %s __all__ promises names the module does not bind: %s"
                  % (stem, ", ".join(missing)))
        else:
            public = [n for n in mod.__dict__ if not n.startswith("_")]
            print("OK %s (%d names bound)" % (stem, len(public)))
'''


def discover_modules() -> list[str]:
    """Discover package modules, the compatibility shim, and extra closure modules."""
    assert os.path.isdir(PACKAGE_DIR), f"package dir missing: {PACKAGE_DIR}"
    names = sorted(
        n for n in os.listdir(PACKAGE_DIR)
        if n.endswith(".py") and not n.startswith("_")
    )
    modules = [os.path.join(PACKAGE_DIR, n) for n in names]
    assert os.path.isfile(COMPAT_SHIM), f"compat shim missing: {COMPAT_SHIM}"
    modules.append(COMPAT_SHIM)
    for extra in EXTRA_MODULES:
        # A listed module that moved must fail here, not silently drop out of
        # the check the way a glob would.
        assert os.path.isfile(extra), (
            f"EXTRA_MODULES lists a module that does not exist: {extra}. "
            f"Update the list in the change that moved or removed it."
        )
        modules.append(extra)
    return modules


def check_package_bodies_execute_on_floor() -> None:
    modules = discover_modules()
    # A silent glob miss would make every assertion below vacuous, so the count
    # is asserted before anything runs.
    assert modules, f"no modules discovered in {PACKAGE_DIR}: this check would pass vacuously"
    print(f"floor check covers {len(modules)} module(s): "
          + ", ".join(os.path.basename(m) for m in modules))

    proc = subprocess.run(
        [FLOOR_PYTHON, "-c", _CHILD, *modules],
        capture_output=True, text=True, timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"the floor interpreter itself failed:\n{out}"

    lines = [ln for ln in out.splitlines() if ln.strip()]
    version = next((ln for ln in lines if ln.startswith("FLOOR ")), "")
    assert version == "FLOOR %d.%d" % FLOOR_VERSION, (
        f"{FLOOR_PYTHON} does not report the floor version: {version!r}\n{out}"
    )

    failures = [ln for ln in lines if ln.startswith("FAIL ")]
    assert not failures, (
        "a covered module cannot be imported on the Python "
        f"{FLOOR_VERSION[0]}.{FLOOR_VERSION[1]} floor. Either an annotation in "
        "an evaluated position (parameter, return, class body or base list) "
        "names something the module does not bind at runtime -- quote it, or "
        "import it at runtime -- or an __all__ entry has no matching import:"
        "\n  " + "\n  ".join(failures)
    )

    ok = [ln for ln in lines if ln.startswith("OK ")]
    assert len(ok) == len(modules), (
        f"expected {len(modules)} module results, got {len(ok)}:\n{out}"
    )
    for ln in ok:
        print("  " + ln)
    print(f"PASS: every covered module body executes on Python "
          f"{FLOOR_VERSION[0]}.{FLOOR_VERSION[1]}")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_floor_import")
    if not os.path.exists(FLOOR_PYTHON):
        print(f"NOTE: {FLOOR_PYTHON} is not installed -- skipping the "
              f"deployment-floor import check. Install a "
              f"{FLOOR_VERSION[0]}.{FLOOR_VERSION[1]} interpreter to run it.")
        print("SKIP: scenario_floor_import")
        return
    check_package_bodies_execute_on_floor()
    print("ALL PASS: scenario_floor_import")


if __name__ == "__main__":
    sys.exit(main())
