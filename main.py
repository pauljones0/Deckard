"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import os
import sys

# Set glibc arena limits before imports and D-Bus because execve replaces the process.
# libc reads them at startup; SC_REEXEC stops loops and sys.orig_argv preserves flags.
if "MALLOC_ARENA_MAX" not in os.environ and "SC_REEXEC" not in os.environ:
    os.environ["MALLOC_ARENA_MAX"] = "2"
    os.environ["MALLOC_TRIM_THRESHOLD_"] = "131072"
    os.environ["SC_REEXEC"] = "1"
    os.execve(sys.executable, sys.orig_argv, os.environ)

import setproctitle

setproctitle.setproctitle("Deckard")

# Dump all-thread tracebacks on fatal signals or SIGQUIT to stderr.
# main() redirects output after --data or settings resolve gl.DATA_PATH.
import faulthandler, signal
try:
    faulthandler.enable()
    faulthandler.register(signal.SIGQUIT)
except (AttributeError, ValueError, OSError):
    pass

# Move the old var-app tree and leave a compatibility symlink before importing globals.
# globals creates the data directory at import time and would invalidate migration checks.
import appinfo
from rebrand_migration import migrate as _rebrand_migrate, migrate_native_var_app_to_xdg as _xdg_migrate
_rebrand_migrate()
# Native only, and does nothing under flatpak. It moves ~/.var/app/<id> to
# the XDG data dir, after the rename, so the renamed tree lands first.
_xdg_migrate()

# Handle running-instance requests before globals; other invocations continue unchanged.
# Use the shared parser; preserve argparse exits and print errors before hooks or logs exist.
from cli_args import argparser as _cli_argparser
from src.backend import cli_fast_path as _cli_fast_path

try:
    _cli_outcome = _cli_fast_path.handle_preboot_cli(_cli_argparser.parse_args())
except Exception as _error:
    _cli_outcome = _cli_fast_path.Outcome(
        exit_code=1,
        failures=(f"Deckard could not carry out that command: {_error}",))
if _cli_outcome.exit_code is not None:
    for _line in _cli_outcome.output:
        print(_line)
    for _failure in _cli_outcome.failures:
        print(_failure, file=sys.stderr)
    sys.exit(_cli_outcome.exit_code)

import sys
from loguru import logger as log
import os
import time
import threading

# Cap OpenCV's lazy parallel_for_ pool before its first call instead of using one thread per core.
# Only cvtColor uses this pool; PIL resizing and FFmpeg video threads are unaffected.
import cv2
cv2.setNumThreads(2)

import globals as gl

from src.app import App
from src.api import start_dbus_service
from src.backend.DeckManagement.DeckManager import DeckManager
from locales.LocaleManager import LocaleManager
from src.backend.MediaManager import MediaManager
from src.backend.AssetManagerBackend import AssetManagerBackend
from src.backend.PageManagement.PageManagerBackend import PageManagerBackend
from src.backend.SettingsManager import SettingsManager
from src.backend.PluginManager.PluginManager import PluginManager
from src.backend.IconPackManagement.IconPackManager import IconPackManager
from src.backend.WallpaperPackManagement.WallpaperPackManager import WallpaperPackManager
from src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaperPackManager import SDPlusBarWallpaperPackManager
from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.store_result import Err
from src.backend.notify import Notifier
from autostart import setup_autostart, ensure_app_desktop_entry
from src.Signals.SignalManager import SignalManager
from src.backend.WindowGrabber.WindowGrabber import WindowGrabber
from src.backend.GnomeExtensions import GnomeExtensions
from src.backend.PermissionManagement.FlatpakPermissionManager import FlatpakPermissionManager
from src.backend.Wayland.Wayland import Wayland
from src.backend.LockScreenManager.LockScreenManager import LockScreenManager
from src.backend.PresenceMonitor.PresenceMonitor import PresenceMonitor
from src.tray import TrayIcon
from src.backend.Logger import Logger, LoggerConfig, Loglevel
from src.backend.log_hooks import install_exception_hooks, redirect_faulthandler
from src.backend import cli_forward, instance_gate

from src.backend.Migration.MigrationManager import MigrationManager
from src.backend.Migration.Migrators.Migrator_1_5_0 import Migrator_1_5_0
from src.backend.Migration.Migrators.Migrator_1_5_0_beta_5 import Migrator_1_5_0_beta_5

import globals as gl

DEFAULT_DATA_PATH = os.path.expanduser(f"~/.var/app/{appinfo.APP_ID}/data")

# Rotated files kept per log sink, oldest deleted first. loguru keeps every
# rotation without this bound, so the log directory grows without limit.
LOG_RETENTION_FILES = 10
# Files and the ring use DEBUG; the console uses INFO because TRACE grows files quickly.
# Exact SC_LOG_TRACE=1 enables TRACE for all sinks, and this boot-only setting is read at import.
LOG_TRACE = os.environ.get("SC_LOG_TRACE") == "1"
FILE_LOG_LEVEL = "TRACE" if LOG_TRACE else "DEBUG"
CONSOLE_LOG_LEVEL = "TRACE" if LOG_TRACE else "INFO"

main_path = os.path.abspath(os.path.dirname(__file__))
gl.MAIN_PATH = main_path

def write_logs(record):
    with gl.logs_lock:
        gl.logs.append(record)

@log.catch
def config_logger():
    log.remove()
    # Install stderr first so file-sink failure cannot leave the process without a sink.
    # The surrounding log.catch then has a handler for its diagnostic.
    log.add(sys.stderr, level=CONSOLE_LOG_LEVEL)
    log.add(write_logs, level=FILE_LOG_LEVEL)
    # Omit inert backtrace/diagnose; redaction clears exceptions and embeds a scrubbed traceback.
    # Isolate the file sink so an unwritable path does not discard stderr and ring sinks.
    try:
        log.add(os.path.join(gl.DATA_PATH, "logs/logs.log"), rotation="3 days",
                retention=LOG_RETENTION_FILES, level=FILE_LOG_LEVEL)
    except OSError as e:
        log.error(f"Could not open the log file; continuing with stderr and ring sinks only: {e}")

    plugin_logger = Logger(
        LoggerConfig(
            name="PLUGIN",
            log_file_path=os.path.join(gl.DATA_PATH, "logs/plugins.log"),
            base_log_level=FILE_LOG_LEVEL,
            rotation="3 days",
            retention=LOG_RETENTION_FILES,
            compression="zip"
        ),
        [
            Loglevel("TRACE", "trace", 5, "<bold><cyan>"),
            Loglevel("DEBUG", "debug", 10, "<bold><blue>"),
            Loglevel("INFO", "info", 20, "<bold><white>"),
            Loglevel("SUCCESS", "success", 25, "<bold><green>"),
            Loglevel("WARNING", "warning", 30, "<bold><yellow>"),
            Loglevel("ERROR", "error", 40, "<red>"),
            Loglevel("CRITICAL", "critical", 50, "<bold><red>"),
        ]
    )

    gl.loggers["plugins"] = plugin_logger

@log.catch
def create_cache_folder():
    os.makedirs(os.path.join(gl.DATA_PATH, "cache"), exist_ok=True)

def create_global_objects():
    gl.tray_icon = TrayIcon()
    # gl.tray_icon.run_detached()

    gl.lm = LocaleManager(csv_path=os.path.join(main_path, "locales", "locales.csv"))
    gl.lm.set_to_os_default()
    gl.lm.set_fallback_language("en_US")

    gl.flatpak_permission_manager = FlatpakPermissionManager()

    gl.gnome_extensions = GnomeExtensions()

    gl.settings_manager = SettingsManager()

    # Construct before plugin loading first reports to the user.
    # The desktop-notification fallback reads app settings.
    gl.notify = Notifier()

    gl.signal_manager = SignalManager()

    gl.media_manager = MediaManager()
    gl.asset_manager_backend = AssetManagerBackend()
    gl.page_manager = PageManagerBackend(gl.settings_manager)
    gl.page_manager.remove_old_backups()
    gl.page_manager.backup_pages()
    gl.icon_pack_manager = IconPackManager()
    gl.wallpaper_pack_manager = WallpaperPackManager()
    gl.sd_plus_bar_wallpaper_pack_manager = SDPlusBarWallpaperPackManager()

    gl.store_backend = StoreBackend()
    # Repair any install left half-swapped by a previous crash before the
    # plugin and pack scanners read the install directories.
    gl.store_backend.recover_interrupted_installs()

    gl.plugin_manager = PluginManager()
    gl.plugin_manager.load_plugins(show_notification=True)
    gl.plugin_manager.generate_action_index()

    gl.window_grabber = WindowGrabber()

    if os.getenv("WAYLAND_DISPLAY", False):
        gl.wayland = Wayland()

    # Construct before LockScreenManager starts its setup thread.
    # Otherwise a lock event can find no presence monitor and be lost.
    gl.presence_monitor = PresenceMonitor()

    gl.lock_screen_detector = LockScreenManager()

    
    # gl.dekstop_grabber = DesktopGrabber()

@log.catch
def update_assets():
    auto_update = gl.settings_manager.app().auto_update

    if gl.argparser.parse_args().devel:
        auto_update = False

    if not auto_update:
        log.info("Skipping store asset update")
        return

    # Normal boot builds the store backend first, but the order is not enforced.
    # Guard it because log.catch would otherwise swallow AttributeError and skip the update.
    if gl.store_backend is None:
        log.warning("Skipping store asset update: the store backend is not built yet")
        return

    log.info("Updating store assets")
    start = time.time()
    result = gl.store_backend.update_everything()
    if isinstance(result, Err):
        log.error("Failed to update store assets")
        gl.notify.error("Failed to update store assets")
        return
    number_of_installed_updates = result.value
    log.info(f"Updating {number_of_installed_updates} store assets took {time.time() - start} seconds")

    if number_of_installed_updates <= 0:
        return

    # Show toast in ui
    gl.notify.info(f"{number_of_installed_updates} assets updated")


def handle_listing_commands():
    """Run --list-devices and --list-pages. True means this call handled one."""
    args = gl.argparser.parse_args()
    
    if args.list_devices:
        print("Scanning for connected StreamDeck devices...")
        print()
        
        try:
            # Minimal initialization to scan for devices
            from StreamDeck.DeviceManager import DeviceManager

            from src.backend.DeckManagement.BetterDeck import device_is_on_bus, release_device_handle
            devices = DeviceManager().enumerate()
            
            if not devices:
                print("No StreamDeck devices found.")
                print("\nTips:")
                print("- Make sure your StreamDeck is connected via USB")
                print("- Check that the device is recognized by your system")
                print("- Try running with sudo if you have permission issues")
                return True
            
            print(f"Found {len(devices)} StreamDeck device(s):")
            print()
            
            for i, device in enumerate(devices):
                print(f"Device {i+1}:")
                try:
                    device_id = getattr(device, 'id', lambda: 'Unknown')()
                    print(f"  Device ID: {device_id}")
                    
                    try:
                        deck_type = getattr(device, 'deck_type', lambda: 'Unknown StreamDeck')()
                        print(f"  Product Name: {deck_type}")
                    except Exception:
                        # The HID backend raises an unspecified error when the
                        # process has no permission for the device.
                        print("  Product Name: Unknown (permission issue)")
                    
                    device_opened = False
                    try:
                        if not device.is_open():
                            device.open()
                            device_opened = True
                        
                        print(f"  Serial Number: {device.get_serial_number()}")
                        key_layout = device.key_layout()
                        print(f"  Key Layout: {key_layout[1]}x{key_layout[0]} ({device.key_count()} keys)")
                        
                        if hasattr(device, 'dial_count') and device.dial_count() > 0:
                            print(f"  Dials: {device.dial_count()}")
                        if hasattr(device, 'is_touch') and device.is_touch():
                            print("  Touchscreen: Yes")
                        print(f"  Connected: {'Yes' if device_is_on_bus(device) else 'No'}")
                        
                        if device_opened:
                            # open() started a reader thread, and a bare
                            # close() leaves it free to take the handle back.
                            release_device_handle(device)
                            
                    except PermissionError:
                        print("  Status: Permission denied")
                        print("  Note: Run 'sudo python main.py --list-devices' or install udev rules")
                    except Exception as open_error:
                        print(f"  Status: Could not access device ({open_error})")
                        print("  Note: This may be a permission issue or device is in use")
                        
                except Exception as e:
                    print(f"  Error: {e}")
                    if "permission" in str(e).lower() or "access" in str(e).lower():
                        print("  Note: Try running with sudo or install proper udev rules")
                
                print()
        except ImportError:
            print("Error: StreamDeck library not available")
        except Exception as e:
            print(f"Error scanning devices: {e}")
        
        print("\nTroubleshooting:")
        print("- If you see permission errors, try: sudo python main.py --list-devices")
        print("- For permanent fix, install udev rules: sudo cp udev.rules /etc/udev/rules.d/70-streamdeck.rules")
        print("- Then run: sudo udevadm control --reload-rules && sudo udevadm trigger")
        print("- After installing udev rules, unplug and replug your StreamDeck")
        
        return True
    
    if args.list_pages:
        print("Scanning for available pages...")
        print()
        
        try:
            import os
            data_path = gl.DATA_PATH if hasattr(gl, 'DATA_PATH') else DEFAULT_DATA_PATH
            pages_dir = os.path.join(data_path, "pages")
            
            if not os.path.exists(pages_dir):
                print(f"Pages directory not found: {pages_dir}")
                print("\nThis might mean Deckard hasn't been set up yet.")
                return True
            
            page_files = [f for f in os.listdir(pages_dir) if f.endswith('.json') and not f.startswith('.')]
            
            if not page_files:
                print("No pages found.")
                print(f"\nPages should be located in: {pages_dir}")
                return True
            
            print(f"Found {len(page_files)} page(s):")
            print()
            
            for page_file in sorted(page_files):
                page_name = os.path.splitext(page_file)[0]
                page_path = os.path.join(pages_dir, page_file)
                
                try:
                    import json
                    with open(page_path, 'r') as f:
                        page_data = json.load(f)
                    
                    print(f"  {page_name}")
                    
                    # Count items with states
                    items_with_states = 0
                    for input_type in ['keys', 'dials', 'touchscreens']:
                        if input_type in page_data:
                            for item_id, item_data in page_data[input_type].items():
                                if 'states' in item_data and item_data['states']:
                                    states_count = len(item_data['states'])
                                    items_with_states += 1
                                    if states_count > 1:
                                        print(f"    - {input_type[:-1]} {item_id}: {states_count} states")
                    
                    if items_with_states == 0:
                        print("    - No configured items")
                    
                except Exception as e:
                    print(f"    - Error reading page: {e}")
                
                print()
                    
        except Exception as e:
            print(f"Error scanning pages: {e}")
        
        return True
    
    return False

def make_api_calls():
    """Forward argv requests and return whether a running instance handled them.
    Absent or parked requests boot; an unserviceable press exits with its reason.

    """
    verdict = cli_forward.route_cli_requests(gl.argparser.parse_args())
    for line in verdict.output:
        print(line)
    for failure in verdict.failures:
        print(failure, file=sys.stderr)
    if verdict.failures:
        sys.exit(1)
    return verdict.handled


def main():
    # Install once before main-thread, GLib, worker-thread, or finalizer failures can occur.
    # Exceptions use stderr until config_logger adds the file and ring sinks.
    install_exception_hooks()

    # Run the listing commands first; they need no full initialization
    if handle_listing_commands():
        return

    if make_api_calls():
        return

    # Add sinks before the instance gate and migrations so early startup reaches files and the ring.
    # Keep them after early CLI returns so short-lived calls do not open or rotate app logs.
    config_logger()
    redirect_faulthandler(os.path.join(gl.DATA_PATH, "logs"))

    gsk_render_env_var = os.environ.get("GSK_RENDERER")
    if gsk_render_env_var != "ngl":
        log.warning('Should you get an Gtk X11 error preventing the app from starting please add '
                    'GSK_RENDERER=ngl to your "/etc/environment" file')

    # Create the application before its owned objects.
    # Registration must select the primary instance before expensive or exclusive work.
    app = App(application_id=appinfo.APP_ID)

    try:
        decision = instance_gate.establish(
            app,
            publish=start_dbus_service,
            close_running=gl.argparser.parse_args().close_running,
        )
    except instance_gate.LaunchAborted as e:
        log.error(str(e))
        sys.exit(1)

    if decision is instance_gate.Decision.REMOTE:
        # Hand parked requests to the instance that now owns the name.
        # This process exits without opening a deck.
        try:
            failures = cli_forward.forward_parked_requests()
        except Exception as e:
            # The requests are already popped. A loss plus an exit code of 0
            # is the silent drop this arm prevents.
            failures = [f"Could not hand the parked requests over: {e}"]
        for failure in failures:
            print(failure, file=sys.stderr)

        # Forward activation so the running instance presents its window.
        # If it dies first, the forward fails and this process exits.
        try:
            app.activate()
        except Exception as e:
            log.warning(f"Could not present the running instance's window: {e}")
        log.info("Already running, exiting")
        sys.exit(1 if failures else 0)

    migration_manager = MigrationManager()
    migration_manager.add_migrator(Migrator_1_5_0())
    migration_manager.add_migrator(Migrator_1_5_0_beta_5())
    migration_manager.run_migrators()

    create_global_objects()

    setup_autostart(gl.settings_manager.app().autostart)
    ensure_app_desktop_entry()
    
    create_cache_folder()
    threading.Thread(target=update_assets, name="update_assets").start()

    from src.backend.DeckManagement.Subclasses.video_cache_sweeper import sweep_stale_video_caches
    threading.Thread(target=sweep_stale_video_caches, args=(15,), name="video_cache_sweep", daemon=True).start()

    # Diagnostic only. Does nothing unless SC_MEM_TELEMETRY=1.
    from src.backend.mem_telemetry import start_if_enabled as start_mem_telemetry
    start_mem_telemetry()

    log.info("Loading app")
    gl.deck_manager = DeckManager()
    gl.deck_manager.load_decks()

    # Install after deck_manager; on_quit reads it unguarded and earlier TERM latches teardown.
    # Install before run() so PyGObject does not register its SIGINT fallback.
    app.register_signal_handlers()

    # Publish just before the loop so boot-time reports queue while the slot is None.
    # Earlier publication would route them through an application with no window.
    gl.app = app
    app.run(gl.argparser.parse_args().app_args)

if __name__ == "__main__":
    # Log unexpected startup errors once; exit nonzero so supervisors see failure.
    # Preserve SystemExit codes for known abort paths.
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        log.opt(exception=True).critical("Deckard exited on an unhandled startup error")
        sys.exit(1)


log.trace("Reached end of main.py")
