"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
 
import contextlib
import re
import sys
import zipfile
import requests
import json
from collections.abc import Callable
from concurrent.futures import CancelledError
from typing import Any, Literal, NamedTuple, TypeGuard, cast, overload
from PIL import Image
from io import BytesIO
from loguru import logger as log
import subprocess
import time
import os
import shutil
import threading

from gi.repository import GLib

from autostart import is_flatpak
from src.backend.Store.StoreCache import StoreCache
from src.backend.Store.StoreURL import RepoRef, parse_repo_url
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend import archive_safety, http_client, services

from src.Signals import Signals

import globals as gl
from src.windows.Store.StoreData import (
    IconData,
    PluginData,
    SDPlusBarWallpaperData,
    StoreData,
    StoreDataT,
    WallpaperData,
    is_min_app_version_satisfied,
)
from src.backend.Store.asset_types import (
    ASSET_TYPES,
    AssetTypeDescriptor,
    ICON,
    PLUGIN,
    SD_PLUS_BAR,
    WALLPAPER,
)
from src.backend.Store import install_recovery, install_reload, install_script, json_root
from src.backend.Store.prepare_pool import PreparePool
from src.backend.Store.catalog_entry import COMMIT_SHA_RE, resolve_pinned_revision
from src.backend.Store.data_type import DataType
from src.backend.Store.store_result import Err, ErrReason, Ok, StoreFetchError, StoreResult


class _ResolvedVersion(NamedTuple):
    """A resolved commit, compatibility verdict, and optional plugin branch."""
    compatible: bool
    commit: str | None
    branch: str | None


def same_repository(a: RepoRef | None, b: RepoRef | None) -> bool:
    """Compare GitHub repository identities without case sensitivity."""
    if a is None or b is None:
        return False
    return (a.user.casefold(), a.repo.casefold()) == (b.user.casefold(), b.repo.casefold())


def repository_key(ref: RepoRef) -> tuple[str, str]:
    """The case-insensitive identity of a repository, for set membership."""
    return (ref.user.casefold(), ref.repo.casefold())


class InstalledAsset(NamedTuple):
    """Local asset metadata, including directory and manifest identities."""
    asset_id: str
    path: str
    sha: str                  # "" when neither .git nor VERSION can be read
    origin: RepoRef | None    # from the ORIGIN stamp, None for an old install
    manifest_id: str | None   # None when the tree has no readable manifest
    is_symlink: bool


class UpdateCheck(NamedTuple):
    """The local and target state needed to decide whether an update exists."""
    url: str
    ref: RepoRef
    asset_id: str | None      # None when the entry is not installed
    local_sha: str | None     # the installed asset's sha, None when not installed
    commit_sha: str | None    # the sha the entry should be installed at
    branch: str | None
    compatible: bool


class StoreBackend:
    STORE_REPO_URL = "https://github.com/StreamController/StreamController-Store" #"https://github.com/StreamController/StreamController-Store"
    STORE_CACHE_PATH = "Store/cache"

    # Read the official catalog only at this vetted commit, never a branch tip.
    # Validate replacements with scripts/vet_store_pin.py and the app-version gate.
    STORE_PIN = "aac7c77cc74f92c46bcd816fe07963d3cff641c3"

    # Connect an installed manifest id to its source repository without a fetch.
    ORIGIN_FILE = "ORIGIN"

    WALLPAPERS_FILE = "Wallpapers.json"
    PLUGIN_FILE = "Plugins.json"
    ICON_FILE = "Icons.json"
    SDPLUSWALLPAPERS_FILE = "SDPlusBarWallpapers.json"


    # Keep the fetch semaphore and shared connection pool at the same limit.
    MAX_CONCURRENT_REQUESTS = http_client.POOL_MAXSIZE

    # Permit one non-hidden path component of at most 128 safe characters.
    ASSET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    @classmethod
    def is_safe_asset_id(cls, asset_id: object) -> TypeGuard[str]:
        """Check a manifest id as one path component without normalizing it."""
        return isinstance(asset_id, str) and bool(cls.ASSET_ID_PATTERN.fullmatch(asset_id))

    # Use the same commit-sha shape for catalog resolution and installation.
    COMMIT_SHA_PATTERN = COMMIT_SHA_RE

    # Accept common ref characters but reject whitespace, shell syntax, and leading dashes.
    SAFE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")

    # Cache installed assets only during one update-check pass.
    # Replacing the complete mapping lets workers see this snapshot or no snapshot.
    _installed_index: "dict[str, dict[str, InstalledAsset]] | None" = None

    # Do not repeat full-catalog searches for unclaimed installs this session.
    _unresolvable_installs: "frozenset[str]" = frozenset()

    @classmethod
    def is_safe_commit_sha(cls, commit_sha: object) -> TypeGuard[str]:
        return isinstance(commit_sha, str) and bool(cls.COMMIT_SHA_PATTERN.fullmatch(commit_sha))

    @classmethod
    def is_safe_ref_name(cls, ref_name: object) -> TypeGuard[str]:
        """Check a remote ref for safe use as a git argument."""
        return isinstance(ref_name, str) and bool(cls.SAFE_REF_PATTERN.fullmatch(ref_name))

    def __init__(self) -> None:
        self.store_cache = StoreCache()

        # Limit HTTP concurrency across catalog, install, and update threads.
        self._fetch_limiter = threading.Semaphore(self.MAX_CONCURRENT_REQUESTS)

        self._prepare_pool = PreparePool(self.MAX_CONCURRENT_REQUESTS)

        # The fallback list, until the background fetch below answers.
        self.official_authors = ["Core447", "StreamController"]
        threading.Thread(target=self._fetch_official_authors_background, daemon=True).start()

    def shutdown(self) -> None:
        """Release the catalog preparation pool."""
        self._prepare_pool.shutdown()

    def _fetch_official_authors_background(self) -> None:
        """Fetches official authors in a background thread and updates self.official_authors."""
        try:
            self.official_authors = self.get_official_authors()
            log.info(f"Official authors updated: {self.official_authors}")
        except Exception as e:
            log.warning(f"Failed to fetch official authors, using fallback: {e}")

    def get_stores(self) -> list[tuple[str, str]]:
        settings = gl.settings_manager.app()

        stores: list[tuple[str, str]] = []
        stores.append((self.STORE_REPO_URL, self.get_official_store_branch()))

        if settings.enable_custom_stores:
            for store in settings.custom_stores:
                url = store.get("url")
                if not url:
                    continue
                if parse_repo_url(url) is None:
                    # Isolate an invalid custom-store setting from the catalog load.
                    log.error(f"Skipping custom store {url!r}: not a store repository url")
                    continue
                custom_branch = store.get("branch")
                if not isinstance(custom_branch, str) or not custom_branch:
                    # Custom stores default to main; the official store uses its vetted pin.
                    custom_branch = "main"
                stores.append((url, custom_branch))

        return stores
    
    def get_custom_plugins(self) -> list[tuple[str, str | None]]:
        # Preserve None to request the custom plugin repository's default branch.
        settings = gl.settings_manager.app()

        plugins: list[tuple[str, str | None]] = []
        if settings.enable_custom_plugins:
            for plugin in settings.custom_plugins:
                url = plugin.get("url")
                if not url:
                    # Ignore an incomplete settings row.
                    continue
                if parse_repo_url(url) is None:
                    log.error(f"Skipping custom plugin {url!r}: not a store repository url")
                    continue
                plugins.append((url, plugin.get("branch")))

        return plugins
    
    def get_official_store_branch(self) -> str:
        """Return the vetted commit used for every official catalog fetch."""
        return self.STORE_PIN

    def request_from_url(self, url: str) -> "requests.Response":
        # Hold one fetch slot through connection, retries, and body read.
        pool = getattr(self, "_prepare_pool", None)  # __new__-built test backends carry no pool
        if pool is not None and pool.stopping:
            raise StoreFetchError(url, "the store backend is shutting down")
        try:
            with self._fetch_limiter:
                req = http_client.get(url, stream=True, timeout=30)
                try:
                    if req.status_code == 200:
                        req.content  # read the body while the connection is open
                        return req
                    log.error(f"Request to {url} failed with status code {req.status_code}")
                    # Drain error bodies so streamed-response close returns sockets to the pool.
                    req.content
                    raise StoreFetchError(url, f"status code {req.status_code}")
                finally:
                    req.close()  # content stays cached on the Response
        except requests.exceptions.RequestException as e:
            log.error(e)
            raise StoreFetchError(url, str(e)) from e
    
    def build_url(self, repo_url: str, file_path: str, branch_name: "str | None" = "main") -> str:
        """
        Replaces the domain in the given repository URL with "raw.githubusercontent.com" and constructs the URL for the specified file path in the repository's branch.

        Parameters:
            repo_url (str): The URL of the repository.
            file_path (str): The path of the file in the repository.
            branch_name (str, optional): Branch or commit; defaults to "main".
                         None builds an unservable URL for caller error handling.

        Returns:
            str: The constructed URL for the specified file path in the repository's branch.
        """
        repo_url = repo_url.replace("github.com", "raw.githubusercontent.com")
        return f"{repo_url}/{branch_name}/{file_path}"

    @overload
    def get_remote_file(self, repo_url: str, file_path: str, branch_name: "str | None" = ...,
                        data_type: Literal[DataType.TEXT] = ..., force_refetch: bool = ...) -> str: ...

    @overload
    def get_remote_file(self, repo_url: str, file_path: str, branch_name: "str | None" = ...,
                        data_type: Literal[DataType.CONTENT] = ..., force_refetch: bool = ...) -> bytes: ...

    def get_remote_file(self, repo_url: str, file_path: str, branch_name: "str | None" = "main",
                        data_type: DataType = DataType.TEXT,
                        force_refetch: bool = False) -> "str | bytes":
        """
        Retrieve a remote file from a GitHub repository.

        Parameters:
            repo_url (str): The URL of the GitHub repository.
            file_path (str): The path to the file within the repository.
            branch_name (str, optional): Branch or commit; defaults to "main".

        Returns:
            str: The content of the remote file.

        Note:
            - Cached files avoid repeated requests.
            - github.com URLs are rewritten to raw.githubusercontent.com.
        """
        # Keep literal modes for the cache read and write overloads.
        read_mode: Literal["r", "rb"] = "r"
        write_mode: Literal["w", "wb"] = "w"
        if data_type == DataType.CONTENT:
            read_mode, write_mode = "rb", "wb"
        elif data_type != DataType.TEXT:
            # Treat a dynamic invalid mode as text instead of returning None.
            log.error(f"Unexpected store data_type {data_type!r}; treating it as text")
            data_type = DataType.TEXT

        # Separate text and binary cache entries for the same repository path.
        is_cached = False
        if not force_refetch:
            is_cached = self.store_cache.is_cached(
                url=repo_url,
                branch=branch_name,
                path=file_path,
                data_type=data_type
            )
        if is_cached:
            with self.store_cache.open_cache_file(url=repo_url, branch=branch_name, path=file_path, data_type=data_type, mode=read_mode) as f:
                return f.read()

        url = self.build_url(repo_url, file_path, branch_name)

        answer: "requests.Response | None"
        try:
            answer = self.request_from_url(url)
        except StoreFetchError:
            answer = None  # offline or 429, so run the fallback path below
        if answer is None:
            # Permit stale fallback after a failed forced fetch.
            # Bound staleness by fetched time because reads renew the last-use clock.
            if self.store_cache.is_cached(url=repo_url, branch=branch_name, path=file_path, data_type=data_type):
                fetched = self.store_cache.get_fetched_timestamp(url=repo_url, branch=branch_name, path=file_path, data_type=data_type)
                if fetched is not None and time.time() - fetched <= StoreCache.DAYS_TO_KEEP * 24 * 60 * 60:
                    log.warning(f"Serving cached copy of {file_path} from {repo_url} after failed fetch")
                    with self.store_cache.open_cache_file(url=repo_url, branch=branch_name, path=file_path, data_type=data_type, mode=read_mode) as f:
                        return f.read()
            raise StoreFetchError(url, f"could not fetch {file_path} and no fresh cache")

        with self.store_cache.open_cache_file(url=repo_url, branch=branch_name, path=file_path, data_type=data_type, mode=write_mode) as f:
            if data_type == DataType.TEXT:
                f.write(answer.text)
            elif data_type == DataType.CONTENT:
                f.write(answer.content)

        if data_type == DataType.TEXT:
            return answer.text
        elif data_type == DataType.CONTENT:
            return cast(bytes, answer.content)

    def get_last_commit(self, repo_url: str, branch_name: str = "main") -> "str | None":
        """Resolve a branch tip under the fetch limit.
        Return None for an invalid or empty response; raise StoreFetchError on network failure."""
        ref = parse_repo_url(repo_url)
        if ref is None:
            log.error(f"Cannot resolve a commit of {repo_url!r}: not a store repository url")
            return None

        url = f"https://api.github.com/repos/{ref.user}/{ref.repo}/commits?sha={branch_name}&per_page=1"

        try:
            with self._fetch_limiter:
                response = http_client.get(url, timeout=30)
        except requests.exceptions.RequestException as e:
            log.error(f"Failed to fetch the last commit of {repo_url}@{branch_name}: {e}")
            raise StoreFetchError(repo_url, f"could not resolve {branch_name}: {e}") from e

        if response.status_code != 200:
            return None

        try:
            commits = response.json()
        except ValueError as e:
            log.error(f"Unparseable commits answer for {repo_url}@{branch_name}: {e}")
            return None
        if not isinstance(commits, list) or not commits:
            return None
        return cast("str | None", commits[0].get("sha"))
    
    def get_official_authors(self) -> list[str]:
        # Read authors at the immutable catalog pin for one consistent snapshot.
        authors_json = self.get_remote_file(self.STORE_REPO_URL, "OfficialAuthors.json", self.STORE_PIN)
        return cast(list[str], json.loads(authors_json))

    def fetch_and_parse_store_json(self, url: str, filename: str, branch: str, n_stores_with_errors: int = 0) -> "tuple[Any, int]":
        try:
            # Refetch moving refs but allow immutable commits to work from cache.
            refetch = COMMIT_SHA_RE.fullmatch(branch) is None
            store_file_json = self.get_remote_file(url, filename, branch, force_refetch=refetch)
            store_file_json = json.loads(store_file_json)
            return store_file_json, n_stores_with_errors
        except StoreFetchError:
            # Count a catalog with no usable cache as failed.
            n_stores_with_errors += 1
            return None, n_stores_with_errors
        except (json.decoder.JSONDecodeError, TypeError) as e:
            n_stores_with_errors += 1
            log.error(e)
            return None, n_stores_with_errors

    def process_store_data(self, filename: str, process_func: Callable[..., Any], get_custom_func: Callable[..., Any] | None, data_class: "type[StoreDataT]", include_images: bool = True, base_dir: str | None = None) -> "list[StoreDataT] | None":
        """Fetch configured catalogs and prepare entries on the fan-out pool.
        Without images, base_dir supplies legacy identity for the local-first update view."""
        n_stores_with_errors = 0
        data_list = []

        if not include_images:
            # Scan each asset directory once for this update-check pass.
            self._installed_index = {}
        try:
            stores = self.get_stores()

            for url, branch in stores:
                store_file_json, n_stores_with_errors = self.fetch_and_parse_store_json(url, filename, branch, n_stores_with_errors)
                if store_file_json is not None:
                    data_list.extend(store_file_json)

            if n_stores_with_errors >= len(stores):
                # Distinguish total fetch failure from a valid empty catalog.
                return None

            custom_entries = [{"url": url, "branch": branch}
                              for url, branch in (get_custom_func() if get_custom_func is not None else [])]

            if not include_images and base_dir is not None:
                # Resolve legacy installs before workers make local update decisions.
                self.resolve_unstamped_installs(base_dir, data_list + custom_entries)

            futures = [self._prepare_pool.submit(process_func, entry, include_images, True) for entry in data_list]
            futures += [self._prepare_pool.submit(process_func, asset, include_images, False)
                        for asset in custom_entries]

            # Isolate each entry failure so one bad entry cannot blank the page.
            results = []
            for future in futures:
                try:
                    results.append(future.result())
                except CancelledError:
                    continue
                except Exception as e:
                    # Drop only the failed entry; total catalog failure is handled separately.
                    log.error(f"Store item preparation failed: {e!r}")
            narrowed: list[StoreDataT] = [result for result in results if isinstance(result, data_class)]

            return narrowed
        finally:
            if not include_images:
                # Do not reuse an installed-assets snapshot across passes.
                self._installed_index = None

    def _as_store_result(self, catalog_entries: "list[StoreDataT] | None") -> "StoreResult[list[StoreDataT]]":
        # Convert total catalog failure to the typed error channel.
        if catalog_entries is None:
            return Err(ErrReason.NO_CONNECTION, "no store catalog could be fetched")
        return Ok(catalog_entries)

    def get_all_plugins(self, include_images: bool = True) -> StoreResult[list[PluginData]]:
        return self._as_store_result(self.process_store_data(self.PLUGIN_FILE, self.prepare_plugin, self.get_custom_plugins, PluginData, include_images, gl.PLUGIN_DIR))

    def get_all_icons(self, include_images: bool = True) -> StoreResult[list[IconData]]:
        return self._as_store_result(self.process_store_data(self.ICON_FILE, self.prepare_icon, None, IconData, include_images, self.icons_dir()))

    def get_all_wallpapers(self, include_images: bool = True) -> StoreResult[list[WallpaperData]]:
        return self._as_store_result(self.process_store_data(self.WALLPAPERS_FILE, self.prepare_wallpaper, None, WallpaperData, include_images, self.wallpapers_dir()))

    def get_all_sd_plus_bar_wallpapers(self, include_images: bool = True) -> StoreResult[list[SDPlusBarWallpaperData]]:
        return self._as_store_result(self.process_store_data(self.SDPLUSWALLPAPERS_FILE, self.prepare_sd_plus_bar_wallpaper, None, SDPlusBarWallpaperData, include_images, self.sd_plus_bar_wallpapers_dir()))
    
    def get_manifest(self, url: str, commit: "str | None") -> "dict[str, Any] | None":
        manifest = self.get_remote_file(url, "manifest.json", commit)
        return json_root.json_object(manifest, f"manifest.json in {url}")

    def get_attribution(self, url: str, commit: "str | None") -> dict[str, Any]:
        try:
            result = self.get_remote_file(url, "attribution.json", commit)
        except StoreFetchError:
            return {}
        try:
            return json_root.json_object(result, f"attribution.json in {url}") or {}
        except (json.decoder.JSONDecodeError, TypeError):
            return {}

    def _resolve_asset_version(self, entry: dict[str, Any], desc: AssetTypeDescriptor, url: str) -> "_ResolvedVersion | None":
        """Resolve an entry's commit and optional plugin branch.
        Return None for no valid pin; let branch-fetch failures reach the per-entry handler."""
        branch: "str | None" = entry.get("branch") if desc.is_plugin else None
        if branch is not None:
            # Give a plugin branch precedence over a pin at every resolution site.
            return _ResolvedVersion(True, self.get_last_commit(url, branch), branch)

        compatible = True
        commit: str | None = None
        if not desc.is_plugin or "commits" in entry or "hash" in entry:
            pinned = resolve_pinned_revision(entry)
            if pinned is None:
                log.error(f"Skipping store entry {url!r}: it pins no version")
                return None
            commit, compatible = pinned.sha, pinned.compatible

        return _ResolvedVersion(compatible, commit, None)

    def _fetch_thumbnail(self, url: str, thumbnail_path: Any, ref: "str | None") -> "Image.Image | None":
        # Keep entries whose thumbnail fetch fails.
        return self.get_web_image(url, thumbnail_path, ref)

    def _translate_descriptions(self, manifest: dict[str, Any]) -> "tuple[str | None, str | None]":
        return (
            gl.lm.get_custom_translation(manifest.get("descriptions", {})),
            gl.lm.get_custom_translation(manifest.get("short-descriptions", {})),
        )

    def _prepare_asset(self, entry: dict[str, Any], desc: AssetTypeDescriptor, include_image: bool = True, verified: bool = False) -> "StoreData | None":
        """Prepare one descriptor-specific catalog entry.
        Excluding images builds only the fields needed for an update decision and install."""
        base_dir = getattr(self, desc.base_dir_attr)()
        if not include_image:
            # Omit display-only fields that require remote data.
            checked = self.check_entry_for_update(entry, base_dir)
            if not isinstance(checked, UpdateCheck):
                return checked
            fields: dict[str, Any] = {
                "github": checked.url,
                "author": checked.ref.user,
                "repository_name": checked.ref.repo,
                "commit_sha": checked.commit_sha,
                "local_sha": checked.local_sha,
                desc.id_field: checked.asset_id,
                "is_compatible": checked.compatible,
                "verified": verified,
            }
            if desc.is_plugin:
                fields["branch"] = checked.branch
            return desc.data_cls(**fields)

        if "url" not in entry:
            # Report and isolate a URL-less catalog entry.
            log.error(f"Skipping store entry without a url: {entry!r}")
            return None
        url = entry["url"]
        ref = self.repo_ref_for_entry(url)
        if ref is None:
            return None

        resolved = self._resolve_asset_version(entry, desc, url)
        if not isinstance(resolved, _ResolvedVersion):
            return resolved
        compatible, commit, branch = resolved
        # A plugin with no pin or branch gets an invalid fetch and is dropped per entry.
        ref_for_fetch: "str | None" = commit or branch

        manifest = self.get_manifest(url, ref_for_fetch)
        if not manifest:
            log.error(f"manifest failed to load for repository {url}")
            return None

        thumbnail_path: Any = manifest.get("thumbnail")
        image = self._fetch_thumbnail(url, thumbnail_path, ref_for_fetch)
        attribution = self.get_attribution(url, ref_for_fetch).get("generic", {})

        translated_description, translated_short_description = self._translate_descriptions(manifest)

        author = ref.user

        # Keep untyped JSON values in Any locals instead of asserting non-optional field types.
        descriptions: Any = manifest.get("descriptions") or None
        short_descriptions: Any = manifest.get("short-descriptions") or None
        tags: Any = manifest.get("tags") or None
        license_descriptions: Any = attribution.get("licence-descriptions", attribution.get("descriptions")) or None

        # Persist identity discovered by this manifest fetch for later local checks.
        self.note_installed_origin(base_dir, manifest.get("id"), url)

        full_fields: dict[str, Any] = {
            "descriptions": descriptions,
            "short_descriptions": short_descriptions,
            "description": translated_description or manifest.get("description"),
            "short_description": translated_short_description or manifest.get("short-description"),

            "github": url or None,
            "author": author or None,
            "official": author in self.official_authors or False,
            "commit_sha": commit,
            "local_sha": self.get_local_sha_for_id(base_dir, manifest.get("id")),
            "minimum_app_version": manifest.get("minimum-app-version") or None,
            "app_version": manifest.get("app-version") or None,
            "repository_name": ref.repo,
            "tags": tags,

            "thumbnail": thumbnail_path or None,
            "image": image,

            "copyright": attribution.get("copyright") or None,
            "original_url": attribution.get("original-url") or None,
            "license": attribution.get("licence") or None,
            "license_descriptions": license_descriptions,

            desc.name_field: manifest.get("name") or None,
            desc.version_field: manifest.get("version") or None,
            desc.id_field: manifest.get("id") or None,

            "is_compatible": compatible,
            "verified": verified,
        }
        if desc.is_plugin:
            full_fields["branch"] = branch
        return desc.data_cls(**full_fields)

    def prepare_plugin(self, plugin: dict[str, Any], include_image: bool = True, verified: bool = False) -> "StoreData | None":
        return self._prepare_asset(plugin, PLUGIN, include_image, verified)

    def get_current_git_commit_hash_without_git(self, repo_path: str) -> str:
        try:
            fetch_head_path = os.path.join(repo_path, '.git', 'FETCH_HEAD')

            with open(fetch_head_path, 'r') as file:
                lines = file.readlines()

                if lines:
                    latest_commit_hash = lines[0].split()[0]
                    return latest_commit_hash
                else:
                    raise ValueError("FETCH_HEAD file is empty")
                    
        except Exception as e:
            raise RuntimeError(f"Unable to retrieve git commit hash: {e}")
    
    def plugins_dir(self) -> str:
        return gl.PLUGIN_DIR

    def icons_dir(self) -> str:
        return os.path.join(gl.DATA_PATH, "icons")

    def wallpapers_dir(self) -> str:
        return os.path.join(gl.DATA_PATH, "wallpapers")

    def sd_plus_bar_wallpapers_dir(self) -> str:
        return os.path.join(gl.DATA_PATH, "sd_plus_bar_wallpapers")

    def scan_installed_assets(self, base_dir: str) -> dict[str, InstalledAsset]:
        """Build local update metadata for safe asset directories under base_dir."""
        index: dict[str, InstalledAsset] = {}
        try:
            names = os.listdir(base_dir)
        except OSError:
            return index
        for asset_id in names:
            if not self.is_safe_asset_id(asset_id):
                continue
            asset_path = os.path.join(base_dir, asset_id)
            if not os.path.isdir(asset_path):
                continue
            index[asset_id] = InstalledAsset(
                asset_id=asset_id,
                path=asset_path,
                sha=self.get_local_sha(asset_path) or "",
                origin=self.read_origin(asset_path),
                manifest_id=self.read_local_manifest_id(asset_path),
                is_symlink=os.path.islink(asset_path),
            )
        return index

    def installed_assets(self, base_dir: str) -> dict[str, InstalledAsset]:
        """Return the pass snapshot for base_dir, or scan when no pass exists.
        Concurrent first scans can duplicate one listing because the snapshot is only a cache."""
        snapshot = self._installed_index
        if snapshot is None:
            return self.scan_installed_assets(base_dir)
        index = snapshot.get(base_dir)
        if index is None:
            index = self.scan_installed_assets(base_dir)
            snapshot[base_dir] = index
        return index

    def read_origin(self, asset_path: str) -> RepoRef | None:
        """Read and normalize an installed tree's source repository."""
        try:
            with open(os.path.join(asset_path, self.ORIGIN_FILE)) as f:
                return parse_repo_url(f.readline().strip())
        except OSError:
            return None

    @staticmethod
    def read_local_manifest_id(asset_path: str) -> str | None:
        """Read the id that an installed tree claims for itself."""
        try:
            with open(os.path.join(asset_path, "manifest.json")) as f:
                asset_id = json.load(f).get("id")
        except (OSError, ValueError):
            return None
        return asset_id if isinstance(asset_id, str) else None

    def stamp_origin(self, asset_path: str, repo_url: str) -> None:
        """Record an installed tree's source repository without writing through symlinks."""
        if os.path.islink(asset_path):
            return
        try:
            with open(os.path.join(asset_path, self.ORIGIN_FILE), "w") as f:
                f.write(f"{repo_url}\n")
        except OSError as e:
            # Continue without the optimization; manifest lookup can recover identity.
            log.warning(f"Could not stamp the origin of {asset_path}: {e}")

    def note_installed_origin(self, base_dir: str, asset_id: object, repo_url: str) -> None:
        """Backfill or correct the source stamp of an identified canonical install.
        When catalogs duplicate an id, the last full preparation overwrites the stamp."""
        if not self.is_safe_asset_id(asset_id) or not isinstance(repo_url, str):
            return
        ref = parse_repo_url(repo_url)
        if ref is None:
            return
        asset_path = os.path.join(base_dir, asset_id)
        if not os.path.isdir(asset_path) or os.path.islink(asset_path):
            return
        if same_repository(self.read_origin(asset_path), ref):
            return
        self.stamp_origin(asset_path, repo_url)

    def match_installed_asset(self, ref: RepoRef, installed: "dict[str, InstalledAsset]") -> "InstalledAsset | None":
        """Find the sole canonical install stamped with a repository.
        Keep an install with no readable manifest eligible so a reinstall can repair it."""
        candidates = [asset for asset in installed.values() if same_repository(asset.origin, ref)]
        if not candidates:
            return None
        canonical = [asset for asset in candidates
                     if asset.manifest_id is None or asset.manifest_id == asset.asset_id]
        if len(canonical) == 1:
            return canonical[0]
        if not canonical:
            log.warning(
                f"Not updating {ref.user}/{ref.repo}: the only directories stamped with it "
                f"({[asset.asset_id for asset in candidates]}) are not named after the id "
                f"their manifest claims"
            )
            return None
        log.warning(
            f"Not updating {ref.user}/{ref.repo}: more than one install claims it "
            f"({[asset.asset_id for asset in canonical]})"
        )
        return None

    def resolve_unstamped_installs(self, base_dir: str, entries: list[Any]) -> None:
        """Identify unstamped installs before an update pass; the first catalog claimant wins.
        Skip symlinks and suppress repeated full-catalog misses only for this session."""
        installed = self.installed_assets(base_dir)
        claimed = set()
        for entry in entries:
            entry_ref = parse_repo_url(entry.get("url"))
            if entry_ref is not None:
                claimed.add(repository_key(entry_ref))

        pending = {
            asset.asset_id: asset for asset in installed.values()
            if not asset.is_symlink
            and asset.path not in self._unresolvable_installs
            and (asset.origin is None or repository_key(asset.origin) not in claimed)
        }
        if not pending:
            return

        def claim_priority(entry: dict[str, Any]) -> int:
            entry_ref = parse_repo_url(entry.get("url"))
            if entry_ref is None:
                return 2
            return 0 if any(entry_ref.repo.lower() in asset_id.lower() for asset_id in pending) else 1

        for entry in sorted(entries, key=claim_priority):
            if not pending:
                return
            try:
                self._claim_pending_install(entry, pending, installed)
            except Exception as e:
                # Isolate malformed remote data so one entry cannot stop every update leg.
                log.error(f"Could not identify installs from store entry {entry.get('url')!r}: {e!r}")

        if pending:
            # The walk covered the whole catalog and nothing claimed these.
            self._unresolvable_installs = frozenset(
                self._unresolvable_installs | {asset.path for asset in pending.values()}
            )

    def _claim_pending_install(self, entry: dict[str, Any], pending: "dict[str, InstalledAsset]", installed: "dict[str, InstalledAsset]") -> None:
        """Stamp the pending directory named by one entry's remote manifest."""
        ref = parse_repo_url(entry.get("url"))
        if ref is None:
            return
        url = entry["url"]
        # Use a branch directly for identity without resolving its tip.
        revision = entry.get("branch")
        if revision is None:
            pinned = resolve_pinned_revision(entry)
            if pinned is None:
                return
            revision = pinned.sha
        manifest = self.get_manifest(url, revision)
        if not manifest:
            return
        asset_id = manifest.get("id")
        if not self.is_safe_asset_id(asset_id):
            return
        asset = pending.pop(asset_id, None)
        if asset is None:
            return
        if asset.manifest_id is not None and asset.manifest_id != asset.asset_id:
            # Do not claim a renamed copy as the canonical install.
            return
        self.stamp_origin(asset.path, url)
        # Publish the new stamp to later checks in this pass.
        installed[asset_id] = asset._replace(origin=ref)

    def check_entry_for_update(self, entry: dict[str, Any], base_dir: str) -> "UpdateCheck | None":
        """Resolve one catalog entry against source stamps and local versions.
        Only a branch-pinned entry needs a request, and a full pass resolves legacy stamps first."""
        ref = self.repo_ref_for_entry(entry.get("url"))
        if ref is None:
            return None
        url = entry["url"]
        installed = self.installed_assets(base_dir)

        branch = entry.get("branch")
        compatible = True
        if branch is not None:
            # Let an unreachable branch tip drop this entry through the fan-out handler.
            target = self.get_last_commit(url, branch)
        else:
            pinned = resolve_pinned_revision(entry)
            if pinned is None:
                log.error(f"Skipping store entry {url!r}: it pins no version")
                return None
            # Preserve the newest incompatible pin for display while blocking installation.
            target = pinned.sha
            compatible = pinned.compatible

        asset = self.match_installed_asset(ref, installed)
        if asset is None:
            return UpdateCheck(url, ref, None, None, target, branch, compatible)

        if asset.is_symlink:
            # Auto-update must not replace a user-managed symlink with a downloaded tree.
            log.info(f"Skipping auto-update of {asset.asset_id}: it is a symlink to a tree this app does not own")
            return UpdateCheck(url, ref, None, None, target, branch, compatible)

        # Treat an unreadable local version as outdated so reinstall can repair it.
        return UpdateCheck(url, ref, asset.asset_id, asset.sha, target, branch, compatible)

    def get_local_sha_for_id(self, base_dir: str, asset_id: object) -> str | None:
        """Read a local version only for a safe asset id."""
        if not self.is_safe_asset_id(asset_id):
            return None
        return self.get_local_sha(os.path.join(base_dir, asset_id))

    def get_local_sha(self, git_dir: str) -> "str | None":
        if not os.path.exists(git_dir):
            return None
        
        if os.path.exists(os.path.join(git_dir, ".git")):
            try:
                sha = self.get_current_git_commit_hash_without_git(git_dir)
                if sha is not None:
                    return sha
            except (ValueError, RuntimeError) as e:
                log.error(e)

        version_file_path = os.path.join(git_dir, "VERSION")
        if not os.path.exists(version_file_path):
            return ""
        
        with open(version_file_path, "r") as f:
            return f.read().strip()
    
    def prepare_icon(self, icon: dict[str, Any], include_image: bool = True, verified: bool = False) -> "StoreData | None":
        return self._prepare_asset(icon, ICON, include_image, verified)

    def prepare_wallpaper(self, wallpaper: dict[str, Any], include_image: bool = True, verified: bool = False) -> "StoreData | None":
        return self._prepare_asset(wallpaper, WALLPAPER, include_image, verified)

    def prepare_sd_plus_bar_wallpaper(self, sd_plus_bar_wallpaper: dict[str, Any], include_image: bool = True, verified: bool = False) -> "StoreData | None":
        return self._prepare_asset(sd_plus_bar_wallpaper, SD_PLUS_BAR, include_image, verified)

    def get_web_image(self, url: str, path: str, branch: "str | None" = "main") -> "Image.Image | None":
        try:
            result = self.get_remote_file(url, path, branch, data_type=DataType.CONTENT)
        except StoreFetchError:
            return None
        except Exception as e:
            # Preserve SystemExit and KeyboardInterrupt in pool workers.
            log.error(f"Failed to fetch image {path} from {url}: {e}")
            return None
        try:
            return Image.open(BytesIO(result))
        except Exception as e:
            log.warning(f"Could not decode image {path} from {url}: {e}")
            return None
    
    def repo_ref_for_entry(self, url: object) -> RepoRef | None:
        """Parse a catalog or settings URL and report an invalid entry."""
        ref = parse_repo_url(url)
        if ref is None:
            log.error(f"Skipping store entry {url!r}: not a store repository url")
        return ref

    def subp_call(self, args: list[str]) -> int:
        return subprocess.call(args)

    def get_main_folder_of_zip(self, zip_path: str) -> str | None:
        extracted_folder_name = None
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_contents = zip_ref.namelist()
            for item in zip_contents:
                if not item.endswith("/"):
                    continue
                if item.count("/") > 1:
                    continue

                if extracted_folder_name is not None:
                    log.error("Multiple folders in zip")
                    return None
                extracted_folder_name = item.split("/")[0]


        if extracted_folder_name is None:
            log.error("Could not find extracted folder name")
            return None

        return extracted_folder_name

    def zip_has_unsafe_members(self, zip_path: str) -> bool:
        """Reject archive members that are absolute or escape the extraction root."""
        unsafe = archive_safety.first_unsafe_member(zip_path)
        if unsafe is None:
            return False
        name, reason = unsafe
        log.error(f"Refusing archive: {reason}: {name!r}")
        return True

    @staticmethod
    def _remove_leftover(path: str) -> None:
        """Remove a transient swap tree, or a stray file or symlink."""
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            with contextlib.suppress(OSError):
                os.remove(path)


    def _staged_tree_acceptable(self, staging_tree: str, expected_id: str | None,
                                gate_app_version: bool = True) -> bool:
        """Require the expected manifest id and, for plugins, a supported minimum app version.
        Callers without an id accept unreadable manifests; packs skip minimum-version gating."""
        try:
            with open(os.path.join(staging_tree, "manifest.json")) as f:
                manifest = json.load(f)
        except (OSError, ValueError) as e:
            if expected_id is None:
                return True
            log.error(f"Staged download has no readable manifest.json ({e}) -- refusing to install as {expected_id!r}")
            return False
        if not isinstance(manifest, dict):
            if expected_id is None:
                return True
            log.error(f"Staged manifest.json is not an object -- refusing to install as {expected_id!r}")
            return False
        if expected_id is not None:
            staged_id = manifest.get("id")
            if staged_id != expected_id:
                log.error(f"Staged download identifies as {staged_id!r}, expected {expected_id!r} -- refusing to install")
                return False
        if gate_app_version:
            minimum = manifest.get("minimum-app-version")
            if not is_min_app_version_satisfied(minimum):
                log.error(f"Staged download requires app version {minimum!r}, this is {gl.app_version} -- refusing to install")
                return False
        return True

    def recover_interrupted_installs(self) -> None:
        """Repair half-swapped installs before plugin and pack scans."""
        install_recovery.recover_interrupted_installs(
            [self.plugins_dir(), self.icons_dir(),
             self.wallpapers_dir(), self.sd_plus_bar_wallpapers_dir()])

    def download_repo(self, repo_url:str, directory:str, commit_sha:str | None = None, branch_name:str | None = None, expected_id:str | None = None,
                      gate_app_version: bool = True) -> StoreResult[None]:
        """Use INVALID_ASSET for manifests and NO_CONNECTION for network or archive faults.
        Use INSTALL_FAILED when Git is missing or a clone/ref command fails."""
        if not is_flatpak() and gl.argparser.parse_args().devel:
            return self.clone_repo(repo_url, directory, commit_sha, branch_name, expected_id, gate_app_version)


        ref = parse_repo_url(repo_url)
        if ref is None:
            log.error(f"Could not derive a repository from {repo_url!r}")
            return Err(ErrReason.INSTALL_FAILED, f"could not derive a repository from {repo_url!r}")
        username = ref.user
        projectname = ref.repo.lower()
        sha = commit_sha
        if commit_sha is None and branch_name is not None:
            try:
                sha = self.get_last_commit(repo_url, branch_name)
            except StoreFetchError:
                return Err(ErrReason.NO_CONNECTION, f"could not resolve branch {branch_name!r} of {repo_url}")
            if sha is None:
                # Report an unresolved branch before constructing an invalid archive URL.
                log.error(f"Could not resolve branch {branch_name!r} of {repo_url}")
                return Err(ErrReason.INSTALL_FAILED, f"branch {branch_name!r} of {repo_url} has no commits")
        if sha is None:
            log.error(f"Refusing to download {repo_url}: no commit sha and no branch")
            return Err(ErrReason.INSTALL_FAILED, f"no commit sha and no branch for {repo_url}")

        zip_url = f"https://github.com/{username}/{projectname}/archive/{sha}.zip"

        zip_path = os.path.join(gl.DATA_PATH, "cache", f"{projectname}-{sha}.zip")

        # Publish the archive path only after the complete download arrives.
        try:
            http_client.download_to_file(zip_url, zip_path, timeout=30)
        except Exception as e:
            log.error(e)
            return Err(ErrReason.NO_CONNECTION, f"download of {projectname} failed: {e}")

        extracted_folder = None
        try:
            # Resolve the case-preserved root before extraction so cleanup covers partial work.
            extracted_folder_name = self.get_main_folder_of_zip(zip_path)
            if extracted_folder_name is None:
                raise ValueError("could not determine the archive's root folder")
            # Reject unsafe members before extraction writes to disk.
            if self.zip_has_unsafe_members(zip_path):
                log.error(f"Refusing to extract {projectname}: archive contains unsafe member paths")
                return Err(ErrReason.NO_CONNECTION, f"{projectname} archive contains unsafe member paths")
            extracted_folder = os.path.join(gl.DATA_PATH, "cache", extracted_folder_name)
            if os.path.exists(extracted_folder):
                shutil.rmtree(extracted_folder)
            shutil.unpack_archive(zip_path, os.path.join(gl.DATA_PATH, "cache"))

            # Validate and stamp the staging tree before the atomic swap publishes it.
            if not self._staged_tree_acceptable(extracted_folder, expected_id, gate_app_version):
                return Err(ErrReason.INVALID_ASSET, f"staged {projectname} tree failed the manifest checks")
            with open(os.path.join(extracted_folder, "VERSION"), "w") as f:
                f.write(sha)
            # Publish origin and version together with the tree.
            self.stamp_origin(extracted_folder, repo_url)

            install_recovery.swap_into_place(extracted_folder, directory)
        except Exception as e:
            log.error(f"Failed to extract/install {projectname}: {e}")
            return Err(ErrReason.NO_CONNECTION, f"failed to extract/install {projectname}: {e}")
        finally:
            # Remove temporary data without replacing the install outcome on cleanup failure.
            try:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
            except OSError:
                pass
            if extracted_folder is not None and os.path.isdir(extracted_folder):
                shutil.rmtree(extracted_folder, ignore_errors=True)

        return Ok(None)

    def clone_repo(self, repo_url:str, local_path:str, commit_sha:str | None = None, branch_name:str | None = None, expected_id:str | None = None,
                   gate_app_version: bool = True) -> StoreResult[None]:
        if commit_sha is not None:
            branch_name = None

        # Validate catalog refs and pass them to git only as argv tokens.
        if commit_sha is not None and not self.is_safe_commit_sha(commit_sha):
            log.error(f"Refusing to clone {repo_url}: malformed commit sha {commit_sha!r}")
            return Err(ErrReason.INVALID_ASSET, f"malformed commit sha {commit_sha!r}")
        if branch_name is not None and not self.is_safe_ref_name(branch_name):
            log.error(f"Refusing to clone {repo_url}: unsafe branch/ref name {branch_name!r}")
            return Err(ErrReason.INVALID_ASSET, f"unsafe branch/ref name {branch_name!r}")

        if shutil.which("git") is None:
            log.error("Git is not installed on this system. Please install it.")
            return Err(ErrReason.INSTALL_FAILED, "git is not installed on this system")

        # Clone and validate in staging before replacing the existing install.
        staging = os.path.join(gl.DATA_PATH, "cache", f".clone-staging.{os.path.basename(os.path.normpath(local_path))}")
        os.makedirs(os.path.join(gl.DATA_PATH, "cache"), exist_ok=True)
        self._remove_leftover(staging)

        try:
            rc = self.subp_call(["git", "clone", repo_url, staging])
            if rc != 0 or not os.path.isdir(staging):
                log.error(f"git clone of {repo_url} failed with exit code {rc}")
                return Err(ErrReason.INSTALL_FAILED, f"git clone of {repo_url} failed (exit {rc})")

            # Mark final and staging paths safe before repository operations.
            self.subp_call(["git", "config", "--global", "--add", "safe.directory", os.path.abspath(local_path)])
            self.subp_call(["git", "config", "--global", "--add", "safe.directory", os.path.abspath(staging)])

            # Create FETCH_HEAD for the local-version reader without a shell.
            self.subp_call(["git", "-C", staging, "pull"])

            # Fail an unreachable commit instead of installing and mis-stamping the default tip.
            if commit_sha is not None:
                rc = self.subp_call(["git", "-C", staging, "reset", "--hard", commit_sha])
                if rc != 0:
                    log.error(f"git reset --hard {commit_sha!r} failed with exit code {rc} for {repo_url} "
                              f"(commit unreachable?) -- refusing to install the default-branch tip")
                    return Err(ErrReason.INSTALL_FAILED, f"git reset --hard {commit_sha!r} failed (exit {rc})")
            elif branch_name is not None:
                # Checkout supports detachable refs.
                # Reject failure instead of using the default tip.
                rc = self.subp_call(["git", "-C", staging, "checkout", branch_name])
                if rc != 0:
                    log.error(f"git checkout {branch_name!r} failed with exit code {rc} for {repo_url}")
                    return Err(ErrReason.INSTALL_FAILED, f"git checkout {branch_name!r} failed (exit {rc})")

            # Validate, stamp, then swap, in the same order as archive installation.
            if not self._staged_tree_acceptable(staging, expected_id, gate_app_version):
                return Err(ErrReason.INVALID_ASSET, "staged tree failed the manifest checks")

            version_stamp = commit_sha or branch_name
            if version_stamp is None:
                log.error(f"Refusing to stamp VERSION for {repo_url}: no commit sha and no branch")
                return Err(ErrReason.INVALID_ASSET, f"no commit sha and no branch for {repo_url}")
            with open(os.path.join(staging, "VERSION"), "w") as f:
                f.write(version_stamp)
            self.stamp_origin(staging, repo_url)

            install_recovery.swap_into_place(staging, local_path)
        except Exception as e:
            log.error(f"Failed to stage devel clone of {repo_url}: {e}")
            return Err(ErrReason.NO_CONNECTION, f"failed to stage devel clone of {repo_url}: {e}")
        finally:
            self._remove_leftover(staging)

        return Ok(None)

    def install_plugin(self, plugin_data:PluginData, auto_update: bool = False,
                       ask_install_script: "Callable[[str], bool] | None" = None) -> StoreResult[None]:
        url = plugin_data.github
        plugin_id = plugin_data.plugin_id

        if not self.is_safe_asset_id(plugin_id):
            # Reject traversal before the id is joined to the install directory.
            log.error(f"Refusing to install plugin with unsafe id {plugin_id!r} from {url}")
            return Err(ErrReason.INVALID_ASSET, f"unsafe plugin id {plugin_id!r}")

        if url is None:
            log.error(f"Refusing to install plugin {plugin_id!r}: no repository url")
            return Err(ErrReason.INVALID_ASSET, f"no repository url for plugin {plugin_id!r}")

        local_path = os.path.join(gl.PLUGIN_DIR, plugin_id)

        # Decide scripts before download so a declined update stays intact.
        # A fresh install proceeds without declined scripts.
        plugin_manager = gl.plugin_manager
        is_update = plugin_manager is not None and plugin_manager.get_plugin_by_id(plugin_id) is not None
        run_scripts = install_script.decide_install_scripts(
            local_path if is_update else None, plugin_id, ask_install_script)
        if is_update and not run_scripts:
            return Ok(None)

        response = self.download_repo(repo_url=url, directory=local_path, commit_sha=plugin_data.commit_sha, branch_name=plugin_data.branch, expected_id=plugin_id)

        if isinstance(response, Err):
            return response

        # Deregister only after a successful swap so failed updates keep the old plugin active.
        if is_update:
            try:
                self.uninstall_plugin(plugin_id, remove_from_pages=False, remove_files=False)
            except Exception as e:
                log.error(f"Deregistering the old version of {plugin_id} failed: {e}")

        # Run all plugin install steps through the confinement and timeout gate.
        outcome = install_script.run_install_steps(local_path, plugin_id, run=run_scripts)
        if outcome not in (install_script.Outcome.RAN, install_script.Outcome.NO_STEPS):
            log.warning(f"Install steps of {plugin_id}: {outcome.value}")

        # Reload after cache invalidation, then refresh UI and decks even on load failure.
        load_error = install_reload.reload_after_install(plugin_id)

        # Report a newly disabled plugin during this install session.
        if load_error is None:
            self.notify_if_installed_disabled(plugin_id)

        sidebar = services.sidebar()
        if sidebar is not None:
            GLib.idle_add(sidebar.action_chooser.plugin_group.update)

        # Refresh active pages only on available decks and controllers.
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            if hasattr(controller, "active_page"):
                if controller.active_page is not None:
                    controller.active_page.load_action_objects()
                    controller.load_page(controller.active_page)

        if load_error is not None:
            return Ok(None)

        gl.signal_manager.trigger_signal(Signals.PluginInstall, plugin_data.plugin_id)

        log.success(f"Plugin {plugin_id} installed successfully under: {local_path} with sha: {plugin_data.commit_sha}")
        return Ok(None)

    @staticmethod
    def notify_if_installed_disabled(plugin_id: str) -> bool:
        """Report when registration disabled the newly installed plugin.
        Return whether a notification was sent."""
        if plugin_id in PluginBase.plugins:
            return False
        entry = PluginBase.disabled_plugins.get(plugin_id)
        if entry is None:
            return False

        reason = entry.get("reason")
        name = getattr(entry.get("object"), "plugin_name", None) or plugin_id
        if reason == "app-out-of-date":
            detail = "it requires a newer version of StreamController"
        elif reason == "plugin-out-of-date":
            detail = "it was built for an older version of StreamController"
        else:
            detail = "its version metadata is invalid"
        body = f"{name} was installed but is disabled: {detail}"
        log.warning(f"Install of {plugin_id}: {body}")

        gl.notify.info(body, title="Plugin disabled")
        return True

    def uninstall_plugin(self, plugin_id:str, remove_from_pages:bool = False, remove_files:bool = True) -> None:
        # Use the locked page snapshot to remove dead actions from every cached page.
        for page in (gl.page_manager.all_cached_pages() if gl.page_manager is not None else []):
            page.remove_plugin_action_objects(plugin_id=plugin_id)
            if remove_from_pages:
                page.remove_plugin_actions_from_json(plugin_id=plugin_id)

        plugin_manager = gl.plugin_manager
        if plugin_manager is None:
            return None
        plugins = plugin_manager.get_plugins()
        plugin = plugin_manager.get_plugin_by_id(plugin_id)
        if plugin is None:
            return None
        # Capture the import folder before uninstall hooks or symlink handling change the path.
        plugin_folder = os.path.basename(os.path.normpath(plugin.PATH))
        if remove_files:
            plugin.on_uninstall()
            
            if os.path.islink(plugin.PATH):
                symlink_target = os.readlink(plugin.PATH)
                log.warning(f"Plugin {plugin.plugin_name} is inside a Symlink!")
                plugin.PATH = symlink_target

            shutil.rmtree(plugin.PATH)

        # plugin_obj = gl.plugin_manager.get_plugin_by_id(plugin_id)
        plugin_manager.remove_plugin_from_list(plugin)

        plugin_manager.generate_action_index()


        del plugin

        if gl.app is not None:
            GLib.idle_add(gl.app.main_win.sidebar.action_chooser.plugin_group.update)
            GLib.idle_add(gl.app.main_win.sidebar.page_selector.update)


        base_module = f"plugins.{plugin_folder}"
        for module in sys.modules.copy():
            if module.startswith(base_module):
                del sys.modules[module]

        # for controller in gl.deck_manager.deck_controller:
            # controller.active_page.update_inputs_with_actions_from_plugin(plugin_id)

        # Refresh active pages only on available decks and controllers.
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            if hasattr(controller, "active_page"):
                if controller.active_page is not None:
                    controller.active_page.load_action_objects()
                    controller.load_page(controller.active_page)

    # Share safe install and removal for data-only packs; plugins need script and reload handling.

    def _install_asset(self, asset: "IconData | WallpaperData | SDPlusBarWallpaperData", desc: AssetTypeDescriptor) -> StoreResult[None]:
        """Transactionally install one data-only asset into its type directory.
        Return INVALID_ASSET for an unsafe id or missing URL."""
        asset_id = asset.asset_id
        if not self.is_safe_asset_id(asset_id):
            log.error(f"Refusing to install {desc.display_name} with unsafe id {asset_id!r} from {asset.github}")
            return Err(ErrReason.INVALID_ASSET, f"unsafe {desc.display_name} id {asset_id!r}")

        github = asset.github
        if github is None:
            log.error(f"Refusing to install {desc.display_name} {asset_id!r}: no repository url")
            return Err(ErrReason.INVALID_ASSET, f"no repository url for {desc.display_name} {asset_id!r}")

        asset_path = os.path.join(getattr(self, desc.base_dir_attr)(), asset_id)
        # Ignore plugin compatibility metadata for data-only packs.
        return self.download_repo(repo_url=github, directory=asset_path, commit_sha=asset.commit_sha, expected_id=asset_id,
                                  gate_app_version=False)

    def _uninstall_asset(self, asset: "IconData | WallpaperData | SDPlusBarWallpaperData", desc: AssetTypeDescriptor) -> "int | None":
        """Delete one data-only asset, or return 400 for an unsafe id."""
        asset_id = asset.asset_id
        if not self.is_safe_asset_id(asset_id):
            log.error(f"Refusing to uninstall {desc.display_name} with unsafe id {asset_id!r}")
            return 400
        asset_path = os.path.join(getattr(self, desc.base_dir_attr)(), asset_id)
        if os.path.exists(asset_path):
            shutil.rmtree(asset_path)
        return None

    def install_icon(self, icon_data:IconData) -> StoreResult[None]:
        return self._install_asset(icon_data, ICON)

    def uninstall_icon(self, icon_data:IconData) -> "int | None":
        return self._uninstall_asset(icon_data, ICON)

    def install_wallpaper(self, wallpaper_data:WallpaperData) -> StoreResult[None]:
        return self._install_asset(wallpaper_data, WALLPAPER)

    def uninstall_wallpaper(self, wallpaper_data:WallpaperData) -> "int | None":
        return self._uninstall_asset(wallpaper_data, WALLPAPER)

    def install_sd_plus_bar_wallpaper(self, sd_plus_bar_wallpaper_data:SDPlusBarWallpaperData) -> StoreResult[None]:
        return self._install_asset(sd_plus_bar_wallpaper_data, SD_PLUS_BAR)

    def uninstall_sd_plus_bar_wallpaper(self, sd_plus_bar_wallpaper_data:SDPlusBarWallpaperData) -> "int | None":
        return self._uninstall_asset(sd_plus_bar_wallpaper_data, SD_PLUS_BAR)

    def get_plugin_for_id(self, plugin_id: "str | None") -> "PluginData | None":
        """Return a catalog plugin by id, or None on lookup or catalog failure."""
        result = self.get_all_plugins()
        if isinstance(result, Err):
            log.error(f"Cannot resolve plugin {plugin_id!r}: {result.detail or result.reason.value}")
            return None
        for plugin in result.value:
            if plugin.plugin_id == plugin_id:
                return plugin
        return None

    def _get_assets_to_update(self, desc: AssetTypeDescriptor) -> "StoreResult[list[StoreData]]":
        """Return installed assets with a newer, compatible, known target version."""
        result = getattr(self, desc.get_all_attr)(include_images=False)
        if isinstance(result, Err):
            return result
        assets = result.value

        to_update: "list[StoreData]" = []
        for asset in assets:
            if asset.local_sha is None:
                continue
            if asset.local_sha == asset.commit_sha:
                continue
            if asset.commit_sha is None:
                # Skip entries with no installable target revision.
                continue
            if asset.is_compatible is False:
                # Keep the working asset when only an incompatible target exists.
                log.warning(
                    f"Skipping update of {desc.display_name} {asset.asset_id}: pinned version "
                    f"{asset.commit_sha} is not compatible with app version {gl.app_version}"
                )
                continue
            to_update.append(asset)

        return Ok(to_update)

    def _update_all(self, desc: AssetTypeDescriptor) -> StoreResult[int]:
        """Reinstall outdated assets and return the success count or catalog error."""
        to_update = getattr(self, desc.get_to_update_attr)()
        if isinstance(to_update, Err):
            return to_update

        n_updated = 0
        install = getattr(self, desc.install_attr)
        for asset in to_update.value:
            result = install(asset)
            # Narrow the result because Err is truthy.
            if isinstance(result, Err):
                log.error(f"Failed to update {desc.display_name} {asset.asset_id}: {result!r}")
                continue
            n_updated += 1

        return Ok(n_updated)

    def get_plugins_to_update(self) -> "StoreResult[list[PluginData]]":
        # Restore the descriptor-specific element type after dynamic dispatch.
        return cast("StoreResult[list[PluginData]]", self._get_assets_to_update(PLUGIN))

    def update_all_plugins(self) -> StoreResult[int]:
        """Returns Ok with the number of plugins updated, or an Err."""
        return self._update_all(PLUGIN)

    def get_icons_to_update(self) -> "StoreResult[list[IconData]]":
        return cast("StoreResult[list[IconData]]", self._get_assets_to_update(ICON))

    def update_all_icons(self) -> StoreResult[int]:
        """Returns Ok with the number of icon packs updated, or an Err."""
        return self._update_all(ICON)

    def get_wallpapers_to_update(self) -> "StoreResult[list[WallpaperData]]":
        return cast("StoreResult[list[WallpaperData]]", self._get_assets_to_update(WALLPAPER))

    def update_all_wallpapers(self) -> StoreResult[int]:
        """Returns Ok with the number of wallpapers updated, or an Err."""
        return self._update_all(WALLPAPER)

    def get_sd_plus_bar_wallpapers_to_update(self) -> "StoreResult[list[SDPlusBarWallpaperData]]":
        return cast("StoreResult[list[SDPlusBarWallpaperData]]", self._get_assets_to_update(SD_PLUS_BAR))

    def update_all_sd_plus_bar_wallpapers(self) -> StoreResult[int]:
        """Return the number of SD+ bar wallpapers updated, or an error."""
        return self._update_all(SD_PLUS_BAR)

    def update_everything(self) -> StoreResult[int]:
        """Return the total assets updated, or the first error."""
        # Run plugins first, preserve public-method dispatch, then aggregate all results.
        results = [getattr(self, desc.update_all_attr)() for desc in ASSET_TYPES]
        for result in results:
            if isinstance(result, Err):
                return result

        return Ok(sum(result.value for result in results if isinstance(result, Ok)))
