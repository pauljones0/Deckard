#!/usr/bin/env python3
"""Require every ty include path to exist, every first-party module to be included or excluded with a reason, and reject both coded and bare mypy suppressions in checked comments.
Fail closed on missing or malformed configuration and unreadable or untokenizable modules; use reasoned # ty: ignore[rule] comments instead."""
from __future__ import annotations

import re
import sys
import tokenize
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Each excluded tree carries its reason.
# Do not also exclude included trees, because that would hide later coverage loss.
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
            # Match skip names on root-relative parts.
            # A worktree path can contain .claude and hide every file if matched absolutely.
            if any(part in SKIPPED_DIRS for part in path.relative_to(REPO_ROOT).parts):
                continue
            found.append(path)
    return sorted(set(found))


def check_no_mypy_ignores(include: list[str]) -> int:
    """Refuse a mypy suppression in the checked set, and return the file count.
    Tokenize comments so matching text in strings and docstrings is ignored.
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
