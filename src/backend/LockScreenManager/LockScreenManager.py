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
        # Match each colon-separated XDG_CURRENT_DESKTOP component so values
        # such as ubuntu:GNOME and GNOME-Classic:GNOME start lock detection.
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

        # Seed the current state because an existing lock sends no new signal.
        # Run the read on this setup daemon so a slow bus does not block startup.
        self.detector.read_initial_lock_state()

    @log.catch
    def lock(self, active: bool, initial: bool = False) -> None:
        """Apply a lock change.
        initial marshals startup deck work from the setup daemon to the main loop; signal callbacks already run there."""
        gl.screen_locked = active
        if gl.presence_monitor:
            # Notify presence before screen-saver work reads the lock.
            # Isolate failure so @log.catch does not stop the remaining lock update.
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

        # Commit before deck work can return early when startup has no manager.
        # This keeps the first later unlock from being mistaken for a no-op.
        self.locked = active

        if initial:
            # Queue startup deck work on the main loop and read self.locked when it runs.
            # A higher-priority signal can otherwise make a captured startup state stale.
            GLib.idle_add(lambda: self._apply_to_decks(self.locked))
            return

        self._apply_to_decks(active)

    @log.catch
    def _apply_to_decks(self, active: bool) -> bool:
        """Show or hide every deck's screen saver on the main thread.
        Return GLib.SOURCE_REMOVE so this also serves as a one-shot idle callback."""
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
