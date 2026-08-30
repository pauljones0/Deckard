"""Crash-safe var-app move; never merge or discard data, and stop on non-skeleton conflicts.
Use markers, locks, and compatibility links; autostart cleanup runs separately each launch."""

# Import only side-effect-free standard-library modules, appinfo, and cli_args.
# migrate runs before globals, so every dependency must be safe that early.
import contextlib
import os
import shutil
import sys
from collections.abc import Iterator
from typing import Callable

import appinfo

# Move the whole directory so its custom data-path pointer stays with the data.
# Keep an old-root symlink because settings, pages, and backups can contain old absolute paths.
OLD_ID = appinfo.OLD_APP_ID
NEW_ID = appinfo.APP_ID
OLD_ROOT = os.path.expanduser(os.path.join("~", ".var", "app", OLD_ID))
NEW_ROOT = os.path.expanduser(os.path.join("~", ".var", "app", NEW_ID))

# A marker joins the non-atomic rename and symlink steps.
# The fsynced pending marker moves with the tree so the next start can finish after a crash.
MARKER_NAME = ".migrated-from-" + OLD_ID
LOCK_NAME = ".deckard-migration.lock"
_STATE_PENDING = "symlink-pending"
_STATE_COMPLETE = "complete"

# Keep a separate marker for the native ~/.var/app/<id> to XDG migration.
# Both migrations can run in rename-then-XDG order.
XDG_MARKER_NAME = ".migrated-to-xdg"


def _is_flatpak() -> bool:
    return os.path.isfile("/.flatpak-info")


def _log(msg: str) -> None:
    # Logging is unconfigured because sinks would open files inside the tree being renamed.
    print(f"[rebrand-migration] {msg}", file=sys.stderr)


def _abort(msg: str) -> None:
    _log("FATAL: " + msg)
    raise SystemExit(1)


def _read_marker(marker_path: str) -> str | None:
    try:
        with open(marker_path) as f:
            return f.read().strip()
    except OSError:
        return None


def _write_marker(marker_path: str, state: str) -> bool:
    """Write the marker durably: fsync the file, replace it, then fsync the directory.
    Return True on success; a durable truncated marker can strand data without a link.
    """
    tmp_path = marker_path + ".tmp"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, (state + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, marker_path)
        dir_fd = os.open(os.path.dirname(marker_path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return True
    except OSError as e:
        _log(f"could not durably write marker {marker_path} ({e})")
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        return False


@contextlib.contextmanager
def _migration_lock(new_root: str) -> "Iterator[None]":
    """Serialize the migration across concurrent first-run launches.
    A second launch waits and re-reads state; continue unlocked when fcntl is unavailable.
    """
    lock_dir = os.path.dirname(new_root)
    fd = None
    try:
        os.makedirs(lock_dir, exist_ok=True)
        fd = os.open(os.path.join(lock_dir, LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError) as e:
            _log(f"could not acquire migration lock ({e}); proceeding without it")
        yield
    finally:
        if fd is not None:
            os.close(fd)


def _old_instance_running() -> bool:
    try:
        from gi.repository import Gio, GLib
        return bool(Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (OLD_ID,)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NO_AUTO_START,
            5000,
            None
        ).unpack()[0])
    except Exception as e:
        _log(f"could not probe the session bus for a pre-rename instance ({e}); assuming none")
        return False


def _data_override_active(argv: list[str]) -> bool:
    """True if argv holds a --data override.
    Use globals parser abbreviations; parse errors report no override to avoid stranding data.
    """
    import cli_args
    try:
        ns, _ = cli_args.argparser.parse_known_args(argv[1:])
    except SystemExit:
        return False
    return ns.data is not None


def _is_skeleton(root: str) -> bool:
    """True only if root is a tree of empty directories.
    Any regular file or symlink marks real state that the migration keeps.
    """
    if os.path.islink(root):
        return False
    for dirpath, dirnames, filenames in os.walk(root):
        if filenames:
            return False
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                return False
    return True


def _finish_symlink(old_root: str, new_root: str, marker_path: str) -> None:
    """Create (or verify) the compat symlink, then mark the migration done."""
    if os.path.lexists(old_root):
        if os.path.islink(old_root) and os.path.realpath(old_root) == os.path.realpath(new_root):
            pass  # already in place
        else:
            _log(
                f"MIGRATION STUCK: your data was moved to {new_root}, but {old_root} "
                f"reappeared as a real directory (a still-installed pre-rename build "
                f"likely recreated it). The compat symlink can't be created, so pages "
                f"referencing the old path may look empty. Fix: quit/uninstall the old "
                f"build, delete {old_root}, and restart Deckard."
            )
            return
    else:
        try:
            os.symlink(new_root, old_root)
            _log(f"compat symlink {old_root} -> {new_root}")
        except OSError as e:
            _log(f"could not create compat symlink ({e}); will retry next start")
            return
    # A failed complete-marker write leaves the moved pending marker intact.
    # The next start re-enters _finish_symlink and retries.
    _write_marker(marker_path, _STATE_COMPLETE)


def migrate(old_root: str = OLD_ROOT, new_root: str = NEW_ROOT,
            argv: list[str] | None = None, require_pre_globals: bool = True,
            marker_name: str = MARKER_NAME,
            running_check: Callable[[], bool] | None = None,
            locked_fn: Callable[[str, str, str, Callable[[], bool]], None] | None = None) -> None:
    # globals creates the new data tree at import and would invalidate the existence check.
    # main calls migrate before importing globals.
    if require_pre_globals and "globals" in sys.modules:
        raise AssertionError(
            "rebrand_migration.migrate() must run before `import globals` -- "
            "globals creates the data dir at import time and poisons the checks below"
        )

    argv = sys.argv if argv is None else argv
    if _data_override_active(argv):
        _log("--data override active; skipping data-dir migration")
        return

    if running_check is None:
        running_check = _old_instance_running
    if locked_fn is None:
        locked_fn = _migrate_locked
    marker_path = os.path.join(new_root, marker_name)
    # Already-migrated and fresh-install paths use one marker read without a lock file.
    state = _read_marker(marker_path)
    if state == _STATE_COMPLETE:
        return
    if state is None and not os.path.lexists(old_root):
        return

    # The real work runs under the lock, and re-reads the state inside, in
    # case another launch completed the migration during the wait.
    with _migration_lock(new_root):
        locked_fn(old_root, new_root, marker_path, running_check)


def _migrate_locked(old_root: str, new_root: str, marker_path: str,
                    running_check: Callable[[], bool]) -> None:
    state = _read_marker(marker_path)
    if state == _STATE_COMPLETE:
        return
    if state == _STATE_PENDING:
        # A previous start crashed between the rename and the symlink.
        _finish_symlink(old_root, new_root, marker_path)
        return

    if not os.path.lexists(old_root):
        return

    if os.path.islink(old_root):
        if os.path.exists(old_root) and os.path.realpath(old_root) == os.path.realpath(new_root):
            # The compat link exists but the marker is absent, so an earlier
            # marker write failed. The data is already at the new root.
            _write_marker(marker_path, _STATE_COMPLETE)
            return
        _abort(
            f"{old_root} is a symlink but does not resolve to {new_root} (broken or "
            f"foreign). Refusing to touch it -- resolve it manually, then restart."
        )

    if running_check():
        _abort(
            f"a pre-rename instance still owns {OLD_ID} on the session bus. Quit the "
            f"running StreamController first, then start Deckard again. Renaming the "
            f"data dir under a live instance would split its writes across two trees."
        )

    if os.path.lexists(new_root):
        if os.path.islink(new_root):
            _abort(
                f"{new_root} is a symlink; the migration expects to create it as a "
                f"real directory. Resolve it manually, then restart."
            )
        if _is_skeleton(new_root):
            _log(f"removing empty skeleton at {new_root} (import-time makedirs residue)")
            shutil.rmtree(new_root)
        else:
            _abort(
                f"both {old_root} and {new_root} contain files. Refusing to merge or "
                f"delete either. Move one of them aside manually, then restart."
            )

    # Require a durable pending marker before rename because it is the only recovery record.
    # A write failure leaves old_root intact for a clean retry.
    if not _write_marker(os.path.join(old_root, os.path.basename(marker_path)), _STATE_PENDING):
        _abort(
            f"could not durably write the migration marker into {old_root}; refusing "
            f"to rename without it. Fix permissions/space on that path and restart."
        )
    try:
        os.rename(old_root, new_root)
    except OSError as e:
        _abort(f"could not move {old_root} -> {new_root}: {e}")
    _log(f"moved {old_root} -> {new_root}")
    _finish_symlink(old_root, new_root, marker_path)


def _xdg_root() -> str:
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(os.path.join("~", ".local", "share"))
    return os.path.join(xdg, "deckard")


def _same_filesystem(src: str, dest: str) -> bool:
    """True if src and the place for dest share one filesystem.
    Probe the nearest existing ancestor; unknown results try rename and report its failure.
    """
    probe = dest
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return os.stat(src).st_dev == os.stat(probe).st_dev
    except OSError:
        return True


def native_data_root(legacy_root: str = NEW_ROOT, xdg_root: str | None = None) -> str:
    """Return XDG, or the old tree when relocation stopped or waited.
    After a successful move the old symlink resolves to XDG, so XDG remains the result."""
    xdg_root = xdg_root or _xdg_root()
    if os.path.exists(xdg_root) or not os.path.exists(legacy_root):
        return xdg_root
    return legacy_root


def _safe_rmtree(path: str) -> None:
    try:
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
    except OSError as e:
        _log(f"could not remove {path} ({e})")


def _fsync_tree(root: str) -> None:
    """fsync every regular file and every directory below root.
    Make contents durable before source deletion; skip symlinks and ignore errors.
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            fp = os.path.join(dirpath, name)
            if os.path.islink(fp):
                continue
            try:
                fd = os.open(fp, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
        try:
            dfd = os.open(dirpath, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


def _finish_copy(old_root: str, new_root: str, marker_path: str) -> None:
    """Finish a published copy at new_root.
    A pending marker proves durability; failed source removal keeps it pending for retry.
    """
    if os.path.isdir(old_root) and not os.path.islink(old_root):
        try:
            shutil.rmtree(old_root)
        except OSError as e:
            _log(f"copy migration: could not remove the old tree {old_root} ({e}); "
                 f"the app runs from {new_root}, cleanup retries next start")
            return  # leave the marker pending
    _finish_symlink(old_root, new_root, marker_path)


def _copy_migrate_locked(old_root: str, new_root: str, marker_path: str,
                         running_check: Callable[[], bool]) -> None:
    """Cross-filesystem variant of _migrate_locked.
    Stage, fsync, mark, and publish the copy before source removal; ignore running_check.
    """
    # Before publish, a crash leaves old_root for a repeated copy.
    # After publish, the pending marker lets _finish_copy complete cleanup.
    state = _read_marker(marker_path)
    if state == _STATE_COMPLETE:
        return
    if state == _STATE_PENDING:
        # A prior run published the copy to new_root. Finish the cleanup.
        _finish_copy(old_root, new_root, marker_path)
        return

    if not os.path.lexists(old_root):
        return

    if os.path.islink(old_root):
        if os.path.exists(old_root) and os.path.realpath(old_root) == os.path.realpath(new_root):
            _write_marker(marker_path, _STATE_COMPLETE)  # backfill a lost marker
            return
        _abort(
            f"{old_root} is a symlink but does not resolve to {new_root} (broken or "
            f"foreign). Refusing to touch it -- resolve it manually, then restart."
        )

    # old_root is a real directory. Guard new_root like the rename path does.
    # Never merge into, and never overwrite, real user data at new_root.
    if os.path.lexists(new_root):
        if os.path.islink(new_root):
            _abort(
                f"{new_root} is a symlink; the migration expects to create it as a "
                f"real directory. Resolve it manually, then restart."
            )
        if _is_skeleton(new_root):
            _log(f"removing empty skeleton at {new_root} (import-time makedirs residue)")
            shutil.rmtree(new_root)
        else:
            _abort(
                f"both {old_root} and {new_root} contain files. Refusing to merge or "
                f"delete either. Move one of them aside manually, then restart."
            )

    # Build a staged sibling and publish it with an atomic same-filesystem rename.
    # Keep old_root until the pending marker proves the copy durable and complete.
    staging = new_root + ".xdg-migrating"
    _safe_rmtree(staging)  # drop any partial staging from an earlier crash
    try:
        shutil.copytree(old_root, staging, symlinks=True)
    except OSError as e:
        _safe_rmtree(staging)
        _log(f"copy migration: copying {old_root} failed ({e}); the app keeps using "
             f"{old_root}, will retry next start")
        return
    # Write the pending marker last so publication proves a complete durable copy.
    # _finish_copy checks it before deleting old_root.
    if not _write_marker(os.path.join(staging, os.path.basename(marker_path)), _STATE_PENDING):
        _safe_rmtree(staging)
        _log("copy migration: could not durably mark the staged copy; will retry next start")
        return
    _fsync_tree(staging)
    try:
        os.rename(staging, new_root)  # same filesystem, so the publish is atomic
    except OSError as e:
        _safe_rmtree(staging)
        _log(f"copy migration: publishing the staged copy failed ({e}); will retry next start")
        return
    _log(f"copied {old_root} -> {new_root}")
    _finish_copy(old_root, new_root, marker_path)


def migrate_native_var_app_to_xdg(old_root: str = NEW_ROOT, xdg_root: str | None = None,
                                  argv: list[str] | None = None,
                                  require_pre_globals: bool = True) -> None:
    """Move the native data root from ~/.var/app/<id> to $XDG_DATA_HOME/deckard.
    Skip Flatpak; run after migrate and leave a compatibility symlink with a separate marker.
    """
    if _is_flatpak():
        return
    new_root = xdg_root or _xdg_root()
    # Use atomic rename on one filesystem and crash-safe copy across filesystems.
    # Resumption uses the same route; an existing old-root symlink takes the completed rename path.
    cross_fs = (os.path.isdir(old_root) and not os.path.islink(old_root)
                and not _same_filesystem(old_root, new_root))
    # No running-instance check is needed because the compatibility link keeps writes in one tree.
    # Between publish and symlink, a live absolute-path open can get one nonfatal ENOENT.
    migrate(old_root=old_root, new_root=new_root, argv=argv,
            require_pre_globals=require_pre_globals,
            marker_name=XDG_MARKER_NAME, running_check=lambda: False,
            locked_fn=_copy_migrate_locked if cross_fs else None)
