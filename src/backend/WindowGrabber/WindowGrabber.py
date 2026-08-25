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
    """The integration that can grab windows in this session, or None when
    none can.

    environment_components holds the XDG_CURRENT_DESKTOP components, and this
    matches them one by one. The variable is a colon-separated list
    ("ubuntu:GNOME", "sway:wlroots:swayfx"). A comparison against the whole
    string leaves a stock distro session with no integration. Automatic page
    switching then does nothing.

    The order of the checks matters. The X11 session check sits above the KDE
    component, so a KDE session on Xorg reads windows through xprop and not
    through kdotool. It sits below the Wayland-only compositors, which have no
    X11 fallback.
    """
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
    """The wm-class and title patterns of one auto-change rule, or None when
    the rule holds neither and can match nothing.

    A pattern the rule does not carry matches every window, so the half the
    user filled in decides on its own. The page editor writes only the fields
    the user filled in, so a rule made by typing a title alone carries no
    wm-class key, and reading that as a pattern matching nothing left the rule
    dead.

    A rule holding neither pattern matches nothing at all. That is what a rule
    looks like while the user is still typing the first pattern, and while a
    field is cleared to be retyped. Reading it as one that matches every
    window hands the deck to the page being edited on the next window change,
    and the page it took over then keeps the deck, because the deck stays on
    a rule page that asks to stay.
    """
    wm_class = str(auto_change_settings.get("wm-class") or "")
    title = str(auto_change_settings.get("title") or "")
    if not wm_class and not title:
        return None
    return (wm_class or ".*", title or ".*")


class WindowGrabber:
    """Routes active-window changes onto the pages that ask for them.

    A watch on the active window costs real work. The X11 and KDE
    integrations poll their helper binary five subprocesses at a time every
    200 ms, and the Sway one runs swaymsg as often. The watch helps only while
    some page carries an enabled window auto-change rule, so a rule gates it.
    The watcher starts with the first rule and stops with the last one. A
    session that never uses the feature polls no windows at all.

    The integration builds on first use, not in the constructor, because it
    costs a helper-binary probe or a D-Bus proxy. A one-shot window query must
    also work while the watcher is off, because the user reaches the page
    editor's matching-window list before the first rule exists.

    A gate pass therefore blocks on subprocess probes, on a synchronous D-Bus
    proxy build, and on a watcher join, and the GTK main thread is one of its
    callers. Every gate pass runs on the background pool, one at a time.
    refresh_watch_state only records that a re-check is due, and one worker
    drains those requests. The state settles rather than updates at once. A
    request that arrives mid-pass gets a further pass, which re-reads the
    rules, so the settled state always matches the last write.
    """

    # Held for the per-deck decision of one routing, and never across a page
    # load. Two routings that overlap read and write the same two fields on a
    # deck, the one saying its page arrived automatically and the one holding
    # the page to go back to, and a pair of interleaved decisions loses the way
    # back to the page the user chose. A load marshals onto the GTK main
    # thread, so a lock held across one is a lock the main thread can wait for
    # while the load waits for the main thread.
    #
    # The lock sits on the class, so an instance holds it from the moment it
    # exists, and the app builds one grabber, so it serializes exactly the
    # decisions that can meet. A holder takes no other lock of this class, and
    # a gate transition may take it, never the reverse.
    _dispatch_lock = threading.RLock()

    # The page each deck is in the middle of loading by hand, held while that
    # load runs. A load marshals onto the GTK main thread, so the deck shows
    # the page it is leaving for the whole of it. An automatic switch that
    # lands in that window would read the page on the deck as the user's own
    # choice and write it down as the way back. An entry names the page the
    # deck is about to show, and the automatic path records that instead.
    #
    # An entry pairs the page with a token the claiming routing holds. Two
    # kinds of routing claim a deck here, the restore and a page picked by
    # hand, and one that retired the other's claim would take the guard off a
    # load still running. A claim retires on its own token only.
    #
    # Weak keys, so a deck torn down mid-load leaves nothing behind. The map
    # sits on the class for the same reason the lock above does, and every
    # holder of it takes that lock.
    _pending_manual_loads: "weakref.WeakKeyDictionary[Any, tuple[object, str]]" = weakref.WeakKeyDictionary()

    def __init__(self) -> None:
        self.environment_components: list[str] = desktop_components()
        self.server: str | None = session_type()

        self._integration_class = select_integration_class(self.environment_components, self.server)
        if self._integration_class is None:
            log.error(f"Unsupported environment: {self.environment_components} with server: {self.server} for window grabber.")
        else:
            log.info(f"Window grabber environment: {self.environment_components} under server: {self.server}")

        # Two locks. _transition_lock serializes a whole gate transition,
        # which reads the rules and then starts or stops, so two transitions
        # cannot interleave and leave the watcher out of step with the rules
        # on disk. _lock guards the fields only, and no holder keeps it across
        # the blocking part of a transition, so a reader (is_watching, a
        # one-shot window query) never waits behind a watcher join. Every
        # holder takes _transition_lock before _lock, and never the reverse.
        self._transition_lock = threading.RLock()
        self._lock = threading.RLock()
        self.integration: Integration | None = None
        self._watching = False

        # Set while no gate pass is queued or running. A caller waits on it to
        # see a settled decision. The app never waits, because the gate
        # settles on its own. A test that asserts that nothing started has no
        # other way to know the pass ended.
        self._gate_idle = threading.Event()
        self._gate_idle.set()
        self._gate_pending = False
        self._gate_running = False
        self._reset_requested = False

        # The re-check requests coalesce the same way, and for the same
        # reason: each one queries the desktop and can load a page, and the
        # page editor asks for one on every keystroke that lands.
        self._recheck_idle = threading.Event()
        self._recheck_idle.set()
        self._recheck_pending = False
        self._recheck_running = False

        self.refresh_watch_state()

    def _ensure_integration(self) -> Integration | None:
        """The integration for this session, built on first use.

        This contains a construction failure. An integration that fails to
        build leaves window grabbing inert, and does not abort the caller,
        which is a gate pass or a window query.
        """
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
        """Begins watching the active window. Idempotent.

        This blocks. It builds the integration when needed and starts its
        watcher. Call it off the GTK main thread; refresh_watch_state() is the
        route that guarantees that.
        """
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
        """Stops watching and reaps the watcher. Idempotent.

        This blocks. The reap joins the watcher within
        WATCHER_STOP_TIMEOUT_S. It holds _transition_lock across the join and
        never _lock, so the join stalls no reader.
        """
        with self._transition_lock:
            with self._lock:
                if not self._watching:
                    return
                self._watching = False
                integration = self.integration

            if integration is not None:
                integration.stop_watching()
            log.info("Stopped watching the active window: no page has a window auto-change rule")

            # Restore after the reap, so a watcher still mid-switch cannot
            # load a page over the restore. That holds while the join
            # succeeds. A watcher abandoned at the timeout inside a page load
            # can still land after the restore and strand that deck again.
            # This happens once and stays rare, because nothing feeds the
            # abandoned watcher further window changes.
            self._restore_auto_loaded_decks()

    def _restore_auto_loaded_decks(self) -> None:
        """Undoes the last automatic switch on every deck still showing one.

        Runs when the gate goes off. An automatically switched deck normally
        returns to its manually chosen page on the next window change that
        matches no rule. Once the last rule goes, no window change follows,
        and without this pass the deck stays on the auto-switched page until
        the user acts.
        """
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
        """Discards the integration so the next use builds a fresh one
        against the session as it stands now.

        The live case is an install of the GNOME shell extension mid-session.
        The existing integration built its D-Bus proxy while nothing owned
        that interface, and that proxy cannot start to report on its own. The
        gate pass afterwards leaves the rules to decide what watches.

        Returns at once, like refresh_watch_state and for the same reason. A
        discard stops and joins the watcher, and the caller here is a button
        handler on the GTK main thread.
        """
        with self._lock:
            self._reset_requested = True

        self.refresh_watch_state()

    def refresh_watch_state(self) -> None:
        """Queues a re-check of the watcher against the rules on disk.

        Returns at once. The pass itself blocks (see start_watching), and the
        callers are page writes, which run on the GTK main thread and on
        plugin threads. Requests coalesce. A re-check queued while one runs
        causes exactly one more pass, which re-reads the rules, so the settled
        state always matches the last write.
        """
        with self._lock:
            self._gate_pending = True
            self._gate_idle.clear()
            if self._gate_running:
                return
            self._gate_running = True

        try:
            run_in_background(self._drain_gate_requests)
        except Exception:
            # Nothing drains the request now, so take back the "a pass is
            # running" claim above. A flag left set makes every later re-gate
            # do nothing for the rest of the session. A dropped request costs
            # one stale decision until the next page write asks again. This
            # branch runs once the background pool shuts down, which is part
            # of quit, and it heals itself rather than trust that.
            with self._lock:
                self._gate_running = False
                self._gate_pending = False
                self._gate_idle.set()
            log.opt(exception=True).warning("Could not schedule a window watcher gate pass")

    def wait_for_gate(self, timeout: float = 10.0) -> bool:
        """Blocks until no gate pass is queued or running. False on timeout.

        This exists for a test that asserts on a settled decision, above all
        that nothing started, which a poll cannot establish. Production code
        never needs it.
        """
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
                # The drain loop must survive anything a pass throws. The
                # running flag otherwise stays set and kills the gate for the
                # rest of the session.
                log.opt(exception=True).error("A window watcher gate pass failed")

    def _apply_watch_state(self) -> None:
        """One gate pass. Read the rules, then match the watcher to them.

        Both steps run under _transition_lock, so no other transition slips
        between the question and the answer. A rule written after the read
        survives too. Its own refresh_watch_state() sets the pending flag, and
        the drain loop runs another pass.
        """
        with self._transition_lock:
            with self._lock:
                reset_requested = self._reset_requested
                self._reset_requested = False
            if reset_requested:
                # Rebuild against the session as it is now. Stop first, so the
                # reap takes the discarded integration's watcher and leaves no
                # thread that nothing references.
                self.stop_watching()
                with self._lock:
                    self.integration = None

            page_manager = gl.page_manager
            if page_manager is None:
                return

            try:
                wanted = page_manager.any_auto_change_rule_enabled()
            except Exception:
                # Leave the current state alone. A guess either way restarts
                # the polling that this gate removes, or kills a working
                # auto-change.
                log.opt(exception=True).warning("Could not determine whether any window auto-change rule is enabled")
                return

            if wanted:
                self.start_watching()
            else:
                self.stop_watching()

    @log.catch
    def get_all_windows(self) -> list[Window]:
        """
        returns a list of [wm_class, title] lists

        This blocks. It builds the integration on first use and then queries
        the desktop. The query runs subprocesses on most desktops. A caller on
        the GTK main thread must therefore marshal it off.
        """
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
        """Queues one pass of the rules over the window in front right now.

        Returns at once; the pass runs on the background pool. A rule edit
        otherwise waits for the next window change, so a rule typed for the
        window the user has in front reads as dead until something else takes
        focus. The page editor asks for a re-check after every rule edit.

        The pass stays off the GTK main thread twice over: the query for the
        front window runs subprocesses on most desktops, and a match loads a
        page. Requests coalesce, like the gate's: one queued while a pass runs
        causes exactly one more pass, which re-reads the rules.
        """
        with self._lock:
            self._recheck_pending = True
            self._recheck_idle.clear()
            if self._recheck_running:
                return
            self._recheck_running = True

        try:
            run_in_background(self._drain_recheck_requests)
        except Exception:
            # Nothing drains the request now, so take back the claim above. A
            # flag left set makes every later re-check do nothing for the rest
            # of the session. This branch runs once the background pool shuts
            # down, which is part of quit, and the rule still applies at the
            # next window change.
            with self._lock:
                self._recheck_running = False
                self._recheck_pending = False
                self._recheck_idle.set()
            log.debug("Could not schedule an active window re-check")

    def wait_for_recheck(self, timeout: float = 10.0) -> bool:
        """Blocks until no re-check is queued or running. False on timeout.

        This exists for a test that asserts on a settled decision, above all
        that nothing queried the desktop. Production code never needs it.
        """
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
                # The drain loop must survive anything a pass throws. The
                # running flag otherwise stays set and kills every later
                # re-check.
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
            # The same gate the watcher takes. With no rule anywhere there is
            # nothing to apply, and going on would build the integration,
            # probe the desktop and publish a foreground window that the API
            # reports only while a rule asks for it.
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
        """Routes a window reported from outside this process.

        Returns at once; the routing runs on the background pool. The caller
        is the D-Bus method that lets a test or a development run stand in for
        the desktop, and D-Bus hands that call to the GTK main thread. A
        routing loads a page, which marshals back onto that same thread, so a
        routing that ran there would wait for itself.
        """
        try:
            run_in_background(self.on_active_window_changed, window)
        except Exception:
            # The background pool is gone, which is part of quit.
            log.debug("Could not route a reported active window")

    def on_active_window_changed(self, window: Window) -> None:
        # log.info(f"Active window changed to: {window}")

        # Tell the D-Bus API about the foreground window change.
        notify_foreground_window_changed(window.title, window.wm_class)

        if gl.deck_manager is None:
            return

        for deck_controller in gl.deck_manager.deck_controller:
            # Skip a closed or disabled deck. A return here would abort auto
            # page switching for every remaining deck as soon as one disabled
            # deck's page regex matched.
            if deck_controller is None or not deck_controller.deck.is_open():
                continue

            try:
                self._apply_auto_change(deck_controller, window)
            except Exception:
                # One deck can fail mid-switch, when a concurrent close()
                # flips is_open() after the check above passed. That must not
                # abort auto-switching for the remaining decks. The watcher
                # threads wrap their loops in @log.catch, so an exception that
                # escapes here kills auto-switching until the next app start.
                log.opt(exception=True).warning(
                    "Auto page switch failed for one deck; continuing with the others"
                )

    def _apply_auto_change(self, deck_controller: "DeckController", window: Window) -> None:
        """Applies the auto-change page rules to a single deck for the given
        foreground window. It can raise when the deck is torn down mid-call,
        and the caller isolates that per deck."""
        page_manager = gl.page_manager
        if page_manager is None:
            return

        if deck_controller.active_page is None:
            # A deck still starting up, or mid-hotplug, has no page yet.
            # Nothing exists to compare or restore, so skip the deck instead
            # of reading active_page.json_path.
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
            # The page went away between the rule read and the load. A None
            # handed to load_page would clear the deck, so the deck keeps the
            # page it shows and the reason goes to the log: no surface exists
            # to tell the user about an automatic switch.
            log.error(f"Auto page change skipped: {matched_path} did not load")
            return

        if not self._claim_auto_page(deck_controller, claimed_page, matched_path):
            return
        # The load runs outside the decision, and a second routing that decided
        # on the same page reaches this too. allow_reload keeps the deck from
        # building the page it already shows a second time: the page manager
        # answers both routings with the one cached page object for this deck.
        deck_controller.load_page(page, allow_reload=False)

    def _auto_page_to_leave(self, deck_controller: "DeckController", page_path: str) -> "Page | None":
        """Answers the page this automatic switch takes the deck off. None
        where the deck already shows page_path or has no page at all.

        This reads the routing state and writes none of it, because the page
        build that follows can fail. A deck marked as automatically loaded
        before that build sits on a page the user chose while it says the page
        arrived on its own, and the page it remembers going back to is that
        very page.
        """
        with self._dispatch_lock:
            active_page = deck_controller.active_page
            if active_page is None:
                return None
            if self._page_the_deck_will_show(deck_controller, active_page) == page_path:
                if deck_controller not in self._pending_manual_loads:
                    # The deck already shows the matched page, and no page
                    # build follows that could fail in between. The page it
                    # shows answers to a rule, so it is marked here, and a
                    # later window change can take the deck off it. A deck
                    # loading a page the user picked is left alone: that page
                    # is their choice even where a rule names it too.
                    deck_controller.page_auto_loaded = True
                return None
            return active_page

    def _claim_auto_page(self, deck_controller: "DeckController", active_page: "Page",
                         page_path: str) -> bool:
        """Records that this deck's page arrived automatically, and answers
        whether this routing still owns the switch.

        This is the whole of the decision that two routings must not interleave.
        A deck taken off its manual page has to remember which page that was,
        and two routings reading that flag at once lose the way back. The
        conditions are read again here because the page build in between runs
        outside the lock.

        It holds the routing lock and does no work of its own. The load stays
        outside, because a page load marshals onto the GTK main thread and
        waits there. A lock held across it is a lock the main thread can be
        waiting for, and then neither side moves until the marshal times out.
        """
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
        """Wraps a page load the user asked for by hand on this deck.

        Nothing outside this class clears the flag that says the deck's page
        arrived automatically. A deck left marked that way after the user
        picks a page of their own goes back to a page they already left the
        next time no rule matches, so the pick clears the flag here.

        The claim stands for the length of the load, which is why this is a
        context manager: the deck shows the page it is leaving until the load
        lands, and an automatic switch in that window must record the picked
        page as the way back, not the one still on the deck.

        A load that raises, such as one on a deck torn down mid-call, leaves
        the deck on the page it already showed. The mark goes back to what it
        was, because a deck left on an automatic page with the mark off has
        the next automatic switch write that page down as the user's choice.
        """
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
        """Returns one deck to its last manually loaded page.

        This applies if the page it shows got there by an automatic switch
        and does not ask to stay.

        Two paths reach this. No rule matches the current window, or the last
        rule goes away and turns the gate off, which leaves no further window
        change to carry the restore. Both paths apply the same conditions, so
        the two cannot drift apart.
        """
        page_manager = gl.page_manager
        if page_manager is None:
            return

        # A deck mid-startup or mid-hotplug has no page to restore from, and
        # a deck that was never auto-switched has nothing to undo. Both are
        # read again inside the claim; this pair only keeps the settings read
        # below off the path for a deck that plainly has nothing to restore.
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
            # The user deleted the manually chosen page. Nothing remains to go
            # back to, and a load of None takes the deck's page away. The deck
            # keeps the automatic flag and the remembered path, so a later
            # window change tries the restore again, and an automatic switch
            # from here does not mistake the page on the deck for a manual
            # choice and overwrite the remembered path with it.
            log.error(f"Manual page restore skipped: {manual_path} did not load")
            return

        token = self._claim_manual_page(deck_controller, active_page, manual_path)
        if token is None:
            return
        try:
            deck_controller.load_page(page, allow_reload=False)
        except BaseException:
            # The load did not land, such as on a deck torn down mid-call, so
            # the deck keeps the automatic page. It carries the mark again,
            # because a later window change is what retries the restore, and
            # an automatic switch here must not read that page as a choice
            # the user made.
            with self._dispatch_lock:
                deck_controller.page_auto_loaded = True
            raise
        finally:
            # The claim stands only while the load runs. It is what keeps an
            # automatic switch in that window off the remembered page.
            self._end_manual_load(deck_controller, token)

    def _manual_page_to_restore(self, deck_controller: "DeckController", active_page: "Page") -> str | None:
        """Answers the page this deck goes back to. None when there is nothing
        to undo.

        This reads the routing state and writes none of it. The build that
        follows can fail, and a deck marked as no longer automatic before that
        build has no way back: no later window change retries the restore, and
        the next automatic switch reads the page on the deck as a manual choice
        and forgets the real one.
        """
        with self._dispatch_lock:
            if not self._restore_still_owned(deck_controller, active_page):
                return None
            return deck_controller.last_manual_loaded_page_path

    def _claim_manual_page(self, deck_controller: "DeckController", active_page: "Page",
                           manual_path: str) -> object | None:
        """Marks the deck as no longer automatically loaded, and answers the
        token that retires the claim. None where this routing no longer owns
        the restore.

        The same decision as an automatic switch, and it holds the routing lock
        for the same reason: the flag it reads is the flag it writes, and two
        routings that both read it before either writes both undo the switch.
        The conditions are read again here because the page build in between
        runs outside the lock. The load stays outside the lock too, because it
        marshals onto the GTK main thread. The claim on the page being restored
        is recorded in the same hold as the flag it clears: between the two, a
        deck showing an automatic page carries no mark of it, and an automatic
        switch there would take that page for the user's own choice.
        """
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
            # Another routing moved the deck after the settings above were
            # read, so those settings describe a page the deck has left. That
            # routing owns the decision now.
            return False
        return deck_controller.last_manual_loaded_page_path is not None
