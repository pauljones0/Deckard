"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

One targeted USB reset for a deck that no longer answers its host.

A deck reaches a state where the device sits on the bus with its descriptors
readable and every open of its HID interface fails. The app then sees a deck it
cannot drive, and only a power cycle of the port revives it: a replug, or one
USBDEVFS_RESET ioctl on the device's usbfs node, which takes the device through
the same port reset and a fresh enumeration. The kernel keeps the device
address across that reset, and the uaccess ACL that gives a desktop session its
own devices makes the ioctl work without root.

The reset is a last step and never a first one. It runs from exactly two
places, each of which has already spent the recovery below it:

  * the reader supervisor's give-up latch, for a deck whose input reader is
    gone and whose handle would not open again;
  * the deck-open retry, for a deck that fails every attempt with a transport
    error and would otherwise be skipped for the rest of the session.

Each place resets a device once. The latch is per controller for the first and
per device identity for the second, and neither is a rate. A reset that does
not revive a deck must not be tried again: the device is then down for a reason
a reset does not reach, and a second one only costs the user another outage.
The two latches are independent, so a deck reset at startup can still be reset
once at run time, when its reader dies hours later.

Nothing here writes to the deck, and nothing here runs on the media thread,
which is the sole device writer. The give-up escalation runs on the watchdog
thread, at a point where the supervisor has already released the handle and
suspended every device write for that deck. The deck-open retry runs before a
media thread for that deck exists, and its transport arm released the handle
before it gave up. A reset issued while a write was in flight would pull the
transport out from under it.

The device is matched through sysfs, by vendor and product id first and then by
serial. A device that reports no serial is still matched, but only while it is
the sole device of its kind on the bus: resetting a second, healthy deck
because it is the same model is worse than resetting nothing at all.

A sandbox that carries no /dev/bus/usb sees none of this. The probe says so
once, and the recovery degrades to what it was before: a log line that asks the
user to replug.
"""
import fcntl
import os
import weakref
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger as log

from src.backend.DeckManagement.reader_supervisor import set_give_up_escalation

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.reader_supervisor import DeckReaderWatchdog


# _IO('U', 20) from linux/usbdevice_fs.h, the request number of USBDEVFS_RESET.
USBDEVFS_RESET = 21780

# Where the kernel lists USB devices, and where usbfs exposes their nodes. Both
# are module attributes, so a scenario points them at a tree it built and needs
# no device on the bus.
SYSFS_USB_DEVICES = "/sys/bus/usb/devices"
USB_DEV_NODES = "/dev/bus/usb"

# The vendor of every deck this app drives, as the integer the library reports.
# DeckManager holds the same vendor as the lower-case hex string udev uses.
ELGATO_VENDOR_ID = 0x0FD9

# How long the deck-open retry waits after a reset before it opens the device
# again. The device re-enumerates inside that window and its node is absent for
# part of it. The give-up escalation waits for nothing of its own: it hands the
# deck back to the supervisor, whose next attempt is one sweep away and retries
# for ten seconds.
RESET_SETTLE_S = 2.0

# Controllers whose give-up already spent this deck's one reset. The set holds
# weak references, so a controller that goes away takes its entry with it, and
# a replug, which builds a new controller, starts with a fresh one.
_escalated: "weakref.WeakSet[DeckController]" = weakref.WeakSet()

# Device identities the deck-open retry already reset in this process. That
# retry runs again for the same device on every hotplug event and on every
# round of the boot rescan, and one reset per round is a reset loop.
_reset_identities: set[str] = set()


class _Candidate(NamedTuple):
    """One USB device that carries the vendor and product id asked for."""

    name: str
    serial: "str | None"
    node: str


def _issue_reset_ioctl(fd: int) -> None:
    """Issue the reset on an open usbfs descriptor.

    The one syscall this module exists for, and a module attribute so a
    scenario records the call and the descriptor it was given without a device
    on the bus.
    """
    fcntl.ioctl(fd, USBDEVFS_RESET, 0)


def _read_sysfs(directory: str, name: str) -> "str | None":
    """One sysfs attribute as text, or None when it is absent or unreadable."""
    try:
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            return handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _read_ids(directory: str) -> "tuple[int, int] | None":
    """The vendor and product id of a sysfs entry, or None when it is not a
    device. An interface entry and a bus entry carry neither attribute."""
    vendor = _read_sysfs(directory, "idVendor")
    product = _read_sysfs(directory, "idProduct")
    if vendor is None or product is None:
        return None
    try:
        return int(vendor, 16), int(product, 16)
    except ValueError:
        return None


def _node_path(directory: str) -> "str | None":
    """The usbfs node of a sysfs entry, built from its bus and device number."""
    bus = _read_sysfs(directory, "busnum")
    device = _read_sysfs(directory, "devnum")
    if bus is None or device is None:
        return None
    try:
        return os.path.join(USB_DEV_NODES, f"{int(bus):03d}", f"{int(device):03d}")
    except ValueError:
        return None


def _candidates(vendor_id: int, product_id: int) -> "list[_Candidate]":
    """Every USB device on the bus with this vendor and product id."""
    found: "list[_Candidate]" = []
    try:
        names = sorted(os.listdir(SYSFS_USB_DEVICES))
    except OSError as e:
        log.warning(f"Cannot list the USB devices under {SYSFS_USB_DEVICES}: {e}")
        return found
    for name in names:
        directory = os.path.join(SYSFS_USB_DEVICES, name)
        if _read_ids(directory) != (vendor_id, product_id):
            continue
        node = _node_path(directory)
        if node is None:
            continue
        found.append(_Candidate(name=name, serial=_read_sysfs(directory, "serial"),
                                node=node))
    return found


def find_device_node(vendor_id: int, product_id: int, serial: "str | None",
                     label: str) -> "str | None":
    """The usbfs node of the one device this identity names, or None.

    A serial that names exactly one device decides it. Without that match the
    vendor and product id decide, and only while they name a single device: a
    wedged deck refuses the serial read that would tell it from a second deck
    of the same model, and that second deck is a healthy one.

    The serial the app holds comes from a HID feature report and the one in
    sysfs comes from the USB string descriptor. They agree on the decks seen so
    far, and a device where they disagree falls through to the single-device
    rule with a line that says so.
    """
    candidates = _candidates(vendor_id, product_id)
    if not candidates:
        log.warning(
            f"Deck {label}: no USB device with the id {vendor_id:04x}:{product_id:04x} is "
            f"listed under {SYSFS_USB_DEVICES}, so there is nothing to reset")
        return None
    if serial is not None:
        named = [c for c in candidates
                 if c.serial is not None and c.serial.casefold() == serial.casefold()]
        if len(named) == 1:
            return named[0].node
        if len(named) > 1:
            log.warning(
                f"Deck {label}: {len(named)} USB devices report this deck's serial, so the "
                f"deck cannot be told from them. No reset is issued. Replug the deck.")
            return None
    if len(candidates) == 1:
        log.info(
            f"Deck {label}: no USB device reports this deck's serial, and one device of "
            f"this model is on the bus, so {candidates[0].name} is the deck")
        return candidates[0].node
    log.warning(
        f"Deck {label}: {len(candidates)} devices of this model are on the bus and none "
        f"reports this deck's serial, so the wedged one cannot be told from the healthy "
        f"ones. No reset is issued. Replug the deck.")
    return None


def reset_usb_device(vendor_id: "int | None", product_id: "int | None",
                     serial: "str | None", label: str) -> "str | None":
    """Reset the device this identity names, and return the node that took the
    reset, or None when nothing was reset.

    The caller releases the deck handle before it asks: the reset takes the
    device through a fresh enumeration, and a write in flight would lose its
    transport under it.
    """
    if vendor_id != ELGATO_VENDOR_ID or product_id is None:
        # A fake deck, a remote deck, or a device this app does not drive.
        log.debug(f"Deck {label}: not an Elgato USB device, so no reset is issued")
        return None
    if not os.path.isdir(SYSFS_USB_DEVICES) or not os.path.isdir(USB_DEV_NODES):
        log.warning(
            f"Deck {label}: this process cannot see the USB device nodes "
            f"({SYSFS_USB_DEVICES} and {USB_DEV_NODES}). A sandbox without USB device "
            f"access carries neither, so the deck cannot be reset here. Replug it.")
        return None
    node = find_device_node(vendor_id, product_id, serial, label)
    if node is None:
        return None
    log.warning(
        f"Deck {label}: this deck does not answer any more, so the USB device at {node} "
        f"takes a reset. The device drops off the bus and comes back within a few "
        f"seconds, which is what a replug does by hand.")
    try:
        fd = os.open(node, os.O_WRONLY)
    except OSError as e:
        log.error(f"Deck {label}: cannot open the USB node {node} to reset it: {e}. "
                  f"Replug the deck.")
        return None
    try:
        _issue_reset_ioctl(fd)
    except OSError as e:
        log.error(f"Deck {label}: the USB reset of {node} failed: {e}. Replug the deck.")
        return None
    finally:
        os.close(fd)
    log.warning(f"Deck {label}: the USB reset of {node} was issued and the device "
                f"re-enumerates now.")
    return node


def _int_call(deck: object, name: str) -> "int | None":
    """One of the library's integer identity getters, or None when the deck
    carries no such getter or it raises."""
    getter = getattr(deck, name, None)
    if getter is None:
        return None
    try:
        value = getter()
    except Exception:
        return None
    return value if isinstance(value, int) else None


def _enumerated_serial(deck: object) -> "str | None":
    """The serial the USB enumeration reported for this deck, or None.

    This reads no device. hidapi carries the string descriptor it read when it
    found the deck on the transport object, so a deck whose handle is wedged
    still answers with it, and a deck that reports none leaves the match to the
    single-device rule.
    """
    getter = getattr(getattr(deck, "device", None), "serial_number", None)
    if getter is None:
        return None
    try:
        value = getter()
    except Exception:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _identity(deck: object, serial: "str | None") -> "tuple[int | None, int | None, str | None]":
    """The vendor id, product id and serial of a deck, read without touching
    the device.

    vendor_id() and product_id() answer from the enumeration the library did
    when it found the deck, so they hold with the handle closed and the device
    wedged. A serial read does not, so the caller passes the serial it already
    has and this falls back to the one the USB enumeration reported.
    """
    return (_int_call(deck, "vendor_id"), _int_call(deck, "product_id"),
            serial or _enumerated_serial(deck))


def reset_wedged_deck(deck: object, serial: "str | None" = None) -> "str | None":
    """Reset a deck that will not open, at most once per device identity for
    the life of the process, and return the node that took the reset.

    The deck-open retry calls this when every attempt failed with a transport
    error. That retry runs again for the same device on every later hotplug
    event and on every round of the boot rescan, so the latch here is what
    keeps a device that a reset does not revive from being reset in a loop.
    """
    vendor_id, product_id, resolved = _identity(deck, serial)
    label = resolved or "unknown"
    if vendor_id != ELGATO_VENDOR_ID or product_id is None:
        log.debug(f"Deck {label}: not an Elgato USB device, so no reset is issued")
        return None
    key = f"{vendor_id:04x}:{product_id:04x}:{resolved or 'no-serial'}"
    if key in _reset_identities:
        log.warning(
            f"Deck {label}: this device already took its one USB reset in this session "
            f"and still does not open, so it is skipped. Replug the deck.")
        return None
    # Latched before the reset and not after, so a reset that raises cannot
    # leave the door open for another one.
    _reset_identities.add(key)
    return reset_usb_device(vendor_id, product_id, resolved, label)


def _escalate(controller: "DeckController", watchdog: "DeckReaderWatchdog") -> None:
    """The reader supervisor's give-up escalation: one USB reset, then one more
    supervised round of reopen attempts.

    The supervisor calls this on the watchdog thread, outside every lock, at
    the moment it latches the give-up. That deck's handle is released and its
    device writes are suspended by then, so the reset cannot land inside a
    write, and the media thread is left alone.

    Once per controller. A deck that still does not answer after the reset and
    the round behind it is not a deck a second reset reaches, and a replug
    builds a new controller, which carries its own reset.
    """
    label = _label(controller)
    if controller in _escalated:
        log.warning(f"Deck {label}: the give-up already spent this deck's one USB reset, "
                    f"so it stays down. Replug it.")
        return
    # Latched before the reset, so a reset that raises cannot leave the door
    # open for another one on the next give-up.
    _escalated.add(controller)
    raw_device = getattr(getattr(controller, "deck", None), "deck", None)
    if raw_device is None:
        return
    # The serial the controller read at bring-up, which a wedged deck no longer
    # answers for.
    vendor_id, product_id, serial = _identity(
        raw_device, getattr(controller, "_serial_number", None))
    if reset_usb_device(vendor_id, product_id, serial, label) is None:
        return
    # supervisor_for() belongs to the watchdog thread, which is the thread the
    # give-up latch calls this on.
    watchdog.supervisor_for(controller).allow_one_more_round()


def _label(controller: "DeckController") -> str:
    return getattr(controller, "_serial_number", None) or "unknown"


def install_give_up_escalation(watchdog: "DeckReaderWatchdog") -> None:
    """Wire the reader supervisor's give-up latch to the USB reset. One call,
    where the watchdog is built."""

    def escalate(controller: "DeckController") -> None:
        _escalate(controller, watchdog)

    set_give_up_escalation(escalate)
