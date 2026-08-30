"""Transactional install swaps and startup recovery for interrupted renames."""
import contextlib
import os
import shutil
import threading

from loguru import logger as log

SWAP_NEW_SUFFIX = ".deckard-new"
SWAP_OLD_SUFFIX = ".deckard-old"

# Serialize UI and update swaps for one destination; different destinations use separate names.
_swap_locks: dict[str, threading.Lock] = {}
_swap_locks_guard = threading.Lock()


def _swap_lock(directory: str) -> threading.Lock:
    """The one lock for a destination, keyed by its resolved path."""
    key = os.path.realpath(directory)
    with _swap_locks_guard:
        return _swap_locks.setdefault(key, threading.Lock())


def swap_into_place(staging_tree: str, directory: str) -> None:
    """Move staging beside the destination while the old install remains intact.
    Fsync the same-parent renames and keep dot-prefixed leftovers recoverable after interruption."""
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
            # Restore the previous install before propagating swap failure.
            if moved_old_aside and not os.path.lexists(directory):
                os.replace(old_tree, directory)
            shutil.rmtree(new_tree, ignore_errors=True)
            fsync_dir(parent)
            raise
        # Persist the new destination before removing the recoverable old tree.
        fsync_dir(parent)
        _remove_leftover(old_tree)
        fsync_dir(parent)


def fsync_dir(path: str) -> None:
    """Best-effort flush a directory entry change without failing the install."""
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
                # Restore the known-good old tree, or use staging if no old tree survives.
                if os.path.lexists(old_tree):
                    os.replace(old_tree, directory)
                    fsync_dir(parent)
                    log.warning(f"Recovered an interrupted install: restored the previous {name!r}")
                elif os.path.lexists(new_tree):
                    os.replace(new_tree, directory)
                    fsync_dir(parent)
                    log.warning(f"Recovered an interrupted install: completed the staged {name!r}")
            # Remove leftovers after recovery or when the destination already exists.
            _remove_leftover(old_tree)
            _remove_leftover(new_tree)
            fsync_dir(parent)
        except OSError as e:
            log.error(f"Could not recover an interrupted install of {name!r} in {parent}: {e}")
