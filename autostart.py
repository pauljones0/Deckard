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

import os
import re
import shlex
import shutil
import sys

import appinfo

# Remove the old desktop entry that relaunches the old build and the old portal remnant.
# Check every launch because an installed old build can recreate either entry.
LEGACY_AUTOSTART_NAMES = ("StreamController.desktop", appinfo.OLD_APP_ID + ".desktop")

import gi
gi.require_version("Xdp", "1.0")
from gi.repository import Gio, Xdp

from typing import Any

from loguru import logger as log

def is_flatpak() -> bool:
    return os.path.isfile('/.flatpak-info')

# The generation prevents a stale portal callback from overwriting a newer on-disk state.
# GTK runs setup and callbacks on the main thread, so the counter needs no lock.
_autostart_generation = 0


def _current_autostart_generation() -> int:
    return _autostart_generation


def remove_legacy_autostart_entries() -> None:
    """Delete the pre-rename autostart entries.
    Run every launch so failed deletes or entries recreated by an old build do not persist.
    """
    autostart_dir = os.path.join(os.environ.get("HOME") or os.path.expanduser("~"),
                                 ".config", "autostart")
    for name in LEGACY_AUTOSTART_NAMES:
        path = os.path.join(autostart_dir, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
                log.info(f"Removed legacy autostart entry: {path}")
            except OSError as e:
                log.error(f"Failed to remove legacy autostart entry {path}: {e}")


@log.catch
def setup_autostart(enable: bool = True) -> None:
    global _autostart_generation
    remove_legacy_autostart_entries()

    _autostart_generation += 1
    generation = _autostart_generation

    if is_flatpak():
        setup_autostart_flatpak(enable, generation)
        if not enable:
            # Also remove the manual fallback entry that a failed portal
            # request left behind.
            setup_autostart_desktop_entry(False)
    else:
        # Native installs do not use the portal, whose callback can race removal.
        # Use only the native desktop file for enable and disable.
        setup_autostart_desktop_entry(enable, native=True)


def setup_autostart_flatpak(enable: bool = True, generation: int | None = None) -> None:
    """Set Flatpak autostart through the background portal."""
    def request_background_callback(portal: Xdp.Portal, result: Gio.AsyncResult, user_data: Any) -> None:
        try:
            success = portal.request_background_finish(result)
        except Exception:
            success = False
        log.info(f"request_background success={success}")
        if success:
            return
        if generation is not None and generation != _current_autostart_generation():
            # A newer setup_autostart() call replaced this request, and its
            # outcome owns the on-disk state.
            log.info("Skipping stale autostart fallback (superseded request)")
            return
        # Fall back to a manual desktop entry, and keep the original intent.
        # A failed disable request removes the entry and does not write it.
        setup_autostart_desktop_entry(enable)

    xdp = Xdp.Portal.new()

    try:
        flag = Xdp.BackgroundFlags.AUTOSTART if enable else Xdp.BackgroundFlags.ACTIVATABLE

        xdp.request_background(
            None,  # parent
            "Autostart Deckard",
            ["/app/bin/launch.sh", "-b"],  # commandline
            flag,
            None,  # cancellable
            request_background_callback,
            None,  # user_data
        )
    except Exception:
        log.error("request_background failed")
        setup_autostart_desktop_entry(enable)

def setup_autostart_desktop_entry(enable: bool = True, native: bool = False) -> None:
    log.info("Setting up autostart using desktop entry")

    import globals as gl

    xdg_config_home = os.path.join(os.environ.get("HOME") or os.path.expanduser("~"), ".config")
    AUTOSTART_DIR = os.path.join(xdg_config_home, "autostart")
    AUTOSTART_DESKTOP_PATH = os.path.join(AUTOSTART_DIR, "Deckard.desktop")

    if enable:
        if native:
            _install_desktop_file("autostart-native.desktop", AUTOSTART_DESKTOP_PATH, exec_args="-b")
        else:
            try:
                os.makedirs(os.path.dirname(AUTOSTART_DESKTOP_PATH), exist_ok=True)
                copy_desktop_file(os.path.join(gl.MAIN_PATH, "flatpak", "autostart.desktop"), AUTOSTART_DESKTOP_PATH, True) # flatpak entry; Exec is the sandbox launcher, kept verbatim
                log.info(f"Autostart set up at: {AUTOSTART_DESKTOP_PATH}")
            except Exception as e:
                log.error(f"Failed to set up autostart at: {AUTOSTART_DESKTOP_PATH} with error: {e}")
    else:
        if os.path.exists(AUTOSTART_DESKTOP_PATH):
            try:
                os.remove(AUTOSTART_DESKTOP_PATH)
                log.info(f"Autostart removed from: {AUTOSTART_DESKTOP_PATH}")
            except Exception as e:
                log.error(f"Failed to remove autostart from: {AUTOSTART_DESKTOP_PATH} with error: {e}")

def ensure_app_desktop_entry() -> None:
    """Install or refresh ~/.local/share/applications/<app id>.desktop.
    Source installs need it so Wayland can map the app_id to a taskbar icon.
    """
    if is_flatpak():
        return
    target = os.path.join(os.environ.get("HOME") or os.path.expanduser("~"),
                          ".local", "share", "applications", f"{appinfo.APP_ID}.desktop")
    _install_desktop_file("deckard-app.desktop", target)


def _launcher_exec(extra_args: str = "") -> str:
    """Absolute launch command for the generated native desktop entries.
    Prefer the PATH wrapper for its allocator environment; otherwise use this interpreter.
    """
    import globals as gl
    wrapper = shutil.which("deckard")
    # Quote each path component. A checkout or venv path with a space makes an
    # Exec= line that the desktop spec word-splits into a broken argv.
    if wrapper:
        cmd = shlex.quote(wrapper)
    else:
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.join(gl.MAIN_PATH, 'main.py'))}"
    return f"{cmd} {extra_args}".rstrip()


def _install_desktop_file(template_name: str, target: str, exec_args: str = "") -> None:
    """Write a native desktop entry from a flatpak template.
    Use absolute Icon and Exec paths, and preserve identical targets to avoid cache rescans.
    """
    import globals as gl
    source = os.path.join(gl.MAIN_PATH, "flatpak", template_name)
    icon_path = os.path.join(gl.MAIN_PATH, "Assets", "icons", "hicolor",
                             "512x512", "apps", f"{appinfo.APP_ID}.png")
    try:
        with open(source) as f:
            content = f.read()
    except OSError as e:
        log.error(f"Desktop template missing at {source}: {e}")
        return
    content = content.replace(f"Icon={appinfo.APP_ID}", f"Icon={icon_path}")
    content = re.sub(r"(?m)^Exec=.*$", lambda m: "Exec=" + _launcher_exec(exec_args), content)
    try:
        with open(target) as f:
            if f.read() == content:
                return  # preserve mtime to avoid a desktop cache rescan
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write(content)
        log.info(f"Desktop entry installed at: {target}")
    except OSError as e:
        log.error(f"Failed to install desktop entry at {target}: {e}")


def copy_desktop_file(source: str, target: str, overwrite: bool = False) -> None:
    if not overwrite and os.path.exists(target):
        log.info(f"Desktop file already exists at: {target}")
        return
    
    if not os.path.exists(source):
        log.error(f"Desktop file does not exist at: {source}")
        return
    
    try:
        shutil.copyfile(source, target)
        log.info(f"Desktop file copied from: {source} to: {target}")
    except Exception as e:
        log.error(f"Failed to copy desktop file from: {source} to: {target} with error: {e}")
