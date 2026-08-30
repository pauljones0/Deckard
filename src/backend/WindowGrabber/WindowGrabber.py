"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import re
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TYPE_CHECKING

from loguru import logger as log

import globals as gl

from src.backend.main_loop import run_in_background
from src.backend.session_info import desktop_components, session_type
from src.backend.WindowGrabber.Window import Window
from src.backend.WindowGrabber.Integration import Integration

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.PageManagement.Page import Page
from src.backend.WindowGrabber.Integrations.Hyprland import Hyprland
from src.backend.WindowGrabber.Integrations.Gnome import Gnome
from src.backend.WindowGrabber.Integrations.Sway import Sway
from src.backend.WindowGrabber.Integrations.X11 import X11
from src.backend.WindowGrabber.Integrations.KDE import KDE
from src.api import notify_foreground_window_changed


def select_integration_class(environment_components: list[str], server: str | None) -> type[Integration] | None:
    """Select a window integration from individual XDG_CURRENT_DESKTOP components, or return None.
    Wayland compositors take priority; X11 precedes KDE so KDE on Xorg uses X11 rather than kdotool."""
    if "hyprland" in environment_components:
        return Hyprland
    if "gnome" in environment_components:
        return Gnome
    # A Sway fork names itself in the same component ("swayfx") and speaks
    # the same IPC.
    if any("sway" in component for component in environment_components):
        return Sway
    if server == "x11":
        return X11
    if "kde" in environment_components:
        return KDE
    return None


def rule_patterns(auto_change_settings: dict[str, Any]) -> tuple[str, str] | None:
    """Return a rule's class and title patterns, using a wildcard for one missing field.
    Return None when both are absent so an incomplete or cleared rule cannot match every window."""
    wm_class = str(auto_change_settings.get("wm-class") or "")
    title = str(auto_change_settings.get("title") or "")
    if not wm_class and not title:
        return None
    return (wm_class or ".*", title or ".*")


class WindowGrabber:
    """Route active windows to matching pages, with a lazy integration and a watcher gated by enabled rules.
    Blocking queries, construction, joins, and page loads use coalesced background passes; one-shot queries remain available while watching is off."""

    # Serialize each deck's auto/manual routing decision, but never hold this lock across a main-thread page load.
    # The class lock predates instances; holders take no other class lock, while gate transitions can take this one.
    _dispatch_lock = threading.RLock()

    # Track each in-flight manual destination so overlapping automatic routing records the page the user selected.
    # Weak keys discard torn-down decks; unique tokens prevent one load from retiring another's claim under _dispatch_lock.
    _pending_manual_loads: "weakref.WeakKeyDictionary[Any, tuple[object, str]]" = weakref.WeakKeyDictionary()

    def __init__(self) -> None:
        self.environment_components: list[str] = desktop_components()
        self.server: str | None = session_type()

        self._integration_class = select_integration_class(self.environment_components, self.server)
        if self._integration_class is None:
            log.error(f"Unsupported environment: {self.environment_components} with server: {self.server} for window grabber.")
        else:
            log.info(f"Window grabber environment: {self.environment_components} under server: {self.server}")

        # _transition_lock serializes rule reads with watcher transitions; _lock guards fields only, never blocking joins.
        # Code that needs both takes _transition_lock before _lock and never the reverse.
        self._transition_lock = threading.RLock()
        self._lock = threading.RLock()
        self.integration: Integration | None = None
        self._watching = False

        # Set when no gate pass is queued or running.
        # Production does not wait; tests use it to prove a settled no-start decision.
        self._gate_idle = threading.Event()
        self._gate_idle.set()
        self._gate_pending = False
        self._gate_running = False
        self._reset_requested = False

        # Coalesce rechecks because each can query the desktop and load a page.
        # The page editor can request one after every accepted keystroke.
        self._recheck_idle = threading.Event()
        self._recheck_idle.set()
        self._recheck_pending = False
        self._recheck_running = False

        self.refresh_watch_state()

    def _ensure_integration(self) -> Integration | None:
        """Return the session integration, built on first use.
        Construction failure leaves window grabbing inert without aborting a gate pass or query."""
        with self._lock:
            if self.integration is None and self._integration_class is not None:
                try:
                    self.integration = self._integration_class(self)
                except Exception:
                    log.opt(exception=True).error("Could not initialize the window grabber integration")
                    return None
            return self.integration

    @property
    def is_watching(self) -> bool:
        with self._lock:
            return self._watching

    def start_watching(self) -> None:
        """Build the integration if needed and begin watching; repeated calls are inert.
        This blocks and must run off the GTK thread through refresh_watch_state."""
        with self._transition_lock:
            with self._lock:
                if self._watching:
                    return
            integration = self._ensure_integration()
            if integration is None:
                return
            integration.start_watching()
            with self._lock:
                self._watching = True
            log.info("Watching the active window: a page has a window auto-change rule")

    def stop_watching(self) -> None:
        """Stop and reap the watcher within WATCHER_STOP_TIMEOUT_S; repeated calls are inert.
        This blocks under _transition_lock but not _lock, so readers do not wait behind the join."""
        with self._transition_lock:
            with self._lock:
                if not self._watching:
                    return
                self._watching = False
                integration = self.integration

            if integration is not None:
                integration.stop_watching()
            log.info("Stopped watching the active window: no page has a window auto-change rule")

            # Restore after reap so a joined watcher cannot load over it.
            # A watcher abandoned during a timed-out page load can still land once, but receives no further changes.
            self._restore_auto_loaded_decks()

    def _restore_auto_loaded_decks(self) -> None:
        """Undo the last automatic switch on every deck still showing one.
        Run when the final rule disables the gate because no later window change can trigger restoration."""
        deck_manager = gl.deck_manager
        if deck_manager is None:
            return

        for deck_controller in deck_manager.deck_controller:
            if deck_controller is None or not deck_controller.deck.is_open():
                continue
            try:
                self._restore_manual_page(deck_controller)
            except Exception:
                # The same isolation as the per-deck routing catch. One deck
                # torn down mid-restore must not strand the others.
                log.opt(exception=True).warning(
                    "Could not restore the manually loaded page for one deck; continuing with the others"
                )

    def reset_integration(self) -> None:
        """Queue disposal of the integration so the next use reflects the current session, including a newly installed GNOME extension.
        Return immediately because disposal can stop and join a watcher while the caller is on the GTK thread."""
        with self._lock:
            self._reset_requested = True

        self.refresh_watch_state()

    def refresh_watch_state(self) -> None:
        """Queue a coalesced watcher recheck against rules on disk and return immediately.
        Blocking passes run off caller threads; a request during a pass causes one more read of the latest rules."""
        with self._lock:
            self._gate_pending = True
            self._gate_idle.clear()
            if self._gate_running:
                return
            self._gate_running = True

        try:
            run_in_background(self._drain_gate_requests)
        except Exception:
            # Scheduling failure leaves no drain, so clear the running and pending claims.
            # A dropped request leaves one stale decision until another page write; shutdown commonly causes this path.
            with self._lock:
                self._gate_running = False
                self._gate_pending = False
                self._gate_idle.set()
            log.opt(exception=True).warning("Could not schedule a window watcher gate pass")

    def wait_for_gate(self, timeout: float = 10.0) -> bool:
        """Block until no gate pass is queued or running, or return false on timeout.
        Tests use this to prove a settled no-start decision; production does not wait."""
        return self._gate_idle.wait(timeout)

    def _drain_gate_requests(self) -> None:
        while True:
            with self._lock:
                if not self._gate_pending:
                    self._gate_running = False
                    self._gate_idle.set()
                    return
                self._gate_pending = False

            try:
                self._apply_watch_state()
            except Exception:
                # Keep draining after a failed pass.
                # An escaping exception would leave the running flag set and disable later gates.
                log.opt(exception=True).error("A window watcher gate pass failed")

    def _apply_watch_state(self) -> None:
        """Read rules and match the watcher to them under _transition_lock.
        A rule written after the read queues another pass, which sees the new state."""
        with self._transition_lock:
            with self._lock:
                reset_requested = self._reset_requested
                self._reset_requested = False
            if reset_requested:
                # Stop before discarding the integration.
                # The reap then leaves no unreferenced watcher thread.
                self.stop_watching()
                with self._lock:
                    self.integration = None

            page_manager = gl.page_manager
            if page_manager is None:
                return

            try:
                wanted = page_manager.any_auto_change_rule_enabled()
            except Exception:
                # Keep the current state when rules cannot be read.
                # Guessing can start unwanted work or stop a working auto-change.
                log.opt(exception=True).warning("Could not determine whether any window auto-change rule is enabled")
                return

            if wanted:
                self.start_watching()
            else:
                self.stop_watching()

    @log.catch
    def get_all_windows(self) -> list[Window]:
        """Return all visible windows after building the integration if needed.
        This can run desktop subprocesses, so GTK-thread callers must marshal it off-thread."""
        integration = self._ensure_integration()
        if integration is None:
            return []

        return integration.get_all_windows()

    def get_all_matching_windows(self, class_regex: str, title_regex: str) -> list[Window]:
        all_windows = self.get_all_windows()

        matching_windows: list[Window] = []
        for window in all_windows:
            if self.get_is_window_matching(window, class_regex, title_regex):
                matching_windows.append(window)

        return matching_windows

    def get_is_window_matching(self, window: Window, class_regex: str | None, title_regex: str | None) -> bool:
        wm_class = window.wm_class
        title = window.title
        if wm_class is None or title is None or class_regex is None or title_regex is None:
            return False
        try:
            class_match = re.search(class_regex, wm_class, re.IGNORECASE)
            title_match = re.search(title_regex, title, re.IGNORECASE)
        except re.error:
            return False
        return bool(class_match and title_match)

    def recheck_active_window(self) -> None:
        """Queue a coalesced background pass of current rules over the foreground window and return immediately.
        Recheck after edits without waiting for focus change; desktop queries and matching page loads must stay off GTK."""
        with self._lock:
            self._recheck_pending = True
            self._recheck_idle.clear()
            if self._recheck_running:
                return
            self._recheck_running = True

        try:
            run_in_background(self._drain_recheck_requests)
        except Exception:
            # Scheduling failure leaves no drain, so clear the running and pending claims.
            # Shutdown commonly causes this path; the rule can still apply at the next window change.
            with self._lock:
                self._recheck_running = False
                self._recheck_pending = False
                self._recheck_idle.set()
            log.debug("Could not schedule an active window re-check")

    def wait_for_recheck(self, timeout: float = 10.0) -> bool:
        """Block until no recheck is queued or running, or return false on timeout.
        Tests use this to prove no desktop query occurred; production does not wait."""
        return self._recheck_idle.wait(timeout)

    def _drain_recheck_requests(self) -> None:
        while True:
            with self._lock:
                if not self._recheck_pending:
                    self._recheck_running = False
                    self._recheck_idle.set()
                    return
                self._recheck_pending = False

            try:
                self._recheck_active_window()
            except Exception:
                # Keep draining after a failed pass.
                # An escaping exception would leave the running flag set and disable later rechecks.
                log.opt(exception=True).error("An active window re-check failed")

    def _recheck_active_window(self) -> None:
        page_manager = gl.page_manager
        if page_manager is None:
            return

        try:
            wanted = page_manager.any_auto_change_rule_enabled()
        except Exception:
            log.opt(exception=True).warning("Could not determine whether any window auto-change rule is enabled")
            return

        if not wanted:
            # Use the watcher gate: no enabled rule means no desktop probe or integration build.
            # The API publishes foreground windows only while a rule requests them.
            return

        integration = self._ensure_integration()
        if integration is None:
            return

        window = integration.get_active_window()
        if window is None:
            # This session has no way to name the front window, or nothing is
            # in front. Neither leaves anything to match the rules against.
            return

        self.on_active_window_changed(window)

    def report_active_window(self, window: Window) -> None:
        """Route an externally reported window on the background pool and return immediately.
        D-Bus invokes this on GTK, while routing loads through GTK; inline routing would wait for itself."""
        try:
            run_in_background(self.on_active_window_changed, window)
        except Exception:
            # The background pool is gone, which is part of quit.
            log.debug("Could not route a reported active window")

    def on_active_window_changed(self, window: Window) -> None:
        # log.info(f"Active window changed to: {window}")

        notify_foreground_window_changed(window.title, window.wm_class)

        if gl.deck_manager is None:
            return

        for deck_controller in gl.deck_manager.deck_controller:
            # Skip each closed or disabled deck without aborting routing for later decks.
            # A return would stop all remaining auto-switches.
            if deck_controller is None or not deck_controller.deck.is_open():
                continue

            try:
                self._apply_auto_change(deck_controller, window)
            except Exception:
                # A concurrent close can fail one deck after the open check.
                # Isolate it so later decks continue and the watcher loop remains alive.
                log.opt(exception=True).warning(
                    "Auto page switch failed for one deck; continuing with the others"
                )

    def _apply_auto_change(self, deck_controller: "DeckController", window: Window) -> None:
        """Apply foreground-window page rules to one deck.
        Teardown can raise during the call, so the caller isolates each deck."""
        page_manager = gl.page_manager
        if page_manager is None:
            return

        if deck_controller.active_page is None:
            # A starting or hotplugging deck has no page to compare or restore.
            # Skip it instead of reading active_page.json_path.
            return

        matched_path: str | None = None
        for page_path in page_manager.get_pages():
            info = page_manager.get_auto_change_settings(page_path)
            enabled = info.get("enable", False)
            decks = info.get("decks", [])
            if not enabled:
                continue

            patterns = rule_patterns(info)
            if patterns is None:
                continue
            wm_regex, title_regex = patterns

            if self.get_is_window_matching(window, wm_regex, title_regex):
                if deck_controller.serial_number() not in decks:
                    continue
                matched_path = page_path
                break

        if matched_path is None:
            self._restore_manual_page(deck_controller)
            return

        claimed_page = self._auto_page_to_leave(deck_controller, matched_path)
        if claimed_page is None:
            return

        log.debug(f"Auto changing page: {matched_path} on deck {deck_controller.deck.get_serial_number()}")
        page = page_manager.get_page(matched_path, deck_controller)
        if page is None:
            # The page disappeared after the rule read; passing None to load_page would clear the deck.
            # Keep the current page and log because automatic switching has no user-facing error surface.
            log.error(f"Auto page change skipped: {matched_path} did not load")
            return

        if not self._claim_auto_page(deck_controller, claimed_page, matched_path):
            return
        # Load outside the decision lock; overlapping routes can select the same cached page object.
        # allow_reload=False prevents rebuilding a page the deck already shows.
        deck_controller.load_page(page, allow_reload=False)

    def _auto_page_to_leave(self, deck_controller: "DeckController", page_path: str) -> "Page | None":
        """Return the page an automatic switch leaves, or None if the deck has no page or will show page_path.
        This is read-only because the next page build can fail; ownership changes only after a successful build."""
        with self._dispatch_lock:
            active_page = deck_controller.active_page
            if active_page is None:
                return None
            if self._page_the_deck_will_show(deck_controller, active_page) == page_path:
                if deck_controller not in self._pending_manual_loads:
                    # Mark an already-shown matched page as automatic because no page build can fail first.
                    # Do not mark an in-flight manual choice, even when a rule also names it.
                    deck_controller.page_auto_loaded = True
                return None
            return active_page

    def _claim_auto_page(self, deck_controller: "DeckController", active_page: "Page",
                         page_path: str) -> bool:
        """Atomically claim an automatic switch and preserve its manual return page after rechecking post-build state.
        Perform no page load under the routing lock because loads marshal to GTK and can deadlock with its waiter."""
        with self._dispatch_lock:
            if deck_controller.active_page is not active_page:
                # Another routing moved the deck while this page built, so it
                # owns the decision now.
                return False
            pending_manual = self._pending_manual_path(deck_controller)
            if self._page_the_deck_will_show(deck_controller, active_page) == page_path:
                return False

            if pending_manual is not None:
                # A page picked by hand is loading onto this deck. It, not the
                # page still on the deck, is the choice to come back to.
                deck_controller.last_manual_loaded_page_path = pending_manual
            elif not deck_controller.page_auto_loaded:
                deck_controller.last_manual_loaded_page_path = active_page.json_path
            deck_controller.page_auto_loaded = True
            return True

    def _page_the_deck_will_show(self, deck_controller: "DeckController",
                                 active_page: "Page") -> str:
        """The page path this deck settles on once the loads in flight land.
        Call under the routing lock."""
        pending_manual = self._pending_manual_path(deck_controller)
        if pending_manual is not None:
            return pending_manual
        return active_page.json_path

    def _pending_manual_path(self, deck_controller: "DeckController") -> str | None:
        """The page a load by hand is bringing to this deck, or None.
        Call under the routing lock."""
        claim = self._pending_manual_loads.get(deck_controller)
        if claim is None:
            return None
        return claim[1]

    def _begin_manual_load(self, deck_controller: "DeckController", page_path: str) -> object:
        """Claims this deck for a load by hand and answers the token that
        retires the claim. Call under the routing lock."""
        token = object()
        self._pending_manual_loads[deck_controller] = (token, page_path)
        return token

    def _end_manual_load(self, deck_controller: "DeckController", token: object) -> None:
        """Retires a claim this routing made. A claim another routing has
        since made on the same deck stands: its load is still running."""
        with self._dispatch_lock:
            claim = self._pending_manual_loads.get(deck_controller)
            if claim is not None and claim[0] is token:
                del self._pending_manual_loads[deck_controller]

    @contextmanager
    def manual_page_load(self, deck_controller: "DeckController", page_path: str) -> Iterator[None]:
        """Wrap a manual load, clear its automatic mark, and claim the destination until the load ends.
        Overlapping routing uses that destination as the return page; a failed load restores a prior automatic mark."""
        with self._dispatch_lock:
            was_auto_loaded = getattr(deck_controller, "page_auto_loaded", False)
            deck_controller.page_auto_loaded = False
            token = self._begin_manual_load(deck_controller, page_path)
        try:
            yield
        except BaseException:
            if was_auto_loaded:
                # Only ever back to the mark, never off one: an automatic
                # switch that ran inside the load owns the mark it set.
                with self._dispatch_lock:
                    deck_controller.page_auto_loaded = True
            raise
        finally:
            self._end_manual_load(deck_controller, token)

    def _restore_manual_page(self, deck_controller: "DeckController") -> None:
        """Return an automatically switched deck to its last manual page unless the current page asks to stay.
        Use the same conditions after no rule matches and after the final rule disables the watcher."""
        page_manager = gl.page_manager
        if page_manager is None:
            return

        # Skip settings reads for a starting deck with no page or one that was never auto-switched.
        # The later claim rechecks both conditions before changing routing state.
        active_page = deck_controller.active_page
        if active_page is None:
            return
        if not getattr(deck_controller, "page_auto_loaded", False):
            return

        active_page_change_info = page_manager.get_auto_change_settings(active_page.json_path)
        if active_page_change_info.get("stay-on-page", True):
            return

        manual_path = self._manual_page_to_restore(deck_controller, active_page)
        if manual_path is None:
            return

        page = page_manager.get_page(manual_path, deck_controller)
        if page is None:
            # A deleted manual page cannot be loaded because None would clear the deck.
            # Keep its path and automatic mark for retries and to prevent overwriting the remembered choice.
            log.error(f"Manual page restore skipped: {manual_path} did not load")
            return

        token = self._claim_manual_page(deck_controller, active_page, manual_path)
        if token is None:
            return
        try:
            deck_controller.load_page(page, allow_reload=False)
        except BaseException:
            # A failed load leaves the automatic page, so restore its mark for a later retry.
            # This also prevents another automatic switch from treating that page as a manual choice.
            with self._dispatch_lock:
                deck_controller.page_auto_loaded = True
            raise
        finally:
            # The claim stands only while the load runs. It is what keeps an
            # automatic switch in that window off the remembered page.
            self._end_manual_load(deck_controller, token)

    def _manual_page_to_restore(self, deck_controller: "DeckController", active_page: "Page") -> str | None:
        """Return the manual page to restore, or None when this routing owns no restore.
        This stays read-only until the page build succeeds so failure remains retryable and preserves the real manual choice."""
        with self._dispatch_lock:
            if not self._restore_still_owned(deck_controller, active_page):
                return None
            return deck_controller.last_manual_loaded_page_path

    def _claim_manual_page(self, deck_controller: "DeckController", active_page: "Page",
                           manual_path: str) -> object | None:
        """Atomically clear the automatic mark and claim the manual destination if this routing still owns restoration.
        Recheck after the unlocked build, and keep the GTK-marshalled load outside while the claim protects the manual choice."""
        with self._dispatch_lock:
            if not self._restore_still_owned(deck_controller, active_page):
                return None
            deck_controller.page_auto_loaded = False
            return self._begin_manual_load(deck_controller, manual_path)

    def _restore_still_owned(self, deck_controller: "DeckController", active_page: "Page") -> bool:
        """Answers whether a restore off active_page is this routing's to make.
        Call under the routing lock."""
        if not getattr(deck_controller, "page_auto_loaded", False):
            return False
        if deck_controller.active_page is not active_page:
            # Another routing moved the deck after the settings read.
            # Those settings describe an old page, so the newer routing owns the decision.
            return False
        return deck_controller.last_manual_loaded_page_path is not None
