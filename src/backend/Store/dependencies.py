"""The store items a plugin needs, resolved before anything downloads.

A plugin's manifest.json may carry a "dependencies" key, holding a flat
list of the store ids it needs. Each id names another store item, which is
a plugin, an icon pack, a wallpaper pack or an SD+ bar wallpaper pack.
Installing that plugin installs the items it names first. The whole
plugin-facing contract is this:

    "dependencies": ["com.core447.OSPlugin", "com.core447.MaterialIcons"]

An id is the "id" its own manifest carries, which is what the settings and
the install directory are keyed by, and never a repository name or a
folder name.

Every rule below is there because the list is remote data.

Only an id that a vetted catalog already names resolves. The catalog pin
is the trust boundary the whole store rests on, and a dependency must not
become a way to reach a repository nobody vetted, so an id that no catalog
holds is reported to the user and never fetched.

The walk is cycle-safe and depth-bounded. An id seen once is not visited
again, which covers a cycle and a shared dependency alike, and MAX_DEPTH
ends a chain that a hostile or mistaken manifest made long. An item
already installed ends its branch: what sits on disk needs neither a
download nor its own dependencies resolved again.

A malformed list is no reason to refuse the plugin the user asked for. A
"dependencies" value that is not a list, and any element that is not a
non-empty string, is logged and dropped, and the plugin itself still
installs.

Consent covers the set. The prompt names every item, root and
dependencies, before anything downloads, which is the same
decide-before-download rule the install-script gate keeps, and a decline
declines all of it. A failure part-way through stops the items that have
not started and reports which ones landed.

Two things this deliberately does not do. Nothing rolls back: an item that
installed stays installed, because removing a working item to tidy up a
later failure destroys what the user did not ask to lose. And an uninstall
never cascades: removing a plugin leaves the items it named in place,
because another plugin may need the same one and nothing records who asked
for what.

There is no version constraint. A dependency names an id and nothing else,
and the version installed is the one the catalog pins, the same version a
direct install of that item would get.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger as log

from src.backend.Store.asset_types import ASSET_TYPES, PLUGIN, AssetTypeDescriptor
from src.backend.Store.store_result import Err
from src.windows.Store.StoreData import StoreAssetData

# The manifest key that holds the list.
MANIFEST_KEY = "dependencies"

# How deep the walk follows one chain of dependencies. Real sets are one or
# two deep; the bound is what stops a crafted chain from turning one click
# into an unbounded number of installs.
MAX_DEPTH = 8


@dataclass(frozen=True)
class CatalogItem:
    """One store item a plan can install: its catalog record, and the
    descriptor that says which install method takes it."""

    descriptor: AssetTypeDescriptor
    data: StoreAssetData

    @property
    def asset_id(self) -> str:
        # A record with no id never reaches a plan: the index skips it, and
        # a root with none fails the installer's own id check.
        return self.data.asset_id or ""

    @property
    def display_name(self) -> str:
        """What the consent prompt calls this item. The update-check view of
        a catalog carries no name, so the id stands in, and an id is what the
        manifest named anyway."""
        return self.data.asset_name or self.asset_id

    @property
    def installed(self) -> bool:
        # local_sha is the commit the installed copy sits on, and None means
        # nothing of this item is installed.
        return self.data.local_sha is not None


@dataclass(frozen=True)
class Plan:
    """What one install will do, decided before it downloads anything.

    order holds the items to install, dependencies first and the root last,
    so nothing installs before what it needs. unknown holds the ids a
    manifest named that no vetted catalog answers for; they are reported and
    never fetched. truncated says the depth bound ended a chain, so the set
    may be short of what the manifests asked for.
    """

    order: tuple[CatalogItem, ...]
    unknown: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def dependencies(self) -> tuple[CatalogItem, ...]:
        # The root is the last entry, by construction in resolve().
        return self.order[:-1]

    def names(self) -> list[str]:
        """Every item in install order, for the prompt that names the set."""
        return [item.display_name for item in self.order]


@dataclass(frozen=True)
class InstallReport:
    """What one plan actually did.

    installed names the items that landed, in the order they landed. failed
    names the one that stopped the run, and error carries its reason. Items
    after the failure never started. declined says a person refused the set
    before anything downloaded, and nothing was touched.
    """

    installed: tuple[str, ...] = ()
    failed: str | None = None
    error: Err | None = None
    declined: bool = False

    @property
    def ok(self) -> bool:
        return self.failed is None and not self.declined


class CatalogIndex:
    """Every store id the vetted catalogs name, loaded one asset class at a
    time.

    A dependency id is nearly always another plugin, and the plugin catalog
    is the one an install has already read, so that class loads first and a
    lookup it answers costs nothing more. The pack classes load only while an
    id is still unresolved, so a manifest that names no pack never pays for
    their catalogs. A class whose catalog will not fetch contributes nothing
    and is not retried, which leaves its ids unknown rather than silently
    resolved against a stale view.
    """

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
        """Load the next asset class. Returns False once every class is
        loaded, which is what ends the lookup loop."""
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
            # Through the descriptor's method name, so a stubbed get_all_*
            # answers here the way it does everywhere else in the store.
            result = getattr(self._backend, descriptor.get_all_attr)()
        except Exception as e:
            log.error(f"Could not read the {noun} catalog while resolving dependencies: {e!r}")
            return
        if isinstance(result, Err):
            log.error(f"Could not read the {noun} catalog while resolving dependencies: "
                      f"{result.detail or result.reason.value}")
            return
        for data in result.value:
            asset_id = data.asset_id
            if not isinstance(asset_id, str) or not asset_id:
                continue
            # The first class that claims an id keeps it. Two classes never
            # share one id in the official catalogs, and a broken one must
            # resolve the same way twice rather than by fetch order.
            self._items.setdefault(asset_id, CatalogItem(descriptor, data))


def manifest_reader(backend: Any) -> "Callable[[CatalogItem], dict[str, Any] | None]":
    """Read an item's manifest at the revision its catalog entry pins.

    The revision comes from the entry and from nowhere else, so nothing
    outside the vetted pin is ever fetched.
    """
    def read(item: CatalogItem) -> "dict[str, Any] | None":
        url = item.data.github
        if url is None:
            return None
        return backend.get_manifest(url, item.data.commit_sha or item.data.branch)

    return read


def declared_dependencies(item: CatalogItem,
                          manifest_of: "Callable[[CatalogItem], dict[str, Any] | None]") -> list[str]:
    """The store ids one item's manifest names, with every malformed entry
    dropped. A manifest that cannot be read, or that holds nonsense here,
    answers an empty list, so the item itself still installs."""
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
    named: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            log.warning(f"Ignoring a dependency of {item.asset_id}: {value!r} is not a store id")
            continue
        named.append(value)
    return named


def resolve(root: CatalogItem, index: CatalogIndex,
            manifest_of: "Callable[[CatalogItem], dict[str, Any] | None]",
            max_depth: int = MAX_DEPTH) -> Plan:
    """Decide what installing root will install, and in what order.

    The root is always in the plan, and always last, so a reinstall of an
    installed item still runs. A dependency already installed is not in the
    plan at all, and neither are its own dependencies: what is on disk is
    taken as complete.
    """
    order: list[CatalogItem] = []
    unknown: list[str] = []
    # The root counts as seen, so a manifest that names it back is a cycle
    # this skips rather than a second install of the same thing.
    seen: set[str] = {root.asset_id}
    truncated = False

    def walk(item: CatalogItem, depth: int) -> None:
        nonlocal truncated
        if depth >= max_depth:
            truncated = True
            log.warning(f"Not following the dependencies of {item.asset_id} past depth {max_depth}")
            return
        for asset_id in declared_dependencies(item, manifest_of):
            if asset_id in seen:
                continue
            seen.add(asset_id)
            dependency = index.get(asset_id)
            if dependency is None:
                log.warning(f"{item.asset_id} needs {asset_id!r}, which no vetted store catalog holds")
                unknown.append(asset_id)
                continue
            if dependency.installed:
                continue
            # Depth first, then this one, so every item lands after the
            # items it needs.
            walk(dependency, depth + 1)
            order.append(dependency)

    walk(root, 0)
    order.append(root)
    return Plan(tuple(order), tuple(unknown), truncated)


def install_plan(plan: Plan, backend: Any,
                 ask_install_script: "Callable[[str], bool] | None" = None) -> InstallReport:
    """Install every item of a plan, in the plan's order. Stops at the first
    failure and reports what landed; nothing already installed is undone."""
    installed: list[str] = []
    for item in plan.order:
        kwargs: dict[str, Any] = {}
        if item.descriptor.is_plugin and ask_install_script is not None:
            # Only a plugin runs an install script, so only a plugin install
            # takes the prompt that gates one.
            kwargs["ask_install_script"] = ask_install_script
        result = getattr(backend, item.descriptor.install_attr)(item.data, **kwargs)
        # The install methods answer a StoreResult, where an Err is the
        # failure and every other value is the one success. Narrow it; an Err
        # is truthy, so a truth test would count a failure as a success.
        if isinstance(result, Err):
            log.error(f"Stopping the install set at {item.asset_id}: "
                      f"{result.detail or result.reason.value}")
            return InstallReport(tuple(installed), item.asset_id, result)
        installed.append(item.asset_id)
    return InstallReport(tuple(installed))


def plugin_item(data: StoreAssetData) -> CatalogItem:
    """One catalog plugin record as the root of a plan."""
    return CatalogItem(PLUGIN, data)


def install_with_dependencies(
        backend: Any, root: CatalogItem, *,
        confirm_set: "Callable[[str, list[str]], bool] | None" = None,
        ask_install_script: "Callable[[str], bool] | None" = None,
        max_depth: int = MAX_DEPTH) -> InstallReport:
    """Resolve what root needs, ask once about the whole set, and install it.

    confirm_set names the root and every item beside it, and is asked before
    the first download. It is asked only when the set is larger than the root
    alone, so a plugin that needs nothing keeps the prompts it always had.
    Pass None for an unattended path, such as the first-run install, where no
    prompt can be answered.
    """
    plan = resolve(root, CatalogIndex(backend), manifest_reader(backend), max_depth)
    if plan.dependencies and confirm_set is not None:
        if not confirm_set(root.display_name, plan.names()):
            log.info(f"Not installing {root.asset_id}: the set it needs was not confirmed")
            return InstallReport(declined=True)
    return install_plan(plan, backend, ask_install_script)


def failure_message(report: InstallReport, root_name: str) -> str:
    """What to tell the user about a set that stopped part-way."""
    failed = report.failed or root_name
    if report.installed:
        return (f"{failed} could not be installed. These installed first and stay "
                f"installed: {', '.join(report.installed)}.")
    return f"{failed} could not be installed."
