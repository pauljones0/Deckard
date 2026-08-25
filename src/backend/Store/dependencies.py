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

Only a plugin declares dependencies. The walk reads the key on a plugin
and on nothing else, so a data-only pack cannot pull in a plugin, which
would turn installing a set of pictures into installing code. A plugin
naming a pack is the case that exists, and it still works.

Only an id that a catalog already names resolves. A dependency must not
become a way to reach a repository the catalogs do not list, so an id no
catalog holds is reported and never fetched. What the catalogs are worth
is what the user configured them to be: the official catalog is read at
the vetted pin, and a custom store or a custom plugin entry the user added
is trusted because the user added it. An id that only such a source names
still resolves, so the trust here is the trust in the configured
catalogs and not a claim about any one of them.

The walk is cycle-safe and depth-bounded. An id seen once is not visited
again, which covers a cycle and a shared dependency alike, and MAX_DEPTH
ends a chain that a hostile or mistaken manifest made long. An item
already installed ends its branch: what sits on disk is left alone, and
that holds whatever version it is, so an out-of-date dependency is not
quietly updated by installing something that names it. Updating an item is
the update path's job.

A malformed list is no reason to refuse the plugin the user asked for. A
"dependencies" value that is not a list, and any element that is not a
store id, is logged and dropped, and the plugin itself still installs. An
element is held to the same id shape the installer applies to a manifest
id, so an id with a newline, a control character, a path separator or no
bound on its length never enters the plan or the consent dialog that names
it. An id is matched exactly, after leading and trailing spaces are
removed, and the match is case sensitive, because a store id is.

Consent covers the set. The prompt names every item, root and
dependencies, with the asset class of each, before anything downloads,
which is the same decide-before-download rule the install-script gate
keeps, and a decline declines all of it. It also names what could not be
resolved, so a plugin whose whole set is unknown does not install looking
healthy. A failure part-way through stops the items that have not started
and reports which ones landed.

Two things this deliberately does not do. Nothing rolls back: an item that
installed stays installed, because removing a working item to tidy up a
later failure destroys what the user did not ask to lose. And an uninstall
never cascades: removing a plugin leaves the items it named in place,
because another plugin may need the same one and nothing records who asked
for what.

There is no version constraint. A dependency names an id and nothing else.
An item the plan installs gets the version its catalog entry pins, the
same version a direct install would get; an item already on disk keeps the
version it has.
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

    order holds the items to install, and the root is always the last of
    them. Every item is listed after the items it needs, except inside a
    cycle, where no such order exists at all: a manifest set that names
    itself round a loop is broken, and the walk installs each item once in
    the order it reached them rather than refuse the install.

    unknown holds the ids a manifest named that no catalog answers for;
    they are reported and never fetched. truncated says the depth bound
    ended a chain that had more to declare, so the set is short of what the
    manifests asked for.
    """

    order: tuple[CatalogItem, ...]
    unknown: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def dependencies(self) -> tuple[CatalogItem, ...]:
        # The root is the last entry, by construction in resolve().
        return self.order[:-1]

    def names(self) -> list[str]:
        """Every item in install order, for the prompt that names the set.

        Each entry carries its asset class, so a plugin among a list of
        packs is visible as one before the user agrees to install it.
        """
        return [f"{item.display_name} ({item.descriptor.display_name})"
                for item in self.order]


@dataclass(frozen=True)
class InstallReport:
    """What one plan actually did.

    installed holds the items that landed, in the order they landed. failed
    holds the one that stopped the run, and error carries its reason. Items
    after the failure never started. declined says a person refused the set
    before anything downloaded, and nothing was touched.
    """

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
    """Every store id the configured catalogs name, loaded one asset class
    at a time.

    A dependency id is nearly always another plugin, and the plugin catalog
    is the one an install has already read, so that class loads first and a
    lookup it answers costs nothing more. The pack classes load only while an
    id is still unresolved, so a manifest that names no pack never pays for
    their catalogs. A class whose catalog will not fetch contributes nothing
    and is not retried, which leaves its ids unknown rather than silently
    resolved against a stale view.

    A catalog is read in the display view, which costs a thumbnail per
    entry, and the cheaper update-check view cannot serve this lookup. That
    view fills an entry's id from the install it matched on disk, so an
    entry that is not installed comes back with no id at all, and an
    uninstalled item is the only kind a dependency resolution can act on.
    Reading a catalog here is therefore not free, which is why nothing
    reads one until an id actually needs resolving, why the classes load
    one at a time, and why a plugin that names nothing reads none.
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
            # answers here the way it does everywhere else in the store. The
            # display view, deliberately: see the class docstring.
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
    """Read an item's manifest at the revision its catalog entry names.

    The revision comes from the entry and from nowhere else, so a
    dependency cannot send a fetch anywhere the catalogs did not point.
    That revision is a pinned commit for an entry that carries one, and a
    branch name for an entry that does not, which a branch-pinned custom
    plugin is. A branch moves after anyone reads it, so what comes back
    there is the tip of the day and not a fixed revision.
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
    answers an empty list, so the item itself still installs.

    Only a plugin declares dependencies. A data-only pack answers an empty
    list without its manifest being read at all, so a pack cannot pull in a
    plugin and turn a set of pictures into an install of code.
    """
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
    # Deferred, because this leaf module must stay importable before the
    # store backend pulls in the whole store layer.
    from src.backend.Store.StoreBackend import StoreBackend

    named: list[str] = []
    for value in raw:
        # Stripped first, so a padded id matches the index instead of
        # missing it and reading as unknown. The match itself is exact and
        # case sensitive, because a store id is.
        candidate = value.strip() if isinstance(value, str) else value
        # The id shape gate, the same one the installer applies to a manifest
        # id. An id that fails it can never match a real catalog id, so
        # nothing legitimate is lost, and an unsafe id is dropped as
        # malformed here rather than carried on into the plan. Without this a
        # crafted id, with an internal newline or of any length, would reach
        # the set-consent dialog body through plan.unknown, above the
        # buttons that approve installing code.
        if not StoreBackend.is_safe_asset_id(candidate):
            log.warning(f"Ignoring a dependency of {item.asset_id}: {value!r} is not a store id")
            continue
        named.append(candidate)
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
        named = declared_dependencies(item, manifest_of)
        if not named:
            # Nothing declared, so the bound below never applies. An item
            # that sits exactly at the bound and needs nothing has not been
            # cut short, and must not report the set as truncated.
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
    installed: list[CatalogItem] = []
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
            return InstallReport(tuple(installed), item, result)
        installed.append(item)
    return InstallReport(tuple(installed))


def plugin_item(data: StoreAssetData) -> CatalogItem:
    """One catalog plugin record as the root of a plan."""
    return CatalogItem(PLUGIN, data)


def install_with_dependencies(
        backend: Any, root: CatalogItem, *,
        confirm_set: "Callable[[str, Plan], bool] | None" = None,
        ask_install_script: "Callable[[str], bool] | None" = None,
        max_depth: int = MAX_DEPTH) -> InstallReport:
    """Resolve what root needs, ask once about the whole set, and install it.

    confirm_set gets the root's name and the whole plan, and is asked before
    the first download. It is asked whenever the plan says more than "this
    one item installs cleanly": that is a set larger than the root, an id
    that would not resolve, or a chain the depth bound cut short. A plugin
    that needs nothing, and whose manifest asked for nothing that went
    missing, keeps the prompts it always had.

    Pass None for an unattended path, where no prompt can be answered.
    """
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
