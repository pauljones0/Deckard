"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
# Shared durable-JSON writer. Stdlib only, because the migrators run before
# SettingsManager and the globals consumers exist, and must still import this.
from typing import Any

import contextlib
import json
import os
import shutil
import stat
import tempfile
import time

# Reap a target's temp file after this many seconds if a hard kill orphaned it
# between write and rename.
STALE_TMP_MAX_AGE = 60 * 60

# Keep three forensic sidecars per primary so repeated corruption cannot fill
# the configuration directory; these copies are not version history.
CORRUPT_SIDECAR_KEEP = 3


def _process_umask() -> int:
    """Read Linux umask without changing state; elsewhere set and restore it.
    The fallback briefly exposes a zero process mask to concurrent opens."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _reap_stale_tmp_siblings(dir_path: str, target_basename: str) -> None:
    """Remove this target's orphaned temp files after STALE_TMP_MAX_AGE.
    The age guard preserves concurrent live writes, and races with another reaper are harmless."""
    prefix = f".save-{target_basename}."
    try:
        entries = os.listdir(dir_path)
    except OSError:
        return
    now = time.time()
    for entry in entries:
        if not (entry.startswith(prefix) and entry.endswith(".tmp")):
            continue
        path = os.path.join(dir_path, entry)
        try:
            if now - os.stat(path).st_mtime > STALE_TMP_MAX_AGE:
                os.remove(path)
        except OSError:
            pass


def quarantine_corrupt_file(file_path: str) -> tuple[bool, str]:
    """Atomically move corrupt data to a bounded .corrupt[.n] path; return (moved, path).
    Failure can leave the source in place or mean another actor already moved it."""
    candidate = file_path + ".corrupt"
    n = 0
    # Probe through .corrupt.10000. If that last path exists, os.replace overwrites it.
    while os.path.exists(candidate) and n < 10000:
        n += 1
        candidate = f"{file_path}.corrupt.{n}"
    try:
        os.replace(file_path, candidate)
        return True, candidate
    except OSError:
        # A rename error can mean a concurrent quarantine already moved the source.
        # The caller uses the corrupt-read result to decide whether to recover.
        return False, file_path


def prune_corrupt_sidecars(primary_path: str, keep: int = CORRUPT_SIDECAR_KEEP,
                           protect: "str | list[str] | tuple[str, ...] | None" = None) -> list[str]:
    """Prune oldest regular .corrupt[.numeric] sidecars by mtime; name breaks ties; ignore errors.
    Protect the new sidecar since rename keeps old mtime; it remains and counts toward keep."""
    keep = max(keep, 0)
    if protect is None:
        protected = set()
    elif isinstance(protect, str):
        protected = {os.path.abspath(protect)}
    else:
        protected = {os.path.abspath(p) for p in protect}
    dir_path = os.path.dirname(primary_path) or "."
    plain = os.path.basename(primary_path) + ".corrupt"
    numbered = plain + "."

    try:
        entries = os.listdir(dir_path)
    except OSError:
        return []

    sidecars: list[tuple[float, str, str]] = []
    for entry in entries:
        if entry != plain:
            suffix = entry[len(numbered):] if entry.startswith(numbered) else ""
            if not (suffix.isascii() and suffix.isdigit()):
                continue
        path = os.path.join(dir_path, entry)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        # The name gives equal mtimes a stable order but does not indicate age.
        sidecars.append((st.st_mtime, entry, path))

    n_remove = len(sidecars) - keep
    if n_remove <= 0:
        return []

    sidecars.sort()
    removed: list[str] = []
    for _mtime, _entry, path in sidecars:
        if n_remove <= 0:
            break
        if os.path.abspath(path) in protected:
            continue
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
        # Count it either way. A concurrent pruner removes a sidecar just as
        # this call does.
        n_remove -= 1
    return removed


def require_containment(base_dir: str, path: str) -> str:
    """Return path's real path when it is base_dir or below it; otherwise raise ValueError.
    Resolve both sides like atomic_write_json so traversal and symlinks cannot escape."""
    real_base = os.path.realpath(base_dir)
    real_path = os.path.realpath(path)
    if real_path != real_base and not real_path.startswith(real_base + os.sep):
        raise ValueError(f"{path!r} resolves outside {base_dir!r}")
    return real_path


def atomic_write_json(file_path: str, data: Any, indent: int | None = 4) -> None:
    """Durably replace real file_path with fsynced JSON and preserve or derive its mode.
    Same-directory temp and directory fsync expose old or new data; realpath preserves symlinks."""
    file_path = os.path.realpath(file_path)
    dir_path = os.path.dirname(file_path) or "."
    os.makedirs(dir_path, exist_ok=True)

    basename = os.path.basename(file_path)
    _reap_stale_tmp_siblings(dir_path, basename)

    fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=f".save-{basename}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        # Keep an existing mode or apply umask to mkstemp's 0600 for a new file;
        # hardcoded 0644 can expose plugin API tokens despite a restrictive umask.
        try:
            mode = os.stat(file_path).st_mode & 0o777
        except FileNotFoundError:
            mode = 0o666 & ~_process_umask()
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, file_path)
        # fsync the directory so the rename itself becomes durable.
        try:
            dir_fd = os.open(dir_path, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def atomic_copy_file(src_path: str, dst_path: str, overwrite: bool = True) -> None:
    """Durably copy content and mode through a fresh same-directory temp without timestamps.
    Use dst/src mode; keep dst on error; no-overwrite links atomically or raises FileExistsError."""
    dst_path = os.path.realpath(dst_path)
    dir_path = os.path.dirname(dst_path) or "."
    os.makedirs(dir_path, exist_ok=True)

    basename = os.path.basename(dst_path)
    _reap_stale_tmp_siblings(dir_path, basename)

    fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=f".save-{basename}.", suffix=".tmp")
    try:
        with open(src_path, "rb") as src_f, os.fdopen(fd, "wb") as tmp_f:
            shutil.copyfileobj(src_f, tmp_f)
            tmp_f.flush()
            os.fsync(tmp_f.fileno())
        try:
            mode = os.stat(dst_path).st_mode & 0o777
        except FileNotFoundError:
            mode = os.stat(src_path).st_mode & 0o777
        os.chmod(tmp_path, mode)
        if overwrite:
            os.replace(tmp_path, dst_path)
        else:
            os.link(tmp_path, dst_path)
            # The published file has its own name now; the temp is surplus,
            # and a failed unlink here leaves only reaper work.
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
        # fsync the directory so the publish itself becomes durable.
        try:
            dir_fd = os.open(dir_path, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
