"""Shared resolution for flat-hash and app-version-map catalog pins."""

import re
from collections.abc import Collection
from typing import Any, NamedTuple

from loguru import logger as log
from packaging import version

import globals as gl

# Require one 40-character hexadecimal commit shape for resolution and installation.
COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class PinnedRevision(NamedTuple):
    """A pinned commit and its app-version-map compatibility verdict.
    A valid flat hash wins over any version map and resolves as compatible."""
    sha: str
    compatible: bool


def resolve_pinned_revision(entry: dict[str, Any]) -> PinnedRevision | None:
    """Resolve a valid flat hash, ignoring any version map, or return None.
    Fall back to the map for an invalid hash and let malformed version keys raise."""
    sha = entry.get("hash")
    if sha is not None:
        if _is_commit_sha(sha):
            return PinnedRevision(sha, True)
        log.error(f"Ignoring hash {sha!r} of store entry {entry.get('url')!r}: not a commit sha")
    commits = entry.get("commits")
    if not isinstance(commits, dict) or not commits:
        return None
    newest = newest_compatible_version(commits)
    compatible = newest is not None
    if newest is None:
        newest = newest_version(list(commits.keys()))
        if newest is None:
            return None
    sha = commits[newest]
    if not _is_commit_sha(sha):
        log.error(f"Ignoring version {newest!r} of store entry {entry.get('url')!r}: {sha!r} is not a commit sha")
        return None
    return PinnedRevision(sha, compatible)


def _is_commit_sha(value: object) -> bool:
    return isinstance(value, str) and bool(COMMIT_SHA_RE.fullmatch(value))


def newest_compatible_version(available_versions: Collection[str]) -> str | None:
    if gl.exact_app_version_check:
        if gl.app_version in available_versions:
            return gl.app_version
        else:
            return None

    current_major = version.parse(gl.app_version).major

    compatible_versions = [v for v in available_versions if version.parse(v).major == current_major]
    parsed_compatible_versions = [version.parse(v) for v in compatible_versions]

    if compatible_versions:
        max_index = parsed_compatible_versions.index(max(parsed_compatible_versions))
        return compatible_versions[max_index]
    else:
        return None


def newest_version(available_versions: list[str]) -> str | None:
    # None for an empty list, which the callers guard for; max() on an
    # empty sequence would raise instead.
    if not available_versions:
        return None
    parsed_versions = [version.parse(v) for v in available_versions]

    max_index = parsed_versions.index(max(parsed_versions))
    return available_versions[max_index]
