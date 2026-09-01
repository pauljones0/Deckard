"""Verify offline dependency resolution, set consent, and installation order."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import globals as gl  # noqa: F401,E402

from src.backend.Store import dependencies  # noqa: E402
from src.backend.Store.asset_types import ICON, PLUGIN  # noqa: E402
from src.backend.Store.store_result import Err, ErrReason, Ok  # noqa: E402
from src.windows.Store.StoreData import IconData, PluginData  # noqa: E402

WATCHDOG_SECONDS = 60

PINNED = "0" * 40
OTHER = "1" * 40


def plugin(asset_id: str, installed: bool = False, stale: bool = False) -> PluginData:
    return PluginData(github=f"https://github.com/test/{asset_id}",
                      plugin_id=asset_id,
                      plugin_name=asset_id.rsplit(".", 1)[-1],
                      commit_sha=PINNED,
                      local_sha=(OTHER if stale else PINNED) if installed else None)


def icon(asset_id: str, installed: bool = False) -> IconData:
    return IconData(github=f"https://github.com/test/{asset_id}",
                    icon_id=asset_id,
                    icon_name=asset_id.rsplit(".", 1)[-1],
                    commit_sha=PINNED,
                    local_sha=PINNED if installed else None)


class FakeBackend:
    """Provide catalogs, manifests, failures, and install records to dependency tests."""

    def __init__(self, plugins=(), icons=(), manifests=None,
                 plugin_catalog_error=None):
        self._plugins = list(plugins)
        self._icons = list(icons)
        self.manifests = dict(manifests or {})
        self._plugin_catalog_error = plugin_catalog_error
        self.catalog_reads: list[str] = []
        self.catalog_views: list[bool] = []
        self.manifest_reads: list[str] = []
        self.installs: list[str] = []
        self.install_kwargs: list[dict] = []
        self.fail_on: "str | None" = None

    # Catalog reads, by the names the descriptors carry.

    def get_all_plugins(self, include_images: bool = True):
        self.catalog_reads.append("plugin")
        self.catalog_views.append(include_images)
        if self._plugin_catalog_error is not None:
            return self._plugin_catalog_error
        return Ok(list(self._plugins))

    def get_all_icons(self, include_images: bool = True):
        self.catalog_reads.append("icon pack")
        self.catalog_views.append(include_images)
        return Ok(list(self._icons))

    def get_all_wallpapers(self, include_images: bool = True):
        self.catalog_reads.append("wallpaper")
        self.catalog_views.append(include_images)
        return Ok([])

    def get_all_sd_plus_bar_wallpapers(self, include_images: bool = True):
        self.catalog_reads.append("SD+ bar wallpaper")
        self.catalog_views.append(include_images)
        return Ok([])

    def get_manifest(self, url: str, commit):
        asset_id = url.rsplit("/", 1)[-1]
        self.manifest_reads.append(asset_id)
        entry = self.manifests.get(asset_id)
        if isinstance(entry, Exception):
            raise entry
        return entry

    # Installs.

    def install_plugin(self, plugin_data, auto_update: bool = False,
                       ask_install_script=None):
        return self._install(plugin_data.plugin_id,
                             {"ask_install_script": ask_install_script})

    def install_icon(self, icon_data):
        return self._install(icon_data.icon_id, {})

    def _install(self, asset_id, kwargs):
        self.installs.append(asset_id)
        self.install_kwargs.append(kwargs)
        if asset_id == self.fail_on:
            return Err(ErrReason.NO_CONNECTION, "offline")
        return Ok(None)


def plan_for(backend: FakeBackend, root_id: str, max_depth: int = dependencies.MAX_DEPTH):
    root = dependencies.CatalogItem(
        PLUGIN, next(p for p in backend._plugins if p.plugin_id == root_id))
    return dependencies.resolve(root, dependencies.CatalogIndex(backend),
                                dependencies.manifest_reader(backend), max_depth)


def ids(plan) -> list[str]:
    return [item.asset_id for item in plan.order]


# The catalog view the resolution may use

def test_uninstalled_entry_id_requires_full_catalog_view() -> None:
    """Keep dependency lookup on the display view because the cheap view omits uninstalled IDs."""
    from src.backend.Store.StoreBackend import StoreBackend
    from src.backend.Store.StoreCache import StoreCache

    backend = StoreBackend.__new__(StoreBackend)  # __init__ would spawn a fetch thread
    backend.store_cache = StoreCache()
    entry = {"url": "https://github.com/test/Dep", "hash": PINNED}

    cheap = backend._prepare_asset(entry, PLUGIN, include_image=False)
    assert isinstance(cheap, PluginData), f"the cheap view must build a record, got {cheap!r}"
    assert cheap.plugin_id is None, (
        "the update-check view must be understood to carry no id for an entry "
        f"that is not installed, got {cheap.plugin_id!r}. If this ever changes, "
        "CatalogIndex may switch to it and save a thumbnail fetch per entry.")
    assert cheap.commit_sha == PINNED, (
        "the cheap view still resolves the pin, which is why it looks usable "
        f"at a glance, got {cheap.commit_sha!r}")


def test_resolver_uses_catalog_view_with_ids() -> None:
    backend = FakeBackend(plugins=[plugin("com.test.Root"), plugin("com.test.Dep")],
                          manifests={"com.test.Root": {"dependencies": ["com.test.Dep"]},
                                     "com.test.Dep": {}})
    plan_for(backend, "com.test.Root")
    assert backend.catalog_views == [True], (
        "the resolution must read the display view, because the cheap one "
        f"carries no id for an uninstalled entry, got {backend.catalog_views}")


# Resolution

def test_cycle_resolves_once() -> None:
    backend = FakeBackend(
        plugins=[plugin("com.test.A"), plugin("com.test.B")],
        manifests={"com.test.A": {"dependencies": ["com.test.B"]},
                   "com.test.B": {"dependencies": ["com.test.A"]}})

    plan = plan_for(backend, "com.test.A")
    assert ids(plan) == ["com.test.B", "com.test.A"], (
        "a cycle must resolve to each item once, dependency first, "
        f"got {ids(plan)}")


def test_shared_dependency_installs_once_before_dependents() -> None:
    backend = FakeBackend(
        plugins=[plugin("com.test.Root"), plugin("com.test.Left"),
                 plugin("com.test.Right"), plugin("com.test.Shared")],
        manifests={
            "com.test.Root": {"dependencies": ["com.test.Left", "com.test.Right"]},
            "com.test.Left": {"dependencies": ["com.test.Shared"]},
            "com.test.Right": {"dependencies": ["com.test.Shared"]},
            "com.test.Shared": {},
        })

    plan = plan_for(backend, "com.test.Root")
    assert ids(plan) == ["com.test.Shared", "com.test.Left", "com.test.Right",
                         "com.test.Root"], (
        "a shared dependency must appear once, before both of the items that "
        f"name it, got {ids(plan)}")


def test_depth_is_bounded() -> None:
    chain = [plugin(f"com.test.D{i}") for i in range(12)]
    manifests = {f"com.test.D{i}": {"dependencies": [f"com.test.D{i + 1}"]}
                 for i in range(11)}
    manifests["com.test.D11"] = {}
    backend = FakeBackend(plugins=chain, manifests=manifests)

    plan = plan_for(backend, "com.test.D0", max_depth=3)
    assert plan.truncated, "a chain past the depth bound must report truncated"
    assert ids(plan) == ["com.test.D3", "com.test.D2", "com.test.D1", "com.test.D0"], (
        f"the walk must stop at the depth bound, got {ids(plan)}")

    short = FakeBackend(plugins=chain[:3],
                        manifests={"com.test.D0": {"dependencies": ["com.test.D1"]},
                                   "com.test.D1": {"dependencies": ["com.test.D2"]},
                                   "com.test.D2": {}})
    inside = plan_for(short, "com.test.D0", max_depth=3)
    assert not inside.truncated, (
        "a chain that ends inside the bound must not report truncated")
    assert ids(inside) == ["com.test.D2", "com.test.D1", "com.test.D0"], (
        f"it must resolve whole, got {ids(inside)}")


def test_depth_limit_without_remaining_dependencies_is_not_truncated() -> None:
    """Do not report truncation when the item at the depth bound has no dependencies."""
    backend = FakeBackend(
        plugins=[plugin("com.test.D0"), plugin("com.test.D1"), plugin("com.test.D2")],
        manifests={"com.test.D0": {"dependencies": ["com.test.D1"]},
                   "com.test.D1": {"dependencies": ["com.test.D2"]},
                   # The deepest item sits at exactly max_depth and needs
                   # nothing, so nothing was left unfollowed.
                   "com.test.D2": {"dependencies": []}})

    plan = plan_for(backend, "com.test.D0", max_depth=2)
    assert ids(plan) == ["com.test.D2", "com.test.D1", "com.test.D0"], (
        f"the whole chain must resolve, got {ids(plan)}")
    assert not plan.truncated, (
        "an item at the bound that declares nothing must not report the set "
        "as truncated")

    # One that really does declare something at the bound is truncated.
    deeper = FakeBackend(
        plugins=[plugin("com.test.D0"), plugin("com.test.D1"),
                 plugin("com.test.D2"), plugin("com.test.D3")],
        manifests={"com.test.D0": {"dependencies": ["com.test.D1"]},
                   "com.test.D1": {"dependencies": ["com.test.D2"]},
                   "com.test.D2": {"dependencies": ["com.test.D3"]},
                   "com.test.D3": {}})
    cut = plan_for(deeper, "com.test.D0", max_depth=2)
    assert cut.truncated, (
        "an item at the bound that still names something must report the set "
        "as truncated")


def test_unknown_dependency_is_reported_without_fetch() -> None:
    backend = FakeBackend(
        plugins=[plugin("com.test.Root")],
        icons=[icon("com.test.Icons")],
        manifests={"com.test.Root": {"dependencies": ["com.test.Nowhere"]}})

    plan = plan_for(backend, "com.test.Root")
    assert ids(plan) == ["com.test.Root"], (
        f"an unresolvable dependency must not enter the plan, got {ids(plan)}")
    assert plan.unknown == ("com.test.Nowhere",), (
        f"the unresolvable id must be reported, got {plan.unknown}")
    assert "com.test.Nowhere" not in backend.manifest_reads, (
        "an id no catalog holds must never be fetched, got "
        f"{backend.manifest_reads}")


def test_installed_dependency_skips_its_dependency_chain() -> None:
    backend = FakeBackend(
        plugins=[plugin("com.test.Root"), plugin("com.test.Have", installed=True),
                 plugin("com.test.Under")],
        manifests={"com.test.Root": {"dependencies": ["com.test.Have"]},
                   "com.test.Have": {"dependencies": ["com.test.Under"]}})

    plan = plan_for(backend, "com.test.Root")
    assert ids(plan) == ["com.test.Root"], (
        f"an installed dependency must not be reinstalled, got {ids(plan)}")
    assert "com.test.Have" not in backend.manifest_reads, (
        "an installed dependency ends its branch, so its own manifest is not "
        f"read either, got {backend.manifest_reads}")


def test_outdated_installed_dependency_is_unchanged() -> None:
    """Leave installed dependencies unchanged even when their catalog pin is newer."""
    backend = FakeBackend(
        plugins=[plugin("com.test.Root"),
                 plugin("com.test.Stale", installed=True, stale=True)],
        manifests={"com.test.Root": {"dependencies": ["com.test.Stale"]},
                   "com.test.Stale": {}})

    stale = next(p for p in backend._plugins if p.plugin_id == "com.test.Stale")
    assert stale.local_sha != stale.commit_sha, (
        "this leg needs an installed item that is behind the pin, or it "
        "proves nothing")

    plan = plan_for(backend, "com.test.Root")
    assert ids(plan) == ["com.test.Root"], (
        "an item that is installed but out of date must still be left alone, "
        f"got {ids(plan)}")


def test_installed_root_can_be_reinstalled() -> None:
    backend = FakeBackend(plugins=[plugin("com.test.Root", installed=True)],
                          manifests={"com.test.Root": {}})

    plan = plan_for(backend, "com.test.Root")
    assert ids(plan) == ["com.test.Root"], (
        f"a reinstall of an installed root must still run, got {ids(plan)}")


def test_pack_dependency_uses_matching_catalog() -> None:
    backend = FakeBackend(
        plugins=[plugin("com.test.Root")],
        icons=[icon("com.test.Icons")],
        manifests={"com.test.Root": {"dependencies": ["com.test.Icons"]},
                   "com.test.Icons": {}})

    plan = plan_for(backend, "com.test.Root")
    assert ids(plan) == ["com.test.Icons", "com.test.Root"], (
        f"a dependency on an icon pack must resolve, got {ids(plan)}")
    assert plan.order[0].descriptor is ICON, (
        "the pack must carry its own descriptor, so the install goes through "
        f"install_icon, got {plan.order[0].descriptor.display_name}")


def test_pack_cannot_depend_on_plugin() -> None:
    """Only a plugin declares dependencies. A pack that named one would turn
    installing a set of pictures into installing code."""
    backend = FakeBackend(
        plugins=[plugin("com.test.Code")],
        icons=[icon("com.test.Icons")],
        manifests={"com.test.Icons": {"dependencies": ["com.test.Code"]},
                   "com.test.Code": {}})

    root = dependencies.CatalogItem(ICON, backend._icons[0])
    plan = dependencies.resolve(root, dependencies.CatalogIndex(backend),
                                dependencies.manifest_reader(backend))
    assert ids(plan) == ["com.test.Icons"], (
        f"a pack root must install itself and nothing else, got {ids(plan)}")
    assert backend.manifest_reads == [], (
        "a pack's manifest is not even read for dependencies, got "
        f"{backend.manifest_reads}")
    assert backend.catalog_reads == [], (
        f"and no catalog is loaded for it, got {backend.catalog_reads}")


def test_lookup_reads_only_required_catalogs() -> None:
    backend = FakeBackend(plugins=[plugin("com.test.Root"), plugin("com.test.Dep")],
                          manifests={"com.test.Root": {"dependencies": ["com.test.Dep"]},
                                     "com.test.Dep": {}})

    plan_for(backend, "com.test.Root")
    assert backend.catalog_reads == ["plugin"], (
        "an id the plugin catalog answers must not cost the pack catalogs, "
        f"got {backend.catalog_reads}")

    quiet = FakeBackend(plugins=[plugin("com.test.Alone")],
                        manifests={"com.test.Alone": {}})
    plan_for(quiet, "com.test.Alone")
    assert quiet.catalog_reads == [], (
        "a plugin that names no dependency must read no catalog at all, "
        f"got {quiet.catalog_reads}")


def test_malformed_dependencies_do_not_block_root_install() -> None:
    shapes = [
        {"dependencies": "com.test.Dep"},
        {"dependencies": 5},
        {"dependencies": {"com.test.Dep": "1.0"}},
        {"dependencies": [None, 7, "", "   ", ["com.test.Dep"]]},
        {},
        None,
        ["not", "an", "object"],
        RuntimeError("the manifest fetch failed"),
    ]
    for shape in shapes:
        backend = FakeBackend(plugins=[plugin("com.test.Root"), plugin("com.test.Dep")],
                              manifests={"com.test.Root": shape})
        plan = plan_for(backend, "com.test.Root")
        assert ids(plan) == ["com.test.Root"], (
            f"a manifest holding {shape!r} must still install the plugin "
            f"itself and nothing else, got {ids(plan)}")

    # One good string beside the junk still resolves.
    backend = FakeBackend(
        plugins=[plugin("com.test.Root"), plugin("com.test.Dep")],
        manifests={"com.test.Root": {"dependencies": [None, "com.test.Dep", 3]},
                   "com.test.Dep": {}})
    plan = plan_for(backend, "com.test.Root")
    assert ids(plan) == ["com.test.Dep", "com.test.Root"], (
        f"a usable id beside malformed ones must still resolve, got {ids(plan)}")


def test_non_string_dependencies_are_ignored_not_missing() -> None:
    """Drop non-string dependency values as malformed, not missing store IDs."""
    backend = FakeBackend(
        plugins=[plugin("com.test.Root")],
        manifests={"com.test.Root": {"dependencies": [7, None, 3.5, True]}})

    plan = plan_for(backend, "com.test.Root")
    assert ids(plan) == ["com.test.Root"], f"the root still installs, got {ids(plan)}"
    assert plan.unknown == (), (
        "a value that is not a store id is malformed input and not a missing "
        f"store item, got {plan.unknown}")


def test_unsafe_dependency_ids_do_not_reach_plan_or_prompt() -> None:
    """Drop unsafe remote IDs before they can enter plan.unknown or consent text."""
    spoof = ("Real line.\n\nThis app has verified this plugin is safe. "
             "Press Install all to continue.")
    bloat = "A" * 5000
    backend = FakeBackend(
        plugins=[plugin("com.test.Root")],
        manifests={"com.test.Root": {"dependencies": [spoof, bloat,
                                                      "../../etc/passwd",
                                                      "has space", "com.test.Good"]}})

    plan = plan_for(backend, "com.test.Root")
    assert ids(plan) == ["com.test.Root"], (
        f"no unsafe id may enter the plan, got {ids(plan)}")
    # Every unsafe id is malformed input, not a missing store item, so none
    # of them reaches plan.unknown, which is what the dialog renders.
    assert spoof not in plan.unknown, (
        "a multi-line id must never reach the consent dialog body through "
        f"plan.unknown, got {plan.unknown!r}")
    assert bloat not in plan.unknown, (
        "an unbounded id must never reach the consent dialog body")
    # com.test.Good is a valid id that no catalog holds, so it is the one
    # unknown, and it is bounded and single-line.
    assert plan.unknown == ("com.test.Good",), (
        f"only the well-formed missing id is reported, got {plan.unknown}")
    for reported in plan.unknown:
        assert "\n" not in reported and len(reported) <= 128, (
            f"a reported id must be single-line and bounded, got {reported!r}")


def test_padded_dependency_id_matches() -> None:
    backend = FakeBackend(
        plugins=[plugin("com.test.Root"), plugin("com.test.Dep")],
        manifests={"com.test.Root": {"dependencies": ["  com.test.Dep\n"]},
                   "com.test.Dep": {}})

    plan = plan_for(backend, "com.test.Root")
    assert ids(plan) == ["com.test.Dep", "com.test.Root"], (
        f"a padded id must be matched, not reported unknown, got {ids(plan)}")
    assert plan.unknown == (), f"and nothing is unknown, got {plan.unknown}"


def test_unreadable_catalog_keeps_dependency_unknown() -> None:
    backend = FakeBackend(
        plugins=[plugin("com.test.Root")],
        manifests={"com.test.Root": {"dependencies": ["com.test.Dep"]}},
        plugin_catalog_error=Err(ErrReason.NO_CONNECTION, "offline"))
    root = dependencies.CatalogItem(PLUGIN, plugin("com.test.Root"))
    plan = dependencies.resolve(root, dependencies.CatalogIndex(backend),
                                dependencies.manifest_reader(backend))
    assert ids(plan) == ["com.test.Root"], (
        f"an unreadable catalog must not invent a dependency, got {ids(plan)}")
    assert plan.unknown == ("com.test.Dep",), (
        f"the id must be reported unresolved, got {plan.unknown}")


# Install flow

class SetConsent:
    def __init__(self, agree: bool):
        self.agree = agree
        self.asked: list[tuple[str, list[str]]] = []
        self.plans: list = []
        self.installs_when_asked: "int | None" = None
        self.backend: "FakeBackend | None" = None

    def __call__(self, root_name: str, plan) -> bool:
        self.asked.append((root_name, plan.names()))
        self.plans.append(plan)
        if self.backend is not None:
            self.installs_when_asked = len(self.backend.installs)
        return self.agree


def flow_backend() -> FakeBackend:
    return FakeBackend(
        plugins=[plugin("com.test.Root"), plugin("com.test.Dep")],
        icons=[icon("com.test.Icons")],
        manifests={"com.test.Root": {"dependencies": ["com.test.Dep", "com.test.Icons"]},
                   "com.test.Dep": {},
                   "com.test.Icons": {}})


def root_item(backend: FakeBackend):
    return dependencies.plugin_item(
        next(p for p in backend._plugins if p.plugin_id == "com.test.Root"))


def test_consent_lists_set_before_download() -> None:
    backend = flow_backend()
    consent = SetConsent(agree=True)
    consent.backend = backend

    report = dependencies.install_with_dependencies(
        backend, root_item(backend), confirm_set=consent)

    assert len(consent.asked) == 1, (
        f"the set must be confirmed exactly once, got {consent.asked}")
    root_name, names = consent.asked[0]
    assert root_name == "Root", f"the prompt must name the root, got {root_name!r}"
    assert names == ["Dep (plugin)", "Icons (icon pack)", "Root (plugin)"], (
        "the prompt must name every item of the set with its asset class, in "
        f"install order, got {names}")
    assert consent.installs_when_asked == 0, (
        "the prompt must be answered before anything downloads, but "
        f"{consent.installs_when_asked} installs had already run")
    assert backend.installs == ["com.test.Dep", "com.test.Icons", "com.test.Root"], (
        f"the set must install dependencies first, got {backend.installs}")
    assert report.ok and report.installed_ids == (
        "com.test.Dep", "com.test.Icons", "com.test.Root"), (
        f"the report must list every item that landed, got {report!r}")


def test_decline_cancels_dependency_set() -> None:
    backend = flow_backend()
    consent = SetConsent(agree=False)

    report = dependencies.install_with_dependencies(
        backend, root_item(backend), confirm_set=consent)

    assert backend.installs == [], (
        f"a declined set must install nothing at all, got {backend.installs}")
    assert report.declined and not report.ok, (
        f"the report must say the set was declined, got {report!r}")
    assert report.installed_ids == (), (
        f"a declined set landed nothing, got {report.installed_ids}")


def test_dependency_free_plugin_skips_set_prompt() -> None:
    backend = FakeBackend(plugins=[plugin("com.test.Root")],
                          manifests={"com.test.Root": {}})
    consent = SetConsent(agree=True)

    report = dependencies.install_with_dependencies(
        backend, root_item(backend), confirm_set=consent)

    assert consent.asked == [], (
        "a plugin that needs nothing keeps the prompts it always had, "
        f"got {consent.asked}")
    assert backend.installs == ["com.test.Root"] and report.ok, (
        f"it must still install, got {backend.installs} / {report!r}")


def test_missing_dependency_set_is_shown_for_consent() -> None:
    """Every named id unresolvable means the plugin installs without what it
    asked for. Saying nothing would let it look healthy."""
    backend = FakeBackend(
        plugins=[plugin("com.test.Root")],
        manifests={"com.test.Root": {"dependencies": ["com.test.Gone"]}})
    consent = SetConsent(agree=True)

    dependencies.install_with_dependencies(
        backend, root_item(backend), confirm_set=consent)

    assert len(consent.asked) == 1, (
        "a set whose items could not be resolved must still be put to the "
        f"user, got {consent.asked}")
    assert consent.plans[0].unknown == ("com.test.Gone",), (
        f"and the prompt must be given what went missing, got {consent.plans[0].unknown}")

    # Refusing that one installs nothing.
    refused = FakeBackend(
        plugins=[plugin("com.test.Root")],
        manifests={"com.test.Root": {"dependencies": ["com.test.Gone"]}})
    report = dependencies.install_with_dependencies(
        refused, root_item(refused), confirm_set=SetConsent(agree=False))
    assert refused.installs == [] and report.declined, (
        f"refusing must install nothing, got {refused.installs}")


def test_partial_failure_reports_installed_dependencies() -> None:
    backend = flow_backend()
    backend.fail_on = "com.test.Icons"
    consent = SetConsent(agree=True)

    report = dependencies.install_with_dependencies(
        backend, root_item(backend), confirm_set=consent)

    assert backend.installs == ["com.test.Dep", "com.test.Icons"], (
        "the items after a failure must never start, got "
        f"{backend.installs}")
    assert not report.ok, f"a mid-set failure is not a success, got {report!r}"
    assert report.failed_id == "com.test.Icons", (
        f"the report must name the item that failed, got {report.failed_id!r}")
    assert report.installed_ids == ("com.test.Dep",), (
        "the report must name what landed and stays installed, got "
        f"{report.installed_ids}")
    assert isinstance(report.error, Err), (
        f"the report must carry the failure reason, got {report.error!r}")

    message = dependencies.failure_message(report, "Root")
    assert "Icons" in message and "Dep" in message, (
        f"the message must name the failure and what stays, got {message!r}")
    assert "com.test." not in message, (
        "the message must name items the way the prompt named them, and not "
        f"by their ids, got {message!r}")
    assert dependencies.failure_noun(report, "plugin") == "icon pack", (
        "a pack that failed under a plugin root must be titled as a pack, got "
        f"{dependencies.failure_noun(report, 'plugin')!r}")


def test_install_script_prompt_applies_only_to_plugins() -> None:
    backend = flow_backend()

    def prompt(display_name: str) -> bool:
        return True

    dependencies.install_with_dependencies(
        backend, root_item(backend), confirm_set=SetConsent(agree=True),
        ask_install_script=prompt)

    by_id = dict(zip(backend.installs, backend.install_kwargs))
    assert by_id["com.test.Dep"]["ask_install_script"] is prompt, (
        "a plugin install must carry the install-script prompt")
    assert by_id["com.test.Root"]["ask_install_script"] is prompt, (
        "the root plugin install must carry the install-script prompt")
    assert by_id["com.test.Icons"] == {}, (
        "an icon pack runs no install script, so its install takes no prompt, "
        f"got {by_id['com.test.Icons']}")


def test_unattended_install_includes_dependencies() -> None:
    backend = flow_backend()

    report = dependencies.install_with_dependencies(backend, root_item(backend))

    assert backend.installs == ["com.test.Dep", "com.test.Icons", "com.test.Root"], (
        "a path with no prompt surface still installs the set, got "
        f"{backend.installs}")
    assert report.ok, f"it must succeed, got {report!r}"


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, "scenario_store_dependencies")
    test_uninstalled_entry_id_requires_full_catalog_view()
    test_resolver_uses_catalog_view_with_ids()
    test_cycle_resolves_once()
    test_shared_dependency_installs_once_before_dependents()
    test_depth_is_bounded()
    test_depth_limit_without_remaining_dependencies_is_not_truncated()
    test_unknown_dependency_is_reported_without_fetch()
    test_installed_dependency_skips_its_dependency_chain()
    test_outdated_installed_dependency_is_unchanged()
    test_installed_root_can_be_reinstalled()
    test_pack_dependency_uses_matching_catalog()
    test_pack_cannot_depend_on_plugin()
    test_lookup_reads_only_required_catalogs()
    test_malformed_dependencies_do_not_block_root_install()
    test_non_string_dependencies_are_ignored_not_missing()
    test_unsafe_dependency_ids_do_not_reach_plan_or_prompt()
    test_padded_dependency_id_matches()
    test_unreadable_catalog_keeps_dependency_unknown()
    test_consent_lists_set_before_download()
    test_decline_cancels_dependency_set()
    test_dependency_free_plugin_skips_set_prompt()
    test_missing_dependency_set_is_shown_for_consent()
    test_partial_failure_reports_installed_dependencies()
    test_install_script_prompt_applies_only_to_plugins()
    test_unattended_install_includes_dependencies()
    print("PASS: scenario_store_dependencies")


if __name__ == "__main__":
    main()
