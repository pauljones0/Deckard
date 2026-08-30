"""Verify one-shot USB reset recovery for reader give-up and deck-open retry,
with exact identity, node revalidation, sandbox degradation, and the ioctl seam."""
import errno
import gc
import os
import threading
import weakref

import fixtures  # must be first; isolates DATA_PATH before import globals
import globals as gl

from loguru import logger as log

from StreamDeck.Transport.Transport import TransportError

from faulty_fake_deck import FaultyFakeDeck

from src.backend.DeckManagement import reader_supervisor, usb_reset
from src.backend.DeckManagement.DeckController import DeckController
from src.backend.DeckManagement.DeckManager import DeckManager
from src.backend.DeckManagement.reader_supervisor import DeckReaderWatchdog

# The Stream Deck Plus product id. Any Elgato product id serves; the vendor is
# what the reset refuses to act without.
PRODUCT_ID = 0x0084
ELGATO = usb_reset.ELGATO_VENDOR_ID
# The shipped ioctl seam, held before any leg stands a recorder in its place.
SHIPPED_IOCTL = usb_reset._issue_reset_ioctl
# A vendor this app drives nothing of. The Linux Foundation root hub carries
# it, so a bus always has one.
OTHER_VENDOR = 0x1D6B


class Enumeration:
    """Provide the enumerated serial and transport mutex before a controller exists."""

    def __init__(self, serial: str):
        self._serial = serial
        self.mutex = threading.Lock()

    def serial_number(self) -> str:
        return self._serial


class ElgatoFakeDeck(FaultyFakeDeck):
    """Report the Elgato identity required by reset validation."""

    def vendor_id(self) -> int:
        return usb_reset.ELGATO_VENDOR_ID

    def product_id(self) -> int:
        return PRODUCT_ID


class WedgedOpenDeck(ElgatoFakeDeck):
    """Start closed and fail a configured number of HID opens with transport errors."""

    def __init__(self, *args, transport_failures: int = 0, enumerated_serial=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._open = False
        self.transport_failures = transport_failures
        self.open_calls = 0
        if enumerated_serial is not None:
            self.device = Enumeration(enumerated_serial)

    def open(self, *args, **kwargs):
        self.open_calls += 1
        if self.transport_failures > 0:
            self.transport_failures -= 1
            raise TransportError("WedgedOpenDeck: the device does not open")
        super().open(*args, **kwargs)


class ResetRecorder:
    """Record reset targets by the device and inode behind each descriptor."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, fd: int) -> None:
        info = os.fstat(fd)
        self.calls.append((info.st_dev, info.st_ino))

    def hit(self, node: str) -> int:
        """How many resets landed on this node."""
        info = os.stat(node)
        return self.calls.count((info.st_dev, info.st_ino))


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def device_descriptor(vendor_id: int, product_id: int) -> bytes:
    """Build a full 18-byte USB descriptor with ids at offsets 8 and 10."""
    return bytes([
        18, 1,                                      # bLength, bDescriptorType
        0x00, 0x02,                                 # bcdUSB 2.00
        0, 0, 0, 64,                                # class, subclass, protocol, packet
        vendor_id & 0xFF, (vendor_id >> 8) & 0xFF,  # idVendor, little endian
        product_id & 0xFF, (product_id >> 8) & 0xFF,  # idProduct, little endian
        0x00, 0x01,                                 # bcdDevice
        1, 2, 3,                                    # iManufacturer, iProduct, iSerial
        1,                                          # bNumConfigurations
    ])


class FakeBus:
    """Build descriptor-backed sysfs and usbfs entries from name, ids, bus,
    device, and optional serial tuples, plus an interface and foreign root hub."""

    def __init__(self, name: str, devices) -> None:
        root = os.path.join(gl.DATA_PATH, name)
        self.sysfs = os.path.join(root, "sys", "bus", "usb", "devices")
        self.nodes = os.path.join(root, "dev", "bus", "usb")
        os.makedirs(self.sysfs, exist_ok=True)
        os.makedirs(self.nodes, exist_ok=True)
        _write(os.path.join(self.sysfs, "1-2:1.0", "bInterfaceNumber"), "00")
        _write(os.path.join(self.sysfs, "usb1", "idVendor"), "1d6b")
        _write(os.path.join(self.sysfs, "usb1", "idProduct"), "0002")
        _write(os.path.join(self.sysfs, "usb1", "busnum"), "1")
        _write(os.path.join(self.sysfs, "usb1", "devnum"), "1")
        for entry_name, vendor_id, product_id, busnum, devnum, serial in devices:
            entry = os.path.join(self.sysfs, entry_name)
            _write(os.path.join(entry, "idVendor"), f"{vendor_id:04x}")
            _write(os.path.join(entry, "idProduct"), f"{product_id:04x}")
            _write(os.path.join(entry, "busnum"), str(busnum))
            _write(os.path.join(entry, "devnum"), str(devnum))
            if serial is not None:
                _write(os.path.join(entry, "serial"), serial)
            _write_bytes(self.node(busnum, devnum),
                         device_descriptor(vendor_id, product_id))
        self.recorder = ResetRecorder()
        self._shipped = (usb_reset.SYSFS_USB_DEVICES, usb_reset.USB_DEV_NODES,
                         usb_reset._issue_reset_ioctl, usb_reset.RESET_SETTLE_S)
        usb_reset.SYSFS_USB_DEVICES = self.sysfs
        usb_reset.USB_DEV_NODES = self.nodes
        usb_reset._issue_reset_ioctl = self.recorder
        # The settle wait covers a real re-enumeration, and there is none here.
        usb_reset.RESET_SETTLE_S = 0.0

    def node(self, busnum: int, devnum: int) -> str:
        return os.path.join(self.nodes, f"{busnum:03d}", f"{devnum:03d}")

    def write_descriptor(self, busnum: int, devnum: int,
                         vendor_id: int, product_id: int) -> None:
        """Replace a node identity while its sysfs listing remains unchanged."""
        _write_bytes(self.node(busnum, devnum), device_descriptor(vendor_id, product_id))

    def restore(self) -> None:
        """Put every module attribute and both latches back."""
        (usb_reset.SYSFS_USB_DEVICES, usb_reset.USB_DEV_NODES,
         usb_reset._issue_reset_ioctl, usb_reset.RESET_SETTLE_S) = self._shipped
        usb_reset._escalated.clear()
        usb_reset._reset_identities.clear()
        reader_supervisor.set_give_up_escalation(None)


def make_controller(serial: str):
    """A real DeckController over a deck that reports the Elgato vendor."""
    fixtures.seed_page("Main")
    deck = ElgatoFakeDeck(serial_number=serial, deck_type="Fake Deck")
    controller = DeckController(gl.deck_manager, deck)
    gl.deck_manager.deck_controller.append(controller)
    return controller


def drive_to_give_up(supervisor, label: str) -> None:
    """Submit reopen attempts directly until the give-up latch fires."""
    for _ in range(reader_supervisor.MAX_CONSECUTIVE_ATTEMPTS + 1):
        if not supervisor.request_reopen():
            return
        assert fixtures.wait_until(lambda: not supervisor.attempt_in_flight(), timeout=30), (
            f"{label}: an attempt never finished on the media thread, so the run never "
            f"reached the give-up this leg needs")
    raise AssertionError(f"{label}: the give-up latch never fired")


def warnings_sink(records: list):
    return log.add(lambda msg: records.append(str(msg)), level="INFO")


def test_a_give_up_resets_the_deck_once() -> None:
    """Give-up resets the matched node once and grants one fresh attempt round."""
    bus = FakeBus("usb-escalate", [
        ("3-1", ELGATO, PRODUCT_ID, 3, 7, "ESCALATE-SERIAL"),
        # A second Elgato deck of another model, on the same bus. The match must
        # not take it, and it proves the product id is part of the match.
        ("3-2", ELGATO, 0x0060, 3, 8, "OTHER-MODEL"),
    ])
    controller = make_controller("ESCALATE-SERIAL")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    usb_reset.install_give_up_escalation(watchdog)
    records: list = []
    sink_id = warnings_sink(records)
    try:
        supervisor = watchdog.supervisor_for(controller)
        drive_to_give_up(supervisor, "escalate")

        assert len(bus.recorder.calls) == 1, (
            f"the give-up issued {len(bus.recorder.calls)} USB resets, not one")
        assert bus.recorder.hit(bus.node(3, 7)) == 1, (
            f"the reset did not land on this deck's node {bus.node(3, 7)}; the recorded "
            f"nodes were {bus.recorder.calls}")
        assert bus.recorder.hit(bus.node(3, 8)) == 0, (
            "the reset landed on the other deck on the bus, which was healthy")
        assert not supervisor.given_up, (
            "the deck was reset and then left given up, so the reset revived a device "
            "the app no longer drives")
        assert supervisor.consecutive_attempts == 0, (
            f"the extra round starts with {supervisor.consecutive_attempts} attempts "
            f"already spent, so the reset buys fewer attempts than a fresh round")
        assert any("takes a reset" in r for r in records), (
            "the reset was issued with nothing in the log to say so; a user reading the "
            "log has to be able to tell a reset from a replug")

        # The second give-up must not reset again. A device that a reset and a
        # full round did not revive is not one a second reset reaches.
        drive_to_give_up(supervisor, "escalate-again")
        assert supervisor.given_up, (
            "the deck was re-armed a second time, so a device that never comes back "
            "loops through resets for the rest of the session")
        assert len(bus.recorder.calls) == 1, (
            f"the deck was reset {len(bus.recorder.calls)} times; the escalation spends "
            f"one reset per controller")
        assert any("already spent" in r for r in records), (
            "the second give-up said nothing about the reset it declined")
        assert controller.media_player.device_writes_suspended, (
            "a deck left alone for good still takes device writes. Every one of them "
            "raises against a handle that is down, arms another full repaint and logs, "
            "which is the two-second composite-and-fail loop the give-up exists to stop.")
    finally:
        log.remove(sink_id)
        bus.restore()
        fixtures.teardown(controller)
    print("PASS: a give-up resets the deck once and asks for one more round")


def test_an_ambiguous_device_is_never_reset() -> None:
    """Do not reset either of two same-model devices without usable serials."""
    bus = FakeBus("usb-ambiguous", [
        ("3-1", ELGATO, PRODUCT_ID, 3, 7, None),
        ("3-2", ELGATO, PRODUCT_ID, 3, 8, None),
    ])
    controller = make_controller("AMBIGUOUS-SERIAL")
    watchdog = DeckReaderWatchdog(gl.deck_manager)
    usb_reset.install_give_up_escalation(watchdog)
    records: list = []
    sink_id = warnings_sink(records)
    try:
        supervisor = watchdog.supervisor_for(controller)
        drive_to_give_up(supervisor, "ambiguous")

        assert not bus.recorder.calls, (
            f"a reset was issued while two devices of this model were on the bus and "
            f"neither could be named: {bus.recorder.calls}")
        assert supervisor.given_up, (
            "the deck was handed another round although nothing was reset, so the "
            "attempt cap buys a second run for no reason")
        assert any("cannot be told from the healthy ones" in r for r in records), (
            "the skipped reset left no line saying why the deck was not reset")

        # A refused reset spends no latch, so a later give-up must retry matching.
        supervisor.allow_one_more_round()
        drive_to_give_up(supervisor, "ambiguous-again")
        assert len([r for r in records if "cannot be told from the healthy ones" in r]) == 2, (
            "the second give-up did not attempt the match again, so a give-up that "
            "reached no device still spent this deck's one reset")
        assert not any("already spent" in r for r in records), (
            "the deck was told it had spent a reset that was never issued")
    finally:
        log.remove(sink_id)
        bus.restore()
        fixtures.teardown(controller)
    print("PASS: an ambiguous device is never reset")


def test_the_open_retry_resets_and_tries_once_more() -> None:
    """A full transport-error round buys one reset and one more open round."""
    bus = FakeBus("usb-boot", [
        ("3-1", ELGATO, PRODUCT_ID, 3, 4, "BOOT-SERIAL"),
        ("3-2", ELGATO, PRODUCT_ID, 3, 5, "OTHER-SERIAL"),
    ])
    records: list = []
    sink_id = warnings_sink(records)
    controller = None
    try:
        fixtures.seed_page("Main")
        deck = WedgedOpenDeck(serial_number="boot-reset", deck_type="Fake Deck",
                              transport_failures=2, enumerated_serial="BOOT-SERIAL")
        # Record the open count when reset occurs to prove the first round ended.
        opens_at_reset: list[int] = []
        recorder = bus.recorder

        def witness(fd: int) -> None:
            opens_at_reset.append(deck.open_calls)
            recorder(fd)

        usb_reset._issue_reset_ioctl = witness
        controller = DeckManager._init_deck_controller_with_retry(
            gl.deck_manager, deck, attempts=2, retry_delay=0.0)

        assert controller is not None, (
            "the deck never registered, so the round behind the reset bought nothing")
        assert bus.recorder.hit(bus.node(3, 4)) == 1, (
            f"the reset did not land on the wedged deck's node; recorded {bus.recorder.calls}")
        assert bus.recorder.hit(bus.node(3, 5)) == 0, (
            "the reset landed on the other deck, which the serial ruled out")
        assert opens_at_reset == [2], (
            f"the reset was issued after {opens_at_reset} opens of the device, and the "
            f"round before it spends both of its attempts first")
        assert deck.open_calls > 2, (
            "the device was never opened again after the reset, so the reset bought no "
            "round at all")

        # Repeated retries of one device identity must not form a reset loop.
        again = WedgedOpenDeck(serial_number="boot-reset-2", deck_type="Fake Deck",
                               transport_failures=99, enumerated_serial="BOOT-SERIAL")
        assert DeckManager._init_deck_controller_with_retry(
            gl.deck_manager, again, attempts=2, retry_delay=0.0) is None, (
            "a deck whose every open fails registered anyway")
        assert len(bus.recorder.calls) == 1, (
            f"this device was reset {len(bus.recorder.calls)} times; the deck-open retry "
            f"spends one reset per device identity for the life of the process")
        assert any("already took its one USB reset" in r for r in records), (
            "the declined second reset left no line saying why")
    finally:
        log.remove(sink_id)
        bus.restore()
        if controller is not None:
            fixtures.teardown(controller)
    print("PASS: the deck-open retry resets once and constructs once more")


def test_a_reset_the_app_could_not_reach_spends_nothing() -> None:
    """Failure to reach usbfs spends no reset latch for later retry rounds."""
    bus = FakeBus("usb-refused", [("3-1", ELGATO, PRODUCT_ID, 3, 9, "REFUSED-SERIAL")])
    records: list = []
    sink_id = warnings_sink(records)
    try:
        fixtures.seed_page("Main")
        visible_nodes = usb_reset.USB_DEV_NODES
        usb_reset.USB_DEV_NODES = os.path.join(gl.DATA_PATH, "usb-refused", "absent")
        first = WedgedOpenDeck(serial_number="refused-1", deck_type="Fake Deck",
                               transport_failures=99, enumerated_serial="REFUSED-SERIAL")
        assert DeckManager._init_deck_controller_with_retry(
            gl.deck_manager, first, attempts=1, retry_delay=0.0) is None, (
            "a deck whose every open fails registered anyway")
        assert not bus.recorder.calls, (
            f"a reset was issued with no usbfs tree in reach: {bus.recorder.calls}")

        # The nodes are back, and this device has not spent anything yet.
        usb_reset.USB_DEV_NODES = visible_nodes
        second = WedgedOpenDeck(serial_number="refused-2", deck_type="Fake Deck",
                                transport_failures=99, enumerated_serial="REFUSED-SERIAL")
        assert DeckManager._init_deck_controller_with_retry(
            gl.deck_manager, second, attempts=1, retry_delay=0.0) is None, (
            "a deck whose every open fails registered anyway")
        assert bus.recorder.hit(bus.node(3, 9)) == 1, (
            f"the round that could reach the device did not reset it; recorded "
            f"{bus.recorder.calls}")
        assert not any("already took its one USB reset" in r for r in records), (
            "the device was told it had spent a reset that was never issued")
    finally:
        log.remove(sink_id)
        bus.restore()
    print("PASS: a reset the app could not reach spends nothing")


def test_a_failure_that_is_not_transport_resets_nothing() -> None:
    """A non-transport open failure does not retry or reset the device."""
    bus = FakeBus("usb-generic", [("3-1", ELGATO, PRODUCT_ID, 3, 4, "GENERIC-SERIAL")])
    try:
        fixtures.seed_page("Main")

        class RefusingDeck(WedgedOpenDeck):
            def open(self, *args, **kwargs):
                self.open_calls += 1
                raise RuntimeError("RefusingDeck: another process holds this deck")

        deck = RefusingDeck(serial_number="generic-fail", deck_type="Fake Deck",
                            enumerated_serial="GENERIC-SERIAL")
        controller = DeckManager._init_deck_controller_with_retry(
            gl.deck_manager, deck, attempts=2, retry_delay=0.0)

        assert controller is None, "a deck whose open raises must not register"
        assert not bus.recorder.calls, (
            f"a failure that is not a transport error reset the device: {bus.recorder.calls}")
        assert deck.open_calls == 1, (
            f"the retry spent {deck.open_calls} attempts on a failure it does not retry")
    finally:
        bus.restore()
    print("PASS: a failure that is not a transport error resets nothing")


def test_a_device_of_another_vendor_is_never_reset() -> None:
    """Reject reset requests for non-Elgato devices even when serial and product match."""
    bus = FakeBus("usb-vendor", [
        ("3-1", ELGATO, PRODUCT_ID, 3, 4, "VENDOR-SERIAL"),
        ("3-3", OTHER_VENDOR, PRODUCT_ID, 3, 6, "OTHER-VENDOR-SERIAL"),
    ])
    try:
        plain = FaultyFakeDeck(serial_number="not-elgato", deck_type="Fake Deck")
        assert usb_reset.reset_wedged_deck(plain) is None, (
            "a deck that is not an Elgato USB device reported a reset")
        assert usb_reset.reset_usb_device(OTHER_VENDOR, PRODUCT_ID, "OTHER-VENDOR-SERIAL",
                                          "other-vendor") is None, (
            "the reset accepted a vendor this app does not drive")
        assert not bus.recorder.calls, (
            f"a device of another vendor was reset: {bus.recorder.calls}")
    finally:
        bus.restore()
    print("PASS: a device of another vendor is never reset")


def test_two_devices_with_one_serial_are_never_reset() -> None:
    """A serial reported by two devices is ambiguous and resets neither."""
    bus = FakeBus("usb-twins", [
        ("3-1", ELGATO, PRODUCT_ID, 3, 4, "TWIN-SERIAL"),
        ("3-2", ELGATO, PRODUCT_ID, 3, 5, "TWIN-SERIAL"),
    ])
    records: list = []
    sink_id = warnings_sink(records)
    try:
        assert usb_reset.reset_usb_device(ELGATO, PRODUCT_ID, "TWIN-SERIAL", "twins") is None, (
            "a reset was issued for a serial that names two devices")
        assert not bus.recorder.calls, (
            f"one of two devices reporting the same serial was reset: {bus.recorder.calls}")
        assert any("report this deck's serial" in r for r in records), (
            "the skipped reset left no line saying why the deck was not reset")
    finally:
        log.remove(sink_id)
        bus.restore()
    print("PASS: two devices with one serial are never reset")


def test_a_node_that_changed_device_is_never_reset() -> None:
    """Revalidate an opened node so bus-number reuse cannot reset a stranger."""
    bus = FakeBus("usb-stranger", [("3-1", ELGATO, PRODUCT_ID, 3, 7, "STRANGER-SERIAL")])
    records: list = []
    sink_id = warnings_sink(records)
    try:
        # The listing still names 3-1 as the deck; the node behind it does not.
        bus.write_descriptor(3, 7, OTHER_VENDOR, 0x0002)
        assert usb_reset.reset_usb_device(ELGATO, PRODUCT_ID, "STRANGER-SERIAL",
                                          "stranger") is None, (
            "a node that reports another device reported a reset")
        assert not bus.recorder.calls, (
            f"the reset landed on a device that took the deck's bus and device number: "
            f"{bus.recorder.calls}")
        assert any("is another device now" in r for r in records), (
            "the refused reset left no line saying the node changed device")

        # The same node, with the deck behind it again, is reset.
        bus.write_descriptor(3, 7, ELGATO, PRODUCT_ID)
        assert usb_reset.reset_usb_device(ELGATO, PRODUCT_ID, "STRANGER-SERIAL",
                                          "stranger") == bus.node(3, 7), (
            "the check refused the deck's own node, so it refuses every reset")
        assert bus.recorder.hit(bus.node(3, 7)) == 1, (
            "the reset did not reach the deck once its node was the deck's again")
    finally:
        log.remove(sink_id)
        bus.restore()
    print("PASS: a node that changed device is never reset")


def test_a_node_with_a_short_descriptor_is_never_reset() -> None:
    """Reject a node that returns ids but not a complete device descriptor."""
    bus = FakeBus("usb-short", [("3-1", ELGATO, PRODUCT_ID, 3, 7, "SHORT-SERIAL")])
    records: list = []
    sink_id = warnings_sink(records)
    try:
        # Cut the descriptor after the product id: both ids read back correctly
        # and the descriptor is still not a descriptor.
        _write_bytes(bus.node(3, 7), device_descriptor(ELGATO, PRODUCT_ID)[:12])
        assert usb_reset.reset_usb_device(ELGATO, PRODUCT_ID, "SHORT-SERIAL",
                                          "short") is None, (
            "a node that answered with half a descriptor reported a reset")
        assert not bus.recorder.calls, (
            f"the reset went to a node that stopped answering mid-descriptor: "
            f"{bus.recorder.calls}")
        assert any("no readable device descriptor" in r for r in records), (
            "the refused reset left no line saying the node answered no descriptor")
    finally:
        log.remove(sink_id)
        bus.restore()
    print("PASS: a node with a short descriptor is never reset")


def test_the_escalation_hook_belongs_to_its_watchdog() -> None:
    """The escalation hook holds its owner weakly and only that owner can clear it."""
    bus = FakeBus("usb-hook", [("3-1", ELGATO, PRODUCT_ID, 3, 7, "HOOK-SERIAL")])
    controller = make_controller("HOOK-SERIAL")
    first = DeckReaderWatchdog(gl.deck_manager)
    second = DeckReaderWatchdog(gl.deck_manager)
    try:
        stray = DeckReaderWatchdog(gl.deck_manager)
        usb_reset.install_give_up_escalation(stray)
        stray_ref = weakref.ref(stray)
        del stray
        gc.collect()
        assert stray_ref() is None, (
            "the installed hook holds its watchdog alive, and through it the deck "
            "manager and every controller that manager ever registered")

        usb_reset.install_give_up_escalation(first)
        usb_reset.install_give_up_escalation(second)
        usb_reset.clear_give_up_escalation(first)
        assert reader_supervisor._give_up_escalation is not None, (
            "one watchdog's teardown cleared the hook another one installed, so the "
            "live manager loses its recovery")

        usb_reset.clear_give_up_escalation(second)
        assert reader_supervisor._give_up_escalation is None, (
            "the watchdog that owns the hook did not clear it on the way out")

        supervisor = second.supervisor_for(controller)
        drive_to_give_up(supervisor, "hook")
        assert not bus.recorder.calls, (
            f"a give-up escalated after the hook was cleared: {bus.recorder.calls}")
        assert supervisor.given_up, "the deck was re-armed with no hook installed"
    finally:
        bus.restore()
        fixtures.teardown(controller)
    print("PASS: the escalation hook belongs to its watchdog and holds it weakly")


def test_the_ioctl_seam_reaches_the_descriptor_it_is_handed() -> None:
    """The shipped ioctl seam reaches its descriptor, proven by ENOTTY on a file."""
    path = os.path.join(gl.DATA_PATH, "usb-seam-node")
    _write_bytes(path, device_descriptor(ELGATO, PRODUCT_ID))
    fd = os.open(path, os.O_RDWR)
    try:
        raised = None
        try:
            SHIPPED_IOCTL(fd)
        except OSError as e:
            raised = e
        assert raised is not None, (
            "the reset ioctl returned on a regular file, so the seam issues no ioctl at "
            "all and every other leg records a call that does nothing")
        assert raised.errno == errno.ENOTTY, (
            f"the ioctl failed with {raised.errno}, not ENOTTY. ENOTTY is the answer of a "
            f"file that takes no ioctl, and any other error means the call never got "
            f"that far")
    finally:
        os.close(fd)
    print("PASS: the ioctl seam reaches the descriptor it is handed")


def test_a_sandbox_without_usb_nodes_degrades() -> None:
    """Missing usbfs access performs no reset and logs the reason once."""
    bus = FakeBus("usb-sandbox", [("3-1", ELGATO, PRODUCT_ID, 3, 4, "SANDBOX-SERIAL")])
    records: list = []
    sink_id = warnings_sink(records)
    try:
        usb_reset.USB_DEV_NODES = os.path.join(gl.DATA_PATH, "usb-sandbox", "absent")
        assert usb_reset.reset_usb_device(usb_reset.ELGATO_VENDOR_ID, PRODUCT_ID,
                                          "SANDBOX-SERIAL", "sandbox") is None, (
            "a process that cannot see the USB nodes reported a reset")
        assert not bus.recorder.calls, (
            f"a reset was issued with no usbfs tree in reach: {bus.recorder.calls}")
        sandbox_lines = [r for r in records if "cannot see the USB device nodes" in r]
        assert len(sandbox_lines) == 1, (
            f"the degrade said so {len(sandbox_lines)} times; it belongs in the log once "
            f"per attempt, and it has to name the reason")
    finally:
        log.remove(sink_id)
        bus.restore()
    print("PASS: a sandbox without USB nodes degrades with one line")


def test_the_reset_request_number_is_the_kernel_one() -> None:
    """USBDEVFS_RESET equals the kernel _IO('U', 20) request."""
    assert usb_reset.USBDEVFS_RESET == (ord("U") << 8) | 20, (
        f"the reset request number is {usb_reset.USBDEVFS_RESET}, which is not "
        f"_IO('U', 20) from linux/usbdevice_fs.h")
    print("PASS: the reset request number is the kernel's")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_usb_reset")

    # One ordinary controller first. It installs the integration globals and
    # warms every lazily started global thread, so a leg measures its own deck.
    warm = fixtures.make_headless_controller(serial="usb-reset-warm")
    fixtures.wait_until(lambda: warm.active_page is not None, timeout=10)
    fixtures.teardown(warm)

    test_the_reset_request_number_is_the_kernel_one()
    test_a_give_up_resets_the_deck_once()
    test_an_ambiguous_device_is_never_reset()
    test_the_open_retry_resets_and_tries_once_more()
    test_a_reset_the_app_could_not_reach_spends_nothing()
    test_a_failure_that_is_not_transport_resets_nothing()
    test_a_device_of_another_vendor_is_never_reset()
    test_two_devices_with_one_serial_are_never_reset()
    test_a_node_that_changed_device_is_never_reset()
    test_a_node_with_a_short_descriptor_is_never_reset()
    test_the_escalation_hook_belongs_to_its_watchdog()
    test_the_ioctl_seam_reaches_the_descriptor_it_is_handed()
    test_a_sandbox_without_usb_nodes_degrades()
    print("ALL PASS: scenario_usb_reset")


if __name__ == "__main__":
    main()
