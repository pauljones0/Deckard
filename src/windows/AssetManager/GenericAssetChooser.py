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
with the shared fuzzy search and sort. GenericPackFlowBox and
GenericPackPreview are the grid shell and the card of the first;
GenericAssetFlowBox and GenericAssetPreview are those of the second.
GenericPackChooserStack holds one page of each and names the subsystem.

A subsystem therefore adds no widget class of its own. It names the classes it
builds with, the packs it reads, and the title of its leaf page.

Both build the same way. The build worker thread gathers the data, which is
the pack discovery and the disk I/O, and one run_on_main callback constructs
and attaches every widget. GTK4 is main-thread-only, and a widget tree built
on the worker is the off-main GTK crash class.
tests/scenario_asset_chooser_offmain.py catches it.

The search helpers at the top use no GTK and no self, so a headless test pins
them.
"""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk

# Import python modules
import os
import threading

from rapidfuzz import fuzz
from loguru import logger as log

# Import own modules
from GtkHelper.GtkHelper import run_on_main
from src.backend.PackManagement.pack_family import PackAsset
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


# A candidate must score at least this against the query to stay in the grid.
SEARCH_SCORE_THRESHOLD = 50

def asset_display_name(item: Any, attr: str = "path") -> str:
    """The string that the search matches on.

    It is the file name of the asset, without the directory and without the
    extension, which is what the preview label shows.
    """
    return cast(str, os.path.splitext(os.path.basename(getattr(item, attr)))[0])


def asset_matches_search(item: Any, search: str, attr: str = "path") -> bool:
    """Filter predicate for one asset.

    An empty query keeps everything. Any other query needs a display name
    that scores at least SEARCH_SCORE_THRESHOLD.
    """
    if search == "":
        return True
    score = fuzz.ratio(asset_display_name(item, attr).lower(), search.lower())
    return bool(score >= SEARCH_SCORE_THRESHOLD)


def compare_assets(item1: Any, item2: Any, search: str, attr: str = "path") -> int:
    """GTK sort comparator over two assets.

    It returns -1 when item1 comes first, 1 when item1 comes last, and 0 on a
    tie. An empty query gives case-sensitive alphabetical order by display
    name. Any other query gives a descending fuzzy score, and equal scores tie,
    which keeps the input order, because sorted is stable.
    """
    name1 = asset_display_name(item1, attr)
    name2 = asset_display_name(item2, attr)

    if search == "":
        if name1 < name2:
            return -1
        if name1 > name2:
            return 1
        return 0

    score1 = fuzz.ratio(name1.lower(), search.lower())
    score2 = fuzz.ratio(name2.lower(), search.lower())

    if score1 > score2:
        return -1
    if score1 < score2:
        return 1
    return 0


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

        self.asset: AssetT = None  # type: ignore[assignment]  # late-init: set_asset

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
    PACK_FLOW_BOX_CLASS: type = None  # type: ignore[assignment]  # late-init: subclass override, e.g. IconPacks.PackChooser
    # Preview subclass with a pack attribute. Its constructor takes a chooser
    # and a pack.
    PACK_PREVIEW_CLASS: type = None  # type: ignore[assignment]  # late-init: subclass override, e.g. IconPacks.PackChooser
    # Name of the stack child holding this type's asset chooser.
    LEAF_CHILD_NAME: str = None  # type: ignore[assignment]  # late-init: subclass override, e.g. IconPacks.PackChooser
    # Pack previews constructed per main-loop callback.
    PACK_APPEND_BATCH: int = 10

    # _build_ui binds this on the main loop, so a reader must accept None.
    # A marshal that timed out never binds it. See _handle_build_failure.
    pack_flow = None

    def __init__(self, stack: StackT, asset_manager: "AssetManager") -> None:
        super().__init__()
        self.asset_manager = asset_manager
        self.stack = stack

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

    It holds a recycling DynamicFlowBox and the shared fuzzy search and sort.
    """

    # DynamicFlowBox subclass. Its constructor takes a preview class and a
    # chooser.
    FLOW_BOX_CLASS: type = None  # type: ignore[assignment]  # late-init: subclass override, e.g. IconPacks.Icons.IconChooser
    # Preview subclass; ctor takes no arguments (the flow box pools them).
    PREVIEW_CLASS: type = None  # type: ignore[assignment]  # late-init: subclass override, e.g. IconPacks.Icons.IconChooser
    # Attribute holding an asset's file path.
    ASSET_PATH_ATTR: str = "path"

    # Class-level defaults. The ChooserPage constructor connects the search
    # entry, and therefore on_search_changed, before __init__ reaches its own
    # attributes.
    asset_flow: "DynamicFlowBox[PreviewT, AssetT] | None" = None
    _pending_pack: "PackT | None" = None

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
        self.main_box.append(self.asset_flow)

        # Connect flow box select signal
        self.asset_flow.flow_box.connect("child-activated", self.on_child_activated)

        # A pack activated while this callback was still queued renders now
        # instead of being dropped (see load_for_pack).
        if self._pending_pack is not None:
            pack, self._pending_pack = self._pending_pack, None
            self.load_for_pack(pack)

    def load_for_pack(self, pack: PackT) -> None:
        if self.asset_flow is None:
            # The build still waits on the main loop. Keep the request
            # instead of dropping it or raising AttributeError.
            self._pending_pack = pack
            return
        self.asset_flow.set_item_list(self.get_assets(pack))
        # The recycler owns its page size (it sized its preview pool to it).
        self.asset_flow.show_range(0, self.asset_flow.N_ITEMS_PER_PAGE)

    def select_asset(self, path: str) -> None:
        """Select the asset at path once the grid renders it."""
        self.selected_path = path

    def _reset_build_state(self) -> None:
        # No grid renders it and no build consumes it, so a kept request
        # strands without a word.
        self._pending_pack = None

    def on_child_activated(self, flow_box: Gtk.FlowBox, child: PreviewT) -> None:
        asset = self.get_child_asset(child)
        self.asset_manager.deliver_selection(getattr(asset, self.ASSET_PATH_ATTR))

    def preview_factory(self, preview: PreviewT, asset: AssetT) -> None:
        # Called from DynamicFlowBox._apply_range's main-loop callback.
        self.bind_preview(preview, asset)
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

    def on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        if self.asset_flow is None:
            # Nothing rendered yet (build still queued on the main loop);
            # the first render already reads the current search text.
            return
        self.asset_flow.show_range(0, self.asset_flow.N_ITEMS_PER_PAGE)

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

    PACK_CHOOSER_CLASS: "type[GenericPackChooserPage[Any, Any]]" = None  # type: ignore[assignment]  # late-init: subclass override, e.g. IconPacks.Stack
    LEAF_CHOOSER_CLASS: "type[LeafT]" = None  # type: ignore[assignment]  # late-init: subclass override, e.g. IconPacks.Stack
    LEAF_CHILD_TITLE: str = None  # type: ignore[assignment]  # late-init: subclass override, e.g. IconPacks.Stack

    def __init__(self, asset_manager: "AssetManager", *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.asset_manager = asset_manager

        self.prepare()
        self.build()

    def prepare(self) -> None:
        """State that the two pages may touch as soon as they exist.

        It runs before build(), because each page starts a build worker in its
        constructor and that worker calls back into the stack.
        """

    def build(self) -> None:
        self.pack_chooser = self.PACK_CHOOSER_CLASS(self, self.asset_manager)
        self.add_titled(self.pack_chooser, "pack-chooser", "Chooser")

        self.leaf_chooser = self.LEAF_CHOOSER_CLASS(self, self.asset_manager)
        self.add_titled(self.leaf_chooser, self.PACK_CHOOSER_CLASS.LEAF_CHILD_NAME,
                        self.LEAF_CHILD_TITLE)
