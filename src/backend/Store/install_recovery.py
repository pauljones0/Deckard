"""Crash recovery for the store's install swap.

An install stages a new tree, then swaps it into place with two renames: the
old install moves to a dot-prefixed sibling, and the new tree moves onto the
destination. A power loss or a kill between those renames can leave the
destination absent with the old tree parked under its dot name. This module
finds those leftovers at startup and restores the destination.

The scanners for plugins and packs skip a dot-prefixed entry, so a leftover
never reads as a real install; recovery finds them by these suffixes.
"""
import contextlib
import os
import shutil
import threading

from loguru import logger as log

SWAP_NEW_SUFFIX = ".deckard-new"
SWAP_OLD_SUFFIX = ".deckard-old"

# One lock per install destination. Two installs of the same asset run from the
# UI install and update threads, and the swap's fixed staging names would
# otherwise let them interfere. The lock serializes the swap for one
# destination; different destinations never share names.
_swap_locks: dict[str, threading.Lock] = {}
_swap_locks_guard = threading.Lock()


def _swap_lock(directory: str) -> threading.Lock:
    """The one lock for a destination, keyed by its resolved path."""
    key = os.path.realpath(directory)
    with _swap_locks_guard:
        return _swap_locks.setdefault(key, threading.Lock())


def swap_into_place(staging_tree: str, directory: str) -> None:
    """Replace directory with the fully staged tree, and delete the old install
    only after the new one is in place.

    This first moves the staged tree next to the destination. That move is the
    one step that can cross a filesystem, because an environment variable can
    put an install dir on another device. It runs while the old install stays
    intact. The two renames that follow share a parent and are atomic, and each
    is followed by a directory fsync so the rename survives a power loss. The
    transient siblings carry a dot prefix, so the plugin and pack scanners never
    read a crash leftover as a real install, and recover_interrupted_installs
    restores one at startup. The next install of the same asset sweeps a
    leftover.
    """
    parent = os.path.dirname(os.path.abspath(directory))
    name = os.path.basename(os.path.normpath(directory))
    os.makedirs(parent, exist_ok=True)
    new_tree = os.path.join(parent, f".{name}{SWAP_NEW_SUFFIX}")
    old_tree = os.path.join(parent, f".{name}{SWAP_OLD_SUFFIX}")

    with _swap_lock(directory):
        _remove_leftover(new_tree)
        _remove_leftover(old_tree)

        shutil.move(staging_tree, new_tree)
        moved_old_aside = False
        try:
            if os.path.lexists(directory):
                os.replace(directory, old_tree)
                moved_old_aside = True
            os.replace(new_tree, directory)
        except Exception:
            # Put the old install back, then report the failure.
            if moved_old_aside and not os.path.lexists(directory):
                os.replace(old_tree, directory)
            shutil.rmtree(new_tree, ignore_errors=True)
            fsync_dir(parent)
            raise
        # Persist the rename before the old tree goes away. A crash after this
        # fsync leaves the destination in place; a crash before it, if the
        # rename had not reached disk, leaves the old tree parked under its dot
        # name, and recover_interrupted_installs restores it.
        fsync_dir(parent)
        _remove_leftover(old_tree)
        fsync_dir(parent)


def fsync_dir(path: str) -> None:
    """Flush a directory entry change to disk, so a rename survives a power
    loss. Best effort: a filesystem that cannot fsync a directory must not
    fail the install."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _remove_leftover(path: str) -> None:
    """Remove a parked swap tree, or a stray file or symlink."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        with contextlib.suppress(OSError):
            os.remove(path)


def recover_interrupted_installs(base_dirs: list[str]) -> None:
    """Repair every install destination under base_dirs left half-swapped."""
    for base_dir in base_dirs:
        _recover_dir(base_dir)


def _recover_dir(parent: str) -> None:
    if not os.path.isdir(parent):
        return
    try:
        entries = os.listdir(parent)
    except OSError as e:
        log.warning(f"Could not scan {parent} for interrupted installs: {e}")
        return

    # The destination names that have a parked new or old tree.
    names: set[str] = set()
    for entry in entries:
        if not entry.startswith("."):
            continue
        for suffix in (SWAP_NEW_SUFFIX, SWAP_OLD_SUFFIX):
            if entry.endswith(suffix):
                names.add(entry[1:-len(suffix)])

    for name in names:
        directory = os.path.join(parent, name)
        new_tree = os.path.join(parent, f".{name}{SWAP_NEW_SUFFIX}")
        old_tree = os.path.join(parent, f".{name}{SWAP_OLD_SUFFIX}")
        try:
            if not os.path.lexists(directory):
                # The destination is gone. Prefer the previous install, the
                # known-good tree; fall back to the fully staged new tree only
                # when no old tree survives.
                if os.path.lexists(old_tree):
                    os.replace(old_tree, directory)
                    fsync_dir(parent)
                    log.warning(f"Recovered an interrupted install: restored the previous {name!r}")
                elif os.path.lexists(new_tree):
                    os.replace(new_tree, directory)
                    fsync_dir(parent)
                    log.warning(f"Recovered an interrupted install: completed the staged {name!r}")
            # Sweep whatever leftovers remain, whether the destination was
            # restored above or already stood in place.
            _remove_leftover(old_tree)
            _remove_leftover(new_tree)
            fsync_dir(parent)
        except OSError as e:
            log.error(f"Could not recover an interrupted install of {name!r} in {parent}: {e}")
