#!/usr/bin/env python3
"""Allow bare json.load/dump only in named files; otherwise use healing and atomic writes.
Reject hidden imports/stale/other entries/missing/unparseable/hidden files; skip strings/aliases."""
from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
THIS_SCRIPT = "scripts/check_settings_json.py"

# Govern root modules with sanctioned raw reads individually.
# Reject entries outside the walk because they sanction nothing.
GOVERNED_FILES = ("globals.py", "main.py")
GOVERNED_TREES = ("src", "GtkHelper")

# The two file-level json entry points that this check governs. The string
# forms, json.loads and json.dumps, stay out of scope. See the docstring.
FILE_JSON_FUNCS = frozenset({"load", "dump"})

# The from json import <name> names that would hide a governed call.
HIDDEN_IMPORT_NAMES = frozenset({"load", "dump"})

# Allow direct JSON file access only in these files for the stated reason.
# All other settings access uses the healing loader and atomic writer.
ALLOWLIST: dict[str, str] = {
    # The store owns the healing loader and the atomic writer's json.dump.
    "src/backend/settings_store.py": "the settings store's read-with-heal loader",
    "src/backend/atomic_json.py": "the atomic settings writer -- the one json.dump every write funnels through",
    # The data-path bootstrap. It runs before the store, or anything else, is
    # importable, and it defines the data path that the store resolves against.
    "globals.py": "the data-path bootstrap read, which runs before the store exists",
    # A dev-only page dump printed by the CLI, reading page files directly.
    "main.py": "the CLI page-listing dump, a dev listing that never runs in the app proper",
    # A migrator reads an old on-disk format once and rewrites it into the
    # current shape, before the store surfaces describe the tree.
    "src/backend/Migration/Migrator.py": "a migrator reading a legacy file to rewrite it",
    "src/backend/Migration/Migrators/Migrator_1_5_0.py": "a migrator reading legacy deck/page files",
    "src/backend/Migration/Migrators/Migrator_1_5_0_beta_5.py": "a migrator reading legacy page/settings files",
    # A pack manifest is a read-only source file inside a pack, not an app
    # setting. The app never writes one, so nothing overwrites a corrupt one.
    "src/backend/PackManagement/pack_family.py": "a pack's read-only manifest, for every pack family",
    # The plugin manifest.json and about.json are read-only source files that
    # the app never writes, under a log-and-leave policy that the store omits.
    "src/backend/PluginManager/PluginBase.py": "a plugin's read-only manifest/about source files",
    # The on-disk state of the plugin store. It holds a cache index with its
    # own locked flush protocol, and the id inside a downloaded manifest.
    "src/backend/Store/StoreCache.py": "the plugin store's cache index, on its own flush protocol",
    "src/backend/Store/StoreBackend.py": "reading a downloaded asset's id out of its manifest",
    # A parse validates a page backup before a heal trusts it. The page
    # machinery owns the page files end to end.
    "src/backend/PageManagement/page_document.py": "validating a page backup before healing from it",
    # An importer reads a foreign export and translates it into pages. It reads
    # the file once, and the app neither owns it nor writes it back.
    "src/windows/PageManager/Importer/Importer.py": "probing a foreign export's shape before importing it",
    "src/windows/PageManager/Importer/StreamController/StreamController.py": "reading a StreamController export to import it",
    "src/windows/PageManager/Importer/StreamDeckUI/StreamDeckUI.py": "reading a StreamDeck UI export to import it",
    "src/windows/PageManager/elements/MenuButton.py": "reading a page file chosen for import",
}


def relative(path: Path, root: Path) -> str:
    """Repo-relative posix path, or the absolute one if it sits outside the repo."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def parse(path: Path, root: Path, failures: list[str]) -> ast.Module | None:
    """Parse path, or record why the parse failed. This never skips silently."""
    try:
        source = path.read_bytes()
    except OSError as error:
        failures.append(
            f"{relative(path, root)}: cannot be read ({error}). This check covers every "
            "file under the governed roots, so one it cannot read is one it cannot report on."
        )
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as error:
        failures.append(
            f"{relative(path, root)}:{error.lineno}: does not parse ({error.msg}). Fix the "
            "syntax -- an unparseable file is unchecked, not clean."
        )
        return None


def collect_governed_files(
    root: Path, governed_files: tuple[str, ...], governed_trees: tuple[str, ...], failures: list[str]
) -> list[Path]:
    """Every governed file, sorted. Reports anything that shrinks the sweep."""
    found: list[Path] = []

    for name in governed_files:
        path = root / name
        if not path.is_file():
            failures.append(
                f"{name}: governed file is missing. It is named individually because the "
                f"allowlist sanctions a raw read in it, so an unswept one is a hole in the "
                f"check. Restore it, or update GOVERNED_FILES in {THIS_SCRIPT} in the same "
                "change that moves it."
            )
            continue
        found.append(path)

    for tree in governed_trees:
        base = root / tree
        if not base.is_dir():
            failures.append(
                f"{tree}/: governed root is not a directory. Every json call under it would "
                f"go unchecked, so this check refuses to report success. Restore the tree, or "
                f"update GOVERNED_TREES in {THIS_SCRIPT} in the same change that moves it."
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
                        f"{relative(child, root)}: symlinked directory under a governed root. "
                        "The walk does not descend into it, so every json call inside it would "
                        "go unseen. Replace it with a real directory, or teach this check to "
                        "follow symlinks."
                    )
                    continue
                keep.append(name)
            dirnames[:] = keep   # prune in place, so os.walk skips what is gone

            found.extend(here / name for name in filenames if name.endswith(".py"))

    return sorted(found)


def json_aliases(tree: ast.Module) -> set[str]:
    """Names that the file uses for the json module.
    Include json and aliases imported at any depth.
    """
    aliases = {"json"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "json" and alias.asname:
                    aliases.add(alias.asname)
    return aliases


def check_file(
    path: Path, root: Path, allowlist: dict[str, str], used: set[str], failures: list[str]
) -> int:
    """Pin every file-level json call in one file. Returns the count.
    Record used allowlist entries so stale exemptions fail.
    """
    where = relative(path, root)
    tree = parse(path, root, failures)
    if tree is None:
        return 0

    aliases = json_aliases(tree)
    sanctioned = where in allowlist

    problems: list[tuple[int, str]] = []
    calls = 0

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "json":
            for alias in node.names:
                if alias.name in HIDDEN_IMPORT_NAMES:
                    problems.append((
                        node.lineno,
                        f"{where}:{node.lineno}: `from json import {alias.name}` binds a file "
                        "entry point under a bare name this check cannot see at the call site. "
                        f"Write `json.{alias.name}` -- or, for a settings file, go through the "
                        "settings store.",
                    ))
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in FILE_JSON_FUNCS
            and isinstance(func.value, ast.Name)
            and func.value.id in aliases
        ):
            calls += 1
            if not sanctioned:
                problems.append((
                    node.lineno,
                    f"{where}:{node.lineno}: opens a JSON file with `json.{func.attr}`. A "
                    "settings file is read and written through the settings store "
                    "(src/backend/settings_store.py), whose loader heals a corrupt one and "
                    "whose writer is atomic. If this genuinely is not a settings file, add "
                    f"{where!r} to the ALLOWLIST in {THIS_SCRIPT}, with the reason -- a "
                    "two-line, reviewable decision.",
                ))

    if sanctioned and calls:
        used.add(where)

    failures.extend(message for _, message in sorted(problems))
    return calls


def check_allowlist_table(
    root: Path, governed_files: tuple[str, ...], governed_trees: tuple[str, ...],
    allowlist: dict[str, str], failures: list[str],
) -> None:
    """Reject an allowlist entry that the walk never visits.
    An unvisited entry sanctions nothing.
    """
    for name in sorted(allowlist):
        under_governed = name in governed_files or any(
            name.startswith(f"{tree}/") for tree in governed_trees
        )
        if not under_governed:
            failures.append(
                f"{name}: listed in the ALLOWLIST but outside the governed set "
                f"({', '.join(governed_files + governed_trees)}). Nothing enforces this entry. "
                "Delete it, or add the tree/file it lives in to the governed set so its calls "
                "are actually checked."
            )
        elif not (root / name).is_file():
            failures.append(
                f"{name}: listed in the ALLOWLIST but not present in the tree. Delete the "
                "entry, or point it at the file's new path."
            )


def check_allowlist_use(allowlist: dict[str, str], used: set[str], failures: list[str]) -> None:
    """Drop an allowlist entry once its reader is gone.
    Reject standing exemptions with no JSON call.
    """
    for name in sorted(allowlist):
        if name not in used:
            failures.append(
                f"{name}: listed in the ALLOWLIST but calls no json.load/json.dump (or is no "
                "longer parseable/present). Drop the entry rather than leaving a raw-JSON "
                "exemption standing for a read nothing performs."
            )


def run_check(
    root: Path, governed_files: tuple[str, ...], governed_trees: tuple[str, ...],
    allowlist: dict[str, str],
) -> tuple[list[str], int, int]:
    """The whole check against one tree. Returns failures, files and calls."""
    failures: list[str] = []
    check_allowlist_table(root, governed_files, governed_trees, allowlist, failures)
    files = collect_governed_files(root, governed_files, governed_trees, failures)
    used: set[str] = set()
    calls = sum(check_file(path, root, allowlist, used, failures) for path in files)
    check_allowlist_use(allowlist, used, failures)
    return failures, len(files), calls


def self_test() -> int:
    """Prove the check goes red on a planted bare reader and a stale entry."""
    problems: list[str] = []
    tree_root = Path(tempfile.mkdtemp(prefix="settings-json-selftest-"))
    try:
        backend = tree_root / "src" / "backend"
        backend.mkdir(parents=True)
        (tree_root / "GtkHelper").mkdir()
        (tree_root / "globals.py").write_text("x = 1\n")
        (tree_root / "main.py").write_text("y = 2\n")
        (backend / "settings_store.py").write_text(
            "import json\n\n\ndef load(p):\n    with open(p) as h:\n        return json.load(h)\n"
        )
        (backend / "clean.py").write_text("z = 3\n")
        allow = {"src/backend/settings_store.py": "the loader"}
        gfiles = ("globals.py", "main.py")
        gtrees = ("src", "GtkHelper")

        failures, _, _ = run_check(tree_root, gfiles, gtrees, allow)
        if failures:
            problems.append(f"a clean tree was reported red: {failures}")

        rogue = backend / "rogue.py"
        rogue.write_text("import json\n\n\ndef g(p):\n    return json.load(open(p))\n")
        failures, _, _ = run_check(tree_root, gfiles, gtrees, allow)
        if not any("rogue.py" in f for f in failures):
            problems.append(f"a planted bare json.load was NOT caught red: {failures}")
        rogue.unlink()

        # A from json import load must go red too.
        hidden = backend / "hidden.py"
        hidden.write_text("from json import load\n\n\ndef g(p):\n    return load(open(p))\n")
        failures, _, _ = run_check(tree_root, gfiles, gtrees, allow)
        if not any("hidden.py" in f for f in failures):
            problems.append(f"a `from json import load` was NOT caught red: {failures}")
        hidden.unlink()

        # A stale allowlist entry goes red when the file stops reading json.
        (backend / "settings_store.py").write_text("still_no_json = True\n")
        failures, _, _ = run_check(tree_root, gfiles, gtrees, allow)
        if not any("settings_store.py" in f for f in failures):
            problems.append(f"a stale allowlist entry was NOT caught red: {failures}")
    finally:
        shutil.rmtree(tree_root, ignore_errors=True)

    if problems:
        print("settings-json ratchet self-test: FAIL", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("settings-json ratchet self-test: PASS (planted reader, hidden import and stale entry all caught)")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv[1:]:
        return self_test()

    failures, files, calls = run_check(REPO_ROOT, GOVERNED_FILES, GOVERNED_TREES, ALLOWLIST)

    if failures:
        print(
            f"settings-json ratchet: {len(failures)} problem(s) "
            f"(the allowlist and the rules live in {THIS_SCRIPT}).",
            file=sys.stderr,
        )
        for message in failures:
            print(f"  - {message}", file=sys.stderr)
        return 1

    print(
        f"settings-json ratchet: {calls} json.load/json.dump calls across {files} governed "
        f"files, all within the {len(ALLOWLIST)}-entry allowlist."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
