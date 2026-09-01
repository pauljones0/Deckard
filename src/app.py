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
import signal
import threading
import time
from collections.abc import Callable
from typing import Any, TYPE_CHECKING, cast, override

if TYPE_CHECKING:
    from types import FrameType

import gi

from src.windows.Store.ResponsibleNotesDialog import ResponsibleNotesDialog
from src.windows.Donate.DonateWindow import DonateWindow

import appinfo
import globals as gl

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Xdp", "1.0")

from gi.repository import Gtk, Adw, Gdk, Gio, GLib, Xdp

from loguru import logger as log
import os

from src.backend import timer_wheel
from src.backend import ui_port
from src.backend import startup_queue
from src.backend.PageManagement import page_flush
from src.backend.Store import dependencies
from src.backend.Store.install_request import (
    INSTALL_ACTION,
    UPDATE_ACTION,
    ConfirmedActionGate,
    is_safe_store_id,
)
from src.backend.Store.store_result import Ok
from src.windows.ui_adapter import GtkUIAdapter
from src.windows.mainWindow.mainWindow import MainWindow
from src.windows.AssetManager.AssetManager import AssetManager
from src.windows.Store.Store import Store
from src.windows.Shortcuts.Shortcuts import ShortcutsWindow
from src.windows.Onboarding.OnboardingWindow import OnboardingWindow
from src.windows.Permissions.FlatpakPermissionRequest import FlatpakPermissionRequestWindow

from src.Signals import Signals
from src.api import stop_dbus_service

if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckManager import DeckManager

import globals as gl


# Escalate a queued Ctrl+C after two seconds only if teardown has not started.
# Elapsed time, not press count, avoids busy-loop and key-repeat false escalation.
SIGINT_ESCALATE_AFTER_S = 2.0


def unix_signal_add(priority: int, signum: int, callback: Callable[[], bool]) -> bool:
    """Install a main-loop signal source through old GLib or current GLibUnix APIs.
    Return False when neither API is available or installation fails."""
    add = getattr(GLib, "unix_signal_add", None)
    if add is None:
        try:
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix  # ty: ignore[unresolved-import]  # gi stub: PyGObject-stubs ships no GLibUnix-2.0; the host GLib decides whether it exists, which this try/except probes
            add = GLibUnix.signal_add
        except (ImportError, ValueError, AttributeError):
            return False
    try:
        add(priority, signum, callback)
    except Exception as e:
        # Contain refused signals, missing Unix support, and API argument mismatches.
        log.warning(f"Could not install a GLib unix-signal source for {signum}: {e}")
        return False
    return True


class App(Adw.Application):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        # Re-entry latch for on_quit. Set at construction, so the first signal
        # finds it whenever the handlers go up.
        self._quit_started = False

        # Time of the first Ctrl+C, for the escalation in _on_sigint, and None
        # until then. It lives here for the same reason as the latch above.
        self._sigint_first_at: float | None = None

        # Keep the UI adapter for quit-time detach; None permits TERM before activation.
        self._ui_adapter: GtkUIAdapter | None = None

        # Declare activation-owned slots so early gl.app readers get None, not AttributeError.
        self.deck_manager: "DeckManager | None" = None  # late-init: on_activate
        self.style_manager: "Adw.StyleManager | None" = None  # late-init: on_activate

        # The asset chooser window, built on first use and nulled again by its
        # own close handler, so the next request builds a fresh one.
        self.asset_manager: "AssetManager | None" = None  # late-init: let_user_select_asset

        self.connect("activate", self.on_activate)

        display = Gdk.Display.get_default()
        if display is not None:
            css_provider = Gtk.CssProvider()
            css_provider.load_from_path(os.path.join(gl.top_level_dir, "style.css"))
            Gtk.StyleContext.add_provider_for_display(display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            icon_theme = Gtk.IconTheme.get_for_display(display)
            icon_theme.add_search_path(os.path.join(gl.top_level_dir, "Assets", "icons"))

    def on_activate(self, app: "App") -> None:
        log.trace("running: on_activate")
        if getattr(self, "_activate_completed", False):
            # Forward later activations to reopen without rebuilding cached UI bindings.
            # Only completion latches; a failed constructor can publish a half-built main_win.
            self.on_reopen()
            return

        # Registration constructs App before globals; activation runs after managers exist.
        self.deck_manager = gl.deck_manager

        app_settings = gl.settings_manager.app()

        allow_white_mode = app_settings.allow_white_mode

        # Count the launch. This sits below the re-activation return, so a
        # second launch that presents this window counts as a reopen.
        app_settings.app_launches = app_settings.app_launches + 1
        app_settings.save()

        self.style_manager = self.get_style_manager()
        if allow_white_mode:
            self.style_manager.set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        else:
            self.style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK) # Not everything looks good in light mode at the moment #TODO

        # Install before MainWindow so boot-time pages bind instead of staying dirty.
        # Attach afterward to add map handlers and rescan constructor-created children.
        adapter = GtkUIAdapter()
        self._ui_adapter = adapter
        ui_port.install(adapter)
        try:
            deck_manager = self.deck_manager
            if deck_manager is None:
                # main() builds it before app.run().
                raise RuntimeError("on_activate ran before the deck manager was built")
            self.main_win = MainWindow(application=app, deck_manager=deck_manager)
        except Exception:
            ui_port.install(None)
            self._ui_adapter = None
            raise
        adapter.attach_window(self.main_win)
        if not gl.argparser.parse_args().b:
            self.main_win.present()

        self.show_onboarding()
        # Call directly because the constructor already drained its completion tasks.
        self.show_donate()
        # self.show_permissions()

        self.shortcuts = ShortcutsWindow(app=app, application=app)
        # self.shortcuts.present()

        on_reopen_action = Gio.SimpleAction.new("reopen", None)
        on_reopen_action.connect("activate", self.on_reopen)
        self.add_action(on_reopen_action)

        on_quit_action = Gio.SimpleAction.new("quit", None)
        on_quit_action.connect("activate", self.on_quit)
        self.add_action(on_quit_action)

        self.add_signals()

        # Publish first, drain second. That order lets an appender that races
        # this drain reclaim its own task. See src/backend/startup_queue.py.
        gl.app = self
        startup_queue.get().drain_app_ready()

        # Warm plugin backends off-main so subprocess startup cannot block GTK.
        # Background mode has no configuration UI to start them before input.
        if gl.plugin_manager is not None:
            gl.plugin_manager.warm_up_plugins()

        # Set this last. Everything above completed, so a re-activation can
        # take the present-only early return.
        self._activate_completed = True

        log.success("Finished loading app")

    def on_reopen(self, *args: Any, **kwargs: Any) -> None:
        self.main_win.present()
        log.info("awake")

        self.show_donate(ignore_background_launch=True)

    def let_user_select_asset(self, default_path: str | None, callback_func: Callable[..., Any] | None = None, *callback_args: Any, **callback_kwargs: Any) -> None:
        # Reuse the chooser until its close handler clears both stored references.
        asset_manager = self.asset_manager
        if asset_manager is None:
            asset_manager = AssetManager(application=self, main_window=self.main_win)
            self.asset_manager = asset_manager
            gl.asset_manager = asset_manager
        asset_manager.show_for_path(default_path, callback_func, *callback_args, **callback_kwargs)

    def show_donate(self, ignore_background_launch: bool = False) -> None:
        if not ignore_background_launch and gl.argparser.parse_args().b:
            return
        if gl.showed_donate_window:
            return
        gl.showed_donate_window = True

        app_settings = gl.settings_manager.app()

        if not app_settings.show_donate_window:
            return
        if app_settings.app_launches < 4:
            return
        if hasattr(self, "onboarding"):
            return
        if hasattr(self, "permissions"):
            return

        self.donate = DonateWindow()
        self.donate.present(self.main_win)

    def show_onboarding(self) -> None:
        if gl.argparser.parse_args().b:
            return
        if os.path.exists(os.path.join(gl.DATA_PATH, ".skip-onboarding")):
            return

        self.onboarding = OnboardingWindow(application=self, main_win=self.main_win)
        self.onboarding.present(self.main_win)

        # Disable onboarding for future sessions
        with open(os.path.join(gl.DATA_PATH, ".skip-onboarding"), "w") as f:
            f.write("")

    def show_permissions(self) -> None:
        portal = Xdp.Portal.new()
        if not portal.running_under_flatpak():
            return
        if os.path.exists(os.path.join(gl.DATA_PATH, ".skip-permissions")):
            return
        self.permissions = FlatpakPermissionRequestWindow(application=self, main_window=self.main_win)
        if hasattr(self, "onboarding"):
            if self.onboarding.is_visible():
                return
        self.permissions.present()

    def on_quit(self, *args: Any) -> None:
        # Run once across TERM, HUP, Ctrl+C, Gio, tray, and window-close routes.
        # Re-entry would repeat window, signal, deck, and watchdog teardown.
        if self._quit_started:
            return
        self._quit_started = True

        log.info("Quitting...")

        # Arm the six-second force-quit watchdog before every teardown step.
        # UI, D-Bus, window, or plugin-hook stalls must not park shutdown.
        timer_wheel.schedule(6, self.force_quit, name="force_quit_timer")

        # Detach UI first so live media and tick threads dirty-mark instead of painting destruction.
        ui_port.install(None)
        # Detach the adapter's widget, throttle, and coalescer references while threads wind down.
        # getattr permits quit before App construction completed.
        adapter = getattr(self, "_ui_adapter", None)
        if adapter is not None:
            adapter.detach_window()
            self._ui_adapter = None

        stop_dbus_service()

        # Guard TERM before window activation so teardown still terminates backends.
        self._destroy_main_window()

        # Dispatch AppQuit synchronously before os._exit, isolating each plugin failure.
        # An aborted fan-out would skip deck close and backend termination with no retry.
        gl.signal_manager.trigger_signal_sync(Signals.AppQuit)

        gl.threads_running = False

        # Stop and bounded-join boot rescans before closing controllers.
        # No in-flight enumeration may register a controller during shutdown.
        if gl.deck_manager is not None:
            gl.deck_manager.stop_boot_rescan()

        # Flush daemon-debounced page edits before os._exit can lose them.
        # Unbounded atomic fsyncs remain covered by the six-second watchdog.
        try:
            page_flush.get().flush_all()
        except Exception as e:
            log.warning(f"Could not write pending page edits during shutdown: {e}")

        # Stop non-daemon store workers before joins and before cache-index flush.
        # A later fetch would dirty the flushed index and os._exit would lose it.
        try:
            if gl.store_backend is not None:
                gl.store_backend.shutdown()
        except Exception as e:
            log.warning(f"Could not stop the store backend during shutdown: {e}")

        # Flush deferred cache-index clocks because os._exit skips atexit and daemon timers.
        # Contain failures; the watchdog bounds an atomic write on a wedged filesystem.
        try:
            if gl.store_backend is not None:
                gl.store_backend.store_cache.flush_index()
        except Exception as e:
            log.warning(f"Could not flush the store cache index during shutdown: {e}")

        # Detach queued plugin sinks before force_quit can skip POSIX semaphore cleanup.
        # Keep synchronous app sinks; isolate each detach failure during teardown.
        for logger_obj in gl.loggers.values():
            try:
                logger_obj.remove_sink()
            except Exception as e:
                log.warning(f"Failed to detach log sink during shutdown: {e}")

        # Start ClearAndClose and bounded media-writer joins before controller close and slow joins.
        # A device left open at force_quit can fail the next startup.
        deck_manager = gl.deck_manager
        if deck_manager is not None:
            deck_manager.close_all()

        for ctrl in (deck_manager.deck_controller if deck_manager is not None else []):
            # Skip plugin action hooks during main-thread quit under the watchdog.
            # close_all already sent ClearAndClose, so device close is idempotent.
            ctrl.close(remove_media=True, app_quit=True)

        if deck_manager is not None:
            deck_manager.stop_usb_monitoring()

        from src.backend.main_loop import shutdown_background_pool
        shutdown_background_pool()

        from src.windows.AssetManager.thumbnail_loader import (
            shutdown_thumbnail_pool,
        )
        shutdown_thumbnail_pool()

        # Stop new event lanes without joining daemon runners or wedged observers.
        from src.backend.PluginManager import event_dispatch
        event_dispatch.shutdown()

        # Join attached cv2 tile builders before C++ runtime teardown can abort.
        from src.backend.DeckManagement.Subclasses import mp4_tile_cache
        mp4_tile_cache.shutdown_builders()

        for thread in threading.enumerate():
            if thread is not threading.current_thread() and not thread.daemon:
                thread.join(timeout=5)
                if thread.is_alive():
                    log.error(f"Thread {thread.name} did not exit in time")

        # Terminate the plugin and action backend subprocesses. They are the
        # only child processes this app owns.
        if gl.plugin_manager is not None:
            gl.plugin_manager.terminate_all_backends()

        gl.tray_icon.stop()

        log.success("Stopped Deckard. Have a nice day!")
        log.stop()
        # Use os._exit, not sys.exit. Interpreter teardown aborts in libusb on
        # the hidapi read thread during exit.
        os._exit(0)

    def _destroy_main_window(self) -> None:
        """Destroy an existing main window without re-entering the close dialog path."""
        main_win = getattr(self, "main_win", None)
        if main_win is None:
            # A TERM arrived before on_activate built the window.
            return
        if not main_win.get_realized():
            # GTK 4.22 aborts when destroy, remove, or detach disposes an unrealized window.
            # Background windows have no surface, so skip and continue backend teardown.
            log.debug("Main window was never realized (background mode); "
                      "skipping destroy to avoid the GTK unrealized-dispose "
                      "abort")
            return
        try:
            main_win.destroy()
        except Exception as e:
            # MainWindow can publish before construction fails, leaving a half-built object.
            # Native unrealized-dispose aborts cannot reach this handler.
            log.warning(f"Failed to destroy the main window during shutdown: {e}")

    def force_quit(self) -> None:
        log.info("Forcing quit...")
        # Kill each separate-session backend without blocking before os._exit.
        # This is safe from the timer thread and beside concurrent normal teardown.
        try:
            if gl.plugin_manager is not None:
                gl.plugin_manager.terminate_all_backends()
        except Exception as e:
            log.warning(f"Failed to terminate plugin backends during force quit: {e}")
        os._exit(1)

    def _on_unix_signal(self, *args: Any) -> bool:
        """Run on_quit for SIGTERM or SIGHUP and keep the Unix signal source.
        Do not use this return contract for idle or Gio quit routes."""
        # Let teardown exceptions drop the source so a later TERM uses the default action.
        self.on_quit()
        # Continue keeps GLib from restoring SIG_DFL after the quit latch returns.
        return GLib.SOURCE_CONTINUE

    def _on_sigint(self, signum: int, frame: "FrameType | None") -> None:
        """Queue SIGINT teardown and escalate only before the quit latch starts."""
        now = time.monotonic()
        if self._sigint_first_at is None:
            self._sigint_first_at = now
        elif (not self._quit_started
                and now - self._sigint_first_at >= SIGINT_ESCALATE_AFTER_S):
            # Escalate if the main loop never dispatches queued teardown.
            # Once latched, its watchdog bounds stalls without cutting ordered deck close short.
            log.warning(
                f"Interrupt requested {now - self._sigint_first_at:.1f}s ago and "
                f"the teardown never started (the main loop is not dispatching); "
                f"forcing quit"
            )
            # Restore SIG_DFL so another Ctrl+C escapes a logging-lock wedge.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            # force_quit is the one call that is safe from handler context,
            # because it only terminates the backends and calls os._exit(1).
            self.force_quit()
            return
        # Queue teardown instead of interrupting render, GTK, or locked sections.
        # PRIORITY_DEFAULT matches TERM and HUP sources above redraw idles.
        GLib.idle_add(self.on_quit, priority=GLib.PRIORITY_DEFAULT)

    def register_signal_handlers(self) -> None:
        # Keep SIGINT at Python level so Gio cannot replace it with app.quit().
        # The PyGObject wakeup bridge runs it promptly, and it only queues main-loop work.
        signal.signal(signal.SIGINT, self._on_sigint)
        # Use main-loop TERM and HUP sources so separate-session backends get full teardown.
        # Register now so pre-loop signals stay pending for _on_unix_signal.
        for signum in (signal.SIGTERM, signal.SIGHUP):
            if unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_unix_signal):
                continue
            # Fall back to a Python handler when GLib exposes no Unix source.
            log.warning(
                f"No GLib unix-signal source available for {signum}; falling "
                f"back to a Python-level handler"
            )
            signal.signal(signum, self._on_unix_signal)

    def add_signals(self) -> None:
        # Any session-bus peer can activate these exported actions.
        # Confirm before work; internal store paths bypass both gates.
        self.update_assets_gate = ConfirmedActionGate(
            UPDATE_ACTION, self._update_all_assets, self._confirm_update_request)
        self.update_all_assets_action = self.update_assets_gate.add_to(self)

        self.install_gate = ConfirmedActionGate(
            INSTALL_ACTION, self._install_plugin, self._confirm_install_request,
            target_type="s", validate=is_safe_store_id)
        self.install_plugin_action = self.install_gate.add_to(self)

    def _dialog_parent(self) -> "Gtk.Window | None":
        """Return a confirmation parent, or None so tray-only prompts stand alone."""
        window = self.get_active_window()
        if window is not None:
            return window
        return cast("Gtk.Window | None", getattr(self, "main_win", None))

    def _confirm_update_request(self, _subject: str) -> bool:
        """Confirm an exported all-assets update from the gate thread.
        The dialog marshals itself to GTK."""
        from src.windows.Store.install_consent import make_update_confirm
        return make_update_confirm(self._dialog_parent())()

    @log.catch
    def _update_all_assets(self, _subject: str = "") -> None:
        self.set_working(True)

        store_backend = gl.store_backend
        if store_backend is None:
            self.set_working(False)
            return
        result = store_backend.update_everything()

        self.set_working(False)

        # update_everything returns Ok(count) or Err. A failure must not
        # report success.
        if isinstance(result, Ok):
            self.send_notification("dialog-information-symbolic", "Assets updated",
                                     f"{result.value} assets have been updated")
        else:
            self.send_notification("dialog-information-symbolic", "Asset update failed",
                                     "Could not reach the store to update assets")

    def _confirm_install_request(self, plugin_id: str) -> bool:
        """Whether an install that arrived on the exported action may start.
        It runs on the gate's own thread, so the dialog marshals itself."""
        from src.windows.Store.install_consent import make_install_confirm
        return make_install_confirm(self._dialog_parent())(plugin_id)

    @log.catch
    def _install_plugin(self, plugin_id: str) -> None:
        store_backend = gl.store_backend
        if store_backend is None:
            log.error(f"Cannot install plugin {plugin_id}: no store backend")
            return
        plugin = store_backend.get_plugin_for_id(plugin_id=plugin_id)

        self.set_working(True)

        if plugin is None:
            self.send_notification("dialog-information-symbolic", "Failed to install plugin",
                                   f"The plugin {plugin_id} could not be installed")
            self.set_working(False)
            return

        # Both gate-thread prompts marshal to GTK.
        # Confirm the full set before download and each plugin script before execution.
        from src.windows.Store.install_consent import make_install_script_consent, make_dependency_consent
        window = self._dialog_parent()
        report = dependencies.install_with_dependencies(
            store_backend, dependencies.plugin_item(plugin),
            confirm_set=make_dependency_consent(window),
            ask_install_script=make_install_script_consent(window))
        if report.declined:
            self.set_working(False)
            return
        if not report.ok:
            self.send_notification("dialog-information-symbolic", "Failed to install plugin",
                                   dependencies.failure_message(report, plugin_id))
        elif gl.plugin_manager is not None and gl.plugin_manager.get_plugin_by_id(plugin_id) is None:
            # Reload already reported why the installed plugin did not start.
            pass
        else:
            self.send_notification("dialog-information-symbolic", "Plugin installed",
                                   f"The plugin {plugin_id} was successfully installed")

        self.set_working(False)

    def set_working(self, working: bool) -> None:
        # Use self, not gl.app. This is an App method, so the application
        # object exists, and gl.app is this same instance.
        if working:
            GLib.idle_add(self.mark_busy)
            GLib.idle_add(self.main_win.set_cursor_from_name, "wait")
        else:
            GLib.idle_add(self.unmark_busy)
            GLib.idle_add(self.main_win.set_cursor_from_name, "default")

    @override
    def send_notification(self,  # ty: ignore[invalid-method-override]  # shadows Gio.Application.send_notification with the (icon, title, body) form of this app; the parent_send binding below reaches the base signature
                          icon_name: str,
                          title: str,
                          body: str,
                          button: tuple[str, str, GLib.Variant | None] | None = None,
                          category: str = "im.error") -> None:
        """Queue notification settings and construction on GTK from any caller thread."""
        parent_send = super().send_notification

        def _send() -> bool:
            if not gl.settings_manager.app().show_notifications:
                return GLib.SOURCE_REMOVE

            notif = Gio.Notification()
            notif.set_icon(Gio.Icon.new_for_string(icon_name))
            notif.set_category(category)
            notif.set_title(title)
            notif.set_body(body)
            if button:
                notif.add_button_with_target(button[0], button[1], button[2])

            parent_send(appinfo.APP_ID, notif)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_send)

    def send_outdated_plugin_notification(self, plugin_id: str) -> None:
        self.send_notification(
            "software-update-available-symbolic",
            "Plugin out of date",
            f"The plugin {plugin_id} is out of date and needs to be updated"
        )

    def send_missing_plugin_notification(self, plugin_id: str) -> None:
        self.send_notification(
            "dialog-information-symbolic",
            "Plugin missing",
            f"The plugin {plugin_id} is missing. Please install it.",
            button=("Install", "app.install-plugin", GLib.Variant.new_string(plugin_id))
        )
    def open_store(self, callback_agreed: bool | None = None) -> None:
        agreed = gl.settings_manager.app().responsibility_notes_accepted

        if not agreed:
            if callback_agreed is None:
                resp_dialog = ResponsibleNotesDialog(self.get_active_window(), self.open_store)
                resp_dialog.present()
            return
        
        if gl.store is None:
            gl.store = Store(application=self, main_window=self.main_win)
        gl.store.present()
