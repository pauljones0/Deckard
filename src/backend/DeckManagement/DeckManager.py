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
# Import Python modules
import threading
import time
from collections.abc import Iterable
from typing import Any
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.Devices import StreamDeck
from loguru import logger as log
from usbmonitor import USBMonitor
import os


# Import own modules
from src.backend.DeckManagement.BetterDeck import open_device_handle, release_device_handle
from src.backend.DeckManagement import usb_reset
from src.backend.DeckManagement.reader_supervisor import DeckReaderWatchdog
from src.backend.DeckManagement.Subclasses.RemoteDeckManager import RemoteDeckManager
from src.backend.DeckManagement.deck_controller.controller import DeckController
from src.backend.DeckManagement.deck_controller.media_writer import ClearAndCloseMsg
from src.backend import ui_port
from src.backend.SettingsManager import SettingsManager
from src.backend.DeckManagement.Subclasses.FakeDeck import FakeDeck
from src.api import publish_controller, unpublish_controller

# Import globals
import globals as gl

import gi

gi.require_version("Xdp", "1.0")
from gi.repository import Xdp

ELGATO_VENDOR_ID = "0fd9"


def close_all_controllers(controllers: "Iterable[Any]", join_timeout: float = 2.0) -> None:
    """Runs the terminal close protocol for every open controller.

    The test harness StubDeckManager calls this function, so the harness and
    DeckManager.close_all drive the same code.

    The function submits the terminal ClearAndClose to every open controller
    first, then joins each media thread with a bound. A controller with no
    media_player thread closes directly.
    """
    pending_joins = []
    for controller in list(controllers):
        if controller.deck is None:
            continue
        if not controller.deck.is_open():
            continue

        log.info(f"Closing deck: {controller.deck.get_serial_number()}")
        media_player = getattr(controller, "media_player", None)
        if media_player is None:
            # No writer thread, e.g. a controller that failed mid-construction.
            # Release the handle directly and log a failure.
            try:
                controller._release_handle()
            except Exception as e:
                log.error(f"Failed to close deck cleanly: {e}")
            continue
        # Stop the reader from this thread, ahead of the message. The writer
        # then releases the handle with nothing left to join, and the join
        # this quit path is bounded by stays inside its budget. A failure
        # here must not cost the deck its clear and close.
        try:
            controller.deck.stop_read_thread()
        except Exception as e:
            log.error(f"Failed to stop the reader thread for deck: {e}")
        try:
            media_player.submit_control(ClearAndCloseMsg())
            pending_joins.append(controller)
        except Exception as e:
            log.error(f"Failed to submit ClearAndClose for deck: {e}")

    # The message drives the writer's own clear and close, so this join only
    # waits for that work to land. The app force_quit timer backstops a stuck
    # writer. The headerBar quit path has no such timer, so this bounded join
    # is its only safety.
    for controller in pending_joins:
        controller.media_player.stop(timeout=join_timeout)


def init_deck_controller_round(deck_manager: Any, deck: Any, attempts: int,
                               retry_delay: float) -> "tuple[DeckController | None, bool]":
    """Run one round of open-and-construct attempts for a deck.

    It returns the controller, or None with whether every attempt failed with
    a transport error. That flag separates the two ways a round ends with no
    controller. A transport error means the device is there and will not talk,
    which the caller has one more answer for. Any other failure is a deck
    another process holds, or a construction that raised, and neither is a
    device state a reset changes.

    A function and not a method, because the test harness StubDeckManager
    stands in for the manager the same way close_all_controllers takes its
    controllers.
    """
    # Opening a deck and reading its serial right after open is sometimes
    # flaky (TransportError -1). Retry, and never let one bad deck crash
    # startup. The startup path (load_hardware_deck) and the hotplug and
    # boot-rescan path (add_newly_connected_deck) share this, because a deck
    # the rescan picks up mid-boot flakes as often as one enumerated at
    # startup.
    for attempt in range(1, attempts + 1):
        try:
            if not deck.is_open():
                # The library always opens with resume-from-suspend enabled.
                # This also lifts the release shadow of an earlier attempt,
                # which a bare open() would not.
                open_device_handle(deck)
            return DeckController(deck_manager, deck), False
        except StreamDeck.TransportError as e:
            log.warning(f"Transport error initializing deck (attempt {attempt}/{attempts}): {e}")
            try:
                # The raw handle, because the wrapper lives on the controller
                # that failed to build.
                release_device_handle(deck)
            except Exception:
                pass
            if attempt < attempts:
                time.sleep(retry_delay)
        except Exception as e:
            log.error(f"Failed to initialize deck, maybe it's already connected to another instance? Error: {e}")
            # The constructor guards its bring-up and its tail, and a raise
            # between the two arrives here with the handle still open and its
            # reader running.
            try:
                release_device_handle(deck)
            except Exception:
                log.opt(exception=True).warning("Failed to release the deck handle after a failed init")
            return None, False
    return None, True


class DeckManager:
    # Backoff schedule for the startup re-enumeration, about 60 s in total.
    # An instance can override it, so the harness can shrink it.
    BOOT_RESCAN_DELAYS: tuple[float, ...] = (2.0, 3.0, 5.0, 10.0, 15.0, 25.0)

    def __init__(self) -> None:
        #TODO: Maybe outsource some objects
        self.deck_controller: list[DeckController] = []
        # Guards concurrent add/remove of deck_controller (called from the USB
        # monitor, resume, Flatpak poll and media-thread error paths).
        self._controllers_lock = threading.Lock()
        # Serializes the two connect_new_decks() callers, the USB hotplug
        # monitor and the boot rescan below. The already-loaded check and the
        # controller registration must be atomic, or two concurrent
        # enumerations of one fresh deck both pass the check and register it
        # twice.
        self._connect_decks_lock = threading.Lock()
        # Startup re-enumeration. load_hardware_decks() arms it when the boot
        # enumeration comes back empty, because autostart races USB device
        # init. Deck arrival, exhausted backoff or quit stops it.
        self._boot_rescan_thread: threading.Thread | None = None
        self._boot_rescan_stop = threading.Event()
        self.fake_deck_controller: list[DeckController] = []
        self.settings_manager = SettingsManager()
        self.page_manager = gl.page_manager
        # self.page_manager.load_pages()

        # USB monitor to detect connections and disconnections
        self.usb_monitor = USBMonitor()
        self.usb_monitor.start_monitoring(on_connect=self.on_connect, on_disconnect=self.on_disconnect)

        self.flatpak_disconnect_thread = FlatpakDeckDisconnectThread(self)

        # The third liveness check, and the only one that watches the input
        # reader instead of USB presence. A deck whose reader thread dies
        # under a live device passes both checks above and takes no input at
        # all, so this one revives it. See reader_supervisor.
        self.reader_watchdog = DeckReaderWatchdog(self)
        # A deck the supervisor gives up on takes one targeted USB reset and
        # one more round of attempts. See usb_reset.
        usb_reset.install_give_up_escalation(self.reader_watchdog)
        self.reader_watchdog.start()

        portal = Xdp.Portal.new()
        self.flatpak = portal.running_under_flatpak() # on_disconnect does not work under Flatpak. A separate thread polls instead. #TODO: Find a better solution
        if self.flatpak:
            log.info("Running under Flatpak. Using separate thread to detect device disconnection.")
            self.flatpak_disconnect_thread.start()

        self.remote_deck_manager = RemoteDeckManager(self)
        if gl.settings_manager.app().n_remote_decks > 0:
            self.load_remote_decks()


    def load_remote_decks(self) -> None:
        print(" load remote decks")
        self.remote_deck_manager.start()
        for controller in self.remote_deck_manager.deck_controllers:
            if controller in self.deck_controller:
                continue

            self.deck_controller.append(controller)
            publish_controller(controller)
            # Announce once per newly registered controller. Do not walk every
            # remote controller here. That sends N announcements for deck N,
            # which is N duplicate add_page calls for the first deck.
            ui_port.get().on_deck_added(controller)

        ui_port.get().on_page_list_changed()
        ui_port.get().refresh_deck_availability()

    def remove_remote_decks(self) -> None:
        for controller in self.remote_deck_manager.deck_controllers:
            self.remove_controller(controller)
        ui_port.get().refresh_deck_availability()
        self.remote_deck_manager.stop()

    def load_decks(self) -> None:
        if not gl.argparser.parse_args().skip_load_hardware_decks:
            self.load_hardware_decks()

        self.load_fake_decks()
    
    def load_hardware_decks(self) -> None:
        decks=DeviceManager().enumerate()
        for deck in decks:
            self.load_hardware_deck(deck)
        if not decks:
            # Autostart can race USB device init at boot. The deck is not yet
            # enumerable, and the USB monitor reports only future hotplug
            # events, so without a re-scan the user must replug and restart.
            # Warn only when deck settings exist. A machine that never had a
            # hardware deck reaches this branch normally.
            message = "No decks enumerable at startup; starting bounded re-enumeration"
            if self._hardware_decks_expected():
                log.warning(message)
            else:
                log.info(message)
            self.start_boot_rescan()

    def _hardware_decks_expected(self) -> bool:
        """True when this install used a hardware deck before.

        Deck settings persist per serial under settings/decks. Fake and remote
        decks use recognizable serial prefixes.
        """
        decks_dir = os.path.join(gl.DATA_PATH, "settings", "decks")
        try:
            names = os.listdir(decks_dir)
        except OSError:
            return False
        for name in names:
            base = os.path.splitext(name)[0]
            if base and not base.startswith(("fake-deck", "remote-deck")):
                return True
        return False

    def start_boot_rescan(self) -> None:
        """Re-enumerates decks in the background after an empty startup pass.

        BOOT_RESCAN_DELAYS bounds the backoff at about 60 s in total. The
        thread is a daemon, so it never blocks startup. It stops on the first
        registration (not on mere enumerability, see _boot_rescan_loop), on
        exhausted backoff, or on app quit through stop_boot_rescan.
        """
        if self._boot_rescan_thread is not None and self._boot_rescan_thread.is_alive():
            return
        self._boot_rescan_stop.clear()
        self._boot_rescan_thread = threading.Thread(
            target=self._boot_rescan_loop,
            name="BootDeckRescan",
            daemon=True,
        )
        self._boot_rescan_thread.start()

    def _boot_rescan_loop(self) -> None:
        for attempt, delay in enumerate(self.BOOT_RESCAN_DELAYS, start=1):
            if self._boot_rescan_stop.wait(delay):
                return
            if not gl.threads_running:
                return
            try:
                # connect_new_decks() only adds fresh controllers, and it never
                # touches or resurrects an existing or closed one. Its lock and
                # already-loaded check keep a deck that arrives from the USB
                # hotplug monitor mid-backoff from registering twice.
                n_registered = self.connect_new_decks()
            except Exception as e:
                log.error(f"Boot deck rescan attempt {attempt} failed: {e}")
                continue
            # Stop only once a deck registers (a controller exists), never on
            # mere enumerability. A deck that appears but flakes its open
            # (TransportError -1, the boot-storm failure) fires no further
            # hotplug event, so a stop here strands it behind a success log.
            # A failed pickup leaves the deck unloaded, so the next round's
            # connect_new_decks() retries it, and the bounded schedule still
            # terminates.
            if n_registered > 0:
                log.info(f"Boot deck rescan attempt {attempt}: {n_registered} deck(s) registered")
                return
        log.info(
            "Boot deck rescan exhausted its backoff window without registering a deck "
            "(any initialization errors are logged above); USB hotplug monitoring remains active"
        )

    def stop_boot_rescan(self) -> None:
        """Stop a pending boot rescan promptly (called on app quit). Safe to
        call when no rescan is running."""
        self._boot_rescan_stop.set()
        thread = self._boot_rescan_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def load_hardware_deck(self, deck: Any, attempts: int = 3, retry_delay: float = 0.5) -> None:
        deck_controller = self._init_deck_controller_with_retry(deck, attempts=attempts, retry_delay=retry_delay)
        if deck_controller is not None:
            self.deck_controller.append(deck_controller)
            # After the append, so the DBus API's object set and its
            # live-derived Controllers property always agree. Publishing is a
            # no-op until the service starts, which sweeps up whatever the
            # boot enumeration registered before it.
            publish_controller(deck_controller)

    def _init_deck_controller_with_retry(self, deck: Any, attempts: int = 3, retry_delay: float = 0.5) -> DeckController | None:
        controller, transport_exhausted = init_deck_controller_round(self, deck, attempts, retry_delay)
        if controller is not None or not transport_exhausted:
            return controller
        # Every attempt failed with a transport error, which is the deck that
        # sits on the bus and will not open. One targeted reset of that device,
        # then one more round. Only on this arm, and at most once per device:
        # a deck that failed for any other reason is not one a reset revives,
        # and this is a recovery step, never a boot step of its own.
        if usb_reset.reset_wedged_deck(deck) is None:
            log.error("Giving up on deck after repeated transport errors; skipping it. Replugging the deck usually fixes this.")
            return None
        # The device re-enumerates, and its node is gone for part of that. The
        # wait lives here and not inside the reset, because the give-up
        # escalation shares that reset and must not hold up a watchdog sweep.
        time.sleep(usb_reset.RESET_SETTLE_S)
        controller, _ = init_deck_controller_round(self, deck, attempts, retry_delay)
        if controller is None:
            log.error("Giving up on deck: it still does not open after a USB reset. Replugging the deck usually fixes this.")
        return controller

    def load_fake_decks(self) -> None:
        old_n_fake_decks = len(self.fake_deck_controller)
        # The spin row writes an int, but a hand-edited settings file can
        # leave a float or a numeric string here, and the comparisons below
        # are counts.
        n_fake_decks = int(gl.settings_manager.app().n_fake_decks)

        if n_fake_decks > old_n_fake_decks:
            log.info(f"Loading {n_fake_decks - old_n_fake_decks} fake deck(s)")
            # Load difference in number of fake decks
            for _ in range(n_fake_decks - old_n_fake_decks):
                a = f"Fake Deck {len(self.fake_deck_controller)+1}"
                fake_deck = FakeDeck(serial_number = f"fake-deck-{len(self.fake_deck_controller)+1}", deck_type=f"Fake Deck {len(self.fake_deck_controller)+1}")
                self.add_newly_connected_deck(fake_deck, is_fake=True)

        elif n_fake_decks < old_n_fake_decks:
            # Remove difference in number of fake decks
            log.info(f"Removing {old_n_fake_decks - n_fake_decks} fake deck(s)")
            for controller in self.fake_deck_controller[-(old_n_fake_decks - n_fake_decks):]:
                # Remove controller from fake_decks
                self.fake_deck_controller.remove(controller)
                # Route through remove_controller. A direct pop of the two
                # lists plus a stack-page detach never tears the controller
                # down, so its media thread, tick thread and action executor
                # keep running after the fake deck disappears.
                self.remove_controller(controller)

        ui_port.get().refresh_deck_availability()

    def on_connect(self, device_id: Any, device_info: Any) -> None:
        log.info(f"Device {device_id} with info: {device_info} connected")
        # Check if it is a supported device
        if device_info["ID_VENDOR_ID"] != ELGATO_VENDOR_ID:
            return

        self.connect_new_decks()

    def connect_new_decks(self) -> int:
        """Register every enumerable deck that isn't already loaded.

        Returns the number of enumerated decks that are registered after this
        pass, whether already loaded or picked up here. That count is the boot
        rescan's stop condition. A deck that enumerated but failed to
        initialize does not count, so the rescan keeps retrying it.
        """
        # The serialization is global and not per-deck, so a slow open or
        # retry of deck A delays deck B's pickup. The usbmonitor is a
        # poll-diff loop that merges device changes and drops no event while
        # this lock is held. The deferred deck arrives when its caller gets
        # the lock.
        with self._connect_decks_lock:
            decks = DeviceManager().enumerate()

            # Get already loaded deck serial ids
            loaded_deck_ids = [controller.deck.id() for controller in self.deck_controller]

            for deck in decks:
                if deck.id() in loaded_deck_ids:
                    continue
                # Add deck
                self.add_newly_connected_deck(deck)

            # Recompute after the adds. add_newly_connected_deck returns
            # without registering when a deck's open flakes even after the
            # retries, and those must not count as picked up.
            loaded_after = {controller.deck.id() for controller in self.deck_controller}
            n_registered = sum(1 for deck in decks if deck.id() in loaded_after)

        # The port is null-safe, and the adapter idles the call onto the main
        # thread. This method runs on the USB monitor thread or the boot
        # rescan thread, and check_for_errors() is pure GTK work.
        ui_port.get().refresh_deck_availability()

        return n_registered


    def on_disconnect(self, device_id: Any, device_info: Any) -> None:
        log.info(f"Device {device_id} with info: {device_info} disconnected")
        if device_info["ID_VENDOR_ID"] != ELGATO_VENDOR_ID:
            return

        for controller in list(self.deck_controller):
            if not controller.deck.connected():
                self.remove_controller(controller)

        # USB events can arrive before the window exists. Go through the
        # null-safe port, never a direct off-main GTK call.
        ui_port.get().refresh_deck_availability()

    def remove_controller(self, deck_controller: DeckController) -> None:
        # This is idempotent. Several threads can call it for the same
        # controller, and only the first removal reaches close().
        with self._controllers_lock:
            if deck_controller not in self.deck_controller:
                return
            self.deck_controller.remove(deck_controller)

        # The removal is committed, so the DBus API drops the deck's object
        # now. A published object keeps answering calls on a controller that
        # is closing, while the Controllers property already says the deck is
        # gone.
        unpublish_controller(deck_controller)

        # Detach the UI first. on_deck_removed queues the detach synchronously
        # before it returns, so a fast unplug and replug cannot race a late
        # detach against a fresh add and leave two stack children registered
        # for one serial.
        ui_port.get().on_deck_removed(deck_controller)

        # The teardown sweep runs plugin hooks and can block on a wedged
        # callback. Do not run it on the USB monitor thread, which stalls
        # further connect and disconnect events, or on the shared main_loop
        # background pool, which quit's shutdown_background_pool() cancels
        # mid-close. See DeckController.close()'s docstring.
        threading.Thread(
            target=deck_controller.close,
            args=(True,),
            name=f"DeckClose-{getattr(deck_controller, '_serial_number', None) or 'unknown'}",
            daemon=True,
        ).start()

    def get_controller_for_deck(self, deck: "StreamDeck.StreamDeck | FakeDeck") -> DeckController | None:
        for controller in self.deck_controller:
            # Compare against the raw handle the wrapper holds. controller.deck
            # is always a BetterDeck, so it never equals an enumerated device.
            if controller.deck.deck is deck:
                return controller
        return None

    def add_newly_connected_deck(self, deck: "StreamDeck.StreamDeck | FakeDeck", is_fake: bool = False) -> None:
        # Retry the init instead of constructing a DeckController directly. A
        # deck that arrives mid-boot-storm through hotplug or the boot rescan
        # hits the same flaky open and serial read the startup path retries.
        # On final failure this returns without registering, so a later rescan
        # round or a replug can try again.
        deck_controller = self._init_deck_controller_with_retry(deck)
        if deck_controller is None:
            return

        # These are null-safe. With no UI attached they do nothing, and the
        # deck stack picks the controller up when the window is built.
        ui_port.get().on_deck_added(deck_controller)
        ui_port.get().on_page_list_changed()

        self.deck_controller.append(deck_controller)
        # Hotplug, boot re-enumeration and runtime-added fake decks all land
        # here, each on its own thread. Publishing marshals to the main
        # context itself.
        publish_controller(deck_controller)
        if is_fake:
            self.fake_deck_controller.append(deck_controller)

        ui_port.get().refresh_deck_availability()

    def close_all(self) -> None:
        log.info("Closing all decks")
        close_all_controllers(self.deck_controller)

    def stop_usb_monitoring(self) -> None:
        # The reader watchdog stops with the USB monitor. Both watch for a
        # deck to need something, and neither has anything to do once this is
        # called. Without it a manager that a test or a second session builds
        # leaves a thread sweeping controllers it no longer owns.
        self.reader_watchdog.stop()
        # The escalation hook is a module-level slot holding a closure over
        # this watchdog, and through it this manager and every controller it
        # registered. A stopped watchdog latches no give-up, so the hook has
        # nothing left to serve and clearing it is what lets all of that go.
        usb_reset.clear_give_up_escalation(self.reader_watchdog)
        self.usb_monitor.stop_monitoring(timeout=2)

    def get_connected_serials(self) -> list[str]:
        return [controller.serial_number() for controller in self.deck_controller]


class FlatpakDeckDisconnectThread(threading.Thread):
    def __init__(self, deck_manager: DeckManager):
        super().__init__(name="FlatpakDeckDisconnectThread")
        self.deck_manager = deck_manager

    def run(self) -> None:
        while gl.threads_running:
            time.sleep(2)
            for controller in list(self.deck_manager.deck_controller):
                if not controller.deck.connected():
                    self.deck_manager.remove_controller(controller)
                    # Go through the null-safe port. An unguarded off-main
                    # reach into the window crashes this poll thread before
                    # the window exists.
                    ui_port.get().refresh_deck_availability()
