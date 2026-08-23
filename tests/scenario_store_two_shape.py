"""Two-shape pin resolution for store catalog entries.

A catalog entry pins its commit in one of two shapes: the old "commits"
map of app versions to shas, and the flat "hash" sha the migrated catalog
carries. Both shapes stay live across catalog refs, and a few entries
carry both. This pins the resolver's precedence and validation, drives a
hash-only entry through the prepare, update-check and claim paths for
every asset family, pins the branch arm's priority over any pin at all
three sites, and pins the official-branch fallback for an app version
that versions.json does not map.
"""

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from PIL import Image

from src.backend.Store.catalog_entry import PinnedRevision, resolve_pinned_revision
from src.backend.Store.StoreBackend import InstalledAsset, StoreBackend
from src.backend.Store.StoreCache import StoreCache


APP_MAJOR = int(gl.app_version.split(".")[0])
COMPATIBLE_VERSION = f"{APP_MAJOR}.1.0"
INCOMPATIBLE_VERSION = f"{APP_MAJOR + 1}.0.0"

URL = "https://github.com/acme/Widget"
HASH_SHA = "abc123" + "d" * 34
MAP_SHA = "c0ffee" + "0" * 34
INCOMPAT_SHA = "dead" + "b" * 36
BRANCH_SHA = "b12345" + "a" * 34

MANIFEST = {
    "id": "com_acme_Widget",
    "name": "Widget",
    "version": "9.9.9",
    "thumbnail": "store/Thumbnail.png",
    "descriptions": {"en": "long"},
    "short-descriptions": {"en": "short"},
}
IMAGE = Image.new("RGB", (4, 4), (0, 128, 255))

PREPARE_METHODS = [
    "prepare_plugin",
    "prepare_icon",
    "prepare_wallpaper",
    "prepare_sd_plus_bar_wallpaper",
]


def _stub_globals() -> None:
    fixtures.install_stub_globals()
    gl.lm = SimpleNamespace(get_custom_translation=lambda translations: (translations or {}).get("en"))


def _make_backend() -> StoreBackend:
    """A backend with __init__ skipped, because it spawns an authors-fetch
    thread, and only the attributes the exercised paths touch."""
    sb = StoreBackend.__new__(StoreBackend)
    sb.store_cache = StoreCache()
    sb._fetch_limiter = threading.Semaphore(StoreBackend.MAX_CONCURRENT_REQUESTS)
    sb._prepare_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="store-prepare")
    sb.official_authors = []
    sb.official_store_branch_cache = None
    return sb


def _stub_fetch_layer(sb: StoreBackend, seen_refs: list) -> None:
    """The fetch layer under _prepare_asset, recording every manifest ref."""
    def get_manifest(url, commit):
        seen_refs.append(commit)
        return dict(MANIFEST)
    sb.get_manifest = get_manifest
    sb.get_attribution = lambda url, commit: {}
    sb.get_web_image = lambda url, path, branch="main": IMAGE
    sb.get_last_commit = lambda url, branch="main": BRANCH_SHA


def test_resolver_precedence_and_validation() -> None:
    # The flat sha alone pins, as compatible. The shape gate is the
    # install gate's: exactly 40 hex characters, either case.
    assert resolve_pinned_revision({"hash": HASH_SHA}) == PinnedRevision(HASH_SHA, True)
    upper = HASH_SHA.upper()
    assert resolve_pinned_revision({"hash": upper}) == PinnedRevision(upper, True)

    # On a mixed entry the hash wins: the map keys there hold the
    # plugin's own versions, so the map's app-major verdict means
    # nothing. That holds even when the map alone would read as
    # incompatible; the update path trusts the pin, and the catalog ref
    # carries that responsibility.
    mixed = {"hash": HASH_SHA, "commits": {COMPATIBLE_VERSION: MAP_SHA}}
    assert resolve_pinned_revision(mixed) == PinnedRevision(HASH_SHA, True)
    mixed_incompat = {"hash": HASH_SHA, "commits": {INCOMPATIBLE_VERSION: INCOMPAT_SHA}}
    assert resolve_pinned_revision(mixed_incompat) == PinnedRevision(HASH_SHA, True)

    # An invalid hash falls back to the map instead of hiding the entry.
    # An abbreviated sha is invalid: the install gate refuses anything
    # but 40 hex characters, so resolving it would pin an uninstallable
    # revision.
    for bad in ("not a sha", "abcdef1", "abcde", "a" * 41, "g" * 40, 7, ""):
        entry = {"hash": bad, "commits": {COMPATIBLE_VERSION: MAP_SHA}}
        assert resolve_pinned_revision(entry) == PinnedRevision(MAP_SHA, True), (
            f"invalid hash {bad!r} must fall back to the version map"
        )
    assert resolve_pinned_revision({"hash": "not a sha"}) is None

    # Map-only entries keep their semantics: a compatible version pins it,
    # a map with no version for this major pins the newest as incompatible.
    assert resolve_pinned_revision({"commits": {COMPATIBLE_VERSION: MAP_SHA}}) == PinnedRevision(MAP_SHA, True)
    both = {"commits": {COMPATIBLE_VERSION: MAP_SHA, INCOMPATIBLE_VERSION: INCOMPAT_SHA}}
    assert resolve_pinned_revision(both) == PinnedRevision(MAP_SHA, True)
    assert resolve_pinned_revision({"commits": {INCOMPATIBLE_VERSION: INCOMPAT_SHA}}) == PinnedRevision(INCOMPAT_SHA, False)

    # The map arm passes the same shape gate as the hash arm, so a map
    # value can never become a url segment or a cache-path component.
    assert resolve_pinned_revision({"commits": {COMPATIBLE_VERSION: "../../../../escape"}}) is None
    assert resolve_pinned_revision({"commits": {COMPATIBLE_VERSION: None}}) is None

    # No pin at all resolves to None, and the caller drops the entry.
    assert resolve_pinned_revision({}) is None
    assert resolve_pinned_revision({"commits": {}}) is None
    assert resolve_pinned_revision({"commits": "1.1.0"}) is None


def test_prepare_families_on_hash_entry() -> None:
    """A hash-only entry lists in every family, fetched at the hash, and
    reads as compatible: the flat shape carries no version map, so the
    manifest's minimum-app-version gates it downstream instead."""
    _stub_globals()
    for method in PREPARE_METHODS:
        sb = _make_backend()
        seen_refs: list = []
        _stub_fetch_layer(sb, seen_refs)
        entry = {"url": URL, "hash": HASH_SHA}
        row = getattr(sb, method)(entry, include_image=True, verified=False)
        assert row is not None, f"{method}: a hash-only entry must list"
        assert row.commit_sha == HASH_SHA, f"{method}: commit_sha {row.commit_sha!r}"
        assert row.is_compatible is True, f"{method}: a hash pin resolves compatible"
        assert seen_refs == [HASH_SHA], (
            f"{method}: the manifest must be read at the hash, got {seen_refs!r}"
        )

        # The update-check view resolves the same target with no fetch.
        seen_refs.clear()
        checked = getattr(sb, method)(entry, include_image=False, verified=False)
        assert checked is not None and checked.commit_sha == HASH_SHA, (
            f"{method}: update view target {getattr(checked, 'commit_sha', None)!r}"
        )
        assert checked.local_sha is None, f"{method}: nothing is installed here"
        assert seen_refs == [], f"{method}: the update view must fetch no manifest"


def test_branch_wins_over_any_pin() -> None:
    """A plugin entry that carries a branch resolves the branch tip at
    every site, whether the pin beside it is valid or broken. The three
    sites must agree; a pin-first listing would drop an entry the update
    check still resolves."""
    _stub_globals()
    for pin in ({"hash": HASH_SHA}, {"hash": "NOT-A-SHA"}, {"commits": {}}):
        sb = _make_backend()
        seen_refs: list = []
        _stub_fetch_layer(sb, seen_refs)
        entry = {"url": URL, "branch": "main", **pin}
        row = sb.prepare_plugin(entry, include_image=True, verified=False)
        assert row is not None, f"branch entry with pin {pin!r} must list"
        assert row.commit_sha == BRANCH_SHA and row.branch == "main", (
            f"branch must win over pin {pin!r}, got {row.commit_sha!r}"
        )
        checked = sb.prepare_plugin(entry, include_image=False, verified=False)
        assert checked is not None and checked.commit_sha == BRANCH_SHA, (
            f"update view must resolve the branch tip for pin {pin!r}"
        )


def test_claim_reads_the_hash_revision() -> None:
    """Install identification reads the manifest at the hash, so a pending
    directory claims against the same revision the catalog pins."""
    _stub_globals()
    sb = _make_backend()
    seen_refs: list = []
    _stub_fetch_layer(sb, seen_refs)
    stamped: list = []
    sb.stamp_origin = lambda path, url: stamped.append((path, url))

    asset = InstalledAsset(asset_id="com_acme_Widget", path="/unused/com_acme_Widget",
                           sha="", origin=None, manifest_id="com_acme_Widget", is_symlink=False)
    pending = {"com_acme_Widget": asset}
    installed: dict = {}
    sb._claim_pending_install({"url": URL, "hash": HASH_SHA}, pending, installed)
    assert seen_refs == [HASH_SHA], f"claim must read the manifest at the hash, got {seen_refs!r}"
    assert not pending and "com_acme_Widget" in installed, "the pending directory must be claimed"
    assert stamped == [(asset.path, URL)]

    # A branch-pinned entry reads the manifest at the branch name, with
    # no tip lookup and no glance at the pin beside it.
    seen_refs.clear()
    def boom(url, branch="main"):
        raise AssertionError("the claim path must not resolve a branch tip")
    sb.get_last_commit = boom
    pending = {"com_acme_Widget": asset}
    sb._claim_pending_install({"url": URL, "branch": "main", "hash": HASH_SHA}, pending, {})
    assert seen_refs == ["main"], f"claim must read the branch name, got {seen_refs!r}"

    # A pin-less entry claims nothing and fetches nothing.
    seen_refs.clear()
    pending = {"com_acme_Widget": asset}
    sb._claim_pending_install({"url": URL}, pending, {})
    assert seen_refs == [] and pending, "a pin-less entry must not fetch or claim"


def test_official_branch_unmapped_version_falls_back() -> None:
    """An app version that versions.json does not map falls back to the
    pinned store branch. A default to the tip would silently switch the
    catalog content."""
    _stub_globals()
    sb = _make_backend()
    fetches = []
    def get_remote_file(*args, **kwargs):
        fetches.append(args)
        return json.dumps({"9.9.9": "other"})
    sb.get_remote_file = get_remote_file
    assert sb.get_official_store_branch() == StoreBackend.STORE_BRANCH
    # Unlike a failed fetch, an unmapped version is a stable answer, so
    # it caches and a catalog load does not refetch versions.json.
    assert sb.official_store_branch_cache == StoreBackend.STORE_BRANCH
    assert sb.get_official_store_branch() == StoreBackend.STORE_BRANCH
    assert len(fetches) == 1, "the unmapped answer must be served from the cache"

    # A version mapped to null also falls back, but stays uncached like
    # the other malformed-content arms.
    sb.official_store_branch_cache = None
    sb.get_remote_file = lambda *args, **kwargs: json.dumps({gl.app_version: None})
    assert sb.get_official_store_branch() == StoreBackend.STORE_BRANCH
    assert sb.official_store_branch_cache is None

    # A mapped version still follows the mapping, and caches it.
    sb.get_remote_file = lambda *args, **kwargs: json.dumps({gl.app_version: "pinned-branch"})
    assert sb.get_official_store_branch() == "pinned-branch"
    assert sb.official_store_branch_cache == "pinned-branch"


def main() -> None:
    test_resolver_precedence_and_validation()
    test_prepare_families_on_hash_entry()
    test_branch_wins_over_any_pin()
    test_claim_reads_the_hash_revision()
    test_official_branch_unmapped_version_falls_back()
    print("scenario_store_two_shape: OK")


if __name__ == "__main__":
    main()
