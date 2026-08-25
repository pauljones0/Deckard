"""What a zip archive may name, and where its members may land.

An archive carries the paths its author wrote, and nothing checks them for
the reader. A member called "../../x" names a file outside whatever directory
the reader unpacks into, and a member called "/etc/x" names an absolute one.
The two functions here are the input validation every reader of an untrusted
archive in this tree runs before it writes a byte.

CPython's zipfile strips a leading separator and every ".." from a member
during its own extraction, so that library is a first guard for a zip. It is
not the only reader this tree will ever have, a member name reaches other
calls than extraction, and a reader that builds its own destination path gets
no help from it at all. These checks therefore stand on their own.

This module imports the standard library only, so any layer can import it.
"""
from __future__ import annotations

import os
import zipfile


def unsafe_member_reason(name: str) -> str | None:
    """Why name may not be unpacked, or None when it may.

    A member is refused when it names an absolute path, when it carries a
    drive letter or a backslash separator, or when its normalized form starts
    outside the directory it would be written into.
    """
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
    """The first member of the archive that may not be unpacked.

    It answers the member name and the reason, or None when every member is
    safe. A caller refuses the whole archive on an answer. One bad member is
    a bad archive: the members before it are no more trustworthy than the one
    that gave itself away, and a partial unpack leaves the caller holding a
    half-made result it cannot tell from a whole one.
    """
    with zipfile.ZipFile(zip_path, "r") as archive:
        for name in archive.namelist():
            reason = unsafe_member_reason(name)
            if reason is not None:
                return name, reason
    return None


def resolved_within(base_dir: str, candidate: str) -> bool:
    """Whether candidate resolves inside base_dir.

    The last check before a write, and it holds whatever built the path. It
    resolves both sides, so a symlink on the way in cannot point the write
    somewhere else, and it compares whole path components, so a sibling
    directory whose name merely starts with the base name is outside.
    """
    base = os.path.realpath(base_dir)
    target = os.path.realpath(candidate)
    return target == base or target.startswith(base + os.sep)
