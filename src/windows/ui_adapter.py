"""GTK side of the engine-to-UI port.

GtkUIAdapter implements src.backend.ui_port.UIPort against the real widget
tree, so the engine touches no widget. Every method accepts a call from any
thread and returns without a block on the main loop. Widget changes marshal
with GLib.idle_add. Do not use run_on_main here, because a wedged main loop
must not stall the media writer. on_deck_layout_changed is the one exception,
and its docstring says why.
"""
# This module imports nothing from src.windows at module scope, because
# KeyGrid imports it for mark_dirty. The one widget import is function-local,
# in the rotation path.
import threading
import time
from typing import Any, Generic, Protocol, TYPE_CHECKING, TypeVar, cast, override

if TYPE_CHECKING:
    from PIL import Image
    from gi.repository import Gio

    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.InputIdentifier import InputIdentifier
    from src.windows.mainWindow.DeckPlus.ScreenBar import ScreenBar
    from src.windows.mainWindow.elements.DeckStackChild import DeckStackChild
    from src.windows.mainWindow.elements.KeyGrid import KeyGrid
    from src.windows.mainWindow.elements.Sidebar.Sidebar import Sidebar
    from src.windows.mainWindow.mainWindow import MainWindow

from gi.repository import GLib
from loguru import logger as log

import globals as gl
from src.backend import ui_port
from src.backend.DeckManagement.InputIdentifier import Input

# Seconds between on-screen touchscreen previews. The physical touchscreen
# still gets every frame. Only the mirror takes the limit, because one strip
# frame repaints a preview as wide as the whole deck.
TOUCHSCREEN_UI_INTERVAL_S = 0.1
# Key previews paint as fast as the main loop drains them. The slot bounds it.
KEY_UI_INTERVAL_S = 0.0


def mark_dirty(controller: "DeckController", identifier: "InputIdentifier") -> None:
    """Record a frame that the adapter accepted and then dropped.

    push_input_image returns True as soon as a frame reaches the mirror slot of
    the input. The window can unmap before that paint runs. No engine call is
    then left to return False. load_from_changes replays what lands here.
    """
    # The markers dict lives on the controller, so a detached UI client can
    # ask the engine to composite again.
    try:
        controller.ui_image_changes_while_hidden[identifier] = True
    except Exception:
        # A controller torn down mid-flight has nothing left to replay to.
        log.opt(exception=True).debug("Could not record a dropped preview frame")


_PayloadT = TypeVar("_PayloadT")


class MirrorWidget(Protocol, Generic[_PayloadT]):
    """A widget that mirrors one input: it converts a frame and paints the
    result on the main loop. The payload shape is the widget's own; the
    adapter's drain runs the two calls back to back on the loop, so only
    the frame that won the latest-wins slot is ever converted. The
    map-time replay paths convert on their own thread and idle the paint,
    which the payload contract permits."""

    def prepare_mirror_frame(self, image: "Image.Image") -> _PayloadT: ...
    def paint_mirror_frame(self, payload: _PayloadT, /) -> bool: ...


class _MirrorSlot:
    """Latest-wins hand-off of the preview frames of one input.

    A producer leaves the newest raw frame here and arms at most one
    main-loop callback. That callback converts and paints whatever the slot
    holds when it runs, so a superseded frame is dropped unconverted. A
    backlogged loop keeps one callback and one frame per input, and the
    newest frame is the one that lands.
    """

    __slots__ = ("_interval", "_lock", "_pending", "_armed", "_last_drain")

    def __init__(self, interval: float = 0.0) -> None:
        # A floor on the drain rate, for a preview that is worth a limit of its
        # own. 0 drains as fast as the loop allows. A delayed callback still
        # flushes a held frame, so the last frame of a burst lands.
        self._interval = interval
        self._lock = threading.Lock()
        self._pending: object | None = None
        self._armed: bool = False
        # Infinitely far in the past, so the first frame paints at once.
        self._last_drain: float = float("-inf")

    def offer(self, payload: object) -> float | None:
        """Producer side, any thread. Makes payload the frame to paint.

        It replaces a frame that no callback painted yet. Returns the seconds
        to wait before the drain, or None when a drain is already armed. That
        callback then takes this payload, which keeps the callback count at one.
        """
        with self._lock:
            self._pending = payload
            if self._armed:
                return None
            self._armed = True
            return max(0.0, self._interval - (time.monotonic() - self._last_drain))

    def take(self) -> object | None:
        """Main loop. Returns the frame to paint and disarms the slot.

        Returns None when the slot is empty, so a callback that a producer
        armed in the gap between this call and the paint does nothing.
        """
        with self._lock:
            payload, self._pending = self._pending, None
            self._armed = False
            if payload is not None:
                self._last_drain = time.monotonic()
            return payload

    def disarm(self) -> None:
        """Undo an offer whose callback never reached the loop.

        The payload stays. Without this the slot stays armed, and the preview
        of the input freezes with a frame behind it.
        """
        with self._lock:
            self._armed = False


class GtkUIAdapter(ui_port.UIPort):
    def __init__(self) -> None:
        # Maps a controller to its DeckStackChild. DeckStack.add_page and
        # DeckStack.remove_page maintain it. The bind uses object identity at
        # add time, with no serial match and no ListModel scan from the media
        # thread.
        self._children: "dict[DeckController, DeckStackChild]" = {}
        self._window: "MainWindow | None" = None
        # The map and unmap handlers of the window write this bool, and the
        # media thread reads it without a lock. It replaces an off-main
        # main_win.get_mapped() widget read.
        self._window_mapped: bool = False
        # Maps (controller, identifier) to a _MirrorSlot. One slot per input,
        # so a stalled main loop holds one frame per input, not a queue.
        self._mirror_slots: "dict[tuple[DeckController, InputIdentifier], _MirrorSlot]" = {}
        # Maps a controller to a bool. This is the page-sync coalescer.
        self._page_sync_queued: "dict[DeckController, bool]" = {}

    # Setup

    def attach_window(self, window: "MainWindow") -> None:
        """Bind to a built MainWindow.

        It runs after the constructor, because the map and unmap handlers need
        a real window. The adapter installs before the constructor, because
        every boot-time add_page runs inside MainWindow.build().
        """
        self._window = window
        try:
            self._window_mapped = bool(window.get_mapped())
            window.connect("map", self._on_window_map)
            window.connect("unmap", self._on_window_unmap)
        except Exception:
            log.opt(exception=True).warning("Could not track the main window's mapped state")
        # Re-scan the deck stack, so a child that the constructor added binds
        # too.
        self.rescan_children()
        self.reconcile_children()

    def reconcile_children(self) -> None:
        """Heal the decks that the window constructor could not see.

        rescan_children re-binds only the children that exist, so this method
        reconciles both directions against the deck manager list.
        """
        # on_deck_added and on_deck_removed do nothing while _window is None,
        # which is the period that MainWindow.__init__ occupies. A deck that
        # the USB monitor plugs in then gets no stack child, and a deck that it
        # unplugs leaves a stale one.
        window = self._window
        if window is None:
            return
        deck_stack = window.get_deck_stack()
        if deck_stack is None:
            return
        registered = getattr(getattr(gl, "deck_manager", None), "deck_controller", None)
        if registered is None:
            # No deck manager to reconcile against. Return instead of reading
            # this as an empty deck list, because the removal pass below then
            # tears down every bound child. main.py builds gl.deck_manager
            # before App, so this state does not occur.
            return
        live = list(registered)
        for controller in live:
            if controller not in self._children:
                GLib.idle_add(deck_stack.add_page, controller)
        for controller in [c for c in self._children if c not in live]:
            GLib.idle_add(deck_stack.remove_page, controller)
            self.unbind(controller)

    def detach_window(self) -> None:
        self._window = None
        self._window_mapped = False
        self._children.clear()
        self._mirror_slots.clear()
        self._page_sync_queued.clear()

    def rescan_children(self) -> None:
        """Bind every controller whose DeckStackChild is in the stack.

        This makes the bind independent of the adapter install order, and it
        heals a rebuilt window.
        """
        window = self._window
        if window is None:
            return
        deck_stack = window.get_deck_stack()
        if deck_stack is None:
            return
        # The stub's SelectionModel misses the ListModel iteration that
        # PyGObject provides at runtime. The Any item type also keeps the
        # trailing-None guard below alive for the checker.
        pages = cast("Gio.ListModel[Any]", deck_stack.get_pages())
        for page in pages:
            if page is None:
                # The ListModel iteration reads the length once, so a removed
                # trailing index yields None. Only trailing entries are None.
                break
            child = page.get_child()
            controller = getattr(child, "deck_controller", None)
            if controller is not None:
                self._children[controller] = child

    def bind(self, controller: "DeckController", child: "DeckStackChild") -> None:
        self._children[controller] = child

    def unbind(self, controller: "DeckController") -> None:
        self._children.pop(controller, None)
        self._page_sync_queued.pop(controller, None)
        # Snapshot the keys, then delete without a KeyError. This method runs
        # off the main loop, on the USB monitor, boot rescan and flatpak poll
        # threads, and it is not the only writer. The media thread creates a
        # slot at the first mirror of an input, so a scan of the live dict can
        # see the size change. Every armed drain for this controller pops its
        # own slot from the GTK loop once the child goes, so a key in the
        # snapshot can be gone already. A raise here leaves on_deck_removed,
        # skips close(), and strands the media thread and the USB handle.
        for key in [k for k in list(self._mirror_slots) if k[0] is controller]:
            self._mirror_slots.pop(key, None)

    def _on_window_map(self, *args: Any) -> None:
        self._window_mapped = True

    def _on_window_unmap(self, *args: Any) -> None:
        self._window_mapped = False

    # Resolvers

    def _grid(self, child: "DeckStackChild") -> "KeyGrid | None":
        # The chain is absent while the child builds; the typed access reads
        # None then instead of an AttributeError, and a wrong attribute name is
        # now a check-time error.
        try:
            return child.page_settings.deck_config.grid
        except AttributeError:
            return None

    def _screenbar(self, child: "DeckStackChild") -> "ScreenBar | None":
        # screenbar is absent on a deck with no touchscreen, and the whole
        # chain is absent while the child builds; both raise AttributeError
        # here. A built ScreenBar always carries image, its __init__ sets it,
        # so the leaf the caller reads follows from the screenbar existing.
        try:
            return child.page_settings.deck_config.screenbar
        except AttributeError:
            return None

    def _mirror_widget(self, child: "DeckStackChild", identifier: "InputIdentifier") -> "MirrorWidget[Any] | None":
        """The widget that mirrors identifier, or None when there is none.

        This raises during a grid rebuild, because buttons[x][y] can be short
        of these coordinates, so every caller contains the exception.
        """
        if isinstance(identifier, Input.Key):
            grid = self._grid(child)
            if grid is None:
                return None
            x, y = identifier.coords
            return grid.buttons[x][y]
        if isinstance(identifier, Input.Touchscreen):
            screenbar = self._screenbar(child)
            return None if screenbar is None else screenbar.image
        return None

    # Render mirror

    @override
    def push_input_image(self, controller: "DeckController", identifier: "InputIdentifier", image: "Image.Image | None") -> bool:
        try:
            if image is None or not self._window_mapped:
                return False
            child = self._children.get(controller)
            if child is None:
                return False
            widget = self._mirror_widget(child, identifier)
            if widget is None:
                return False

            # Offer the raw frame; the drain converts. A frame the slot
            # supersedes then costs nothing beyond this admission check,
            # and only the winning frame pays PIL and pixbuf work, on the
            # widget the drain re-resolves.
            key = (controller, identifier)
            slot = self._mirror_slots.get(key)
            if slot is None:
                interval = (TOUCHSCREEN_UI_INTERVAL_S
                            if isinstance(identifier, Input.Touchscreen)
                            else KEY_UI_INTERVAL_S)
                slot = self._mirror_slots.setdefault(key, _MirrorSlot(interval))

            delay_s = slot.offer(image)
            if delay_s is None:
                # A drain is already armed and now carries this frame.
                return True
            try:
                # Both arms use idle priority. A pixbuf update above the GTK
                # layout and draw priority of 120 starves the redraw that it
                # feeds. timeout_add defaults to priority 0, so the delayed
                # arm names the priority.
                if delay_s <= 0:
                    GLib.idle_add(self._drain_mirror, controller, identifier)
                else:
                    GLib.timeout_add(int(delay_s * 1000) + 1, self._drain_mirror,
                                     controller, identifier,
                                     priority=GLib.PRIORITY_DEFAULT_IDLE)
            except BaseException:
                # No callback drains this slot now, and an armed slot freezes
                # the input.
                slot.disarm()
                raise
            return True
        except Exception:
            # The failure set is open: the widget lookup races the window
            # teardown, and the GLib scheduling call can raise after the
            # offer. Contain all of it. This code runs under the media tick,
            # whose catch-all waits 0.25 s per exception, and a failed
            # preview must not throttle the deck writer loop.
            log.opt(exception=True).warning(f"Failed to mirror {identifier} into the UI")
            return False

    def _drain_mirror(self, controller: "DeckController", identifier: "InputIdentifier") -> bool:
        # On the main loop, convert and paint the newest frame of this
        # input. Return False, because a GLib callback that returns a true
        # value re-arms.
        slot = self._mirror_slots.get((controller, identifier))
        if slot is None:
            return False
        image = slot.take()
        if image is None:
            return False
        try:
            # Resolve again here instead of a capture at push time. The bind
            # uses deck-stack-child identity, and a grid that rebuilds in the
            # gap would else receive a frame for its orphaned predecessor.
            child = self._children.get(controller)
            if child is None:
                # The unbind landed between the push and this paint, so drop
                # the slot too. A push that races unbind() makes a new one, and
                # a slot keyed by a dead controller pins its whole graph.
                self._mirror_slots.pop((controller, identifier), None)
            widget = None if child is None else self._mirror_widget(child, identifier)
            if not self._window_mapped or widget is None:
                # The adapter accepted and then dropped this frame.
                # push_input_image already returned True, so nothing else
                # records it. The dropped frame was never converted.
                mark_dirty(controller, identifier)
                return False
            # Only the winning frame reaches this conversion; every frame
            # the slot superseded was dropped as a raw image, and one armed
            # drain serves any number of pushes.
            widget.paint_mirror_frame(
                widget.prepare_mirror_frame(cast("Image.Image", image)))
        except Exception:
            log.opt(exception=True).warning(f"Failed to paint the {identifier} mirror")
            mark_dirty(controller, identifier)
        return False

    # Deck sync

    @override
    def on_page_changed(self, controller: "DeckController") -> None:
        # Coalesce the page-load completions into one pending idle, so a burst
        # of page changes does not queue a sidebar rebuild for each one. Each
        # callback renders the live state, so the last completion wins. The
        # check-then-set race between the two trigger threads costs at most two
        # idles that render the same state.
        if self._page_sync_queued.get(controller):
            return
        self._page_sync_queued[controller] = True
        GLib.idle_add(self._run_page_changed, controller)

    def _run_page_changed(self, controller: "DeckController") -> bool:
        # Use pop, not an assignment of False. An idle queued before unbind()
        # still runs after it, and a re-inserted key pins the whole graph of an
        # unplugged controller.
        self._page_sync_queued.pop(controller, None)
        window = self._window
        if window is None:
            return False
        sidebar = window.get_sidebar()
        if sidebar is None:
            return False
        child = self._children.get(controller)
        if child is None:
            return False
        # The sidebar mirrors the selected input of the visible deck, so a
        # page change on a background deck must not reload it.
        deck_stack = window.get_deck_stack()
        if deck_stack is None:
            return False
        if deck_stack.get_visible_child() is not child:
            return False
        # Do not pull the user out of a sub-view. Sidebar.load_for_* sets
        # main_stack back to the input editor, so a refresh while the
        # ActionChooser, the ActionConfigurator or the error page is up moves a
        # user away in the middle of an edit.
        if sidebar.main_stack.get_visible_child() is not sidebar.configurator_stack:
            return False
        sidebar.update()
        return False

    _EDITOR_FOR_ASPECT = {
        "labels": "label_editor",
        "layout": "image_editor",
        "background": "background_editor",
    }

    @override
    def on_input_visuals_changed(self, controller: "DeckController", identifier: "InputIdentifier", state: int, aspect: str) -> None:
        GLib.idle_add(self._run_input_visuals_changed, controller, identifier, state, aspect)

    def _run_input_visuals_changed(self, controller: "DeckController", identifier: "InputIdentifier", state: int, aspect: str) -> bool:
        editor_name = self._EDITOR_FOR_ASPECT.get(aspect)
        if editor_name is None:
            log.warning(f"Unknown UI aspect {aspect!r}")
            return False
        sidebar = self._sidebar_for(controller, identifier)
        if sidebar is None:
            return False
        getattr(sidebar.key_editor, editor_name).load_for_identifier(identifier, state)
        return False

    @override
    def on_input_states_changed(self, controller: "DeckController", identifier: "InputIdentifier", n_states: int) -> None:
        GLib.idle_add(self._run_input_states_changed, controller, identifier, n_states)

    def _run_input_states_changed(self, controller: "DeckController", identifier: "InputIdentifier", n_states: int) -> bool:
        sidebar = self._sidebar_for(controller, identifier, require_active_deck=False)
        if sidebar is None:
            return False
        sidebar.key_editor.state_switcher.set_n_states(n_states)
        return False

    @override
    def on_input_state_selected(self, controller: "DeckController", identifier: "InputIdentifier", state: int) -> None:
        GLib.idle_add(self._run_input_state_selected, controller, identifier, state)

    def _run_input_state_selected(self, controller: "DeckController", identifier: "InputIdentifier", state: int) -> bool:
        sidebar = self._sidebar_for(controller, identifier)
        if sidebar is None:
            return False
        sidebar.active_state = state
        sidebar.update()
        return False

    def _sidebar_for(self, controller: "DeckController", identifier: "InputIdentifier", require_active_deck: bool = True) -> "Sidebar | None":
        """The sidebar, only while it shows identifier of controller.

        This runs on the main loop, and it holds the widget reads.
        """
        window = self._window
        if window is None:
            return None
        sidebar = window.get_sidebar()
        if sidebar is None:
            return None
        if sidebar.active_identifier != identifier:
            return None
        if require_active_deck and window.get_active_controller() is not controller:
            return None
        return sidebar

    @override
    def set_low_fps_warning(self, controller: "DeckController", shown: bool) -> None:
        GLib.idle_add(self._run_set_low_fps_warning, controller, shown)

    def _run_set_low_fps_warning(self, controller: "DeckController", shown: bool) -> bool:
        child = self._children.get(controller)
        if child is None or not hasattr(child, "low_fps_banner"):
            return False
        child.low_fps_banner.set_revealed(shown)
        return False

    @override
    def on_deck_layout_changed(self, controller: "DeckController") -> None:
        """Rebuild the key grid of the deck for a new rotation.

        This runs inline on the main loop, because the one caller of
        set_rotation runs there and reloads the page at once. An idled rebuild
        lets those repaints reach the grid from before the rotation. The
        transposed buttons there raise IndexError, and the frames drop with no
        marker.
        """
        if threading.current_thread() is threading.main_thread():
            self._run_deck_layout_changed(controller)
            return
        GLib.idle_add(self._run_deck_layout_changed, controller)

    def _run_deck_layout_changed(self, controller: "DeckController") -> bool:
        # Function-local, because KeyGrid imports this module for mark_dirty.
        from src.windows.mainWindow.elements.KeyGrid import KeyGrid

        child = self._children.get(controller)
        if child is None:
            return False
        deck_config = child.page_settings.deck_config
        old_grid = deck_config.grid
        deck_config.remove(old_grid)
        deck_config.grid = KeyGrid(controller, old_grid.page_settings_page)
        deck_config.prepend(deck_config.grid)
        return False

    # Deprecated queries

    @override
    def query_input_widget(self, controller: "DeckController", identifier: "InputIdentifier") -> "object | None":
        child = self._children.get(controller)
        if child is None:
            return None
        try:
            return self._mirror_widget(child, identifier)
        except Exception:
            log.opt(exception=True).warning(f"Could not resolve the widget for {identifier}")
        return None

    @override
    def query_deck_widget(self, controller: "DeckController", part: str) -> "object | None":
        child = self._children.get(controller)
        if child is None:
            return None
        if part == "deck_stack_child":
            return child
        if part == "key_grid":
            return self._grid(child)
        return None

    # App level

    @override
    def on_deck_added(self, controller: "DeckController") -> None:
        window = self._window
        if window is None:
            return
        deck_stack = window.get_deck_stack()
        if deck_stack is None:
            return
        GLib.idle_add(deck_stack.add_page, controller)

    @override
    def on_deck_removed(self, controller: "DeckController") -> None:
        # Queue the detach idle here, before the return. The caller starts the
        # slow close thread at once, and a fast unplug and replug must not race
        # a late detach against a new add_page idle, which leaves two stack
        # children for one serial.
        window = self._window
        deck_stack = window.get_deck_stack() if window is not None else None
        if deck_stack is not None:
            GLib.idle_add(deck_stack.remove_page, controller)
        self.unbind(controller)

    @override
    def refresh_deck_availability(self) -> None:
        window = self._window
        if window is None:
            return
        GLib.idle_add(window.check_for_errors)

    @override
    def on_page_list_changed(self) -> None:
        window = self._window
        if window is None:
            return
        sidebar = window.get_sidebar()
        if sidebar is None:
            return
        GLib.idle_add(sidebar.page_selector.update)

    @override
    def notify_plugin_problem(self, plugin_id: str, kind: str) -> None:
        app = getattr(gl, "app", None)
        if app is None:
            return
        # App.send_notification marshals its whole body onto the main loop, so
        # this is callable straight from an action executor thread.
        if kind == "outdated":
            app.send_outdated_plugin_notification(plugin_id)
        elif kind == "missing":
            app.send_missing_plugin_notification(plugin_id)
        else:
            log.warning(f"Unknown plugin problem kind {kind!r}")

    @override
    def notify_user(self, body: str, title: str) -> None:
        notify = getattr(gl, "notify", None)
        if notify is None:
            return
        notify.error(body, title=title)
