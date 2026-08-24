"""The liveness probe enumerates only the deck's own kind of device.

Asking hidapi whether a device is still on the bus means a hid_enumerate, and
the libusb backend opens every device that passes the vendor and product
filter to read its string descriptors. With no filter that is every USB HID
device on the machine, several times a minute, under the process-wide hidapi
mutex every deck read and write also waits on.

The bus here models both halves of that call: the filter, and the open each
passing device pays. Every leg reads the open counters of the devices that are
not the deck, so a probe that widens its filter again is a failed assert and
not a slow test.
"""
import threading

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

from fixtures import FaultyFakeDeck, start_watchdog

from src.backend.DeckManagement import reader_supervisor
from src.backend.DeckManagement.BetterDeck import BetterDeck, device_is_on_bus
from src.backend.DeckManagement.fair_lock import FairLock

ELGATO = 0x0FD9
STREAMDECK_PLUS = 0x0084
STREAMDECK_XL = 0x006C
# Mirabox, the one supported make that is not Elgato. A probe that hard-wired
# the Elgato vendor would enumerate the wrong half of the bus for this deck.
MIRABOX = 0x5548
STREAMDOCK_293S = 0x6670


class BusDevice:
    """One HID device on the modeled bus, counting the times it was opened."""

    def __init__(self, path: str, vendor_id: int, product_id: int, serial: str = ""):
        self.path = path
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.serial = serial
        self.opens = 0

    def entry(self) -> dict:
        return {
            "path": self.path,
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
            "serial_number": self.serial,
        }


class RecordingHidapi:
    """The library's hidapi loader, with hid_enumerate modeled.

    Two facts are modeled, because together they are the whole cost. A vendor
    or product of zero matches everything, which is what an unfiltered call
    passes. Every device that passes the filter is opened to read its strings,
    and each one counts that open.
    """

    def __init__(self, bus: "list[BusDevice]"):
        self.bus = bus
        # (vendor_id, product_id) per call, in order.
        self.calls: list[tuple[int, int]] = []

    def enumerate(self, vendor_id: int = 0, product_id: int = 0) -> list[dict]:
        self.calls.append((vendor_id, product_id))
        found = []
        for device in self.bus:
            if vendor_id and device.vendor_id != vendor_id:
                continue
            if product_id and device.product_id != product_id:
                continue
            device.opens += 1
            found.append(device.entry())
        return found

    def opens_off(self, *keep: BusDevice) -> int:
        """Opens counted on every device but the ones named."""
        return sum(d.opens for d in self.bus if d not in keep)


class HidTransport:
    """The transport object the library hangs on a deck it enumerated.

    It carries the loader, the enumeration entry the device was found by and
    the per-device mutex the fair transport lock replaces. connected() is the
    library's own answer, the unfiltered walk, which the fallback leg and the
    control leg both drive.
    """

    def __init__(self, hidapi: RecordingHidapi, device: BusDevice):
        # The two attributes the probe reads. A leg takes each of them away to
        # model library drift, so connected() below keeps private copies: the
        # library's own answer has to survive the drift the fallback is for.
        self.hidapi = hidapi
        self.device_info = device.entry()
        self._library = hidapi
        self._path = device.path
        self.mutex = FairLock()

    def connected(self) -> bool:
        return any(entry["path"] == self._path
                   for entry in self._library.enumerate())


class HidFakeDeck(FaultyFakeDeck):
    """A fake deck that carries a hidapi transport, as a real deck does.

    FakeDeck answers connected() from its own flag and models no transport, so
    no scenario over it can see which enumeration the probe runs.
    """

    def __init__(self, *args, transport: HidTransport, **kwargs):
        super().__init__(*args, **kwargs)
        self.device = transport

    def connected(self) -> bool:
        return self.device.connected()

    def vendor_id(self) -> int:
        return int(self.device.device_info["vendor_id"])

    def product_id(self) -> int:
        return int(self.device.device_info["product_id"])


def make_bus() -> "tuple[RecordingHidapi, BusDevice, dict[str, BusDevice]]":
    """A bus with the deck under test, a second Elgato deck and four devices
    of other makes: a keyboard, a mouse, a headset and a game controller."""
    deck = BusDevice("/dev/hid/deck", ELGATO, STREAMDECK_PLUS, "plus-1")
    others = {
        "second_deck": BusDevice("/dev/hid/xl", ELGATO, STREAMDECK_XL, "xl-1"),
        "keyboard": BusDevice("/dev/hid/kbd", 0x1462, 0x1001, "kbd-1"),
        "mouse": BusDevice("/dev/hid/mouse", 0x046D, 0xC08B, "mouse-1"),
        "headset": BusDevice("/dev/hid/headset", 0x0B05, 0x1A00, "hs-1"),
        "pad": BusDevice("/dev/hid/pad", 0x054C, 0x0CE6, "pad-1"),
    }
    bus = [deck] + list(others.values())
    return RecordingHidapi(bus), deck, others


def test_a_present_deck_answers_yes_and_opens_nothing_else() -> None:
    hidapi, deck_device, others = make_bus()
    deck = HidFakeDeck(serial_number="hid-present",
                       transport=HidTransport(hidapi, deck_device))
    better = BetterDeck(deck)

    assert better.connected(), "a deck on the bus was reported gone"
    assert hidapi.calls == [(ELGATO, STREAMDECK_PLUS)], (
        f"the probe enumerated with {hidapi.calls}, not once with the deck's "
        f"own vendor and product")
    assert hidapi.opens_off(deck_device) == 0, (
        f"the probe opened devices that are not the deck: "
        f"{[(d.path, d.opens) for d in hidapi.bus if d.opens and d is not deck_device]}")
    assert deck_device.opens == 1, (
        f"the deck was opened {deck_device.opens} times, not once")
    assert others["second_deck"].opens == 0, (
        "the probe opened the other deck, so the product id is not in the filter")
    print("PASS: a present deck answers yes and no other device is opened")


def test_an_absent_deck_answers_no() -> None:
    hidapi, deck_device, others = make_bus()
    deck = HidFakeDeck(serial_number="hid-absent",
                       transport=HidTransport(hidapi, deck_device))
    better = BetterDeck(deck)

    # Unplug the deck and leave every other device where it is, which is what
    # the disconnect sweep and the reader watchdog have to tell apart.
    hidapi.bus.remove(deck_device)

    assert not better.connected(), "an unplugged deck was reported present"
    assert hidapi.calls == [(ELGATO, STREAMDECK_PLUS)], (
        f"the probe enumerated with {hidapi.calls}")
    assert hidapi.opens_off() == 0, (
        "the probe opened a device while answering for an absent deck")

    # A deck of the same model plugged back in on another port answers no,
    # because the path is the identity and the new one carries a fresh path.
    hidapi.bus.append(BusDevice("/dev/hid/deck-b", ELGATO, STREAMDECK_PLUS, "plus-1"))
    assert not better.connected(), (
        "a different device of the same model was taken for this deck")
    print("PASS: an absent deck answers no")


def test_the_unfiltered_walk_opens_the_whole_bus() -> None:
    """The control. It runs the library's own answer over the same bus, so
    the counters above are read against a real number and not against zero."""
    hidapi, deck_device, _ = make_bus()
    transport = HidTransport(hidapi, deck_device)

    assert transport.connected(), "the control answer lost the deck"
    assert hidapi.calls == [(0, 0)], (
        f"the library's own answer enumerated with {hidapi.calls}, not unfiltered")
    opened = [d.path for d in hidapi.bus if d.opens]
    assert len(opened) == len(hidapi.bus), (
        f"the unfiltered walk opened {len(opened)} of {len(hidapi.bus)} devices; "
        f"the model of hid_enumerate no longer matches the cost it stands for")
    print(f"PASS: the unfiltered walk opens all {len(opened)} devices on the bus")


def test_the_filter_follows_the_deck_and_not_one_vendor() -> None:
    hidapi, elgato_device, _ = make_bus()
    mirabox = BusDevice("/dev/hid/dock", MIRABOX, STREAMDOCK_293S, "dock-1")
    hidapi.bus.append(mirabox)
    deck = HidFakeDeck(serial_number="hid-mirabox",
                       transport=HidTransport(hidapi, mirabox))
    better = BetterDeck(deck)

    assert better.connected(), "a supported deck of another make was reported gone"
    assert hidapi.calls == [(MIRABOX, STREAMDOCK_293S)], (
        f"the probe enumerated with {hidapi.calls}, not with this deck's own "
        f"vendor; a hard-wired vendor would scan the wrong half of the bus")
    assert elgato_device.opens == 0, (
        "the probe opened an Elgato device while asking about a Mirabox deck")
    print("PASS: the filter follows the deck's own vendor and product")


def test_a_deck_with_no_hid_transport_keeps_the_library_answer() -> None:
    """A fake deck and a remote deck carry no hidapi transport. Neither may
    lose its answer to a probe that assumes one."""
    plain = FaultyFakeDeck(serial_number="hid-fallback")
    better = BetterDeck(plain)
    assert better.connected(), "a fake deck lost its connected answer"
    plain.simulate_unplug()
    assert not better.connected(), "an unplugged fake deck still answered yes"

    # A transport that carries a mutex and nothing else is the shape library
    # drift leaves behind. It falls back the same way.
    hidapi, deck_device, _ = make_bus()
    drifted = HidFakeDeck(serial_number="hid-drift",
                          transport=HidTransport(hidapi, deck_device))
    del drifted.device.hidapi
    assert device_is_on_bus(drifted), (
        "a transport with no hidapi attribute lost its answer")
    assert hidapi.calls == [(0, 0)], (
        f"the fallback did not reach the library's own answer: {hidapi.calls}")

    # An entry with no path is the other drift shape: the loader is there and
    # the enumeration dict no longer carries the key the identity comes from.
    keyless = HidFakeDeck(serial_number="hid-keyless",
                          transport=HidTransport(hidapi, deck_device))
    keyless.device.device_info.pop("path")
    hidapi.calls.clear()
    assert device_is_on_bus(keyless), "a transport with no path lost its answer"
    assert hidapi.calls == [(0, 0)], (
        f"the keyless fallback did not reach the library's own answer: {hidapi.calls}")
    print("PASS: a deck with no hid transport keeps the library's answer")


def test_the_reader_watchdog_probe_is_filtered() -> None:
    """The watchdog asks this question of every deck whose reader has exited,
    and it is the caller that pays for it every sweep."""
    hidapi, deck_device, _ = make_bus()
    deck = HidFakeDeck(serial_number="hid-watchdog",
                       transport=HidTransport(hidapi, deck_device))
    better = BetterDeck(deck)

    assert reader_supervisor._still_connected(better), (
        "the watchdog reported a present deck gone")
    assert hidapi.calls == [(ELGATO, STREAMDECK_PLUS)], (
        f"the watchdog probe enumerated with {hidapi.calls}")
    assert hidapi.opens_off(deck_device) == 0, (
        "the watchdog probe opened devices that are not the deck")
    print("PASS: the reader watchdog probe is filtered")


def test_the_probe_does_not_wait_on_the_device_write_lock() -> None:
    """The probe reads no handle, so it must not queue behind an image write.

    The transport lock is FIFO and every deck write takes it. A probe that
    took it too would put a status question in that queue, and answer only
    after the write in front of it drained.
    """
    hidapi, deck_device, _ = make_bus()
    deck = HidFakeDeck(serial_number="hid-lock",
                       transport=HidTransport(hidapi, deck_device))
    better = BetterDeck(deck)

    answered = threading.Event()
    result: list[bool] = []
    holding = threading.Event()
    release = threading.Event()

    def hold_the_write_lock() -> None:
        with deck.device.mutex:
            holding.set()
            release.wait(30)

    def probe() -> None:
        result.append(better.connected())
        answered.set()

    writer = threading.Thread(target=hold_the_write_lock, name="hid-lock-writer")
    writer.start()
    try:
        assert holding.wait(10), "the stand-in writer never took the transport lock"
        prober = threading.Thread(target=probe, name="hid-lock-prober")
        prober.start()
        assert answered.wait(10), (
            "the probe never answered while a write held the transport lock")
        prober.join(10)
        assert result == [True], f"the probe answered {result}"
    finally:
        release.set()
        writer.join(10)
    print("PASS: the probe does not wait on the device write lock")


def main() -> None:
    # A probe that parked on a lock is what this scenario is about, so it must
    # fail loud rather than sit until run_all.py's per-scenario timeout.
    start_watchdog(60, label="scenario_filtered_hid_enumeration")
    # The unit tier. Every leg builds a fake deck, which reads its key layout
    # from the settings manager, and nothing here needs a controller.
    fixtures.install_stub_globals()

    test_a_present_deck_answers_yes_and_opens_nothing_else()
    test_an_absent_deck_answers_no()
    test_the_unfiltered_walk_opens_the_whole_bus()
    test_the_filter_follows_the_deck_and_not_one_vendor()
    test_a_deck_with_no_hid_transport_keeps_the_library_answer()
    test_the_reader_watchdog_probe_is_filtered()
    test_the_probe_does_not_wait_on_the_device_write_lock()
    print("ALL PASS: scenario_filtered_hid_enumeration")


if __name__ == "__main__":
    main()
