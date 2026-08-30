"""Verify map and flat-hash catalog pins across prepare, update, and claim paths."""

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

import json
import threading
from types import SimpleNamespace

from PIL import Image

from src.backend.Store.catalog_entry import PinnedRevision, resolve_pinned_revision
from src.backend.Store.StoreBackend import InstalledAsset, StoreBackend
from src.backend.Store.prepare_pool import PreparePool
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
    sb._prepare_pool = PreparePool(StoreBackend.MAX_CONCURRENT_REQUESTS)
    sb.official_authors = []
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

    # A valid flat hash takes priority because mixed-entry map keys are plugin versions.
    mixed = {"hash": HASH_SHA, "commits": {COMPATIBLE_VERSION: MAP_SHA}}
    assert resolve_pinned_revision(mixed) == PinnedRevision(HASH_SHA, True)
    mixed_incompat = {"hash": HASH_SHA, "commits": {INCOMPATIBLE_VERSION: INCOMPAT_SHA}}
    assert resolve_pinned_revision(mixed_incompat) == PinnedRevision(HASH_SHA, True)

    # Invalid flat hashes fall back to the map; both gates require exactly 40 hex characters.
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
    """List hash-only entries as compatible and defer compatibility to their manifests."""
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
    """Resolve a plugin branch before any valid or invalid adjacent pin at every site."""
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


def test_official_ref_is_the_vetted_pin() -> None:
    """Use a full immutable STORE_PIN for the official catalog without a remote lookup."""
    from src.backend.Store.catalog_entry import COMMIT_SHA_RE

    _stub_globals()
    sb = _make_backend()
    def no_fetch(*args, **kwargs):
        raise AssertionError("resolving the official ref must fetch nothing")
    sb.get_remote_file = no_fetch

    assert COMMIT_SHA_RE.fullmatch(StoreBackend.STORE_PIN), (
        "STORE_PIN must be a full commit sha, not a movable ref"
    )
    assert sb.get_official_store_branch() == StoreBackend.STORE_PIN
    assert sb.get_stores()[0] == (StoreBackend.STORE_REPO_URL, StoreBackend.STORE_PIN)

    # The authors file reads at the same pin, and without a forced
    # refetch: a pinned commit is immutable, so the cache may serve it.
    calls: list = []
    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps(["acme"])
    sb.get_remote_file = capture
    assert sb.get_official_authors() == ["acme"]
    (args, kwargs), = calls
    assert args[2] == StoreBackend.STORE_PIN, f"authors must read at the pin, got {args!r}"
    assert not kwargs.get("force_refetch"), "an immutable ref must not force a refetch"


def test_catalog_refetch_follows_ref_mutability() -> None:
    """Use cached immutable pins offline and force refetches for movable branches."""
    _stub_globals()
    sb = _make_backend()
    seen: list = []
    def capture(url, filename, branch, force_refetch=False):
        seen.append(force_refetch)
        return "[]"
    sb.get_remote_file = capture
    sb.fetch_and_parse_store_json(URL, "Plugins.json", StoreBackend.STORE_PIN)
    sb.fetch_and_parse_store_json(URL, "Plugins.json", "main")
    assert seen == [False, True], (
        f"refetch must follow ref mutability (pin, branch), got {seen!r}"
    )


def main() -> None:
    test_resolver_precedence_and_validation()
    test_prepare_families_on_hash_entry()
    test_branch_wins_over_any_pin()
    test_claim_reads_the_hash_revision()
    test_official_ref_is_the_vetted_pin()
    test_catalog_refetch_follows_ref_mutability()
    print("scenario_store_two_shape: OK")


if __name__ == "__main__":
    main()
