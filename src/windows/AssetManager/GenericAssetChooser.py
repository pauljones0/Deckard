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

# Import python modules
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

# Import globals
import globals as gl

# Import typing
from typing import cast, Generic, Protocol, TYPE_CHECKING, TypeVar, Any
if TYPE_CHECKING:
    from pathlib import Path

    from gi.repository import GdkPixbuf

    from src.windows.AssetManager.AssetManager import AssetManager


# The stack child that holds the pack grid. GenericPackChooserStack adds it
# under this name, the leaf page returns to it by this name when a search
# across the packs ends, and the window backs every stack out to it.
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
    """The string that the search matches on.

    It is the file name of the asset, without the directory and without the
    extension, which is what the preview label shows.
    """
    return cast(str, os.path.splitext(os.path.basename(getattr(item, attr)))[0])


def asset_matches_search(item: Any, search: str, attr: str = "path") -> bool:
    """Filter predicate for one asset.

    An empty query keeps everything. Any other query needs a display name that
    holds every word of the query. See asset_search for the ladder.
    """
    return asset_search.matches(asset_display_name(item, attr), search)


def compare_assets(item1: Any, item2: Any, search: str, attr: str = "path") -> int:
    """GTK sort comparator over two assets.

    It returns -1 when item1 comes first, 1 when item1 comes last, and 0 on a
    tie. An empty query gives case-sensitive alphabetical order by display
    name. Any other query gives the relevance order of asset_search, which
    ranks the closest name first and ties only names that rank alike.
    """
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
    """The grid shell of a pack chooser page.

    It holds the Gtk.FlowBox that the page appends pack cards to and connects
    its child-activated signal on.
    """

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
        # The build worker of the chooser decodes pixbuf. This constructor
        # decodes the thumbnail itself, on its own thread, only when the
        # caller supplies no pixbuf.
        super().__init__(
            image_path=pack.get_thumbnail_path() if pixbuf is _PIXBUF_UNSET else None,
            text=pack.name,
            pixbuf=pixbuf
        )
        self.pack = pack
        self.pack_chooser = pack_chooser

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
    """One asset card in a recycling asset grid.

    The grid pools these, so the constructor takes no asset. It builds an
    empty card, and set_asset binds one to it and rebinds it on every recycle.
    """

    def __init__(self) -> None:
        super().__init__()

        self.asset: AssetT = None  # ty: ignore[invalid-assignment]  # late-init: set_asset

    def on_click_info(self, button: Gtk.Button) -> None:
        # The window that owns this preview nulls the slot as it closes, and a
        # recycled child can outlive that, so answer a closed window with a log
        # line rather than a traceback out of the click handler.
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

        # This runs inside the main-loop callback of
        # DynamicFlowBox._apply_range, and the factory function of the chooser
        # is the one caller, so set the text and the image here. A deferral
        # through idle_add leaves the recycled child visible for a frame with
        # the name and thumbnail of the earlier item.
        self.set_text(os.path.splitext(os.path.basename(asset.path))[0])
        self.set_image(asset.path)


class _ChooserBuildPage(ChooserPage):
    """Build bookkeeping and failure recovery for both chooser bases.

    run_on_main raises RuntimeError when the main loop does not run its idle
    within RUN_ON_MAIN_TIMEOUT_S, which is 30 s. Anything that stalls the
    loop reaches that limit. Without this handling, log.catch swallows the
    raise and the page stays stuck. The spinner runs, build_finished stays
    unset, a deferred show_for_path strands, and no retry and no message
    follow.
    """

    build_finished = False
    build_failed = False
    _build_running = False

    def start_build(self) -> bool:
        """Starts the build worker unless one is already in flight. Called
        from the main thread only (constructor, retry_build), so the flag
        needs no lock."""
        if self._build_running:
            return False
        self._build_running = True
        threading.Thread(target=self._run_build,
                         name=f"{type(self).__name__}.build").start()
        return True

    def build(self) -> None:
        """Subclass hook.

        It gathers the data of the page off the main thread, and it
        constructs the widgets inside run_on_main callbacks.
        """
        raise NotImplementedError

    def _run_build(self) -> None:
        try:
            self.build()
        finally:
            self._build_running = False

    def retry_build(self) -> bool:
        """Rebuild a page whose build failed.

        AssetManager calls it when the user reopens the window. A healthy page
        does nothing here.
        """
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
        """Turn the loading page of ChooserPage into an error panel.

        A stopped spinner tells the user more than one that runs forever.
        """
        self.spinner.stop()
        self.loading_label.set_label(gl.lm.get("error"))
        self.set_visible_child_name("loading")
        return False  # one-shot idle

    def _reset_build_state(self) -> None:
        """Drop what the failed build would have consumed.

        Nothing then points at a page that never built.
        """


class GenericPackChooserPage(_ChooserBuildPage, Generic[PackT, StackT]):
    """The pack grid of one asset type.

    Subclasses supply the two widget classes, the stack child to drill into,
    and where the packs come from."""

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

    def __init__(self, stack: StackT, asset_manager: "AssetManager") -> None:
        super().__init__()
        self.asset_manager = asset_manager
        self.stack = stack

        # One gather runs at a time; a request that arrives while one runs
        # replaces the one waiting. See search_across_packs.
        self._search_lock = threading.Lock()
        self._search_pending: "tuple[str, ChooserPage, int] | None" = None
        self._search_running = False

        self.build_finished = False

        self.start_build()

    @log.catch
    def build(self) -> None:
        self.build_finished = False

        # This work belongs on the worker thread. The pack discovery reads
        # the disk, and so does the thumbnail decode. A GdkPixbuf decode is
        # file I/O and not GTK work, so it is safe here, and it must stay
        # here. At about 17 ms per store thumbnail, and over 100 ms for an
        # oversized one, a decode of the whole grid inside the main-loop
        # callback freezes the window for seconds.
        packs = list(self.get_packs().values())
        thumbnails = [Preview.decode_pixbuf(self.get_pack_thumbnail_path(pack))
                      for pack in packs]

        try:
            # Widgets are GTK, so they are built on the main loop.
            run_on_main(self._build_ui)

            # One batch per main-loop callback. A single callback for every
            # preview returns the loop only after the last one, so a large
            # pack set stutters even without the decodes.
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
        # The search entry of this page filters the pack cards. GTK re-runs
        # this predicate on every card the grid holds, and on every card that
        # a later batch appends, so the cards a build adds after a search
        # arrive filtered.
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

    def get_pack_thumbnail_path(self, pack: PackT) -> "Path | None":
        """Where the pack's thumbnail lives. Called on the build worker."""
        return pack.get_thumbnail_path()

    def on_child_activated(self, flow_box: Gtk.FlowBox, child: "_PackPreviewLike[PackT]") -> None:
        # Load the pack's assets, drill into the asset chooser, offer the way back.
        self.get_leaf_chooser().load_for_pack(child.pack)
        self.stack.set_visible_child_name(self.LEAF_CHILD_NAME)
        self.asset_manager.back_button.set_visible(True)

    def filter_pack_child(self, child: Gtk.FlowBoxChild) -> bool:
        """Whether one pack card survives the current query.

        GTK calls this on the main thread, over the cards of one grid, which
        is a few dozen names at most, so the scoring costs nothing here.
        """
        pack = getattr(child, "pack", None)
        if pack is None:
            # Not a pack card. Hiding a child this page does not know about
            # would be the wrong answer to a widget it never built.
            return True
        return asset_search.matches(pack.name, self.search_entry.get_text())

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
        # A query asks for an asset and not for a pack, so it searches every
        # pack. The grid here narrows to the packs named like the query while
        # that runs, and the results replace it when they land. Nothing is
        # recorded as rendered until they do.
        self.search_across_packs(query, self, self._search_generation)

    def on_shown(self) -> None:
        """Empty the entry as this page shows. Main loop only.

        Coming back from a search across the packs is the path that matters:
        the query belongs to that search, and this grid filters its cards on
        it, so a query left here would narrow the grid to whatever pack is
        named like it, which is usually no pack at all.

        The base runs this before its catch-up pass, which is what keeps a
        page turn from starting a gather for a query this page is throwing
        away in the same handler.
        """
        if self.search_entry.get_text():
            self.search_entry.set_text("")

    def search_across_packs(self, query: str, requester: ChooserPage,
                            generation: int) -> None:
        """Show every pack's assets that match query, ranked as one list.

        requester is the page whose entry holds the query, and generation the
        pass it belongs to. Both pages that search across packs pass their
        own: this grid starts the search, and the results page runs the next
        one when the query moves on. Guarding on the requester is what makes a
        page turn or a hidden window drop a search that is still gathering.

        One gather runs at a time. A request that arrives while one runs
        replaces the request waiting behind it, so three quick queries cost
        the gather in flight and one more, and never three scans of the whole
        installation at once. The gather reads the disk, so it runs on a
        worker thread, and every widget it leads to is built inside a
        main-loop callback.
        """
        with self._search_lock:
            self._search_pending = (query, requester, generation)
            if self._search_running:
                # The worker takes this up when its gather ends.
                return
            self._search_running = True
        threading.Thread(target=self._run_pack_search, daemon=True,
                         name=f"{type(self).__name__}.search").start()

    def _run_pack_search(self) -> None:
        """The search worker. It reads packs and touches no widget.

        It serves the newest request each time round, so a query that arrived
        while it gathered is answered without a second worker.
        """
        while True:
            with self._search_lock:
                request = self._search_pending
                self._search_pending = None
                if request is None:
                    self._search_running = False
                    return
            query, requester, generation = request
            try:
                assets = self.collect_matching_assets(query, requester, generation)
            except Exception as error:
                # Narrow on purpose. A gather that raises must leave the page
                # exactly as it found it, with nothing recorded as rendered,
                # so showing the page again or the next keystroke tries once
                # more. A swallow around the whole worker would instead leave
                # a page that believes it is current and never retries.
                log.opt(exception=error).error(
                    f"{type(self).__name__}: the search across packs failed for "
                    f"{query!r}; the page keeps what it shows and searches "
                    f"again on the next keystroke or when it is shown")
                continue
            if not requester.search_is_current(generation):
                # The query moved on, or the page went away, while this pass
                # read the packs. Queue nothing: the newer pass renders.
                continue
            GLib.idle_add(self._show_matching_assets, query, requester,
                          generation, assets)

    def collect_matching_assets(self, query: str, requester: ChooserPage,
                                generation: int) -> "list[Any]":
        """Every pack's assets that match query, best match first.

        One ranker scores the merged names of every pack. Its key is total, so
        the order of the result does not depend on the order the packs came
        in, and the leaf page's own sort reproduces this order from the same
        query.

        It runs on the search worker: the pack discovery and the asset scan
        both read the disk, and a whole installation is tens of thousands of
        files.
        """
        if not requester.search_is_current(generation):
            # Before the discovery and not after it. Reading every pack folder
            # is most of what a gather costs, and a pass that is already stale
            # must not pay it.
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
            return False  # one-shot idle
        leaf = self.get_leaf_chooser()
        on_pack_grid = self.stack.get_visible_child_name() == PACK_CHOOSER_CHILD_NAME
        if not on_pack_grid and not leaf.shows_pack_search:
            # The window moved into the leaf page for one pack while this
            # gather ran, and writing here would replace that pack's grid with
            # a search nobody is waiting for. The requester guard above does
            # not cover it on its own: it holds only because a Gtk.Stack
            # unmaps the child it leaves, which invalidates the page that
            # asked. That is GTK's behaviour and not a promise this module
            # makes, so the target says for itself whether it may be written.
            return False  # one-shot idle
        leaf.load_search_results(self, assets, query)
        requester.search_rendered(query)
        if on_pack_grid:
            # The first results of a search. Later ones land in the page that
            # already shows, and moving the stack again would restart its
            # transition on every keystroke.
            self.stack.set_visible_child_name(self.LEAF_CHILD_NAME)
            self.asset_manager.back_button.set_visible(True)
            # The entry the user typed into leaves with this grid, so the
            # results page takes the typing over. It has to wait for the stack
            # to show that page: an unmapped entry takes no focus.
            GLib.idle_add(leaf.focus_search_entry)
        return False  # one-shot idle

    # Subclass hooks

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
    """The asset grid of one pack.

    It holds a recycling DynamicFlowBox and the shared search and sort.
    """

    # DynamicFlowBox subclass. Its constructor takes a preview class and a
    # chooser.
    FLOW_BOX_CLASS: type = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.Icons.IconChooser
    # Preview subclass; ctor takes no arguments (the flow box pools them).
    PREVIEW_CLASS: type = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.Icons.IconChooser
    # Attribute holding an asset's file path.
    ASSET_PATH_ATTR: str = "path"

    # Class-level defaults. The ChooserPage constructor connects the search
    # entry, and therefore on_search_changed, before __init__ reaches its own
    # attributes.
    asset_flow: "DynamicFlowBox[PreviewT, AssetT] | None" = None
    _pending_pack: "PackT | None" = None
    # The pack grid that gathered what this page shows, and the query it
    # gathered for. Both are set while the grid holds a search across every
    # pack, and both are None and empty while it holds one pack. The page asks
    # that grid again when the query moves on, because the answer to a new
    # query is a new gather and not a filter of what is here.
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

        # A pack activated, or a search that landed, while this callback was
        # still queued renders now instead of being dropped. Only one of the
        # two is ever held: each request drops the other.
        if self._pending_pack is not None:
            pack, self._pending_pack = self._pending_pack, None
            self.load_for_pack(pack)
        elif self._pending_results is not None:
            pending, self._pending_results = self._pending_results, None
            self.load_search_results(*pending)

    @property
    def shows_pack_search(self) -> bool:
        """Whether this grid holds a search across the packs.

        False while it holds the assets of one pack, which is what a drill-in
        loads. A page that writes results into this one asks first, because
        the grid of a pack the user opened is not a grid to overwrite.
        """
        return self._pack_search_source is not None

    def show_empty_notice(self, visible: bool) -> None:
        """Say in words that a search across the packs answered nothing.

        An empty grid on its own reads as a page that has not loaded.
        """
        if self.empty_label is not None:
            self.empty_label.set_visible(visible)

    def load_for_pack(self, pack: PackT) -> None:
        if self.shows_pack_search:
            # The grid held a search across the packs, and it holds one pack
            # now. The query belongs to that search: kept here it would filter
            # the pack by something the user typed in another grid.
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
        """Show assets that a search across every pack gathered for query.

        pack_chooser gathered them, and is the page this one asks again when
        the query moves on. The entry takes the query over from the pack grid,
        whose entry goes away with it, so this page's own filter and sort rank
        what it shows the way the search ranked it, and so the user can carry
        on typing.
        """
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
        """Go back to the pack grid and forget the search. Main loop only.

        An empty query asks for no asset, and the grid it would leave behind
        holds every pack's assets, which is a whole installation and no answer
        at all. The pack grid is the answer, so this page drops what it holds
        and hands the window back to it.
        """
        source = self._pack_search_source
        self._pack_search_source = None
        self._pack_search_query = ""
        if self.search_entry.get_text():
            # A query of separators alone reaches here with the entry still
            # holding them. This page's entry holds a query only while it
            # holds that query's results.
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
        """The pack line a card carries under the asset name.

        None while the grid holds one pack: every card would name the same
        pack, which says nothing, and the line only takes room.
        """
        if not self.shows_pack_search:
            return None
        return asset.pack.name

    def select_asset(self, path: str) -> None:
        """Select the asset at path once the grid renders it."""
        self.selected_path = path

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
            # Every rebind sets this, because the pool recycles a card from a
            # search across the packs into a grid of one pack, where the line
            # it carried would name the wrong thing.
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

    def apply_search(self, query: str) -> None:
        if self.asset_flow is None:
            # Nothing rendered yet (build still queued on the main loop);
            # the first render already reads the current search text.
            return
        source = self._pack_search_source
        if source is not None and query != self._pack_search_query:
            # This grid holds the matches of another query across every pack,
            # so the answer to this one is a new search and not a filter of
            # what is here. A filter would only ever narrow, and the user who
            # deletes a letter asks for more.
            if asset_search.is_empty_query(query):
                self.leave_search_results()
                return
            # Nothing is rendered until the gather lands, so nothing is
            # recorded here: a gather that is dropped or that fails leaves
            # this page knowing it has fallen behind.
            source.search_across_packs(query, self, self._search_generation)
            return
        # Back to the first page: the grid it shows now holds the matches of
        # the query the user has replaced.
        self.asset_flow.show_range(0, self.asset_flow.N_ITEMS_PER_PAGE)
        self.search_rendered(query)

    # Subclass hooks

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
    """The two pages of one pack subsystem, and the switch between them.

    A subclass names the two page classes and the title of the leaf page. The
    name of the leaf stack child comes from the pack page, which drills into
    it by that name, so the two cannot drift apart.
    """

    PACK_CHOOSER_CLASS: "type[GenericPackChooserPage[Any, Any]]" = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.Stack
    LEAF_CHOOSER_CLASS: "type[LeafT]" = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.Stack
    LEAF_CHILD_TITLE: str = None  # ty: ignore[invalid-assignment]  # late-init: subclass override, e.g. IconPacks.Stack

    # build() reads this. A page starts a build worker in its constructor, and
    # that worker calls back into state prepare() creates, so a build that ran
    # first would race an attribute that does not exist yet. The failure it
    # gives is silent: @log.catch on the page build swallows the raise, and a
    # deferred pre-selection then never drains.
    _prepared = False

    @property
    def leaf_child_name(self) -> str:
        """The stack child that holds this family's asset grid.

        The pack page names it, because it drills into it by that name, so a
        reader outside this stack asks here rather than repeating the name and
        letting the two drift apart.
        """
        return self.PACK_CHOOSER_CLASS.LEAF_CHILD_NAME

    def __init__(self, asset_manager: "AssetManager", *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.asset_manager = asset_manager

        self.prepare()
        self._prepared = True
        self.build()

    def prepare(self) -> None:
        """State that the two pages may touch as soon as they exist.

        It runs before build(), because each page starts a build worker in its
        constructor and that worker calls back into the stack.
        """

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
