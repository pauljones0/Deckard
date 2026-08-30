"""
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

Shared plumbing for the pack and asset chooser pages of the AssetManager.

Icon packs, wallpaper packs and SD+ bar wallpaper packs each need a page to
pick a pack and a page to pick an asset from that pack. Without these bases
the six classes hold the same body, and they differ only in the widget classes
they build and the pack manager they read.

This module holds the pages, the widgets they build and the stack that holds
the pair. GenericPackChooserPage is the pack grid, and it drills into the leaf
page. GenericAssetChooserPage is the recycler grid of the assets of a pack,
with the shared search and sort. Both pages search: the pack grid filters its
cards on the pack name, the leaf page filters and ranks the assets of one
pack. Both score through asset_search. GenericPackFlowBox and
GenericPackPreview are the grid shell and the card of the first;
GenericAssetFlowBox and GenericAssetPreview are those of the second.
GenericPackChooserStack holds one page of each and names the subsystem.

A query typed into the pack grid searches across every pack. The pack grid
gathers the assets of all of its packs on a worker thread, ranks the merged
names with one ranker, and shows the matches in the leaf page, where each card
names the pack its asset came from. The leaf page then owns the query: a
changed one runs the search again, and an emptied one goes back to the pack
grid. A search that a page turn or a hidden window overtook renders nothing,
which is what the generation each pass carries decides.

A subsystem therefore adds no widget class of its own. It names the classes it
builds with, the packs it reads, and the title of its leaf page.

Both build the same way. The build worker thread gathers the data, which is
the pack discovery and the disk I/O, and one run_on_main callback constructs
and attaches every widget. GTK4 is main-thread-only, and a widget tree built
on the worker is the off-main GTK crash class.
tests/scenario_asset_chooser_offmain.py catches it.

The search helpers at the top use no GTK and no self, so a headless test pins
them, and the ladder they score with is headless on its own.
"""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk

import os
import threading

from loguru import logger as log

# Import own modules
from GtkHelper.GtkHelper import run_on_main
from src.backend.PackManagement.pack_family import PackAsset
from src.windows.AssetManager import asset_search
from src.windows.AssetManager.ChooserPage import ChooserPage
from src.windows.AssetManager.DynamicFlowBox import DynamicFlowBox
from src.windows.AssetManager.Preview import _PIXBUF_UNSET, Preview, _PixbufUnset

import globals as gl

from typing import cast, Generic, Protocol, TYPE_CHECKING, TypeVar, Any, override
if TYPE_CHECKING:
    from pathlib import Path

    from gi.repository import GdkPixbuf

    from src.windows.AssetManager.AssetManager import AssetManager


# Shared pack-grid child name for stack setup, search return, and session reset
PACK_CHOOSER_CHILD_NAME = "pack-chooser"


class _PackLike(Protocol):
    """What the chooser bases and the pack card read off a pack of any
    subsystem."""

    name: str

    def get_thumbnail_path(self) -> "Path | None": ...

    def get_pack_attribution(self) -> dict[str, Any]: ...


# One pack subsystem binds all four: its pack class, the stack that holds its
# two pages, its pooled preview widget and its asset class.
PackT = TypeVar("PackT", bound=_PackLike)
StackT = TypeVar("StackT", bound=Gtk.Stack)
PreviewT = TypeVar("PreviewT", bound=Gtk.FlowBoxChild)
# The asset classes all descend from PackAsset, which is what carries the file
# path and the attribution the shared card reads.
AssetT = TypeVar("AssetT", bound=PackAsset)
# The leaf page of one subsystem, which its stack holds and its pack grid
# drills into.
LeafT = TypeVar("LeafT", bound="GenericAssetChooserPage[Any, Any, Any, Any]")


class _PackPreviewLike(Protocol[PackT]):
    """A pack-grid preview carries the pack it shows."""

    pack: PackT


def asset_display_name(item: Any, attr: str = "path") -> str:
    """The displayed file name without its directory or extension."""
    return cast(str, os.path.splitext(os.path.basename(getattr(item, attr)))[0])


def asset_matches_search(item: Any, search: str, attr: str = "path") -> bool:
    """Whether the display name matches every query token."""
    return asset_search.matches(asset_display_name(item, attr), search)


def compare_assets(item1: Any, item2: Any, search: str, attr: str = "path") -> int:
    """Order assets alphabetically for an empty query, else by search relevance."""
    name1 = asset_display_name(item1, attr)
    name2 = asset_display_name(item2, attr)

    if asset_search.is_empty_query(search):
        if name1 < name2:
            return -1
        if name1 > name2:
            return 1
        return 0

    return asset_search.compare(name1, name2, search)


class GenericPackFlowBox(Gtk.Box):
    """The Gtk.FlowBox shell for pack cards and activation."""

    def __init__(self, pack_chooser: object, *args: Any, **kwargs: Any) -> None:
        # The chooser passes itself first and positionally. Nothing in this
        # widget reads it back, so it is not stored.
        super().__init__(*args, **kwargs)
        self.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.set_hexpand(True)

        self.build()

    def build(self) -> None:
        self.flow_box = Gtk.FlowBox(hexpand=True, orientation=Gtk.Orientation.HORIZONTAL,
                                    selection_mode=Gtk.SelectionMode.NONE)
        self.append(self.flow_box)


class GenericPackPreview(Preview, Generic[PackT]):
    """One pack card in a pack grid."""

    def __init__(self, pack_chooser: "GenericPackChooserPage[PackT, Any]", pack: PackT,
                 pixbuf: "GdkPixbuf.Pixbuf | None | _PixbufUnset" = _PIXBUF_UNSET) -> None:
        # Decode here only when the build worker did not supply a pixbuf
        super().__init__(
            image_path=pack.get_thumbnail_path() if pixbuf is _PIXBUF_UNSET else None,
            text=pack.name,
            pixbuf=pixbuf
        )
        self.pack = pack
        self.pack_chooser = pack_chooser

    @override
    def on_click_info(self, button: Gtk.Button) -> None:
        attribution = self.pack.get_pack_attribution()
        self.pack_chooser.asset_manager.show_info(
            internal_path=None,
            licence_name=attribution.get("license"),
            license_url=attribution.get("license-url"),
            author=attribution.get("copyright"),
            license_comment=attribution.get("comment")
        )


class GenericAssetFlowBox(DynamicFlowBox[PreviewT, AssetT]):
    """The recycling grid of an asset chooser page."""

    def __init__(self, base_class: "type[PreviewT]", asset_chooser: object,
                 *args: Any, **kwargs: Any) -> None:
        # The chooser passes itself second and positionally. Nothing in this
        # widget reads it back, so it is not stored.
        super().__init__(base_class, *args, **kwargs)
        self.set_hexpand(True)


class GenericAssetPreview(Preview, Generic[AssetT]):
    """A pooled asset card that set_asset rebinds on each recycle."""

    def __init__(self) -> None:
        super().__init__()

        self.asset: AssetT = None  # ty: ignore[invalid-assignment]  # late-init: set_asset

    @override
    def on_click_info(self, button: Gtk.Button) -> None:
        # A recycled child can outlive its window, so log instead of raising
        asset_manager = gl.asset_manager
        if asset_manager is None:
            log.error("The asset manager window is gone; cannot show asset info")
            return
        # One lookup for the four fields below. get_attribution resolves the
        # asset's key against the pack's attribution entries on every call.
        attribution = self.asset.get_attribution()
        asset_manager.show_info(
            internal_path=self.asset.path,
            licence_name=attribution.get("license"),
            license_url=attribution.get("license-url"),
            author=attribution.get("copyright"),
            license_comment=attribution.get("comment"),
            original_url=attribution.get("original-url")
        )

    def set_asset(self, asset: AssetT) -> None:
        self.asset = asset

        # Bind synchronously in _apply_range so no visible frame shows stale data
        self.set_text(os.path.splitext(os.path.basename(asset.path))[0])
        self.set_image(asset.path)


class _ChooserBuildPage(ChooserPage):
    """Recover chooser builds when main-loop marshalling exceeds its 30 s bound."""

    build_finished = False
    build_failed = False
    _build_running = False

    def start_build(self) -> bool:
        """Start one build worker; main-thread callers serialize the flag."""
        if self._build_running:
            return False
        self._build_running = True
        threading.Thread(target=self._run_build,
                         name=f"{type(self).__name__}.build").start()
        return True

    def build(self) -> None:
        """Gather data off-main and construct widgets through run_on_main."""
        raise NotImplementedError

    def _run_build(self) -> None:
        try:
            self.build()
        finally:
            self._build_running = False

    def retry_build(self) -> bool:
        """Restart only a failed build when the window reopens."""
        if not self.build_failed:
            return False
        self.build_failed = False
        self.set_loading(True)
        return self.start_build()

    def _handle_build_failure(self, error: BaseException) -> None:
        log.opt(exception=error).error(
            f"{type(self).__name__}: the main loop did not service this page's "
            f"build; showing an error -- it rebuilds when the window is reopened")
        self.build_finished = False
        self.build_failed = True
        self._reset_build_state()
        # Queue the idle and do not wait. run_on_main is the call that failed,
        # and a second wait parks this worker for another full timeout.
        GLib.idle_add(self._show_build_error)

    def _show_build_error(self) -> bool:
        """Replace the loading page with a stopped error panel."""
        self.spinner.stop()
        self.loading_label.set_label(gl.lm.get("error"))
        self.set_visible_child_name("loading")
        return False

    def _reset_build_state(self) -> None:
        """Drop state that a failed build cannot consume."""


class GenericPackChooserPage(_ChooserBuildPage, Generic[PackT, StackT]):
    """A pack grid whose subclass supplies widgets, leaf name, and pack source."""

    # Gtk.Box subclass that owns a flow_box. Its constructor takes a chooser
    # and keyword properties.
    PACK_FLOW_BOX_CLASS: type = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.PackChooser
    # Preview subclass with a pack attribute. Its constructor takes a chooser
    # and a pack.
    PACK_PREVIEW_CLASS: type = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.PackChooser
    # Name of the stack child holding this type's asset chooser.
    LEAF_CHILD_NAME: str = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.PackChooser
    # Pack previews constructed per main-loop callback.
    PACK_APPEND_BATCH: int = 10

    # _build_ui binds this on the main loop, so a reader must accept None.
    # A marshal that timed out never binds it. See _handle_build_failure.
    pack_flow = None

    # One gather runs at a time; a new request replaces the pending request
    # Class defaults exist before ChooserPage can emit during base construction
    _search_lock = threading.Lock()
    _search_pending: "tuple[str, ChooserPage, int] | None" = None
    _search_running = False

    def __init__(self, stack: StackT, asset_manager: "AssetManager") -> None:
        super().__init__()
        self.asset_manager = asset_manager
        self.stack = stack

        self.build_finished = False

        self.start_build()

    @log.catch
    @override
    def build(self) -> None:
        self.build_finished = False

        # Keep pack discovery and thumbnail file I/O on this worker
        packs = list(self.get_packs().values())
        thumbnails = [Preview.decode_pixbuf(self.get_pack_thumbnail_path(pack))
                      for pack in packs]

        try:
            # Widgets are GTK, so they are built on the main loop.
            run_on_main(self._build_ui)

            # Yield between preview batches so a large pack set does not stall GTK
            for start in range(0, len(packs), self.PACK_APPEND_BATCH):
                stop = start + self.PACK_APPEND_BATCH
                run_on_main(self._append_packs, list(zip(packs[start:stop],
                                                         thumbnails[start:stop])))
        except Exception as e:
            self._handle_build_failure(e)
            return

        self.set_loading(False)

        self.build_finished = True
        self.on_build_finished()

    def _build_ui(self) -> None:
        """Runs on the main loop only."""
        self.type_box.set_visible(False)

        self.pack_flow = self.PACK_FLOW_BOX_CLASS(self, orientation=Gtk.Orientation.HORIZONTAL,
                                                  hexpand=True)
        self.scrolled_box.prepend(self.pack_flow)

        self.pack_flow.flow_box.connect("child-activated", self.on_child_activated)
        # GTK applies the current search to existing and later batched cards
        self.pack_flow.flow_box.set_filter_func(self.filter_pack_child)

    def _append_packs(self, batch: "list[tuple[PackT, GdkPixbuf.Pixbuf | None]]") -> None:
        """Runs on the main loop only, over one batch of pack and pixbuf pairs."""
        pack_flow = self.pack_flow
        if pack_flow is None:
            # The marshal of _build_ui never landed, which
            # _handle_build_failure covers, so no grid takes an append.
            return
        flow_box = pack_flow.flow_box
        for pack, pixbuf in batch:
            preview = self.PACK_PREVIEW_CLASS(self, pack, pixbuf=pixbuf)
            flow_box.append(preview)

    def reload(self) -> None:
        """Rebuild the pack grid from disk on the main loop.

        Keep an active build; if it listed before an import rename, the next open catches up.
        """
        if self._build_running:
            return
        pack_flow = self.pack_flow
        if pack_flow is not None:
            self.scrolled_box.remove(pack_flow)
            self.pack_flow = None
        self.build_finished = False
        self.build_failed = False
        self.set_loading(True)
        self.start_build()

    def get_pack_thumbnail_path(self, pack: PackT) -> "Path | None":
        """Where the pack's thumbnail lives. Called on the build worker."""
        return pack.get_thumbnail_path()

    def on_child_activated(self, flow_box: Gtk.FlowBox, child: "_PackPreviewLike[PackT]") -> None:
        # Load the pack's assets, drill into the asset chooser, offer the way back.
        self.get_leaf_chooser().load_for_pack(child.pack)
        self.stack.set_visible_child_name(self.LEAF_CHILD_NAME)
        self.asset_manager.back_button.set_visible(True)

    def filter_pack_child(self, child: Gtk.FlowBoxChild) -> bool:
        """Whether one pack card matches the current main-thread query."""
        pack = getattr(child, "pack", None)
        if pack is None:
            # Not a pack card. Hiding a child this page does not know about
            # would be the wrong answer to a widget it never built.
            return True
        return asset_search.matches(pack.name, self.search_entry.get_text())

    @override
    def apply_search(self, query: str) -> None:
        pack_flow = self.pack_flow
        if pack_flow is None:
            # The marshal of _build_ui never landed, so no grid holds cards.
            return
        # The predicate reads the entry itself. This only tells GTK that its
        # answer changed.
        pack_flow.flow_box.invalidate_filter()

        if asset_search.is_empty_query(query):
            # The pack grid is the whole answer to an empty query, and it is
            # on screen now.
            self.search_rendered(query)
            return
        # Search every pack for assets; record rendering only when results land
        self.search_across_packs(query, self, self._search_generation)

    @override
    def on_shown(self) -> None:
        """Clear the cross-pack query before the main-thread catch-up pass."""
        if self.search_entry.get_text():
            self.search_entry.set_text("")

    def search_across_packs(self, query: str, requester: ChooserPage,
                            generation: int) -> None:
        """Gather and rank matching assets on one worker for the requester's generation.

        A new query replaces the pending one; page turns and hidden pages stale the result.
        """
        with self._search_lock:
            self._search_pending = (query, requester, generation)
            if self._search_running:
                # The worker takes this up when its gather ends.
                return
            self._search_running = True
        self._start_search_worker()

    def _start_search_worker(self) -> None:
        """Run the gather worker. The caller has claimed the flag for it."""
        threading.Thread(target=self._run_pack_search, daemon=True,
                         name=f"{type(self).__name__}.search").start()

    def _run_pack_search(self) -> None:
        """Serve the newest pending query while reading packs off-main."""
        try:
            while True:
                with self._search_lock:
                    request = self._search_pending
                    self._search_pending = None
                    if request is None:
                        # Clear the worker flag in the same hold that finds no request
                        self._search_running = False
                        return
                query, requester, generation = request
                try:
                    assets = self.collect_matching_assets(query, requester, generation)
                except Exception as error:
                    # Leave failed gathers unrendered so remap or typing retries
                    log.opt(exception=error).error(
                        f"{type(self).__name__}: the search across packs failed for "
                        f"{query!r}; the page keeps what it shows and searches "
                        f"again on the next keystroke or when it is shown")
                    continue
                if not requester.search_is_current(generation):
                    # Queue nothing when a newer pass or hidden page stales this result
                    continue
                GLib.idle_add(self._show_matching_assets, query, requester,
                              generation, assets)
        finally:
            self._release_search_worker()

    def _release_search_worker(self) -> None:
        """Release or transfer worker ownership after any exit.

        Preserve the original exception while restarting a stranded request.
        """
        with self._search_lock:
            if not self._search_running:
                # The clean exit cleared it, and another worker may already
                # hold the flag.
                return
            # Transfer the flag directly to a request stranded during worker exit
            stranded = self._search_pending is not None
            self._search_running = stranded
        if stranded:
            self._start_search_worker()

    def collect_matching_assets(self, query: str, requester: ChooserPage,
                                generation: int) -> "list[Any]":
        """Collect all matching assets off-main with one deterministic ranker."""
        if not requester.search_is_current(generation):
            # Reject stale work before the expensive pack-folder discovery
            return []
        ranker = asset_search.QueryRanker(query)
        leaf = self.get_leaf_chooser()
        path_attr = leaf.ASSET_PATH_ATTR
        matched: list[Any] = []
        for pack in self.get_packs().values():
            if not requester.search_is_current(generation):
                # Leave a scan of a whole installation as soon as its answer
                # is one that nothing will render.
                return []
            for asset in leaf.get_assets(pack):
                if ranker.matches(asset_display_name(asset, path_attr)):
                    matched.append(asset)
        matched.sort(key=lambda asset: ranker.rank_key(
            asset_display_name(asset, path_attr)))
        return matched

    def _show_matching_assets(self, query: str, requester: ChooserPage,
                              generation: int, assets: "list[Any]") -> bool:
        """Runs on the main loop only."""
        if not requester.search_is_current(generation):
            return False
        leaf = self.get_leaf_chooser()
        on_pack_grid = self.stack.get_visible_child_name() == PACK_CHOOSER_CHILD_NAME
        if not on_pack_grid and not leaf.shows_pack_search:
            # Do not replace a drilled-in pack grid with obsolete cross-pack results
            return False
        leaf.load_search_results(self, assets, query)
        requester.search_rendered(query)
        if on_pack_grid:
            # Switch only for the first results to avoid restarting each transition
            self.stack.set_visible_child_name(self.LEAF_CHILD_NAME)
            self.asset_manager.back_button.set_visible(True)
            # Transfer focus after the results page maps
            GLib.idle_add(leaf.focus_search_entry)
        return False

    def get_packs(self) -> dict[str, PackT]:
        """{name: pack} for this asset type. Called on the build worker."""
        raise NotImplementedError

    def get_leaf_chooser(self) -> "GenericAssetChooserPage[PackT, Any, Any, Any]":
        """The sibling page that shows one pack's assets."""
        raise NotImplementedError

    def on_build_finished(self) -> None:
        """Called after build(), off the main thread. Only the icon stack
        gates deferred work on it."""


class GenericAssetChooserPage(_ChooserBuildPage, Generic[PackT, AssetT, PreviewT, StackT]):
    """A recycling asset grid with shared search and sort."""

    # DynamicFlowBox subclass. Its constructor takes a preview class and a
    # chooser.
    FLOW_BOX_CLASS: type = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.Icons.IconChooser
    # Preview subclass; ctor takes no arguments (the flow box pools them).
    PREVIEW_CLASS: type = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.Icons.IconChooser
    # Attribute holding an asset's file path.
    ASSET_PATH_ATTR: str = "path"

    # Class defaults exist before ChooserPage can emit during base construction
    asset_flow: "DynamicFlowBox[PreviewT, AssetT] | None" = None
    _pending_pack: "PackT | None" = None
    # Cross-pack results retain their source because a changed query needs a new gather
    _pack_search_source: "GenericPackChooserPage[PackT, Any] | None" = None
    _pack_search_query: str = ""
    _pending_results: "tuple[GenericPackChooserPage[PackT, Any], list[AssetT], str] | None" = None
    # The line that stands in for an empty grid. _build_ui binds it on the
    # main loop, so every reader accepts None.
    empty_label: "Gtk.Label | None" = None

    def __init__(self, stack: StackT, asset_manager: "AssetManager") -> None:
        super().__init__()
        self.asset_manager = asset_manager
        self.stack = stack

        # None until select_asset picks one. preview_factory only compares
        # it, so None matches nothing.
        self.selected_path: str | None = None

        # _build_ui binds this on the main loop, after this constructor
        # returns, so every reader accepts None.
        self.asset_flow = None
        self._pending_pack = None
        self._pending_results = None
        self._pack_search_source = None
        self._pack_search_query = ""

        self.build_finished = False

        self.start_build()

    @log.catch
    @override
    def build(self) -> None:
        self.build_finished = False
        self.set_loading(True)

        try:
            # The flow box builds a page's worth of previews in its
            # constructor, so the whole construction runs on the main loop.
            run_on_main(self._build_ui)
        except Exception as e:
            self._handle_build_failure(e)
            return

        self.set_loading(False)

        self.build_finished = True
        self.on_build_finished()

    def _build_ui(self) -> None:
        """Runs on the main loop only."""
        self.type_box.set_visible(False)

        self.asset_flow = self.FLOW_BOX_CLASS(self.PREVIEW_CLASS, self)
        self.asset_flow.set_factory(self.preview_factory)
        self.asset_flow.set_filter_func(self.filter_func)
        self.asset_flow.set_sort_func(self.sort_func)
        # The flow box brings its own ScrolledWindow and pagination; the
        # page's default scroller must go or it competes for the height.
        self.main_box.remove(self.scrolled_window)
        # Above the grid, because an empty grid is blank space and a line
        # under blank space reads as a footer rather than as the answer.
        self.empty_label = Gtk.Label(label=gl.lm.get("asset-chooser.search.no-match"),
                                     visible=False, margin_top=15, margin_bottom=15,
                                     css_classes=["dim-label"])
        self.main_box.append(self.empty_label)
        self.main_box.append(self.asset_flow)

        # Connect flow box select signal
        self.asset_flow.flow_box.connect("child-activated", self.on_child_activated)

        # Consume the one pack or search request that arrived before UI construction
        if self._pending_pack is not None:
            pack, self._pending_pack = self._pending_pack, None
            self.load_for_pack(pack)
        elif self._pending_results is not None:
            pending, self._pending_results = self._pending_results, None
            self.load_search_results(*pending)

    @property
    def shows_pack_search(self) -> bool:
        """Whether this grid may receive cross-pack results instead of one pack."""
        return self._pack_search_source is not None

    def show_empty_notice(self, visible: bool) -> None:
        """Show whether a cross-pack search returned no results."""
        if self.empty_label is not None:
            self.empty_label.set_visible(visible)

    def load_for_pack(self, pack: PackT) -> None:
        if self.shows_pack_search:
            # Clear the cross-pack query before loading one drilled-in pack
            self._pack_search_source = None
            self._pack_search_query = ""
            if self.search_entry.get_text():
                self.search_entry.set_text("")
        self.show_empty_notice(False)
        if self.asset_flow is None:
            # The build still waits on the main loop. Keep the request
            # instead of dropping it or raising AttributeError.
            self._pending_results = None
            self._pending_pack = pack
            return
        self.asset_flow.set_item_list(self.get_assets(pack))
        # The recycler owns its page size (it sized its preview pool to it).
        self.asset_flow.show_range(0, self.asset_flow.N_ITEMS_PER_PAGE)

    def load_search_results(self, pack_chooser: "GenericPackChooserPage[PackT, Any]",
                            assets: "list[AssetT]", query: str) -> None:
        """Show cross-pack results and retain their source for changed queries."""
        if self.asset_flow is None:
            # The build still waits on the main loop, as in load_for_pack.
            self._pending_pack = None
            self._pending_results = (pack_chooser, assets, query)
            return
        self._pack_search_source = pack_chooser
        self._pack_search_query = query
        if self.search_entry.get_text() != query:
            self.search_entry.set_text(query)
        self.show_empty_notice(not assets)
        self.asset_flow.set_item_list(assets)
        self.asset_flow.show_range(0, self.asset_flow.N_ITEMS_PER_PAGE)

    def leave_search_results(self) -> None:
        """Drop an empty cross-pack search and return to the pack grid."""
        source = self._pack_search_source
        self._pack_search_source = None
        self._pack_search_query = ""
        if self.search_entry.get_text():
            # Clear separator-only text when its normalized query is empty
            self.search_entry.set_text("")
        self.show_empty_notice(False)
        if self.asset_flow is not None:
            self.asset_flow.set_item_list([])
        self.stack.set_visible_child_name(PACK_CHOOSER_CHILD_NAME)
        self.asset_manager.back_button.set_visible(False)
        # The page turn is what this pass renders, so the entry and what the
        # page believes it shows agree again.
        self.search_rendered(self.search_entry.get_text())
        if source is not None:
            # The typing follows the query back to the grid that owns it.
            GLib.idle_add(source.focus_search_entry)

    def pack_label_for(self, asset: AssetT) -> str | None:
        """The pack subtitle for cross-pack results, else None."""
        if not self.shows_pack_search:
            return None
        return cast(str, asset.pack.name)

    def select_asset(self, path: str) -> None:
        """Select the asset at path once the grid renders it."""
        self.selected_path = path

    @override
    def _reset_build_state(self) -> None:
        # No grid renders either request and no build consumes them, so a kept
        # one strands without a word.
        self._pending_pack = None
        self._pending_results = None

    def on_child_activated(self, flow_box: Gtk.FlowBox, child: PreviewT) -> None:
        asset = self.get_child_asset(child)
        self.asset_manager.deliver_selection(getattr(asset, self.ASSET_PATH_ATTR))

    def preview_factory(self, preview: PreviewT, asset: AssetT) -> None:
        # Called from DynamicFlowBox._apply_range's main-loop callback.
        self.bind_preview(preview, asset)
        if isinstance(preview, Preview):
            # Always replace a recycled card's cross-pack subtitle
            preview.set_subtitle(self.pack_label_for(asset))
        if self.selected_path == getattr(asset, self.ASSET_PATH_ATTR):
            # The recycler that calls this is the flow box itself, so it
            # exists; the class default keeps the annotation optional.
            if self.asset_flow is not None:
                self.asset_flow.flow_box.select_child(preview)

    def filter_func(self, item: AssetT) -> bool:
        return asset_matches_search(item, self.search_entry.get_text(),
                                    self.ASSET_PATH_ATTR)

    def sort_func(self, item1: AssetT, item2: AssetT) -> int:
        return compare_assets(item1, item2, self.search_entry.get_text(),
                              self.ASSET_PATH_ATTR)

    @override
    def apply_search(self, query: str) -> None:
        if self.asset_flow is None:
            # Nothing rendered yet (build still queued on the main loop);
            # the first render already reads the current search text.
            return
        source = self._pack_search_source
        if source is not None and query != self._pack_search_query:
            # A changed cross-pack query needs a new gather, not a narrowing filter
            if asset_search.is_empty_query(query):
                self.leave_search_results()
                return
            # Record nothing until results land so dropped gathers remain stale
            source.search_across_packs(query, self, self._search_generation)
            return
        # Back to the first page: the grid it shows now holds the matches of
        # the query the user has replaced.
        self.asset_flow.show_range(0, self.asset_flow.N_ITEMS_PER_PAGE)
        self.search_rendered(query)

    def get_assets(self, pack: PackT) -> list[AssetT]:
        """The assets of the pack, in the order the grid receives them."""
        raise NotImplementedError

    def bind_preview(self, preview: PreviewT, asset: AssetT) -> None:
        """Show asset in the recycled preview."""
        raise NotImplementedError

    def get_child_asset(self, child: PreviewT) -> AssetT:
        """The asset that an activated flow-box child shows."""
        raise NotImplementedError

    def on_build_finished(self) -> None:
        """Called after build(), off the main thread. Only the icon stack
        gates deferred work on it."""


class GenericPackChooserStack(Gtk.Stack, Generic[LeafT]):
    """The pack and asset pages of one subsystem with one shared leaf name."""

    PACK_CHOOSER_CLASS: "type[GenericPackChooserPage[Any, Any]]" = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.Stack
    LEAF_CHOOSER_CLASS: "type[LeafT]" = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.Stack
    LEAF_CHILD_TITLE: str = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.Stack

    # prepare must create callback state before either page starts its build worker
    _prepared = False

    @property
    def leaf_child_name(self) -> str:
        """The pack page's canonical child name for this asset grid."""
        return self.PACK_CHOOSER_CLASS.LEAF_CHILD_NAME

    def __init__(self, asset_manager: "AssetManager", *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.asset_manager = asset_manager

        self.prepare()
        self._prepared = True
        self.build()

    def prepare(self) -> None:
        """Create shared state before page constructors start their workers."""

    def build(self) -> None:
        if not self._prepared:
            raise RuntimeError(
                f"{type(self).__name__}.build() ran before prepare(). Each page "
                "starts a build worker in its constructor, and that worker "
                "reaches state prepare() creates.")
        self.pack_chooser = self.PACK_CHOOSER_CLASS(self, self.asset_manager)
        self.add_titled(self.pack_chooser, PACK_CHOOSER_CHILD_NAME, "Chooser")

        self.leaf_chooser = self.LEAF_CHOOSER_CLASS(self, self.asset_manager)
        self.add_titled(self.leaf_chooser, self.PACK_CHOOSER_CLASS.LEAF_CHILD_NAME,
                        self.LEAF_CHILD_TITLE)
