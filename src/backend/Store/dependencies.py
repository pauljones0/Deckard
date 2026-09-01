"""Resolve catalog-listed, id-only plugin dependencies before download and ask consent for the set.
The bounded cycle-safe plan keeps installed items and never rolls back or cascades removal."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger as log

from src.backend.Store.asset_types import ASSET_TYPES, PLUGIN, AssetTypeDescriptor
from src.backend.Store.store_result import Err
from src.windows.Store.StoreData import StoreAssetData

MANIFEST_KEY = "dependencies"

# Bound remote dependency chains to prevent one request from expanding without limit.
MAX_DEPTH = 8


@dataclass(frozen=True)
class CatalogItem:
    """A catalog record and the descriptor that installs it."""

    descriptor: AssetTypeDescriptor
    asset: StoreAssetData

    @property
    def asset_id(self) -> str:
        # Records without ids are excluded from indexes and rejected as roots.
        return self.asset.asset_id or ""

    @property
    def display_name(self) -> str:
        """Return the display name, or the manifest id when no name is available."""
        return self.asset.asset_name or self.asset_id

    @property
    def installed(self) -> bool:
        return self.asset.local_sha is not None


@dataclass(frozen=True)
class Plan:
    """A pre-download install order with unresolved ids and depth truncation state.
    Dependencies precede the root except within cycles, where each item appears once."""

    order: tuple[CatalogItem, ...]
    unknown: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def dependencies(self) -> tuple[CatalogItem, ...]:
        return self.order[:-1]

    def names(self) -> list[str]:
        """Name each planned item and its asset class in install order."""
        return [f"{item.display_name} ({item.descriptor.display_name})"
                for item in self.order]


@dataclass(frozen=True)
class InstallReport:
    """The installed items, first failure, or pre-download refusal for one plan."""

    installed: tuple[CatalogItem, ...] = ()
    failed: CatalogItem | None = None
    error: Err | None = None
    declined: bool = False

    @property
    def ok(self) -> bool:
        return self.failed is None and not self.declined

    @property
    def installed_ids(self) -> tuple[str, ...]:
        return tuple(item.asset_id for item in self.installed)

    @property
    def failed_id(self) -> "str | None":
        return self.failed.asset_id if self.failed is not None else None


class CatalogIndex:
    """Lazy index of store ids from configured catalogs, loaded one asset class at a time.
    Use display catalogs because update views omit ids for uninstalled dependency candidates."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self._loaded: list[str] = []
        self._items: dict[str, CatalogItem] = {}

    def get(self, asset_id: str) -> "CatalogItem | None":
        while True:
            item = self._items.get(asset_id)
            if item is not None:
                return item
            if not self._load_next():
                return None

    def _load_next(self) -> bool:
        """Load the next asset class, or return False when none remain."""
        for descriptor in ASSET_TYPES:
            if descriptor.display_name in self._loaded:
                continue
            self._loaded.append(descriptor.display_name)
            self._load(descriptor)
            return True
        return False

    def _load(self, descriptor: AssetTypeDescriptor) -> None:
        noun = descriptor.display_name
        try:
            # Preserve dynamic get_all_* dispatch and load the display view for complete ids.
            result = getattr(self._backend, descriptor.get_all_attr)()
        except Exception as e:
            log.error(f"Could not read the {noun} catalog while resolving dependencies: {e!r}")
            return
        if isinstance(result, Err):
            log.error(f"Could not read the {noun} catalog while resolving dependencies: "
                      f"{result.detail or result.reason.value}")
            return
        for asset in result.value:
            asset_id = asset.asset_id
            if not isinstance(asset_id, str) or not asset_id:
                continue
            # Keep the first asset class for duplicate ids to make resolution stable.
            self._items.setdefault(asset_id, CatalogItem(descriptor, asset))


def manifest_reader(backend: Any) -> "Callable[[CatalogItem], dict[str, Any] | None]":
    """Build a reader that fetches manifests only at catalog-specified commits or branches."""
    def read(item: CatalogItem) -> "dict[str, Any] | None":
        url = item.asset.github
        if url is None:
            return None
        return backend.get_manifest(url, item.asset.commit_sha or item.asset.branch)

    return read


def declared_dependencies(item: CatalogItem,
                          manifest_of: "Callable[[CatalogItem], dict[str, Any] | None]") -> list[str]:
    """Return safe store ids declared by a plugin, dropping malformed data.
    Data-only packs never read dependencies and cannot cause code installation."""
    if not item.descriptor.is_plugin:
        return []
    try:
        manifest = manifest_of(item)
    except Exception as e:
        log.warning(f"Could not read the manifest of {item.asset_id} for its dependencies: {e!r}")
        return []
    if not isinstance(manifest, dict):
        return []
    raw = manifest.get(MANIFEST_KEY)
    if raw is None:
        return []
    if not isinstance(raw, list):
        log.warning(f"Ignoring the dependencies of {item.asset_id}: {MANIFEST_KEY} holds "
                    f"a {type(raw).__name__} and not a list")
        return []
    # Defer the backend import to keep this leaf module independently importable.
    from src.backend.Store.StoreBackend import StoreBackend

    named: list[str] = []
    for value in raw:
        # Trim outer spaces, then preserve exact case-sensitive id matching.
        candidate = value.strip() if isinstance(value, str) else value
        # Apply the installer id gate before remote text reaches the consent dialog.
        if not StoreBackend.is_safe_asset_id(candidate):
            log.warning(f"Ignoring a dependency of {item.asset_id}: {value!r} is not a store id")
            continue
        named.append(candidate)
    return named


def resolve(root: CatalogItem, index: CatalogIndex,
            manifest_of: "Callable[[CatalogItem], dict[str, Any] | None]",
            max_depth: int = MAX_DEPTH) -> Plan:
    """Build a dependency-first plan with the root last.
    Skip installed dependency branches but always include the root."""
    order: list[CatalogItem] = []
    unknown: list[str] = []
    # Mark the root seen so a cycle cannot schedule it twice.
    seen: set[str] = {root.asset_id}
    truncated = False

    def walk(item: CatalogItem, depth: int) -> None:
        nonlocal truncated
        named = declared_dependencies(item, manifest_of)
        if not named:
            # An empty leaf at the depth bound is complete, not truncated.
            return
        if depth >= max_depth:
            truncated = True
            log.warning(f"Not following the dependencies of {item.asset_id} past depth {max_depth}")
            return
        for asset_id in named:
            if asset_id in seen:
                continue
            seen.add(asset_id)
            dependency = index.get(asset_id)
            if dependency is None:
                log.warning(f"{item.asset_id} needs {asset_id!r}, which no store catalog holds")
                unknown.append(asset_id)
                continue
            if dependency.installed:
                continue
            # Install each dependency after its own dependencies.
            walk(dependency, depth + 1)
            order.append(dependency)

    walk(root, 0)
    order.append(root)
    return Plan(tuple(order), tuple(unknown), truncated)


def install_plan(plan: Plan, backend: Any,
                 ask_install_script: "Callable[[str], bool] | None" = None) -> InstallReport:
    """Install in plan order, stop at the first failure, and keep items already installed."""
    installed: list[CatalogItem] = []
    for item in plan.order:
        kwargs: dict[str, Any] = {}
        if item.descriptor.is_plugin and ask_install_script is not None:
            # Pass install-script consent only to plugin installers.
            kwargs["ask_install_script"] = ask_install_script
        result = getattr(backend, item.descriptor.install_attr)(item.asset, **kwargs)
        # Narrow the result because Err is truthy.
        if isinstance(result, Err):
            log.error(f"Stopping the install set at {item.asset_id}: "
                      f"{result.detail or result.reason.value}")
            return InstallReport(tuple(installed), item, result)
        installed.append(item)
    return InstallReport(tuple(installed))


def plugin_item(asset: StoreAssetData) -> CatalogItem:
    """One catalog plugin record as the root of a plan."""
    return CatalogItem(PLUGIN, asset)


def install_with_dependencies(
        backend: Any, root: CatalogItem, *,
        confirm_set: "Callable[[str, Plan], bool] | None" = None,
        ask_install_script: "Callable[[str], bool] | None" = None,
        max_depth: int = MAX_DEPTH) -> InstallReport:
    """Resolve and install a dependency set.
    Ask via confirm_set for dependencies, unknown ids, or truncation; otherwise run unattended."""
    plan = resolve(root, CatalogIndex(backend), manifest_reader(backend), max_depth)
    degraded = bool(plan.dependencies or plan.unknown or plan.truncated)
    if degraded and confirm_set is not None:
        if not confirm_set(root.display_name, plan):
            log.info(f"Not installing {root.asset_id}: the set it needs was not confirmed")
            return InstallReport(declined=True)
    return install_plan(plan, backend, ask_install_script)


def failure_message(report: InstallReport, root_name: str) -> str:
    """What to tell the user about a set that stopped part-way. It names
    items the way the prompt named them, and not by their ids."""
    failed = report.failed.display_name if report.failed is not None else root_name
    if report.installed:
        landed = ", ".join(item.display_name for item in report.installed)
        return (f"{failed} could not be installed. These installed first and stay "
                f"installed: {landed}.")
    return f"{failed} could not be installed."


def failure_noun(report: InstallReport, fallback: str) -> str:
    """The asset class of the item that failed, for a title. A pack that
    failed under a plugin root must not be reported as a plugin."""
    if report.failed is not None:
        return report.failed.descriptor.display_name
    return fallback
