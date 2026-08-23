#!/usr/bin/env python3
"""Type-gate coverage guard. It stops the gate from checking nothing.

Run it the same way CI does, from anywhere:

    python scripts/check_type_gate.py

Exit 0 means every entry in [tool.ty.src] include exists on disk, every
first-party module outside the named exclusions is inside it, and no module in
it carries a mypy-shaped suppression.

The checker takes an explicit include list. A path in that list that does not
exist is not an error to it: ty drops the entry, checks whatever is left, prints
"All checks passed!" and exits 0. Rename GtkHelper/ and forget this file and the
gate goes green while a whole tree stops being checked. Measured on ty 0.0.73
with a deliberate typo, and again with an include list of one absent directory,
which gives "WARN No python files found under the given path(s)" and still exits
0. A warning nobody reads is not a gate.

The second check exists because the include list replaced import following. The
previous checker took two directories plus every module it reached through an
import, so a new root module was covered the moment something imported it. Nothing imports main.py, which is how it went unchecked
for the whole life of that gate. An explicit list fixes that, and trades it for
a new way to lose coverage: add a top-level package, forget the list, and it is
invisible. So this walks the repo root and insists that every first-party
module is either included or named in EXCLUDED below, with a reason.

The third check refuses a mypy-shaped suppression in the checked set. Neither
form of one is visible to the checker. `# type: ignore[assignment]` suppresses
nothing, because ty reads no mypy error code, and ty does not report it as
unused either, so it sits dead over an error the gate then has to catch some
other way. A bare `# type: ignore` is the opposite and worse: ty honours it as
a blanket suppression that silences every rule on its line, and neither
blanket-ignore-comment nor unused-ignore-comment says a word about it. Both
measured on ty 0.0.73. One form is silently dead and the other is silently
absolute, so this refuses both and asks for `# ty: ignore[rule]` instead.

A guard that fails open reads as green and covers nothing, so this one also
fails when its own footing moves: a missing pyproject.toml, a missing or
malformed [tool.ty.src] table, an include list that is not a list of strings,
or an empty one, and a checked module it cannot read or tokenize. Each is a
loud failure that names the fix, never a silent skip.

To put a new tree under the gate: add it to include in pyproject.toml. To leave
one out on purpose: add it to EXCLUDED here, with the reason written next to it.
"""
from __future__ import annotations

import re
import sys
import tokenize
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Trees and files deliberately outside the gate. The reason is the entry. A
# tree that the include list already names does not belong here: the escape
# walk tests the include list first, so an entry here would hide the day that
# tree leaves the include list.
EXCLUDED: dict[str, str] = {
    "tests": "the scenario harness; its fixtures and fakes are written against runtime shapes, not declared ones",
    "scripts": "stdlib-only guards and helpers that the gate's own jobs run",
    "aur-deckard-git": "a full copy of the tree for packaging; checking it would check everything twice",
    "flatpak": "packaging helpers, run by the build and not by the app",
    "packaging": "packaging recipes, run by the build and not by the app",
}

# Directories the walk never descends into, whatever they hold.
SKIPPED_DIRS = {".git", "__pycache__", ".venv", ".claude", ".ruff_cache", "build", "dist"}

# A mypy suppression in any spelling. mypy accepted the whole family, so this
# matches the whole family rather than the one shape the tree happens to hold.
MYPY_IGNORE = re.compile(r"#\s*type\s*:\s*ignore", re.IGNORECASE)


def fail(*lines: str) -> None:
    print("type-gate coverage:", lines[0], file=sys.stderr)
    for line in lines[1:]:
        print("   ", line, file=sys.stderr)
    raise SystemExit(1)


def read_include() -> list[str]:
    config = REPO_ROOT / "pyproject.toml"
    if not config.is_file():
        fail("pyproject.toml is missing.",
             "The type gate reads its checked set from [tool.ty.src] include there.")
    with config.open("rb") as handle:
        data = tomllib.load(handle)
    ty = data.get("tool", {}).get("ty", {})
    if not ty:
        fail("pyproject.toml has no [tool.ty] table.",
             "Without it the checker falls back to its own defaults and this guard",
             "cannot say what is covered.")
    src = ty.get("src", {})
    if "include" not in src:
        fail("[tool.ty.src] declares no include list.",
             "The checker then walks the whole project root, which pulls in tests/,",
             "scripts/ and any packaging copy. Name the checked set explicitly.")
    include = src["include"]
    if not isinstance(include, list) or not include:
        fail("[tool.ty.src] include is not a non-empty list.",
             f"Found: {include!r}")
    if not all(isinstance(entry, str) for entry in include):
        fail("[tool.ty.src] include holds a non-string entry.",
             f"Found: {include!r}")
    return include


def check_entries_exist(include: list[str]) -> None:
    missing = [entry for entry in include if not (REPO_ROOT / entry).exists()]
    if missing:
        fail("an include entry does not exist on disk:",
             *(f"{entry}  ->  no such file or directory" for entry in missing),
             "The checker drops the entry and still exits 0, so that tree is no",
             "longer checked and nothing says so. Fix the path in pyproject.toml,",
             "or delete the entry if the tree is gone for good.")


def check_nothing_escapes(include: list[str]) -> None:
    included = {entry.rstrip("/") for entry in include}
    escaped: list[str] = []

    for path in sorted(REPO_ROOT.iterdir()):
        name = path.name
        if name in SKIPPED_DIRS or name in included or name in EXCLUDED:
            continue
        if path.is_file() and path.suffix == ".py":
            escaped.append(name)
        elif path.is_dir() and not name.startswith("."):
            if any(path.rglob("*.py")):
                escaped.append(name + "/")

    if escaped:
        fail("a first-party module sits outside the checked set:",
             *(f"{entry}" for entry in escaped),
             "Add it to [tool.ty.src] include in pyproject.toml, or add it to",
             "EXCLUDED in this script with the reason it stays out.")


def checked_modules(include: list[str]) -> list[Path]:
    """Every .py file the include list puts under the gate."""
    found: list[Path] = []
    for entry in include:
        target = REPO_ROOT / entry
        if target.is_file():
            if target.suffix == ".py":
                found.append(target)
            continue
        for path in target.rglob("*.py"):
            # Relative parts, because the checkout itself can sit under a
            # directory this set names. A worktree lives under .claude, and
            # testing the absolute path there skips every file in the tree.
            if any(part in SKIPPED_DIRS for part in path.relative_to(REPO_ROOT).parts):
                continue
            found.append(path)
    return sorted(set(found))


def check_no_mypy_ignores(include: list[str]) -> int:
    """Refuse a mypy suppression in the checked set, and return the file count.

    It reads comments through the tokenizer, so the same text inside a string
    or a docstring is not a hit.
    """
    offenders: list[str] = []
    modules = checked_modules(include)

    for path in modules:
        name = path.relative_to(REPO_ROOT).as_posix()
        try:
            with tokenize.open(path) as handle:
                tokens = list(tokenize.generate_tokens(handle.readline))
        except (OSError, SyntaxError, UnicodeDecodeError, tokenize.TokenError) as error:
            fail(f"{name}: a checked module will not tokenize, so its comments went unread.",
                 f"{type(error).__name__}: {error}",
                 "This guard refuses to report success over a file it could not read.")
        for token in tokens:
            if token.type == tokenize.COMMENT and MYPY_IGNORE.search(token.string):
                offenders.append(f"{name}:{token.start[0]}: {token.string.strip()}")

    if offenders:
        fail("a mypy suppression sits in the checked set:",
             *offenders,
             "A coded one suppresses nothing and reports nothing; a bare one",
             "silences every rule on its line and reports nothing. Write",
             "# ty: ignore[rule] with the reason instead, or fix the type.")
    return len(modules)


def main() -> int:
    include = read_include()
    check_entries_exist(include)
    check_nothing_escapes(include)
    scanned = check_no_mypy_ignores(include)
    trees = sum(1 for entry in include if (REPO_ROOT / entry).is_dir())
    modules = len(include) - trees
    print(f"type gate: {trees} trees and {modules} root modules in the checked set, "
          f"{len(EXCLUDED)} exclusions named, nothing escaped, "
          f"{scanned} modules free of a mypy suppression.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
