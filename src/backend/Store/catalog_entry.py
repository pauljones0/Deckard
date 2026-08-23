"""Revision resolution for one store catalog entry.

A catalog entry pins the commit to fetch under a "commits" map of app
versions to commit shas. Three consumers make that decision: the catalog
listing, the update check and the install identification. This module is
the one implementation behind all three, so their pin semantics cannot
drift apart.
"""

from collections.abc import Collection
from typing import Any, NamedTuple

from packaging import version

import globals as gl


class PinnedRevision(NamedTuple):
    """The commit an entry pins, and whether a compatible release backs it.

    compatible is False when no version in the entry's map matches this
    app. The store still lists such an entry, and refuses to install it.
    """
    sha: str
    compatible: bool


def resolve_pinned_revision(entry: dict[str, Any]) -> PinnedRevision | None:
    """Decide the revision one catalog entry pins. Returns None when the
    entry pins nothing, and the caller drops or skips the entry.

    A garbage version key raises out of the version parse, like the
    per-version decision always has, and every caller catches or drops per
    entry.
    """
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
