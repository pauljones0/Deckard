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
import threading

from gi.repository import GLib

from src.backend.session_info import desktop_components
from src.backend.LockScreenManager.Detectors.Gnome import GnomeLockScreenDetector
from src.backend.LockScreenManager.Detectors.Cinnamon import CinnamonLockScreenDetector
from src.backend.LockScreenManager.Detectors.KDE import KDELockScreenDetector
from src.backend.LockScreenManager.Detectors.Hyprland import HyprlandLockScreenDetector
from src.backend.LockScreenManager.Detectors.Logind import LogindLockScreenDetector
from loguru import logger as log

import globals as gl

class LockScreenManager:
    def __init__(self) -> None:
        self.locked = False
        self.detector = None

        threading.Thread(target=self.setup, daemon=True).start() # Detector setup can block, so keep it off the caller's thread

    @log.catch
    def setup(self) -> None:
        # XDG_CURRENT_DESKTOP holds a colon-separated list ("ubuntu:GNOME",
        # "GNOME-Classic:GNOME"), so match one component. A match on the whole
        # string misses these sessions and lock detection never starts.
        env_components = desktop_components()
        if "gnome" in env_components:
            self.detector = GnomeLockScreenDetector(self)
        elif "x-cinnamon" in env_components:
            self.detector = CinnamonLockScreenDetector(self)
        elif "kde" in env_components:
            self.detector = KDELockScreenDetector(self)
        elif "hyprland" in env_components:
            self.detector = HyprlandLockScreenDetector(self)
        else:
            self.detector = LogindLockScreenDetector(self)

        # The detectors above only wire up the signal. A session already
        # locked when the app starts sends no lock signal, so read the
        # current state once here and lock() at once when it reads locked.
        # Decks enumerated after this read the seeded gl.screen_locked at init
        # and come up on the screen saver; decks already enumerated get locked
        # by the lock() call. The read runs here, on the setup daemon thread,
        # so a slow bus never holds app startup.
        self.detector.read_initial_lock_state()

    @log.catch
    def lock(self, active: bool, initial: bool = False) -> None:
        """Apply a lock change.

        initial marks the one-shot startup read, which runs on the setup
        daemon thread. Its deck work goes to the main loop, because the deck
        loop below touches the screen saver and the interaction flag, which
        are main-thread surfaces. The signal-driven path leaves initial False:
        GDBus dispatches those callbacks on the main loop already.
        """
        gl.screen_locked = active
        if gl.presence_monitor:
            # Tell the monitor before the screensaver work below reads the
            # lock. Catch here, because this method's @log.catch returns on an
            # exception, which stops the lock from reaching allow_interaction,
            # the screensaver, and self.locked.
            try:
                gl.presence_monitor.on_lock_changed(active)
            except Exception:
                log.opt(exception=True).warning(
                    "LockScreenManager: the presence monitor failed to handle a "
                    "lock change; continuing with the lock itself"
                )

        if active:
            if not gl.settings_manager.app().lock_on_lock_screen:
                return

        if active == self.locked:
            return
        
        log.info(f"Locking screen: {active}")

        # Commit the tracked state at the transition, before the deck loop can
        # return early. The startup initial-state read locks while no deck
        # manager exists yet; the guard below then returns without deck work.
        # Setting self.locked here keeps it in step with gl.screen_locked, so
        # the first real unlock is not read as a no-op and its deck loop runs
        # on the decks that enumerated after the read.
        self.locked = active

        if initial:
            # The startup read runs on the setup daemon thread. A deck
            # manager that already exists by then would take the deck loop
            # off the main thread, so queue it instead. The loop runs when
            # the main loop next idles, whether or not it pumps yet. The
            # idle re-reads self.locked when it fires: a real lock change
            # can land between the queue and the fire (signals dispatch at a
            # higher priority than idles), and applying the captured startup
            # state would overwrite the newer one.
            GLib.idle_add(lambda: self._apply_to_decks(self.locked))
            return

        self._apply_to_decks(active)

    @log.catch
    def _apply_to_decks(self, active: bool) -> bool:
        """Show or hide the screen saver on every deck. Main thread only.

        Returns GLib.SOURCE_REMOVE, so it also serves as a one-shot idle
        callback.
        """
        deck_manager = gl.deck_manager
        if deck_manager is None:
            return GLib.SOURCE_REMOVE
        for controller in deck_manager.deck_controller:
            controller.allow_interaction = not active
            if active:
                controller.screen_saver.show()
            else:
                controller.screen_saver.hide()
        return GLib.SOURCE_REMOVE
