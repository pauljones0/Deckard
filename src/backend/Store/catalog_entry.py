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

# A git commit sha holds exactly 40 hex characters. A pinned sha becomes
# a raw url segment, a cache-key component, and a git argv token, so one
# pattern gates every pin arm: a malformed value must fail loudly rather
# than reach a url, a cache path, or "git reset --hard". The install
# gate, StoreBackend.is_safe_commit_sha, applies this same pattern, so a
# revision this module resolves is one the install accepts.
COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class PinnedRevision(NamedTuple):
    """The commit an entry pins, and whether a compatible release backs it.

    compatible is False when only the version map resolved and no version
    in it matches this app. The store still lists such an entry, and
    refuses to install it. A "hash" pin resolves as compatible: the shape
    carries no version map to judge against. The store window still
    badges a manifest that requires a newer app, but the update path
    trusts the pin, so a catalog ref must only pin what the app can run.
    """
    sha: str
    compatible: bool


def resolve_pinned_revision(entry: dict[str, Any]) -> PinnedRevision | None:
    """Decide the revision one catalog entry pins. Returns None when the
    entry pins nothing valid, and the caller drops or skips the entry.

    "hash" wins over "commits" when an entry carries both. On such mixed
    entries the map keys hold the plugin's own versions, not app
    versions, so the map's app-major verdict means nothing there; the
    flat sha is the field the migrated catalog maintains. An invalid
    "hash" falls back to the map, so one malformed field cannot hide an
    entry the map still resolves.

    A garbage version key raises out of the version parse, like the
    per-version decision always has, and every caller catches or drops
    per entry.
    """
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
