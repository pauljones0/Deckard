#!/usr/bin/env python3
"""Cap physical lines in src and GtkHelper with a default cap, shrink-only grandfather caps that tighten after TIGHTEN_SLACK and return to default at DEFAULT_CAP, plus a hard cap for the re-export-only deck-controller shim.
Fail when roots, grandfather files, or the shim are missing or outside coverage, or when symlinked directories hide modules; count a final unterminated line."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Govern application and helper code in a subset of the type-check roots.
# Test length follows fixture and scenario needs, so tests stay outside this cap.
ROOTS = ("src", "GtkHelper")

DEFAULT_CAP = 1200

# How far a grandfathered file may sit below its cap before the cap must follow
# it down.
TIGHTEN_SLACK = 100

# The deck-controller compatibility surface contains only imports and __all__.
# Its separate cap is permanent, not a grandfather allowance.
SHIM_PATH = "src/backend/DeckManagement/DeckController.py"
SHIM_CAP = 100

# Modules above DEFAULT_CAP use shrink-only caps that tighten as described above.
GRANDFATHER: dict[str, int] = {
    # These deck-controller modules retain a typed constructor signature and narrowed class state.
    # Subclasses must declare the narrowed state at class level for attribute readers.
    "src/backend/DeckManagement/deck_controller/controller.py": 1670,
    "src/backend/DeckManagement/deck_controller/inputs.py": 1457,
    "src/backend/Store/StoreBackend.py": 1948,
}


def physical_lines(path: Path) -> int:
    """Count the lines of path. A last line without a terminator counts."""
    data = path.read_bytes()
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def iter_modules(failures: list[str]) -> list[Path]:
    """Every .py file under the governed roots, sorted, without the caches.
    Do not follow symlinks; report missing roots or hidden directories that reduce coverage.
    """
    found: list[Path] = []
    for root in ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            failures.append(
                f"{root}/: governed root is not a directory. Every module under it "
                "would go uncapped, so this check refuses to report success. Restore "
                "the tree, or update ROOTS in the same change that moves it."
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
                        f"{child.relative_to(REPO_ROOT).as_posix()}: symlinked directory "
                        "under a governed root. The walk does not descend into it, so "
                        "every module inside it would go uncapped. Replace it with a real "
                        "directory, or teach this check to follow symlinks."
                    )
                    continue
                keep.append(name)
            dirnames[:] = keep   # prune in place, so os.walk skips what is gone

            found.extend(here / name for name in filenames if name.endswith(".py"))

    return sorted(found)


def check_shim(failures: list[str]) -> None:
    """Hold the deck controller shim under its hard cap."""
    shim = REPO_ROOT / SHIM_PATH
    if not shim.is_file():
        failures.append(
            f"{SHIM_PATH}: missing. The deck controller shim is the compatibility "
            "path plugins import from, and its hard cap is what keeps the package "
            "split from unwinding. Moving or deleting it needs this check updated "
            "in the same change."
        )
        return

    lines = physical_lines(shim)
    if lines > SHIM_CAP:
        failures.append(
            f"{SHIM_PATH}: {lines} lines, hard cap {SHIM_CAP} (over by {lines - SHIM_CAP}). "
            "This module re-exports the deck controller package and defines nothing: "
            "import statements and __all__. Code that belongs to a deck controller "
            "belongs in src/backend/DeckManagement/deck_controller/. The cap is what "
            "keeps the split from unwinding one convenience function at a time, so "
            "for accreted code, moving it into the package is the fix and raising "
            "the cap is not. A genuinely new compatibility name is the other case: "
            "the surface tracks what upstream binds at this path, and widening it "
            "does justify raising the cap -- deliberately, in a change that says so."
        )


def check_grandfather_table(failures: list[str]) -> None:
    """Reject a table that has drifted from the tree it describes."""
    if SHIM_PATH in GRANDFATHER:
        failures.append(
            f"{SHIM_PATH}: listed in GRANDFATHER. The shim is governed by its own "
            f"hard cap of {SHIM_CAP} lines and must not be grandfathered -- remove "
            "the entry."
        )
    for name in sorted(GRANDFATHER):
        if not any(name.startswith(f"{root}/") for root in ROOTS):
            # An entry that the walk never visits caps nothing, and it still
            # reads as a cap. That is the one way this table can lie.
            failures.append(
                f"{name}: listed in GRANDFATHER but outside the governed roots "
                f"({', '.join(ROOTS)}). Nothing enforces this entry. Delete it, or add "
                "the tree it lives in to ROOTS so the file is actually capped."
            )
        elif not (REPO_ROOT / name).is_file():
            failures.append(
                f"{name}: listed in GRANDFATHER but not present in the tree. "
                "Delete the entry, or point it at the file's new path."
            )


def check_module(path: Path, failures: list[str]) -> None:
    """Hold one module to its cap, either grandfathered or the default."""
    name = path.relative_to(REPO_ROOT).as_posix()
    if name == SHIM_PATH:
        return  # check_shim owns this one

    lines = physical_lines(path)
    cap = GRANDFATHER.get(name)

    if cap is None:
        if lines > DEFAULT_CAP:
            failures.append(
                f"{name}: {lines} lines, cap {DEFAULT_CAP} (over by {lines - DEFAULT_CAP}). "
                "Split the module along a seam that stands on its own -- a module this "
                "long stops being reviewable as a unit."
            )
        return

    if lines > cap:
        failures.append(
            f"{name}: {lines} lines, grandfathered cap {cap} (over by {lines - cap}). "
            "Grandfathered modules are allowed to shrink and nothing else. Move the "
            "addition into a module of its own rather than growing this one."
        )
    elif lines <= DEFAULT_CAP:
        failures.append(
            f"{name}: {lines} lines, now within the {DEFAULT_CAP}-line default cap. "
            "Delete its GRANDFATHER entry -- the default cap covers this module from "
            "here on."
        )
    elif lines < cap - TIGHTEN_SLACK:
        failures.append(
            f"{name}: {lines} lines, grandfathered cap {cap} -- {cap - lines} lines of "
            f"slack, more than the {TIGHTEN_SLACK} allowed. Lower the GRANDFATHER entry "
            f"to {lines} so the room this file gave back cannot be spent on new growth."
        )


def main() -> int:
    failures: list[str] = []

    check_grandfather_table(failures)
    check_shim(failures)

    modules = iter_modules(failures)
    for path in modules:
        check_module(path, failures)

    if failures:
        print(
            f"Module-size ratchet: {len(failures)} problem(s) "
            f"(default cap {DEFAULT_CAP} lines; see scripts/check_module_sizes.py).",
            file=sys.stderr,
        )
        for message in failures:
            print(f"  - {message}", file=sys.stderr)
        return 1

    print(
        f"Module-size ratchet: {len(modules)} modules within cap "
        f"({DEFAULT_CAP} lines default, {len(GRANDFATHER)} grandfathered, "
        f"shim hard cap {SHIM_CAP})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
