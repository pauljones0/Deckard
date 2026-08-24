"""A deck that only a USB reset revives takes exactly one, and only where it
can be told apart from every healthy device.

The reset itself is one ioctl on a usbfs node, which no scenario can issue: a
regular file answers it with ENOTTY and there is no deck on the bus. What every
leg here drives for real is everything around that one call. The bus is a
directory tree laid out the way the kernel lays sysfs and usbfs out, the
production code walks it, matches the device, builds the node path and opens
it, and a recorder stands in for the ioctl and says which node the descriptor
it was handed belongs to.

Two production paths reach the reset and both are driven here: the reader
supervisor's give-up latch, which resets and then asks for one more round of
reopen attempts, and the deck-open retry, which resets after a round of
transport errors and constructs once more. Each is latched, and a leg spends
the latch twice to prove the second reset never happens.
"""
import os
import threading

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
# A vendor this app drives nothing of. The Linux Foundation root hub carries
# it, so a bus always has one.
OTHER_VENDOR = 0x1D6B


class Enumeration:
    """The transport object hidapi hangs the enumeration strings on.

    A deck that will not open still answers with the serial the USB
    enumeration read, and the deck-open retry has no other serial to match on:
    its controller never got far enough to read one.

    The mutex is the attribute the fair transport lock is installed on. A
    transport without it is library drift, and the installer says so.
    """

    def __init__(self, serial: str):
        self._serial = serial
        self.mutex = threading.Lock()

    def serial_number(self) -> str:
        return self._serial


class ElgatoFakeDeck(FaultyFakeDeck):
    """A fake deck that reports the Elgato vendor.

    FakeDeck reports vendor 0, which the reset refuses, so a deck that models
    the field incident has to say what a real deck says.
    """

    def vendor_id(self) -> int:
        return usb_reset.ELGATO_VENDOR_ID

    def product_id(self) -> int:
        return PRODUCT_ID


class WedgedOpenDeck(ElgatoFakeDeck):
    """A deck whose open() fails with a transport error a set number of times.

    That is the field state the deck-open retry gives up on: the device
    enumerates, its identity reads, and every open of the HID interface fails.
    It starts closed, because a deck that reports itself open is never opened
    by the retry at all.
    """

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
    """Stands in for the reset ioctl and records which node it was asked for.

    The descriptor is the whole contract: the production code opens the node it
    matched and hands that descriptor over, so the inode behind it names the
    device that would have been reset.
    """

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


class FakeBus:
    """A sysfs listing and a usbfs node tree, with the module pointed at them.

    devices are (name, vendor_id, product_id, busnum, devnum, serial) tuples,
    and a serial of None writes no serial attribute, which is a device that
    reports none.

    The tree also carries the two sysfs entries that are not devices: an
    interface, which has no idVendor, and a root hub of another vendor. A walk
    that took either for a device would reset the wrong thing.
    """

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
            _write(self.node(busnum, devnum), "")
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
    """Submit reopen attempts until the give-up latch fires.

    The attempts are not what these legs measure. The reader scenario owns the
    recovery policy, and a give-up needs a run of attempts to reach, so this
    submits them directly rather than modeling a reader that dies five times.
    """
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
    """The give-up latch resets the deck's own node and asks for one more round.

    The reset is the whole point of the escalation, and the round behind it is
    what makes the reset worth issuing: without it the deck stays given up and
    the reset revives a device the app has stopped driving.
    """
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
    finally:
        log.remove(sink_id)
        bus.restore()
        fixtures.teardown(controller)
    print("PASS: a give-up resets the deck once and asks for one more round")


def test_an_ambiguous_device_is_never_reset() -> None:
    """Two decks of one model, and no serial to tell them apart: reset neither.

    A wedged deck can refuse the serial read that would name it. Resetting a
    second, healthy deck of the same model because it is the same model takes
    a working deck down, which is worse than leaving the wedged one down.
    """
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
    finally:
        log.remove(sink_id)
        bus.restore()
        fixtures.teardown(controller)
    print("PASS: an ambiguous device is never reset")


def test_the_open_retry_resets_and_tries_once_more() -> None:
    """A round of transport errors buys one reset and one more round.

    This is the field incident: the deck enumerates, every open fails, and the
    app skips it for the session. The serial comes from the USB enumeration
    here, because the controller that would have read one never got built.
    """
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
        # The opens the device had taken when the reset was issued. The count
        # says the reset comes after a whole round and not inside one, without
        # naming how many opens one attempt makes: a controller opens the
        # handle again itself.
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

        # A second wedged deck of the same identity gets no second reset. The
        # boot rescan and every hotplug event run this retry again, and a reset
        # per round is a reset loop.
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


def test_a_failure_that_is_not_transport_resets_nothing() -> None:
    """A deck another process holds is not a deck a reset revives.

    The retired boot-time sweep reset every Elgato device it could find. The
    recovery that replaced it fires on one arm only, and this is the arm next
    to it.
    """
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
    """Only an Elgato device is reset, whatever asks.

    A fake deck and a remote deck reach the same give-up arms and neither has
    a USB node behind it, and the bus carries devices this app drives nothing
    of. The device of another vendor here sits on the bus with a serial that
    matches, so nothing but the vendor keeps the reset off it.
    """
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
    """Two devices that report the same serial name no single device.

    A serial that matches twice is no better than no serial at all, and the
    single-device rule below it does not apply either: there are two.
    """
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


def test_a_sandbox_without_usb_nodes_degrades() -> None:
    """No /dev/bus/usb, no reset, and one line that says so.

    A flatpak sandbox without USB device access carries no usbfs tree. The
    recovery then degrades to what the app did before it: it asks the user to
    replug.
    """
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
    """USBDEVFS_RESET is _IO('U', 20).

    The scenarios record the ioctl instead of issuing it, so nothing else here
    would notice the day this number is edited, and an ioctl with the wrong
    request number on a real device does something else.
    """
    assert usb_reset.USBDEVFS_RESET == (ord("U") << 8) | 20, (
        f"the reset request number is {usb_reset.USBDEVFS_RESET}, which is not "
        f"_IO('U', 20) from linux/usbdevice_fs.h")
    print("PASS: the reset request number is the kernel's")


def main() -> None:
    fixtures.start_watchdog(240, label="scenario_usb_reset")

    # One ordinary controller first. It installs the integration globals and
    # warms every lazily started global thread, so a leg measures its own deck.
    warm = fixtures.make_headless_controller(serial="usb-reset-warm")
    fixtures.wait_until(lambda: warm.active_page is not None, timeout=10)
    fixtures.teardown(warm)

    test_the_reset_request_number_is_the_kernel_one()
    test_a_give_up_resets_the_deck_once()
    test_an_ambiguous_device_is_never_reset()
    test_the_open_retry_resets_and_tries_once_more()
    test_a_failure_that_is_not_transport_resets_nothing()
    test_a_device_of_another_vendor_is_never_reset()
    test_two_devices_with_one_serial_are_never_reset()
    test_a_sandbox_without_usb_nodes_degrades()
    print("ALL PASS: scenario_usb_reset")


if __name__ == "__main__":
    main()
