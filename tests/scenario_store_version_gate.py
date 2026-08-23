"""
Regression test for the minimum-app-version gate, and for the shared store
tab and card that carry it.

StoreData.is_min_app_version_satisfied is the one implementation, and it
compares inclusively, so an asset requiring exactly the running version is
compatible. StorePreview delegates to it.

The second half is the anti-drift guard. Every store tab is one
descriptor-driven page class, and every card one descriptor-driven preview
class. Each began as four copies that drifted apart, one convenient edit at a
time, and the version gate was the copy that drifted furthest. A subclass may
hold what its asset class really does differently, and never a copy of a
method the shared class already carries.
"""

# No GTK widget is built here, because the method never touches self.
import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

import ast
from pathlib import Path

from packaging import version

from src.windows.Store.StoreData import is_min_app_version_satisfied


def test_helper_gate_semantics() -> None:
    app = version.parse(gl.app_version)

    assert is_min_app_version_satisfied(None) is True, "no requirement -> compatible"
    assert is_min_app_version_satisfied("0.0.1") is True, "older requirement -> compatible"
    assert is_min_app_version_satisfied(gl.app_version) is True, (
        f"an asset requiring exactly the running version ({gl.app_version}) "
        "must be compatible -- this was the off-by-one"
    )
    newer = f"{app.major + 1}.0.0"
    assert is_min_app_version_satisfied(newer) is False, "newer requirement -> incompatible"

    # A garbage version string from a remote catalog must not raise out of
    # the preview build. It fails open like the None case, with a warning.
    assert is_min_app_version_satisfied("not-a-version") is True


def test_verdict_matches_runtime_gate_on_suffixed_versions() -> None:
    """The store badge must agree with the runtime plugin loader.

    PluginBase.is_minimum_version_ok compares base versions, with the pre-
    release, post-release, dev and local suffixes stripped.
    """
    # A raw parsed compare diverges on a pre-release build, where an asset
    # pinned to the release loads at runtime but reads as incompatible.
    running = version.parse(gl.app_version)
    base = running.base_version  # e.g. "1.5.0" for a "1.5.0-beta.15" build

    def runtime_gate_says(minimum: str) -> bool:
        # Mirror of PluginBase.is_minimum_version_ok / _get_parsed_base_version.
        if minimum is None:
            return True
        min_base = version.parse(version.parse(minimum).base_version)
        app_base = version.parse(base)
        return app_base >= min_base

    for minimum in (
        base,             # the plain release, where a beta build diverges
        f"{base}.post1",  # post-release suffix
        f"{base}rc1",     # pre-release suffix on the same base
        "0.0.1",
        f"{version.parse(base).major + 1}.0.0",  # genuinely newer -> incompatible
    ):
        assert is_min_app_version_satisfied(minimum) == runtime_gate_says(minimum), (
            f"store badge and runtime gate disagree on min={minimum!r} "
            f"(running {gl.app_version!r})"
        )

    # Spell out the concrete case, so a regression names itself. On a
    # pre-release build, requiring exactly the release must display as
    # compatible, because it loads at runtime.
    if running.is_prerelease:
        assert is_min_app_version_satisfied(base) is True, (
            f"on pre-release build {gl.app_version!r}, an asset requiring the "
            f"release {base!r} must display compatible -- the runtime loader loads it"
        )


def test_preview_delegates_to_helper() -> None:
    from src.windows.Store.Preview import StorePreview

    # The method never dereferences self, so an unbound call needs no GTK
    # widget. Equality must pass, where a strict comparison returns False.
    assert StorePreview.check_required_version(None, gl.app_version) is True
    assert StorePreview.check_required_version(None, None) is True
    app = version.parse(gl.app_version)
    assert StorePreview.check_required_version(None, f"{app.major + 1}.0.0") is False


def test_duplicate_copies_are_gone() -> None:
    from src.windows.Store.StorePage import StorePage
    from src.windows.Store.Plugins.PluginPage import PluginPage, PluginPreview
    from src.windows.Store.Preview import StorePreview

    assert "check_required_version" not in vars(StorePage), (
        "StorePage must no longer carry its own copy of the version gate"
    )
    assert "check_required_version" not in vars(PluginPage), (
        "PluginPage must no longer carry its own copy of the version gate"
    )
    assert "check_required_version" not in vars(PluginPreview), (
        "PluginPreview must inherit StorePreview's gate, not duplicate it"
    )
    assert PluginPreview.check_required_version is StorePreview.check_required_version


# The methods the shared tab and the shared card carry for every asset class.
# A subclass that defines one of these holds a per-family copy again.
COLLAPSED_PAGE_METHODS = frozenset({
    "__init__",        # the search placeholder both sections take
    "load",            # the fetch, the compatibility split and the append
    "preview_cls",     # the card class, resolved from the descriptor name
    "build_preview",   # the card itself
})
COLLAPSED_PREVIEW_METHODS = frozenset({
    "__init__",                 # labels, image, badges, border, state, description
    "install",
    "notify_install_failure",
    "update",
    "on_click_main",
    "check_required_version",
})

# What one asset class may still do differently, and why. Everything else
# belongs in the shared class.
ALLOWED_PAGE_OVERRIDES = {
    "PluginPage": frozenset(),
}
ALLOWED_PREVIEW_OVERRIDES = {
    # get_install_state_for reads a compatibility gate no data-only asset has.
    # uninstall names a plugin id, where the shared one passes the record.
    "PluginPreview": frozenset({"get_install_state_for", "uninstall"}),
}

STORE_WINDOW_DIR = Path(__file__).resolve().parent.parent / "src" / "windows" / "Store"

# The one subclass of each shared class. A new one is a deliberate edit here,
# together with the reason it cannot be a descriptor row.
EXPECTED_PAGE_SUBCLASSES = {"PluginPage"}
EXPECTED_PREVIEW_SUBCLASSES = {"PluginPreview"}
# The shared classes are the only thing that subclasses the store page and
# preview bases. A new tab that reaches past them re-opens the copy problem.
EXPECTED_BASE_SUBCLASSES = {
    "StorePage": {"StoreAssetPage"},
    "StorePreview": {"StoreAssetPreview"},
}


def _class_defs() -> "list[tuple[Path, ast.ClassDef]]":
    """Every class defined under src/windows/Store, with the file it lives in.

    The scan reads the tree rather than __subclasses__, so a page module that
    nothing imports is covered too.
    """
    found: list[tuple[Path, ast.ClassDef]] = []
    paths = sorted(STORE_WINDOW_DIR.rglob("*.py"))
    assert paths, f"no store window modules found under {STORE_WINDOW_DIR}"
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                found.append((path, node))
    return found


def _base_names(node: "ast.ClassDef") -> "set[str]":
    """The names this class lists as its direct bases."""
    names: set[str] = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _class_index(defs: "list[tuple[Path, ast.ClassDef]]") -> "dict[str, ast.ClassDef]":
    """Class name to its definition, over the whole scanned tree.

    A duplicate name would make the closure below follow the wrong bases, so
    it fails here instead of reading as a class with no ancestry.
    """
    index: dict[str, ast.ClassDef] = {}
    for path, node in defs:
        assert node.name not in index, (
            f"two classes under {STORE_WINDOW_DIR.name} are named {node.name} "
            f"({path.name} is the second). The ancestry closure this guard "
            "walks cannot tell them apart."
        )
        index[node.name] = node
    return index


def _ancestor_names(node: "ast.ClassDef",
                    index: "dict[str, ast.ClassDef]") -> "set[str]":
    """Every base of this class, direct and inherited.

    A direct-base read alone lets a copy hide one level down: a class that
    subclasses PluginPreview and re-grows install() is a card the descriptor
    table can still name, and a check that looks at its bases only would see
    a class it does not govern.
    """
    ancestors: set[str] = set()
    queue = list(_base_names(node))
    while queue:
        name = queue.pop()
        if name in ancestors:
            continue
        ancestors.add(name)
        parent = index.get(name)
        if parent is not None:
            queue.extend(_base_names(parent))
    return ancestors


def _defined_names(node: "ast.ClassDef") -> "set[str]":
    return {
        child.name
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_shared_store_classes_carry_the_collapsed_methods() -> None:
    """The guard's own footing. A rename that empties the lists above would
    make every check below pass over nothing."""
    from src.windows.Store.AssetPage import StoreAssetPage, StoreAssetPreview
    from src.windows.Store.Preview import StorePreview

    missing_page = COLLAPSED_PAGE_METHODS - set(vars(StoreAssetPage))
    assert not missing_page, (
        f"StoreAssetPage no longer defines {sorted(missing_page)}. Either the "
        "method moved, and COLLAPSED_PAGE_METHODS names its new spelling, or "
        "the collapse came undone."
    )
    # check_required_version sits one level up, on StorePreview, which is the
    # spot the first half of this scenario pins.
    shared_preview_names = set(vars(StoreAssetPreview)) | set(vars(StorePreview))
    missing_preview = COLLAPSED_PREVIEW_METHODS - shared_preview_names
    assert not missing_preview, (
        f"neither StoreAssetPreview nor StorePreview defines "
        f"{sorted(missing_preview)}. Either the method moved, and "
        "COLLAPSED_PREVIEW_METHODS names its new spelling, or the collapse "
        "came undone."
    )


def test_no_store_tab_regrows_a_per_family_copy() -> None:
    """A subclass of the shared tab or card may not redefine a collapsed
    method. Four copies of one method is where the version gate drifted."""
    seen_pages: set[str] = set()
    seen_previews: set[str] = set()
    offences: list[str] = []

    defs = _class_defs()
    index = _class_index(defs)

    for path, node in defs:
        # Ancestry, so a copy one level below the shared class is governed
        # too. The direct bases stay in their own name for the reach-past
        # check below, which is about what a class inherits from first.
        bases = _ancestor_names(node, index)
        direct_bases = _base_names(node)
        defined = _defined_names(node)

        if "StoreAssetPage" in bases:
            seen_pages.add(node.name)
            allowed = ALLOWED_PAGE_OVERRIDES.get(node.name, frozenset())
            regrown = sorted((defined & COLLAPSED_PAGE_METHODS) - allowed)
            if regrown:
                offences.append(
                    f"{path.name}: {node.name} redefines {regrown}, which "
                    "StoreAssetPage already carries for every asset class")

        if "StoreAssetPreview" in bases:
            seen_previews.add(node.name)
            allowed = ALLOWED_PREVIEW_OVERRIDES.get(node.name, frozenset())
            regrown = sorted((defined & COLLAPSED_PREVIEW_METHODS) - allowed)
            if regrown:
                offences.append(
                    f"{path.name}: {node.name} redefines {regrown}, which "
                    "StoreAssetPreview already carries for every asset class")

        for base, expected in EXPECTED_BASE_SUBCLASSES.items():
            if base in direct_bases and node.name not in expected:
                offences.append(
                    f"{path.name}: {node.name} subclasses {base} directly. "
                    f"Only {sorted(expected)} may, and a new asset class is a "
                    "descriptor row, not a page of its own")

    assert not offences, "per-family store copies came back:\n  " + "\n  ".join(offences)

    assert seen_pages == EXPECTED_PAGE_SUBCLASSES, (
        f"store tab subclasses changed: {sorted(seen_pages)} against the "
        f"expected {sorted(EXPECTED_PAGE_SUBCLASSES)}. An asset class that "
        "needs a page of its own is a deliberate edit to this list."
    )
    assert seen_previews == EXPECTED_PREVIEW_SUBCLASSES, (
        f"store card subclasses changed: {sorted(seen_previews)} against the "
        f"expected {sorted(EXPECTED_PREVIEW_SUBCLASSES)}."
    )


def test_every_asset_class_reaches_the_store_through_a_descriptor_row() -> None:
    """The tabs the store window builds come from the descriptor table, so a
    new asset class is a row and not a fourth copy of the page."""
    from src.backend.Store.asset_types import ASSET_TYPES

    for descriptor in ASSET_TYPES:
        for field in ("badge_key_prefix", "search_placeholder_key",
                      "preview_cls_name"):
            value = getattr(descriptor, field)
            assert isinstance(value, str) and value, (
                f"{descriptor.display_name} descriptor carries no {field}; the "
                "shared store tab reads that name at build time"
            )


def test_uninstall_row_matches_the_card_that_reads_it() -> None:
    """A row names a record-taking uninstall, or the card overrides it.

    The shared uninstall passes the record. A row that named a method taking
    something else, as the plugin one does by naming an id, would call it with
    the wrong argument the moment its card stopped overriding uninstall.
    """
    import inspect

    from src.backend.Store.StoreBackend import StoreBackend
    from src.backend.Store.asset_types import ASSET_TYPES
    from src.windows.Store.AssetPage import StoreAssetPage, StoreAssetPreview
    from src.windows.Store.Plugins.PluginPage import PluginPage

    # The page class that carries each row, which is the module its card name
    # resolves in. Building one with __new__ runs the real resolution without
    # a GTK widget.
    page_cls_for = {True: PluginPage, False: StoreAssetPage}

    for descriptor in ASSET_TYPES:
        page = page_cls_for[descriptor.is_plugin].__new__(page_cls_for[descriptor.is_plugin])
        page.descriptor = descriptor
        card_cls = page.preview_cls()

        if descriptor.uninstall_attr is None:
            assert card_cls.uninstall is not StoreAssetPreview.uninstall, (
                f"the {descriptor.display_name} row names no record-taking "
                f"uninstall, so {card_cls.__name__} must define one of its "
                "own. The shared uninstall refuses this row and raises."
            )
            continue

        assert card_cls.uninstall is StoreAssetPreview.uninstall, (
            f"{card_cls.__name__} overrides uninstall although its row names "
            f"{descriptor.uninstall_attr}. One of the two is dead."
        )
        method = getattr(StoreBackend, descriptor.uninstall_attr)
        # None stands in for self. The bind proves the named method takes the
        # record positionally, which is how the shared uninstall calls it.
        inspect.signature(method).bind(None, descriptor.data_cls())


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_version_gate")
    test_helper_gate_semantics()
    test_verdict_matches_runtime_gate_on_suffixed_versions()
    test_preview_delegates_to_helper()
    test_duplicate_copies_are_gone()
    test_shared_store_classes_carry_the_collapsed_methods()
    test_no_store_tab_regrows_a_per_family_copy()
    test_every_asset_class_reaches_the_store_through_a_descriptor_row()
    test_uninstall_row_matches_the_card_that_reads_it()
    print("scenario_store_version_gate: PASS")


if __name__ == "__main__":
    main()
