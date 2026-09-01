"""Verify offline update-check cost and ORIGIN-based install identity."""

# Once the installs are stamped, an update check fetches the catalog files and
# nothing else.

import io
import json
import os
import shutil
import threading
from types import SimpleNamespace

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from PIL import Image

import src.backend.Store.StoreBackend as store_backend_module
from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.prepare_pool import PreparePool
from src.backend.Store.store_result import Ok, Err, StoreFetchError


APP_MAJOR = int(gl.app_version.split(".")[0])
OLD_VERSION = f"{APP_MAJOR}.0.0"
NEW_VERSION = f"{APP_MAJOR}.1.0"


def _sha(seed: str) -> str:
    """A 40 hex char stand-in for a commit sha, derived from a name, so a
    failure names the entry it came from."""
    body = "".join(c for c in seed.lower() if c in "0123456789abcdef") or "a"
    return (body * 40)[:40]


class _Entry:
    """One catalog entry plus the local state that decides its verdict.
    """
    # installed selects the local SHA; lists_old_sha keeps or replaces the old catalog pin.

    def __init__(self, repo: str, asset_id: str, installed: str | None = None,
                 lists_old_sha: bool = False, stamped: bool = True,
                 symlink: bool = False, readable_sha: bool = True,
                 shared_sha: str | None = None, branch: str | None = None,
                 stamped_url: str | None = None, bad_manifest: bool = False,
                 versions: dict | None = None):
        self.repo = repo
        self.asset_id = asset_id
        self.url = f"https://github.com/acme/{repo}"
        # A renamed or transferred repository can retain its prior URL in ORIGIN.
        self.stamped_url = stamped_url
        # Serves something that is not json where the manifest should be.
        self.bad_manifest = bad_manifest
        # A catalog whose version keys are not versions.
        self.versions = versions
        self.old_sha = _sha(repo + "old")
        self.new_sha = shared_sha or _sha(repo + "new")
        self.installed = installed
        self.lists_old_sha = lists_old_sha
        self.stamped = stamped
        self.symlink = symlink
        self.readable_sha = readable_sha
        self.branch = branch

    @property
    def local_sha(self) -> str | None:
        if self.installed is None:
            return None
        return self.old_sha if self.installed == "old" else self.new_sha

    def catalog_json(self) -> dict:
        if self.branch is not None:
            return {"url": self.url, "branch": self.branch}
        if self.versions is not None:
            return {"url": self.url, "commits": dict(self.versions)}
        commits = {NEW_VERSION: self.new_sha}
        if self.lists_old_sha:
            commits = {OLD_VERSION: self.old_sha, NEW_VERSION: self.new_sha}
        return {"url": self.url, "commits": commits}

    def install(self, base_dir: str, as_name: str | None = None) -> str | None:
        """Write the manifest, VERSION, and ORIGIN layout produced by install methods."""
        if self.installed is None:
            return None
        os.makedirs(base_dir, exist_ok=True)
        path = os.path.join(base_dir, as_name or self.asset_id)
        if self.symlink:
            target = os.path.join(gl.DATA_PATH, "checkouts", self.asset_id)
            os.makedirs(target, exist_ok=True)
            _write_asset_files(target, self.asset_id, self.local_sha,
                               (self.stamped_url or self.url) if self.stamped else None,
                               self.readable_sha)
            if os.path.islink(path):
                os.remove(path)
            os.symlink(target, path)
            return path
        os.makedirs(path, exist_ok=True)
        _write_asset_files(path, self.asset_id, self.local_sha,
                           (self.stamped_url or self.url) if self.stamped else None,
                           self.readable_sha)
        return path


def _write_asset_files(path: str, asset_id: str, sha: str | None,
                       origin: str | None, readable_sha: bool) -> None:
    with open(os.path.join(path, "manifest.json"), "w") as f:
        json.dump({"id": asset_id, "name": asset_id, "version": NEW_VERSION}, f)
    if readable_sha and sha is not None:
        with open(os.path.join(path, "VERSION"), "w") as f:
            f.write(sha)
    if origin is not None:
        with open(os.path.join(path, StoreBackend.ORIGIN_FILE), "w") as f:
            f.write(origin + "\n")


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


THUMBNAIL_BYTES = _png_bytes()


class _RemoteResponse:
    """What request_from_url hands back. get_remote_file reads .text for a
    text fetch and .content for a binary one."""

    def __init__(self, text: str = "", content: bytes = b""):
        self.text = text
        self.content = content


class _FakeStore:
    """Serve store data from memory and count each request type."""

    def __init__(self, catalogs: dict):
        self.catalogs = catalogs
        self.entries = [entry for entries in catalogs.values() for entry in entries]
        self.urls: list[str] = []
        self.manifest_fetches = 0
        self.image_fetches = 0
        self.image_decodes = 0
        self._lock = threading.Lock()

    def request_from_url(self, url: str) -> "_RemoteResponse":
        with self._lock:
            self.urls.append(url)
        path = url.split("/", 5)[-1] if url.count("/") >= 5 else url
        # Match exact filenames because the SD+ filename ends with Wallpapers.json.
        entries = self.catalogs.get(path.rsplit("/", 1)[-1])
        if entries is not None:
            return _RemoteResponse(text=json.dumps([e.catalog_json() for e in entries]))
        if path.endswith("manifest.json"):
            entry = self._entry_for(url)
            if entry is None:
                raise StoreFetchError(url, "not found")
            if entry.bad_manifest:
                # What a truncated write or a proxy error page serves in
                # place of json.
                with self._lock:
                    self.manifest_fetches += 1
                return _RemoteResponse(text="<html>504 Gateway Timeout</html>")
            with self._lock:
                self.manifest_fetches += 1
            return _RemoteResponse(text=json.dumps({
                "id": entry.asset_id,
                "name": entry.repo,
                "version": NEW_VERSION,
                "thumbnail": "store/Thumbnail.png",
            }))
        if path.endswith("attribution.json"):
            # Optional attribution files still cost one request when they return 404.
            raise StoreFetchError(url, "not found")
        if path.endswith(".png"):
            with self._lock:
                self.image_fetches += 1
            return _RemoteResponse(content=THUMBNAIL_BYTES)
        raise StoreFetchError(url, "not found")

    def _entry_for(self, url: str) -> "_Entry | None":
        for entry in self.entries:
            if f"/acme/{entry.repo}/" in url:
                return entry
        return None

    def requests_for_repo(self, repo: str) -> list[str]:
        return [url for url in self.urls if f"/acme/{repo}/" in url]

    @property
    def catalog_requests(self) -> list[str]:
        return [url for url in self.urls if url.rsplit("/", 1)[-1] in self.catalogs]

    @property
    def non_catalog_requests(self) -> list[str]:
        catalog = set(self.catalog_requests)
        return [url for url in self.urls if url not in catalog]


def _catalogs(plugins=(), icons=(), wallpapers=(), sd_plus=()) -> dict:
    return {
        StoreBackend.PLUGIN_FILE: list(plugins),
        StoreBackend.ICON_FILE: list(icons),
        StoreBackend.WALLPAPERS_FILE: list(wallpapers),
        StoreBackend.SDPLUSWALLPAPERS_FILE: list(sd_plus),
    }


def _asset_dirs(sb: StoreBackend) -> tuple[str, ...]:
    return (gl.PLUGIN_DIR, sb.icons_dir(), sb.wallpapers_dir(),
            sb.sd_plus_bar_wallpapers_dir())


def _reset_local_state() -> None:
    """Reset installs and store cache so cached manifests cannot hide fetches."""
    sb = StoreBackend.__new__(StoreBackend)
    for base_dir in _asset_dirs(sb) + (os.path.join(gl.DATA_PATH, "checkouts"),):
        shutil.rmtree(base_dir, ignore_errors=True)
        os.makedirs(base_dir, exist_ok=True)
    shutil.rmtree(os.path.join(gl.DATA_PATH, "Store"), ignore_errors=True)


def _make_backend(store: _FakeStore) -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__, which spawns a fetch thread
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    # What __init__ would have built for the catalog fan-out.
    sb._fetch_limiter = threading.Semaphore(StoreBackend.MAX_CONCURRENT_REQUESTS)
    sb._prepare_pool = PreparePool(StoreBackend.MAX_CONCURRENT_REQUESTS)
    sb.official_authors = []
    sb.request_from_url = store.request_from_url
    return sb


class _Offline:
    """Refuse shared-session access that bypasses the per-backend request stub."""

    def __enter__(self):
        self._get = store_backend_module.http_client.get
        self._download = store_backend_module.http_client.download_to_file

        def refuse(*args, **kwargs):
            raise AssertionError(f"the update check reached the network: {args!r}")

        store_backend_module.http_client.get = refuse
        store_backend_module.http_client.download_to_file = refuse
        return self

    def __exit__(self, *exc):
        store_backend_module.http_client.get = self._get
        store_backend_module.http_client.download_to_file = self._download
        return False


class _CountingDecodes:
    def __init__(self, store: _FakeStore):
        self.store = store

    def __enter__(self):
        self._real_open = store_backend_module.Image.open

        def counting_open(*args, **kwargs):
            self.store.image_decodes += 1
            return self._real_open(*args, **kwargs)

        store_backend_module.Image.open = counting_open
        return self

    def __exit__(self, *exc):
        store_backend_module.Image.open = self._real_open
        return False


# Refuse update records that omit fields dereferenced by their install method.
INSTALL_FIELDS = {
    "plugin": ("github", "plugin_id", "commit_sha"),
    "icon": ("github", "icon_id", "commit_sha"),
    "wallpaper": ("github", "wallpaper_id", "commit_sha"),
    "sd_plus": ("github", "id", "commit_sha"),
}


def _recording_installs(sb: StoreBackend) -> list[tuple[str, str]]:
    """Stubs every install_* to record (asset id, commit sha), after it
    asserts the data object carries what the real one dereferences."""
    installed: list[tuple[str, str]] = []

    def record(kind: str, data, ok):
        for field in INSTALL_FIELDS[kind]:
            assert getattr(data, field), (
                f"install_{kind} reads {field}, which the update check left "
                f"{getattr(data, field)!r} on {data!r}"
            )
        assert hasattr(data, "branch"), "install_plugin reads branch"
        assert data.is_compatible is not False, (
            f"an incompatible entry must never reach install_{kind}: {data!r}"
        )
        installed.append((getattr(data, INSTALL_FIELDS[kind][1]), data.commit_sha))
        return ok

    sb.install_plugin = lambda data, auto_update=False: record("plugin", data, True)
    sb.install_icon = lambda data: record("icon", data, 200)
    sb.install_wallpaper = lambda data: record("wallpaper", data, 200)
    sb.install_sd_plus_bar_wallpaper = lambda data: record("sd_plus", data, 200)
    return installed


def _stub_globals(**kwargs) -> None:
    fixtures.install_stub_globals(**kwargs)
    gl.lm = SimpleNamespace(get_custom_translation=lambda translations: None)


# Outdated fixtures replace a version-key SHA, so the catalog no longer lists the installed SHA.
def _main_catalogs() -> dict:
    plugins = [_Entry(f"Uninstalled{i}Plugin", f"com_acme_Uninstalled{i}Plugin") for i in range(6)]
    plugins += [
        _Entry("CurrentPlugin", "com_acme_CurrentPlugin", installed="new"),
        _Entry("OutdatedPlugin", "com_acme_OutdatedPlugin", installed="old"),
    ]
    icons = [
        _Entry("UninstalledIcons0", "com_acme_UninstalledIcons0"),
        _Entry("UninstalledIcons1", "com_acme_UninstalledIcons1"),
        _Entry("OutdatedIcons", "com_acme_OutdatedIcons", installed="old"),
    ]
    wallpapers = [
        _Entry("UninstalledWalls0", "com_acme_UninstalledWalls0"),
        _Entry("UninstalledWalls1", "com_acme_UninstalledWalls1"),
        _Entry("CurrentWalls", "com_acme_CurrentWalls", installed="new"),
    ]
    sd_plus = [
        _Entry("UninstalledBars0", "com_acme_UninstalledBars0"),
        _Entry("UninstalledBars1", "com_acme_UninstalledBars1"),
        _Entry("OutdatedBars", "com_acme_OutdatedBars", installed="old"),
    ]
    return _catalogs(plugins, icons, wallpapers, sd_plus)


def _install_catalogs(sb: StoreBackend, catalogs: dict) -> None:
    for filename, base_dir in (
        (StoreBackend.PLUGIN_FILE, gl.PLUGIN_DIR),
        (StoreBackend.ICON_FILE, sb.icons_dir()),
        (StoreBackend.WALLPAPERS_FILE, sb.wallpapers_dir()),
        (StoreBackend.SDPLUSWALLPAPERS_FILE, sb.sd_plus_bar_wallpapers_dir()),
    ):
        for entry in catalogs[filename]:
            entry.install(base_dir)


def test_update_check_skips_uninstalled() -> None:
    _stub_globals()
    _reset_local_state()

    catalogs = _main_catalogs()
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    _install_catalogs(sb, catalogs)
    installed = _recording_installs(sb)

    with _Offline(), _CountingDecodes(store):
        n_updated = sb.update_everything()

    entries = store.entries
    print(
        f"scenario_store_update_check_cost: {len(entries)} catalog entries "
        f"({sum(1 for e in entries if e.installed is None)} uninstalled, "
        f"{sum(1 for e in entries if e.installed is not None)} installed) -> "
        f"{len(store.urls)} requests "
        f"({len(store.catalog_requests)} catalog, {len(store.non_catalog_requests)} per-entry), "
        f"{store.manifest_fetches} manifest fetches, "
        f"{store.image_fetches} image fetches, {store.image_decodes} image decodes"
    )

    # Every outdated entry here had its sha replaced in place under the same
    # version key, which is what the real store does on a bump.
    assert isinstance(n_updated, Ok) and n_updated.value == 3, (
        f"the three outdated installed assets must be updated -- an install "
        f"whose sha the catalog replaced in place is still that install, "
        f"got {n_updated!r}"
    )
    assert sorted(installed) == sorted([
        ("com_acme_OutdatedPlugin", _sha("OutdatedPluginnew")),
        ("com_acme_OutdatedIcons", _sha("OutdatedIconsnew")),
        ("com_acme_OutdatedBars", _sha("OutdatedBarsnew")),
    ]), (
        "exactly the outdated installed assets must be reinstalled at the "
        f"newest compatible commit, got {installed}"
    )

    assert store.image_fetches == 0, (
        f"an update check must not download thumbnails, got {store.image_fetches}"
    )
    assert store.image_decodes == 0, (
        f"an update check must not decode images, got {store.image_decodes}"
    )

    for entry in entries:
        if entry.installed is not None:
            continue
        assert store.requests_for_repo(entry.repo) == [], (
            f"an entry the user never installed ({entry.repo}) must cost no "
            f"request of its own, got {store.requests_for_repo(entry.repo)}"
        )

    assert store.non_catalog_requests == [], (
        "stamped installs answer the update question locally -- nothing "
        f"beyond the catalog files may be fetched, got {store.non_catalog_requests}"
    )
    assert len(store.catalog_requests) == 4, (
        f"exactly one fetch per catalog file, got {store.catalog_requests}"
    )


def test_backup_directory_never_claims() -> None:
    """Do not claim an ORIGIN-matching backup whose directory name differs from its manifest ID."""
    _stub_globals()
    _reset_local_state()

    entry = _Entry("Alpha", "com_acme_Alpha", installed="old")
    catalogs = _catalogs(plugins=[entry])
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    entry.install(gl.PLUGIN_DIR)
    entry.install(gl.PLUGIN_DIR, as_name="com_acme_Alpha_backup")
    installed = _recording_installs(sb)

    with _Offline():
        to_update = sb.update_all_plugins()

    assert isinstance(to_update, Ok) and to_update.value == 1, (
        f"exactly the real install may be updated, got {to_update!r}"
    )
    assert installed == [("com_acme_Alpha", entry.new_sha)], (
        f"the copy kept aside must never be installed over, got {installed}"
    )

    # With only the copy present there is nothing safe to update.
    _reset_local_state()
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    entry.install(gl.PLUGIN_DIR, as_name="com_acme_Alpha_backup")
    installed = _recording_installs(sb)
    with _Offline():
        n = sb.update_all_plugins()
    assert isinstance(n, Ok) and (n.value, installed) == (0, []), (
        f"a directory whose name is not its manifest id must not be installed "
        f"over -- that download is refused by the staged-id check, got {n}, {installed}"
    )


def test_shared_commit_resolves_own_install() -> None:
    """Resolve same-commit fork entries by repository identity, not SHA alone."""
    _stub_globals()
    _reset_local_state()

    shared = _sha("sharedforkcommit")
    upstream = _Entry("Upstream", "com_acme_Upstream", installed="old", shared_sha=shared)
    fork = _Entry("Fork", "com_acme_Fork", installed="old", shared_sha=shared)
    catalogs = _catalogs(plugins=[upstream, fork])
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    upstream.install(gl.PLUGIN_DIR)
    fork.install(gl.PLUGIN_DIR)

    with _Offline():
        to_update = sb.get_plugins_to_update()

    assert not isinstance(to_update, Err)
    ids = sorted(plugin.plugin_id for plugin in to_update.value)
    assert ids == ["com_acme_Fork", "com_acme_Upstream"], (
        f"each entry must resolve to its own install, got {ids}"
    )
    for plugin in to_update.value:
        assert plugin.plugin_id.split("_")[-1] in plugin.github, (
            f"{plugin.plugin_id} resolved to {plugin.github}"
        )


def test_legacy_install_identified_once() -> None:
    """Identify and stamp each legacy install with one manifest fetch, once."""
    _stub_globals()
    _reset_local_state()

    legacy = _Entry("Legacy", "com_acme_Legacy", installed="old", stamped=False)
    others = [_Entry(f"Other{i}", f"com_acme_Other{i}") for i in range(5)]
    catalogs = _catalogs(plugins=[legacy] + others)
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    _install_catalogs(sb, catalogs)
    installed = _recording_installs(sb)

    with _Offline():
        n = sb.update_all_plugins()

    print(
        f"scenario_store_update_check_cost: legacy sweep over "
        f"{len(catalogs[StoreBackend.PLUGIN_FILE])} entries with 1 unstamped install "
        f"-> {store.manifest_fetches} manifest fetches, {store.image_fetches} image fetches"
    )
    assert isinstance(n, Ok) and (n.value, installed) == (1, [("com_acme_Legacy", legacy.new_sha)]), (
        f"the legacy install must still be identified and updated, got {n}, {installed}"
    )
    assert store.image_fetches == 0, "the legacy lookup must not fetch images"
    assert store.manifest_fetches == 1, (
        f"identifying one legacy install must cost one manifest fetch, not one "
        f"per catalog entry, got {store.manifest_fetches}"
    )

    origin = os.path.join(gl.PLUGIN_DIR, "com_acme_Legacy", StoreBackend.ORIGIN_FILE)
    assert os.path.isfile(origin), "identifying a legacy install must stamp it"
    with open(origin) as f:
        assert f.read().strip() == legacy.url

    # On a second launch with a cold store cache the stamp answers, so
    # nothing is fetched.
    shutil.rmtree(os.path.join(gl.DATA_PATH, "Store"), ignore_errors=True)
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    _recording_installs(sb)
    with _Offline():
        sb.update_all_plugins()
    assert store.manifest_fetches == 0, (
        f"a stamped install must cost no manifest fetch, got {store.manifest_fetches}"
    )
    assert store.non_catalog_requests == [], (
        f"nothing beyond the catalog may be fetched once stamped, got {store.non_catalog_requests}"
    )


def test_symlinked_install_left_alone() -> None:
    """Do not auto-update user-managed symlink installs."""
    _stub_globals()
    _reset_local_state()

    # A link whose target was once installed carries the stamp, so the entry
    # resolves to it and the symlink guard stops the update.
    linked = _Entry("Linked", "com_acme_Linked", installed="old", symlink=True)
    catalogs = _catalogs(plugins=[linked])
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    path = linked.install(gl.PLUGIN_DIR)
    assert path is not None and os.path.islink(path)

    with _Offline():
        to_update = sb.get_plugins_to_update()

    assert not isinstance(to_update, Err)
    assert to_update.value == [], (
        f"a stamped symlinked install must not be auto-updated, got {to_update}"
    )

    # An unstamped link is not a pending legacy install either. It is never
    # looked up, and nothing is written into the tree behind it.
    _reset_local_state()
    unstamped = _Entry("Linked", "com_acme_Linked", installed="old", symlink=True, stamped=False)
    store = _FakeStore(_catalogs(plugins=[unstamped]))
    sb = _make_backend(store)
    path = unstamped.install(gl.PLUGIN_DIR)
    assert path is not None
    with _Offline():
        to_update = sb.get_plugins_to_update()

    assert isinstance(to_update, Ok) and to_update.value == [], (
        f"an unstamped symlink must not be auto-updated, got {to_update}"
    )
    assert store.manifest_fetches == 0, (
        f"a symlinked install must not be looked up at all, got {store.manifest_fetches}"
    )
    assert not os.path.exists(os.path.join(path, StoreBackend.ORIGIN_FILE)), (
        "nothing may be written into a tree this app does not own"
    )


def test_install_unreadable_sha_repaired() -> None:
    """A tree with neither .git nor VERSION is half-written or hand-copied.
    It compares unequal to every commit, so it is reinstalled."""
    _stub_globals()
    _reset_local_state()

    broken = _Entry("Broken", "com_acme_Broken", installed="old", readable_sha=False)
    catalogs = _catalogs(plugins=[broken])
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    broken.install(gl.PLUGIN_DIR)
    installed = _recording_installs(sb)

    with _Offline():
        n = sb.update_all_plugins()

    assert isinstance(n, Ok) and (n.value, installed) == (1, [("com_acme_Broken", broken.new_sha)]), (
        f"an install with no readable sha must be repaired, got {n}, {installed}"
    )


def test_branch_pinned_entry_resolves_tip() -> None:
    """Resolve a custom branch tip while using ORIGIN identity without a manifest fetch."""
    _reset_local_state()
    _stub_globals(app_settings={
        "store": {
            "enable-custom-plugins": True,
            "custom-plugins": [{"url": "https://github.com/acme/CustomPlugin", "branch": "main"}],
        },
    })

    custom = _Entry("CustomPlugin", "com_acme_CustomPlugin", installed="new", branch="main")
    store = _FakeStore(_catalogs())
    sb = _make_backend(store)
    custom.install(gl.PLUGIN_DIR)

    commit_lookups: list[str] = []

    def fake_last_commit(repo_url: str, branch_name: str = "main"):
        commit_lookups.append(f"{repo_url}@{branch_name}")
        return custom.new_sha

    sb.get_last_commit = fake_last_commit
    _recording_installs(sb)

    with _Offline():
        to_update = sb.get_plugins_to_update()

    assert not isinstance(to_update, Err)
    ids = [plugin.plugin_id for plugin in to_update.value]
    assert "com_acme_CustomPlugin" not in ids, (
        f"a custom plugin sitting on the branch tip needs no update, got {ids}"
    )
    assert commit_lookups == ["https://github.com/acme/CustomPlugin@main"], (
        f"exactly one tip lookup for the one branch-pinned entry, got {commit_lookups}"
    )
    assert store.manifest_fetches == 0, (
        f"a stamped branch-pinned install must not cost a manifest, got {store.manifest_fetches}"
    )


def test_store_window_gets_full_prepare() -> None:
    """Fetch full row data and stamp identified installs when include_images is true."""
    _stub_globals()
    _reset_local_state()

    legacy = _Entry("Legacy", "com_acme_Legacy", installed="old", stamped=False)
    plugins_catalog = [legacy] + [_Entry(f"Other{i}", f"com_acme_Other{i}") for i in range(3)]
    catalogs = _catalogs(plugins=plugins_catalog)
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    _install_catalogs(sb, catalogs)

    with _Offline(), _CountingDecodes(store):
        result = sb.get_all_plugins()

    # get_all_* is the typed read boundary, an Ok carrying the catalog list.
    assert isinstance(result, Ok)
    plugins = result.value
    assert len(plugins) == len(plugins_catalog), (
        f"the store window must still list every catalog entry, got {len(plugins)}"
    )
    assert store.image_fetches == len(plugins_catalog), (
        f"every listed entry must still get its thumbnail, got {store.image_fetches}"
    )
    assert store.image_decodes == len(plugins_catalog), (
        f"every fetched thumbnail must still be decoded, got {store.image_decodes}"
    )
    uninstalled = plugins_catalog[1]
    assert store.requests_for_repo(uninstalled.repo), (
        "the store window must still describe entries the user has not installed"
    )
    shown = next(p for p in plugins if p.plugin_id == "com_acme_Legacy")
    assert shown.local_sha == legacy.local_sha, (
        f"the full prepare must still read the installed sha, got {shown.local_sha!r}"
    )
    assert os.path.isfile(os.path.join(gl.PLUGIN_DIR, "com_acme_Legacy", StoreBackend.ORIGIN_FILE)), (
        "a full prepare that identifies an install must record the link it "
        "paid a request for"
    )


def test_bad_entry_does_not_abort_pass() -> None:
    """Continue the pre-pass after invalid manifest JSON or invalid version keys."""
    _stub_globals()
    _reset_local_state()

    legacy = _Entry("Legacy", "com_acme_Legacy", installed="old", stamped=False)
    # Nothing claims it, so the walk covers the whole catalog, including the
    # entries that raise.
    orphan = _Entry("Orphan", "com_acme_Orphan", installed="old", stamped=False)
    broken_manifest = _Entry("BrokenManifest", "com_acme_BrokenManifest", bad_manifest=True)
    broken_versions = _Entry("BrokenVersions", "com_acme_BrokenVersions",
                             versions={"latest": _sha("brokenversions")})
    healthy = _Entry("Healthy", "com_acme_Healthy", installed="old")
    catalogs = _catalogs(plugins=[broken_manifest, broken_versions, legacy, healthy])
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    _install_catalogs(sb, catalogs)
    orphan.install(gl.PLUGIN_DIR)
    installed = _recording_installs(sb)

    with _Offline():
        n = sb.update_all_plugins()

    assert isinstance(n, Ok) and n.value == 2, f"a raising entry must not stop the pass, got {n!r}"
    assert sorted(installed) == sorted([
        ("com_acme_Legacy", legacy.new_sha),
        ("com_acme_Healthy", healthy.new_sha),
    ]), (
        f"every healthy entry must still be processed, got {installed}"
    )
    assert sb._installed_index is None, (
        "the pass state must be cleared however the pass ends"
    )

    # The orphan nothing claimed is not walked for again this session.
    fetches_after_first_walk = store.manifest_fetches
    with _Offline():
        sb.update_all_plugins()
    assert store.manifest_fetches == fetches_after_first_walk, (
        f"a directory nothing claims must not be walked for again this "
        f"session, got {store.manifest_fetches - fetches_after_first_walk} more fetches"
    )


def test_unclaimed_origin_is_reidentified_and_restamped() -> None:
    """Re-identify and restamp an install whose ORIGIN names an unclaimed repository."""
    _stub_globals()
    _reset_local_state()

    renamed = _Entry("NewName", "com_acme_Widget", installed="old",
                     stamped_url="https://github.com/acme/OldName")
    catalogs = _catalogs(plugins=[renamed])
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    renamed.install(gl.PLUGIN_DIR)
    installed = _recording_installs(sb)

    with _Offline():
        n = sb.update_all_plugins()

    assert isinstance(n, Ok) and (n.value, installed) == (1, [("com_acme_Widget", renamed.new_sha)]), (
        f"an install whose repository was renamed must still be updated, got {n}, {installed}"
    )
    origin = os.path.join(gl.PLUGIN_DIR, "com_acme_Widget", StoreBackend.ORIGIN_FILE)
    with open(origin) as f:
        assert f.read().strip() == renamed.url, (
            "the stamp must be rewritten to the repository that actually claims the install"
        )


def test_stamp_matches_catalog_case_insensitive() -> None:
    """Match GitHub owner and repository names case-insensitively without re-identification."""
    _stub_globals()
    _reset_local_state()

    entry = _Entry("Widget", "com_acme_Widget", installed="old",
                   stamped_url="https://github.com/ACME/Widget")
    store = _FakeStore(_catalogs(plugins=[entry]))
    sb = _make_backend(store)
    entry.install(gl.PLUGIN_DIR)
    installed = _recording_installs(sb)

    with _Offline():
        n = sb.update_all_plugins()

    assert isinstance(n, Ok) and (n.value, installed) == (1, [("com_acme_Widget", entry.new_sha)]), (
        f"a differently cased stamp names the same repository, got {n}, {installed}"
    )
    assert store.manifest_fetches == 0, (
        f"a differently cased stamp must not need re-identifying, got {store.manifest_fetches}"
    )


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_store_update_check_cost")
    test_update_check_skips_uninstalled()
    test_backup_directory_never_claims()
    test_shared_commit_resolves_own_install()
    test_legacy_install_identified_once()
    test_symlinked_install_left_alone()
    test_install_unreadable_sha_repaired()
    test_branch_pinned_entry_resolves_tip()
    test_store_window_gets_full_prepare()
    test_bad_entry_does_not_abort_pass()
    test_unclaimed_origin_is_reidentified_and_restamped()
    test_stamp_matches_catalog_case_insensitive()
    print("scenario_store_update_check_cost: PASS")


if __name__ == "__main__":
    main()
