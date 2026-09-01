"""
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

One store tab, and one store card, for every asset class.

The plugin, icon-pack, wallpaper and SD+ bar wallpaper tabs hold the same
body. They differ in the backend methods they fetch and install through, in
the locale keys they label with, and in the card class they build. An
AssetTypeDescriptor names each of those, so the tab and the card exist once
here and read the names off the row.

The plugin tab subclasses both classes, because its catalog rows and its
install button carry behaviour the data-only classes do not.

Nothing here captures a backend method or a card class at import time. Both
are names the descriptor carries, and both resolve when the page runs, so a
test that replaces either sees its replacement used.
"""
# Import gtk modules
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

# Import python modules
import sys

from loguru import logger as log

# Import own modules
from src.backend.Store import dependencies
from src.backend.Store.asset_types import AssetTypeDescriptor
from src.backend.Store.store_result import Err, StoreResult
from src.windows.Store.Preview import StorePreview
from src.windows.Store.StoreData import StoreAssetData
from src.windows.Store.StorePage import StorePage

from typing import TYPE_CHECKING, Any, cast, override
if TYPE_CHECKING:
    from src.windows.Store.Store import Store

# Import globals
import globals as gl


class StoreAssetPage(StorePage):
    """One tab of the store window, for the asset class its descriptor names."""

    # Class-level so each subclass permanently names its asset descriptor
    descriptor: AssetTypeDescriptor

    def __init__(self, store: "Store", descriptor: "AssetTypeDescriptor | None" = None) -> None:
        super().__init__(store=store)
        if descriptor is not None:
            self.descriptor = descriptor

        # Label both independent compatible and incompatible search entries
        placeholder = gl.lm.get(self.descriptor.search_placeholder_key)
        self.compatible_section.search_entry.set_placeholder_text(placeholder)
        self.incompatible_section.search_entry.set_placeholder_text(placeholder)

    # Carry no @log.catch here. StorePage._load_guarded must see the failure,
    # so it can show the error page and arm the tab for a retry.
    @override
    def load(self) -> None:
        self.set_loading()
        backend = self.store.backend
        if backend is None:
            # _load_guarded turns this into the error page and re-arms the
            # tab, the same as any other failure this fetch can raise.
            raise RuntimeError("the store backend is unavailable")
        # Resolve the descriptor's fetch method at call time
        result: StoreResult[list[StoreAssetData]] = getattr(backend, self.descriptor.get_all_attr)()
        if isinstance(result, Err):
            self.show_connection_error()
            return
        for asset in result.value:
            if self.skip_entry(asset):
                continue
            if asset.is_compatible:
                section = self.compatible_section
            else:
                section = self.incompatible_section
            self.append_preview_on_main(section, lambda asset=asset: self.build_preview(asset))

        self.set_loaded()

    def skip_entry(self, asset: StoreAssetData) -> bool:
        """Whether one catalog row stays off the grid. A data-only tab shows
        every row its backend returns."""
        return False

    def preview_cls(self) -> "type[StoreAssetPreview]":
        """Resolve the descriptor's card class from the page module at call time."""
        page_module = sys.modules[type(self).__module__]
        return cast("type[StoreAssetPreview]",
                    getattr(page_module, self.descriptor.preview_cls_name))

    def build_preview(self, asset: StoreAssetData) -> "StoreAssetPreview":
        """Build one card. append_preview_on_main runs this on the main loop."""
        return self.preview_cls()(page=self, asset_data=asset)


class StoreAssetPreview(StorePreview):
    """One card in a store tab, for the asset class its page names."""

    # Data-only incompatible cards use a red border; plugin cards override this
    shows_incompatible_border = True

    def __init__(self, page: StoreAssetPage, asset_data: StoreAssetData) -> None:
        super().__init__(store_page=page)
        self.asset_data = asset_data
        self.descriptor = page.descriptor

        self.set_author_label(asset_data.author)
        self.set_name_label(asset_data.asset_name)
        self.set_image(asset_data.image)
        self.set_url(asset_data.github)

        self.set_official(asset_data.official)
        self.set_verified(asset_data.verified)

        prefix = self.descriptor.badge_key_prefix
        self.warning_badge.set_tooltip(f"{prefix}.warning")
        self.official_badge.set_tooltip(f"{prefix}.official")
        self.verified_badge.set_tooltip(f"{prefix}.verified")

        if self.shows_incompatible_border:
            if not self.check_required_version(asset_data.minimum_app_version):
                self.main_button_box.add_css_class("red-border")
            else:
                self.main_button_box.remove_css_class("red-border")

        self.set_install_state(self.get_install_state_for(asset_data))

        description = asset_data.short_description
        if description in ["", "N/A", None]:
            description = asset_data.description
        self.set_description(description)

    @staticmethod
    def get_install_state_for(asset_data: StoreAssetData) -> int:
        """Return 0 absent, 1 at the catalog commit, or 2 when it differs."""
        if asset_data.local_sha is None:
            return 0
        if asset_data.local_sha == asset_data.commit_sha:
            return 1
        return 2

    @override
    def install(self) -> bool:
        """Install the consented dependency set on a worker and report real success.

        Refusal or failure preserves button state; partial installs remain and are reported.
        """
        backend = self.store.backend
        noun = self.descriptor.display_name
        asset_id = self.asset_data.asset_id
        if backend is None:
            log.error(f"Store backend unavailable; cannot install {asset_id}")
            self.notify_install_failure()
            return False
        from src.windows.Store.install_consent import make_dependency_consent
        report = dependencies.install_with_dependencies(
            backend, dependencies.CatalogItem(self.descriptor, self.asset_data),
            confirm_set=make_dependency_consent(self.store), **self._install_kwargs())
        if report.declined:
            # Nothing downloaded, so the button keeps the state it had.
            return False
        if not report.ok:
            log.error(f"Failed to install {noun} {asset_id}: {report.error!r}")
            # Use dependency detail when another item failed or any item installed
            failed_is_this_card = (report.failed is not None
                                   and report.failed.asset is self.asset_data)
            if report.installed or not failed_is_this_card:
                name = self.asset_data.asset_name or asset_id or noun
                failed_noun = dependencies.failure_noun(report, noun)
                gl.notify.error(dependencies.failure_message(report, name),
                                title=f"{failed_noun[:1].upper()}{failed_noun[1:]} install failed")
            else:
                self.notify_install_failure()
            # Leave the button in its previous state so the user can retry.
            return False
        GLib.idle_add(self.set_install_state, 1)
        return True

    def _install_kwargs(self) -> "dict[str, Any]":
        """Extra install arguments; only plugin subclasses add script consent."""
        return {}

    def notify_install_failure(self) -> None:
        noun = self.descriptor.display_name
        name = self.asset_data.asset_name or self.asset_data.asset_id
        gl.notify.error(f"The {noun} {name} could not be installed",
                        title=f"{noun[:1].upper()}{noun[1:]} install failed")

    @override
    def uninstall(self) -> None:
        uninstall_attr = self.descriptor.uninstall_attr
        if uninstall_attr is None:
            # A descriptor without record-based uninstall requires a preview override
            raise NotImplementedError(
                f"{self.descriptor.display_name} takes no record-passing "
                f"uninstall; {type(self).__name__} must define its own")
        backend = self.store.backend
        if backend is None:
            log.error("Store backend unavailable; cannot uninstall "
                      f"{self.asset_data.asset_id}")
            return
        getattr(backend, uninstall_attr)(self.asset_data)
        self.set_install_state(0)

    @override
    def update(self) -> None:
        self.install()

    @override
    def on_click_main(self, button: Gtk.Button) -> None:
        page = self.store_page
        page.set_info_visible(True)

        page.info_page.set_asset_name(self.asset_data.asset_name)
        page.info_page.set_description(self.asset_data.description)
        page.info_page.set_author(self.asset_data.author)
        page.info_page.set_version(self.asset_data.asset_version)

        page.info_page.set_license(self.asset_data.license)
        page.info_page.set_copyright(self.asset_data.copyright)
        page.info_page.set_original_url(self.asset_data.original_url)
        # Preserve the translation layer's distinct absent and empty results
        page.info_page.set_license_description(
            gl.lm.get_custom_translation(self.asset_data.license_descriptions))
