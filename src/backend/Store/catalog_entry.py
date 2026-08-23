"""Revision resolution for one store catalog entry.

A catalog entry pins the commit to fetch in one of two shapes. The old
shape maps app versions to commit shas under "commits". The new shape
carries one flat sha under "hash", and only the manifest's
minimum-app-version gates compatibility for it. Both shapes are live in
the official store: catalog refs from before the migration carry only
"commits", the current ref carries "hash", and a few entries carry both.
Three consumers make the pin decision: the catalog listing, the update
check and the install identification. This module is the one
implementation behind all three, so their shape precedence cannot drift
apart.
"""

import re
from collections.abc import Collection
from typing import Any, NamedTuple

from loguru import logger as log
from packaging import version

import globals as gl

# A pinned sha becomes a raw url segment and a cache-key component in the
# fetch layer, so only a plain commit sha may pass.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class PinnedRevision(NamedTuple):
    """The commit an entry pins, and whether a compatible release backs it.

    compatible is False when only the version map resolved and no version
    in it matches this app. The store still lists such an entry, and
    refuses to install it. A "hash" pin carries no version map, so it
    resolves as compatible, and the manifest's minimum-app-version gates
    it downstream instead.
    """
    sha: str
    compatible: bool


def resolve_pinned_revision(entry: dict[str, Any]) -> PinnedRevision | None:
    """Decide the revision one catalog entry pins. Returns None when the
    entry pins nothing, and the caller drops or skips the entry.

    "hash" wins over "commits" when an entry carries both: on a mixed
    entry the store rewrites "hash" in place while the map stays a
    migration-time snapshot. An invalid "hash" falls back to the map, so
    one malformed field cannot hide an entry the map still resolves.

    A garbage version key raises out of the version parse, like the
    per-version decision always has, and every caller catches or drops per
    entry.
    """
    sha = entry.get("hash")
    if sha is not None:
        if isinstance(sha, str) and _COMMIT_SHA_RE.fullmatch(sha):
            return PinnedRevision(sha, True)
        log.error(f"Ignoring hash {sha!r} of store entry {entry.get('url')!r}: not a commit sha")
    commits = entry.get("commits")
    if not isinstance(commits, dict) or not commits:
        return None
    newest = newest_compatible_version(commits)
    if newest is not None:
        return PinnedRevision(commits[newest], True)
    newest = newest_version(list(commits.keys()))
    if newest is None:
        return None
    return PinnedRevision(commits[newest], False)


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
