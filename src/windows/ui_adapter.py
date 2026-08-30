"""Implement the UI port without exposing widgets; calls stay nonblocking on any thread.

Use idle_add, not run_on_main, so a wedged loop cannot stall media writes; layout may run inline.
"""
# Keep window imports out of module scope because KeyGrid imports mark_dirty.
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, Protocol, TYPE_CHECKING, TypeVar, cast, override

if TYPE_CHECKING:
    from PIL import Image
    from gi.repository import Gio

    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.InputIdentifier import InputIdentifier
    from src.backend.DeckManagement.deck_controller.input_latency import LatencySample
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

# Limit only the wide on-screen touchscreen mirror; the device gets every frame.
TOUCHSCREEN_UI_INTERVAL_S = 0.1
# Key previews paint as fast as the main loop drains them. The slot bounds it.
KEY_UI_INTERVAL_S = 0.0


def mark_dirty(controller: "DeckController", identifier: "InputIdentifier") -> None:
    """Mark an input after an accepted drop or a scheduling or replay failure.

    The controller marker lets load_from_changes retry it when the window maps.
    """
    # The markers dict lives on the controller, so a detached UI client can
    # ask the engine to composite again.
    try:
        controller.ui_image_changes_while_hidden[identifier] = True
    except Exception:
        # A controller torn down mid-flight has nothing left to replay to.
        log.opt(exception=True).debug("Could not record a dropped preview frame")


def _discard_mirror_frame(controller: "DeckController", frame: object | None,
                          reason: str) -> None:
    if not isinstance(frame, _MirrorFrame):
        return
    tracker = getattr(controller, "input_latency", None)
    if tracker is not None:
        tracker.drop(frame.latency_sample, reason)


_PayloadT = TypeVar("_PayloadT")


@dataclass(frozen=True, slots=True)
class _MirrorFrame:
    """Hold one raw preview frame and its latency sample.

    The drain converts only the winning frame after it resolves the current widget.
    """

    image: object
    latency_sample: "LatencySample | None"


class MirrorWidget(Protocol, Generic[_PayloadT]):
    """Convert and paint one input frame with a widget-specific payload.

    Replay can convert synchronously on its caller or the main thread.
    """

    def prepare_mirror_frame(self, image: "Image.Image") -> _PayloadT: ...
    def paint_mirror_frame(self, payload: _PayloadT, /) -> bool: ...


class _MirrorSlot:
    """Keep the latest raw frame and at most one armed main-loop callback.

    A backlogged loop drops superseded frames unconverted and paints the newest.
    """

    __slots__ = ("_interval", "_lock", "_pending", "_armed", "_last_drain")

    def __init__(self, interval: float = 0.0) -> None:
        # Set a minimum drain interval; zero follows the loop, while a delayed
        # callback still flushes the last frame of a burst.
        self._interval = interval
        self._lock = threading.Lock()
        self._pending: object | None = None
        self._armed: bool = False
        # Infinitely far in the past, so the first frame paints at once.
        self._last_drain: float = float("-inf")

    def offer(self, payload: object,
              on_superseded: "Callable[[object], None] | None" = None) -> float | None:
        """Replace the pending frame from any thread.

        Record displacement now; return delay or None when a callback is armed.
        """
        with self._lock:
            displaced = self._pending
            self._pending = payload
            if self._armed:
                delay = None
            else:
                self._armed = True
                delay = max(0.0, self._interval - (time.monotonic() - self._last_drain))
        if displaced is not None and on_superseded is not None:
            on_superseded(displaced)
        return delay

    def take(self) -> object | None:
        """Take the frame and disarm the slot on the main loop.

        Return None when empty; a separately armed callback owns any later offer.
        """
        with self._lock:
            payload, self._pending = self._pending, None
            self._armed = False
            if payload is not None:
                self._last_drain = time.monotonic()
            return payload

    def disarm(self) -> None:
        """Disarm an offer whose callback never reached the loop.

        Keep the payload so a later offer can schedule it instead of freezing.
        """
        with self._lock:
            self._armed = False

    def discard(self) -> object | None:
        """Drop the pending frame when its controller or window goes away."""
        with self._lock:
            payload, self._pending = self._pending, None
            self._armed = False
            return payload


class GtkUIAdapter(ui_port.UIPort):
    def __init__(self) -> None:
        # DeckStack add and remove operations bind controllers to children by
        # object identity, without a media-thread ListModel scan.
        self._children: "dict[DeckController, DeckStackChild]" = {}
        self._window: "MainWindow | None" = None
        # Map handlers write this flag so the media thread does not read a GTK
        # widget off the main thread.
        self._window_mapped: bool = False
        # Maps (controller, identifier) to a _MirrorSlot. One slot per input,
        # so a stalled main loop holds one frame per input, not a queue.
        self._mirror_slots: "dict[tuple[DeckController, InputIdentifier], _MirrorSlot]" = {}
        # Maps a controller to a bool. This is the page-sync coalescer.
        self._page_sync_queued: "dict[DeckController, bool]" = {}

    # Setup

    def attach_window(self, window: "MainWindow") -> None:
        """Bind after MainWindow construction and install map-state handlers.

        The adapter itself installs earlier so boot-time add_page calls reach it.
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
        """Reconcile stack children in both directions with registered decks.

        This heals add and remove events missed during MainWindow construction.
        """
        window = self._window
        if window is None:
            return
        deck_stack = window.get_deck_stack()
        if deck_stack is None:
            return
        registered = getattr(getattr(gl, "deck_manager", None), "deck_controller", None)
        if registered is None:
            # An absent manager is unknown state, not an empty deck list; do not
            # remove every bound child.
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
        for (controller, _identifier), slot in list(self._mirror_slots.items()):
            _discard_mirror_frame(controller, slot.discard(), "ui_window_detached")
        self._mirror_slots.clear()
        self._page_sync_queued.clear()

    def rescan_children(self) -> None:
        """Bind all controllers with stack children, independent of install order.

        A rescan also heals a rebuilt window.
        """
        window = self._window
        if window is None:
            return
        deck_stack = window.get_deck_stack()
        if deck_stack is None:
            return
        # The stub omits runtime ListModel iteration; Any also preserves the
        # trailing-None guard for the type checker.
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
        # USB, boot-rescan, and Flatpak threads race media-slot creation and GTK
        # drains; snapshot keys and tolerate prior deletion so device close continues.
        for key in [k for k in list(self._mirror_slots) if k[0] is controller]:
            slot = self._mirror_slots.pop(key, None)
            if slot is not None:
                _discard_mirror_frame(controller, slot.discard(), "ui_unbound")

    def _on_window_map(self, *args: Any) -> None:
        self._window_mapped = True

    def _on_window_unmap(self, *args: Any) -> None:
        self._window_mapped = False

    # Resolvers

    def _grid(self, child: "DeckStackChild") -> "KeyGrid | None":
        # The chain is absent during child construction; return None while
        # keeping attribute names visible to the type checker.
        try:
            return child.page_settings.deck_config.grid
        except AttributeError:
            return None

    def _screenbar(self, child: "DeckStackChild") -> "ScreenBar | None":
        # A screenbar is absent without a touchscreen or during construction;
        # every built ScreenBar already has its image.
        try:
            return child.page_settings.deck_config.screenbar
        except AttributeError:
            return None

    def _mirror_widget(self, child: "DeckStackChild", identifier: "InputIdentifier") -> "MirrorWidget[Any] | None":
        """Return the widget for an input, or None when none exists.

        Grid rebuilds can make coordinates temporarily short, so callers contain exceptions.
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
    def push_input_image(self, controller: "DeckController", identifier: "InputIdentifier", image: "Image.Image | None", latency_sample: "LatencySample | None" = None) -> "bool | ui_port.InputImageDropRecorded":
        try:
            if image is None or not self._window_mapped:
                return False
            child = self._children.get(controller)
            if child is None:
                return False
            widget = self._mirror_widget(child, identifier)
            if widget is None:
                return False

            # Offer raw frames so only the latest winner incurs conversion on
            # the widget that the drain resolves again.
            key = (controller, identifier)
            slot = self._mirror_slots.get(key)
            if slot is None:
                interval = (TOUCHSCREEN_UI_INTERVAL_S
                            if isinstance(identifier, Input.Touchscreen)
                            else KEY_UI_INTERVAL_S)
                slot = self._mirror_slots.setdefault(key, _MirrorSlot(interval))

            frame = _MirrorFrame(image, latency_sample)

            def record_superseded_drop(displaced: object) -> None:
                _discard_mirror_frame(controller, displaced, "ui_slot_superseded")

            delay_s = slot.offer(frame, on_superseded=record_superseded_drop)
            if delay_s is None:
                # A drain is already armed and now carries this frame.
                return True
            try:
                # Use idle priority for both arms; priority above GTK draw (120)
                # can starve redraw, and timeout_add otherwise defaults to 0.
                if delay_s <= 0:
                    GLib.idle_add(self._drain_mirror, controller, identifier)
                else:
                    GLib.timeout_add(int(delay_s * 1000) + 1, self._drain_mirror,
                                     controller, identifier,
                                     priority=GLib.PRIORITY_DEFAULT_IDLE)
            except Exception:
                # No callback drains this slot now, and an armed slot freezes
                # the input.
                _discard_mirror_frame(controller, slot.discard(), "ui_schedule_failed")
                mark_dirty(controller, identifier)
                log.warning(f"Could not schedule the {identifier} mirror frame")
                return ui_port.InputImageDropRecorded()
            except BaseException:
                # Clean the slot, but do not swallow SystemExit or KeyboardInterrupt.
                _discard_mirror_frame(controller, slot.discard(), "ui_schedule_failed")
                mark_dirty(controller, identifier)
                raise
            return True
        except Exception:
            # Contain lookup, teardown, and scheduling races here; the media
            # tick catch-all delays 0.25 s and must not throttle device writes.
            log.opt(exception=True).warning(f"Failed to mirror {identifier} into the UI")
            return False

    def _drain_mirror(self, controller: "DeckController", identifier: "InputIdentifier") -> bool:
        # Convert and paint the newest frame on the main loop; return False so
        # GLib does not re-arm the callback.
        slot = self._mirror_slots.get((controller, identifier))
        if slot is None:
            return False
        payload = slot.take()
        if payload is None:
            return False
        frame = payload if isinstance(payload, _MirrorFrame) else _MirrorFrame(payload, None)
        try:
            # Resolve after dequeue so a rebuilt grid does not receive a frame
            # intended for its orphaned predecessor.
            child = self._children.get(controller)
            if child is None:
                # Unbind landed after push; remove the slot so a dead controller
                # does not remain pinned by a racing replacement.
                orphaned = self._mirror_slots.pop((controller, identifier), None)
                if orphaned is not None:
                    _discard_mirror_frame(
                        controller, orphaned.discard(), "ui_unbound")
            widget = None if child is None else self._mirror_widget(child, identifier)
            if not self._window_mapped or widget is None:
                # Record an accepted frame that became unavailable after
                # push_input_image returned True; it was never converted.
                mark_dirty(controller, identifier)
                _discard_mirror_frame(controller, frame, "ui_unavailable")
                return False
            # offer records superseded drops synchronously; only its winning
            # frame reaches conversion, and one armed drain serves all pushes.
            widget.paint_mirror_frame(
                widget.prepare_mirror_frame(cast("Image.Image", frame.image)))
            tracker = getattr(controller, "input_latency", None)
            if tracker is not None:
                tracker.gtk_painted(frame.latency_sample)
        except Exception:
            log.opt(exception=True).warning(f"Failed to paint the {identifier} mirror")
            mark_dirty(controller, identifier)
            _discard_mirror_frame(controller, frame, "ui_paint_failed")
        return False

    # Deck sync

    @override
    def on_page_changed(self, controller: "DeckController") -> None:
        # Coalesce page-load completions into one idle that renders live state;
        # a trigger-thread race can queue at most two equivalent idles.
        if self._page_sync_queued.get(controller):
            return
        self._page_sync_queued[controller] = True
        GLib.idle_add(self._run_page_changed, controller)

    def _run_page_changed(self, controller: "DeckController") -> bool:
        # Pop the key because an idle queued before unbind must not reinsert and
        # pin an unplugged controller.
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
        # Do not refresh from the chooser, configurator, or error sub-view;
        # Sidebar.load_for_* would return to the input editor during an edit.
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
        """Return the sidebar only while it shows this controller input.

        Main-thread only because this method reads widgets.
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
        """Rebuild the key grid inline when rotation changes on the main loop.

        Delaying it lets page reloads target the old transposed grid and drop frames.
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
        # Queue removal before the slow close starts so a fast replug cannot add
        # a new child before a late detach and leave duplicate serials.
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
