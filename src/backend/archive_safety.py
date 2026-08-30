"""Validate untrusted ZIP member names and resolved write targets.
Reject absolute, drive-relative, and parent escapes without relying on zipfile normalization."""
from __future__ import annotations

import os
import zipfile


def unsafe_member_reason(name: str) -> str | None:
    """Return why a member is unsafe, or None.
    Reject absolute, drive-relative, and normalized parent-escaping paths."""
    if name.startswith(("/", "\\")):
        return "the member names an absolute path"
    if len(name) > 1 and name[1] == ":":
        return "the member names a drive-relative path"
    normalized = os.path.normpath(name.replace("\\", "/"))
    # normpath folds "a/../b" down to "b". What is left starting with ".."
    # resolves above the directory the caller unpacks into.
    if normalized == ".." or normalized.startswith(".." + os.sep) or normalized.startswith("../"):
        return "the member resolves outside the directory it unpacks into"
    return None


def first_unsafe_member(zip_path: str) -> tuple[str, str] | None:
    """Return the first unsafe member and reason, or None.
    Callers reject the whole archive before extraction to avoid a partial result."""
    with zipfile.ZipFile(zip_path, "r") as archive:
        for name in archive.namelist():
            reason = unsafe_member_reason(name)
            if reason is not None:
                return name, reason
    return None


def resolved_within(base_dir: str, candidate: str) -> bool:
    """Return whether candidate resolves inside base_dir after symlink resolution.
    Compare full path components so same-prefix sibling directories remain outside."""
    base = os.path.realpath(base_dir)
    target = os.path.realpath(candidate)
    return target == base or target.startswith(base + os.sep)
