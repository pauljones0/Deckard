#!/usr/bin/env python3
"""Freeze globals.py declarations and governed gl attribute stores against explicit tables; fail closed on missing, unreadable, unparseable, symlink-hidden, or unsupported-import input.
Reject computed or deleted slots, but do not police reads, slot-content mutation, dynamic imports, laundered aliases, or behavior behind per-file module-type exemptions."""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GLOBALS_MODULE = "globals.py"
THIS_SCRIPT = "scripts/check_gl_slots.py"

# Govern app trees and tests because any process can invent a slot.
# Exclude pre-globals root modules, tooling, globals itself, and external plugins.
GOVERNED_FILES = ("main.py", "autostart.py", "cli_args.py")
GOVERNED_TREES = ("src", "GtkHelper", "locales", "tests")

# Only FROZEN_SLOTS accepts stores; each entry describes its value, not its readers.
FROZEN_SLOTS: dict[str, str] = {
    # Import-time constants and paths, resolved in globals.py's own body.
    "MAIN_PATH": "install root, assigned by main.py before anything reads it",
    "VAR_APP_PATH": "per-app data root: flatpak's ~/.var/app/<id>, else XDG",
    "STATIC_SETTINGS_FILE_PATH": "static settings file, the data-path override",
    "DATA_PATH": "data root actually in use, after argv and static settings",
    "PLUGIN_DIR": "plugin directory, overridable by env for nix packaging",
    "top_level_dir": "directory globals.py lives in, i.e. the repo/install root",
    "video_extensions": "recognised video suffixes",
    "image_extensions": "recognised raster image suffixes",
    "svg_extensions": "recognised vector image suffixes",
    "app_version": "upstream-aligned version plugin compatibility gates compare against",
    "exact_app_version_check": "whether plugin version gates demand an exact match",
    "deckard_version": "fork release version from the VERSION file, shown to users",
    "logs": "bounded ring buffer of recent log records",
    "logs_lock": "guards the log ring against concurrent iteration",
    "release_notes": "release notes markup shown after an update",
    "fallback_font": "lazily resolved fallback font, served by the module __getattr__",

    # Services, published by main.create_global_objects unless noted.
    "lm": "locale manager",
    "media_manager": "media manager",
    "asset_manager_backend": "asset store backend",
    "asset_manager": "asset manager window, only while it is open",
    "page_manager_window": "page manager window, only while it is open",
    "page_manager": "page manager backend",
    "gnome_extensions": "GNOME extension bridge",
    "settings_manager": "settings manager",
    "app": "the App instance, absent until it activates",
    "deck_manager": "deck manager, published by main() before the loop starts",
    "plugin_manager": "plugin manager",
    "icon_pack_manager": "icon pack manager",
    "wallpaper_pack_manager": "wallpaper pack manager",
    "sd_plus_bar_wallpaper_pack_manager": "SD+ touchscreen wallpaper pack manager",
    "store_backend": "plugin store backend",
    "store": "store window, only while it is open",
    "notify": "desktop notification facade",
    "signal_manager": "app-wide signal bus",
    "window_grabber": "active-window watcher",
    "wayland": "Wayland session bridge; write-only, kept for parity",
    "lock_screen_detector": "lock screen detector",
    "presence_monitor": "user presence/quiescence monitor",
    "flatpak_permission_manager": "flatpak permission manager",
    "tray_icon": "tray icon",

    # Flags and queues. This is shared state with no owning object.
    "threads_running": "cleared on shutdown so worker loops exit",
    "screen_locked": "current lock state, written by the lock screen detector",
    "showed_donate_window": "one-shot latch for the donation prompt",
    "loggers": "named logger registry, keyed by plugin",
    "app_loading_finished_tasks": "zero-arg deliveries queued until App activates",
    "api_page_requests": "page changes parked by the CLI until a deck appears",
    "api_state_requests": "state changes parked by the CLI until a deck appears",
}

# with and if bindings leak into the module but are not assignable API.
# Keep each incidental entry only while the corresponding binding exists.
INCIDENTAL: frozenset[str] = frozenset({
    "settings",           # static settings dict, from the with open(...) block
    "f",                  # the file handle that block opened
    "top_level_folder",   # parent of PLUGIN_DIR, bound only on the nix path
})

# Runtime imports become module attributes; TYPE_CHECKING imports do not exist at runtime.
# Neither kind is a slot.
IMPORTED: frozenset[str] = frozenset({
    # runtime
    "json", "os", "sys", "threading", "appinfo", "deque", "log", "argparser",
    "rebrand_migration", "Callable", "TYPE_CHECKING", "Any",
    # TYPE_CHECKING only
    "App", "LocaleManager", "AssetManagerBackend", "AssetManager",
    "MediaManager", "PageManagerBackend", "SettingsManager", "DeckManager",
    "PluginManager", "IconPackManager", "WallpaperPackManager",
    "SDPlusBarWallpaperPackManager", "StoreBackend", "Notify", "SignalManager",
    "WindowGrabber", "Wayland", "GnomeExtensions", "Store",
    "FlatpakPermissionManager", "PageManager", "LockScreenManager",
    "PresenceMonitor", "TrayIcon", "Logger",
})

# Module machinery. It is not state, not imported, and not assignable.
MACHINERY: frozenset[str] = frozenset({
    "__getattr__",   # serves and caches fallback_font on first read
})

# Exempt module-object machinery stores per file and attribute because they add no declared slot.
# Installed-class behavior is out of scope; other files and inventory attributes still fail.
MODULE_TYPE_STORES: dict[str, frozenset[str]] = {
    # Installs a recording module to pin which slots the render engine reads,
    # and restores the original class in a finally.
    "tests/scenario_engine_gl_surface.py": frozenset({"__class__"}),
}

TABLES: tuple[tuple[str, frozenset[str]], ...] = (
    ("FROZEN_SLOTS", frozenset(FROZEN_SLOTS)),
    ("INCIDENTAL", INCIDENTAL),
    ("IMPORTED", IMPORTED),
    ("MACHINERY", MACHINERY),
)

ADDING_A_SLOT = (
    f"Adding one is two edits in one change: declare it in {GLOBALS_MODULE}, and add "
    f"it to FROZEN_SLOTS in {THIS_SCRIPT}. Prefer not adding one: a new service is "
    "constructor-injected by default -- build it as a local in "
    "main.create_global_objects and pass it to whatever needs it, the way the page "
    "manager backend takes the settings manager."
)


def relative(path: Path) -> str:
    """Repo-relative posix path, or the absolute one if it sits outside the repo."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def parse(path: Path, failures: list[str]) -> ast.Module | None:
    """Parse path, or record why the parse failed. This never skips silently."""
    try:
        source = path.read_bytes()
    except OSError as error:
        failures.append(
            f"{relative(path)}: cannot be read ({error}). This check covers every file "
            "under the governed roots, so one it cannot read is one it cannot report on."
        )
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as error:
        failures.append(
            f"{relative(path)}:{error.lineno}: does not parse ({error.msg}). Fix the "
            "syntax -- an unparseable file is unchecked, not clean."
        )
        return None


# Declaration pin

def declared_names(tree: ast.Module) -> set[str]:
    """Names that tree binds at module scope, minus the ones it deletes.
    Enter compound statements, stop at local-scope boundaries, and include global declarations and literal globals() cache writes.
    """
    bound: set[str] = set()
    deleted: set[str] = set()

    def target(node: ast.expr) -> None:
        if isinstance(node, ast.Name):
            bound.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                target(element)
        elif isinstance(node, ast.Starred):
            target(node.value)
        elif isinstance(node, ast.Subscript):
            # globals()["name"] = value writes a module attribute through the
            # namespace dict. It is the one binding with no name in the source.
            call = node.value
            key = node.slice
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "globals"
                and isinstance(key, ast.Constant)
                and isinstance(key.value, str)
            ):
                bound.add(key.value)

    def walrus(node: ast.AST) -> None:
        """Collect walrus targets from expressions that evaluate at module scope.
        A module-scope comprehension can bind outside itself; a lambda body cannot.
        """
        for field, value in ast.iter_fields(node):
            if isinstance(node, ast.Lambda) and field == "body":
                continue
            for item in (value if isinstance(value, list) else [value]):
                if not isinstance(item, ast.AST) or isinstance(item, ast.stmt):
                    continue   # statements are descend()'s job
                if isinstance(item, ast.NamedExpr):
                    bound.add(item.target.id)
                walrus(item)

    def descend(node: ast.stmt) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            # Function and class bodies bind locals.
            # Decorators, defaults, annotations, and bases evaluate here and can bind module walruses.
            walrus(node)
            return

        walrus(node)

        if isinstance(node, ast.Assign):
            for element in node.targets:
                target(element)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            target(node.target)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.TypeAlias):
            target(node.name)   # type X = ... binds X like an assignment
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            target(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    target(item.optional_vars)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                for pattern in ast.walk(case.pattern):
                    # capture patterns bind through name, mapping rests through rest
                    captured = getattr(pattern, "name", None)
                    captured = captured or getattr(pattern, "rest", None)
                    if isinstance(captured, str):
                        bound.add(captured)
        elif isinstance(node, ast.Delete):
            for element in node.targets:
                if isinstance(element, ast.Name):
                    deleted.add(element.id)

        # Descend through unknown statement shapes.
        # Do not collect except-as names because Python deletes them after the handler.
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                descend(child)
            elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                for statement in child.body:
                    descend(statement)

    for statement in tree.body:
        descend(statement)

    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            # Reaches module scope from inside a def, so it is a declaration
            # wherever it sits.
            bound.update(node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for element in targets:
                if isinstance(element, ast.Subscript):
                    target(element)   # the globals()[...] form, at any depth

    return bound - deleted


def check_tables(failures: list[str]) -> None:
    """Reject a table that overlaps another one, or that is empty.
    MODULE_TYPE_STORES must contain only non-inventory dunders so it cannot exempt real slots.
    """
    exempted = {name for names in MODULE_TYPE_STORES.values() for name in names}
    for name in sorted(exempted):
        if not (name.startswith("__") and name.endswith("__")):
            failures.append(
                f"{THIS_SCRIPT}: MODULE_TYPE_STORES lists `{name}`, which is not a "
                "dunder. This table is only for attributes that reach the module "
                "object's own machinery; any other name here exempts a slot store "
                "from the freeze, which is the one thing it must never do."
            )
    for table_name, entries in TABLES:
        for shared in sorted(exempted & entries):
            failures.append(
                f"{THIS_SCRIPT}: `{shared}` is listed in both MODULE_TYPE_STORES and "
                f"{table_name}. A name globals.py declares is part of the inventory, "
                "and pinning who assigns the inventory is the whole check."
            )

    for index, (name, entries) in enumerate(TABLES):
        if not entries:
            failures.append(
                f"{THIS_SCRIPT}: {name} is empty. An empty table cannot pin anything; "
                "if the population it described is really gone, delete the table and "
                "its use rather than leaving it looking like a check."
            )
        for other_name, other_entries in TABLES[index + 1:]:
            for shared in sorted(entries & other_entries):
                failures.append(
                    f"{THIS_SCRIPT}: `{shared}` is listed in both {name} and "
                    f"{other_name}. One name, one table -- the tables answer what a "
                    "name is, and two answers is no answer."
                )


def check_declarations(failures: list[str]) -> set[str]:
    """Pin globals.py module names to the tables and return the names found."""
    path = REPO_ROOT / GLOBALS_MODULE
    if not path.is_file():
        failures.append(
            f"{GLOBALS_MODULE}: missing. It is the module this check exists to freeze, "
            f"so its absence is a failure, not an empty run. If it moved, point "
            f"{THIS_SCRIPT} at the new path in the same change."
        )
        return set()

    tree = parse(path, failures)
    if tree is None:
        return set()
    if not tree.body:
        failures.append(
            f"{GLOBALS_MODULE}: parsed to an empty module. Every slot would read as "
            "deleted, so this check refuses to report on it."
        )
        return set()

    declared = declared_names(tree)
    listed = {name for _, entries in TABLES for name in entries}

    for name in sorted(declared - listed):
        failures.append(
            f"{GLOBALS_MODULE}: declares `{name}`, which no table in {THIS_SCRIPT} "
            f"lists. The `gl` inventory is frozen: it grows by deliberate edit, never "
            f"by arriving. {ADDING_A_SLOT}"
        )

    for name in sorted(listed - declared):
        table = next(table for table, entries in TABLES if name in entries)
        failures.append(
            f"{THIS_SCRIPT}: lists `{name}` in {table}, but {GLOBALS_MODULE} no longer "
            "declares it. Delete the entry in the same change that removed the "
            "declaration -- a table entry with nothing behind it makes the freeze "
            "describe a module that does not exist."
        )

    return declared


# Assignment pin

def governed_files(failures: list[str]) -> list[Path]:
    """Every governed file, sorted. Reports anything that shrinks the sweep."""
    found: list[Path] = []

    for name in GOVERNED_FILES:
        path = REPO_ROOT / name
        if not path.is_file():
            failures.append(
                f"{name}: governed file is missing. It is one of the modules that "
                f"assigns `gl` slots, so an unswept one is a hole in the freeze. "
                f"Restore it, or update GOVERNED_FILES in {THIS_SCRIPT} in the same "
                "change that moves it."
            )
            continue
        found.append(path)

    for root in GOVERNED_TREES:
        base = REPO_ROOT / root
        if not base.is_dir():
            failures.append(
                f"{root}/: governed root is not a directory. Every `gl` assignment "
                f"under it would go unchecked, so this check refuses to report success. "
                f"Restore the tree, or update GOVERNED_TREES in {THIS_SCRIPT} in the "
                "same change that moves it."
            )
            continue

        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            here = Path(dirpath)
            keep: list[str] = []
            for name in sorted(dirnames):
                if name == "__pycache__":
                    continue
                child = here / name
                if child.is_symlink():
                    failures.append(
                        f"{relative(child)}: symlinked directory under a governed root. "
                        "The walk does not descend into it, so every `gl` assignment "
                        "inside it would go unseen. Replace it with a real directory, "
                        "or teach this check to follow symlinks."
                    )
                    continue
                keep.append(name)
            dirnames[:] = keep   # prune in place, so os.walk skips what is gone

            found.extend(here / name for name in filenames if name.endswith(".py"))

    return sorted(found)


def module_aliases(
    tree: ast.Module, where: str, problems: list[tuple[int, str]]
) -> set[str]:
    """Names that this file binds the globals module to.
    Accept import globals as gl and report forms whose stores the checker cannot see.
    """
    aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != "globals" and not alias.name.startswith("globals."):
                    continue
                if alias.asname:
                    aliases.add(alias.asname)       # the house form; several per file is fine
                else:
                    aliases.add("globals")
                    problems.append((
                        node.lineno,
                        f"{where}:{node.lineno}: `import globals` binds the module under "
                        "the same name as the builtin that returns a namespace dict, so "
                        "an assignment through it reads as neither. Write "
                        "`import globals as gl`, the form every other module uses and "
                        "the one this check resolves.",
                    ))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == "globals":
                names = ", ".join(alias.name for alias in node.names)
                problems.append((
                    node.lineno,
                    f"{where}:{node.lineno}: `from globals import {names}` copies slot "
                    "values into local names, so later reads see whatever the value was "
                    "at import time rather than the slot -- and no store through such a "
                    "name is visible to this check. Write `import globals as gl` and go "
                    "through the module.",
                ))

    return aliases


def check_stores(path: Path, failures: list[str], type_stores_used: set) -> int:
    """Pin every gl attribute store in one file to FROZEN_SLOTS.
    Return the count and record used MODULE_TYPE_STORES exemptions.
    """
    where = relative(path)
    type_stores = MODULE_TYPE_STORES.get(where, frozenset())
    tree = parse(path, failures)
    if tree is None:
        return 0

    # Collect line numbers and report in source order.
    # AST walking groups by node shape instead of source position.
    problems: list[tuple[int, str]] = []
    aliases = module_aliases(tree, where, problems)
    if not aliases:
        failures.extend(message for _, message in sorted(problems))
        return 0

    stores = 0

    def alias_attribute(node: ast.expr) -> str | None:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in aliases:
                return node.attr
        return None

    def store(node: ast.expr, line: int) -> None:
        nonlocal stores
        name = alias_attribute(node)
        if name is not None:
            if name in type_stores:
                type_stores_used.add((where, name))
                return
            stores += 1
            if name not in FROZEN_SLOTS:
                problems.append((
                    line,
                    f"{where}:{line}: assigns `gl.{name}`, which is not a frozen slot. "
                    f"{ADDING_A_SLOT}",
                ))
            return
        if isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                store(element, line)
        elif isinstance(node, ast.Starred):
            store(node.value, line)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for element in node.targets:
                store(element, node.lineno)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            store(node.target, node.lineno)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            store(node.target, node.lineno)
        elif isinstance(node, ast.comprehension):
            store(node.target, node.target.lineno)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    store(item.optional_vars, node.lineno)
        elif isinstance(node, ast.Delete):
            for element in node.targets:
                name = alias_attribute(element)
                if name is not None:
                    problems.append((
                        node.lineno,
                        f"{where}:{node.lineno}: `del gl.{name}` removes a declared slot "
                        "at runtime, which makes the frozen inventory describe a module "
                        "that no longer matches it. Assign the slot back to its empty "
                        "value instead.",
                    ))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("setattr", "delattr") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Name) and first.id in aliases:
                    problems.append((
                        node.lineno,
                        f"{where}:{node.lineno}: `{node.func.id}(gl, ...)` writes a slot "
                        "under a name this check cannot see, which is exactly what the "
                        "freeze exists to prevent. Name the slot in the source "
                        f"(`gl.thing = ...`). {ADDING_A_SLOT}",
                    ))

    failures.extend(message for _, message in sorted(problems))
    return stores


def check_type_store_use(type_stores_used: set, failures: list[str]) -> None:
    """Drop a MODULE_TYPE_STORES entry once its store is gone.
    Reject unused standing exemptions.
    """
    for where, names in sorted(MODULE_TYPE_STORES.items()):
        for name in sorted(names):
            if (where, name) not in type_stores_used:
                failures.append(
                    f"{THIS_SCRIPT}: MODULE_TYPE_STORES exempts `gl.{name}` in {where}, "
                    "which no longer stores it (or is no longer governed). Drop the "
                    "entry rather than leaving an exemption standing for a write "
                    "nothing makes."
                )


def main() -> int:
    failures: list[str] = []

    check_tables(failures)
    declared = check_declarations(failures)

    files = governed_files(failures)
    type_stores_used: set = set()
    stores = sum(check_stores(path, failures, type_stores_used) for path in files)
    check_type_store_use(type_stores_used, failures)

    if failures:
        print(
            f"gl slot freeze: {len(failures)} problem(s) "
            f"(the inventory and the rules live in {THIS_SCRIPT}).",
            file=sys.stderr,
        )
        for message in failures:
            print(f"  - {message}", file=sys.stderr)
        return 1

    print(
        f"gl slot freeze: {len(FROZEN_SLOTS)} slots frozen, {len(declared)} declarations "
        f"pinned in {GLOBALS_MODULE}, {stores} assignments across {len(files)} governed "
        "files all within the table."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
