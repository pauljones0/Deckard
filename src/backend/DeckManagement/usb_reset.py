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
which is the sole device writer. A reset issued while a write was in flight
would pull the transport out from under it, so each caller has stopped every
write to that device first. The give-up escalation runs on the watchdog
thread, where the supervisor has suspended every device write for the deck and
its reader is gone; a handle that is still open there is closed by the next
attempt before it opens one. The deck-open retry runs before a media thread
for that deck exists, and its transport arm released the handle before it gave
up.

The device is matched through sysfs, by vendor and product id first and then by
serial. A device that reports no serial is still matched, but only while it is
the sole device of its kind on the bus: resetting a second, healthy deck
because it is the same model is worse than resetting nothing at all.

That match names a node, and a node is not a device. A wedged deck can leave
the bus between the sysfs walk and the open, and the kernel gives its bus and
device number to whatever arrives next, so the node then points at a stranger.
The open is therefore followed by a second identity check, on the descriptor it
returned: the device descriptor at the head of the node carries the vendor and
product id of the device that descriptor refers to, and the ioctl goes to the
same descriptor. A device that answers with any other identity is left alone.

A sandbox that carries no /dev/bus/usb sees none of this. The probe says so
once, and the recovery degrades to what it was before: a log line that asks the
user to replug.
"""
import fcntl
import os
import threading
import weakref
from typing import TYPE_CHECKING, NamedTuple, cast

from loguru import logger as log

from src.backend.DeckManagement.reader_supervisor import set_give_up_escalation

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.reader_supervisor import DeckReaderWatchdog


# _IO('U', 20) from linux/usbdevice_fs.h, the request number of USBDEVFS_RESET.
USBDEVFS_RESET = 21780

# Where the kernel lists USB devices, and where usbfs exposes their nodes. Both are module
# attributes, so a scenario points them at a tree it built and needs no device on the bus.
SYSFS_USB_DEVICES = "/sys/bus/usb/devices"
USB_DEV_NODES = "/dev/bus/usb"

# The vendor of every deck this app drives, as the integer the library reports.
# DeckManager holds the same vendor as the lower-case hex string udev uses.
ELGATO_VENDOR_ID = 0x0FD9

# The USB device descriptor, which a usbfs node carries at offset zero.
DEVICE_DESCRIPTOR_LEN = 18
DESCRIPTOR_VENDOR_OFFSET = 8
DESCRIPTOR_PRODUCT_OFFSET = 10

# How long the deck-open retry waits after a reset before it opens the device again.
RESET_SETTLE_S = 2.0

# Controllers whose give-up spent this deck's one reset, which means a reset that was issued: an
# escalation that reached no device leaves nothing here.
_escalated: "weakref.WeakSet[DeckController]" = weakref.WeakSet()

# Device identities that took a reset from the deck-open retry in this process.
_reset_identities: set[str] = set()
_reset_identities_lock = threading.Lock()

# The watchdog the installed hook serves, weakly, so a teardown clears its own
# hook and never one a later manager installed.
_escalation_watchdog: "weakref.ReferenceType[DeckReaderWatchdog] | None" = None


class _Candidate(NamedTuple):
    """One USB device that carries the vendor and product id asked for."""

    name: str
    serial: "str | None"
    node: str


def _issue_reset_ioctl(fd: int) -> None:
    """
    Issue the reset on an open usbfs descriptor.
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
    """
    The usbfs node of the one device this identity names, or None.
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


def _descriptor_identity(fd: int) -> "tuple[int, int] | None":
    """
    The vendor and product id the open node itself reports, or None when it carries no readable
    device descriptor.
    """
    try:
        descriptor = os.read(fd, DEVICE_DESCRIPTOR_LEN)
    except OSError:
        return None
    if len(descriptor) < DEVICE_DESCRIPTOR_LEN:
        return None
    return (
        int.from_bytes(
            descriptor[DESCRIPTOR_VENDOR_OFFSET:DESCRIPTOR_VENDOR_OFFSET + 2], "little"),
        int.from_bytes(
            descriptor[DESCRIPTOR_PRODUCT_OFFSET:DESCRIPTOR_PRODUCT_OFFSET + 2], "little"),
    )


def reset_usb_device(vendor_id: "int | None", product_id: "int | None",
                     serial: "str | None", label: str) -> "str | None":
    """
    Reset the device this identity names, and return the node that took the reset, or None when
    nothing was reset.
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
        # Read and write: the ioctl needs the write side, and the identity
        # check below needs the read side.
        fd = os.open(node, os.O_RDWR)
    except OSError as e:
        log.error(f"Deck {label}: cannot open the USB node {node} to reset it: {e}. "
                  f"Replug the deck.")
        return None
    try:
        # The device behind the node, asked through the descriptor the ioctl goes to.
        found = _descriptor_identity(fd)
        if found != (vendor_id, product_id):
            reported = ("no readable device descriptor" if found is None
                        else f"{found[0]:04x}:{found[1]:04x}")
            log.error(
                f"Deck {label}: the USB node {node} reports {reported}, not "
                f"{vendor_id:04x}:{product_id:04x}, so it is another device now. No "
                f"reset is issued. Replug the deck.")
            return None
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
    """
    The serial the USB enumeration reported for this deck, or None.
    """
    getter = getattr(getattr(deck, "device", None), "serial_number", None)
    if getter is None:
        return None
    try:
        value = getter()
    except Exception:
        return None
    if isinstance(value, str) and value.strip():
        return cast(str, value.strip())
    return None


def _identity(deck: object, serial: "str | None") -> "tuple[int | None, int | None, str | None]":
    """
    The vendor id, product id and serial of a deck, read without touching the device.
    """
    return (_int_call(deck, "vendor_id"), _int_call(deck, "product_id"),
            serial or _enumerated_serial(deck))


def reset_wedged_deck(deck: object, serial: "str | None" = None) -> "str | None":
    """
    Reset a deck that will not open, at most once per device identity for the life of the process,
    and return the node that took the reset.
    """
    vendor_id, product_id, resolved = _identity(deck, serial)
    label = resolved or "unknown"
    if vendor_id != ELGATO_VENDOR_ID or product_id is None:
        log.debug(f"Deck {label}: not an Elgato USB device, so no reset is issued")
        return None
    key = f"{vendor_id:04x}:{product_id:04x}:{resolved or 'no-serial'}"
    with _reset_identities_lock:
        if key in _reset_identities:
            log.warning(
                f"Deck {label}: this device already took its one USB reset in this "
                f"session and still does not open, so it is skipped. Replug the deck.")
            return None
        # Claimed ahead of the reset, so two threads cannot both reset this device: the boot
        # enumeration and the USB hotplug monitor reach here on threads of their own.
        _reset_identities.add(key)
    node = reset_usb_device(vendor_id, product_id, resolved, label)
    if node is None:
        # No reset was issued, so this device has not spent anything.
        with _reset_identities_lock:
            _reset_identities.discard(key)
    return node


def _escalate(controller: "DeckController", watchdog: "DeckReaderWatchdog") -> None:
    """
    The reader supervisor's give-up escalation: one USB reset, then one more supervised round of
    reopen attempts.
    """
    label = _label(controller)
    if controller in _escalated:
        log.warning(f"Deck {label}: the give-up already spent this deck's one USB reset, "
                    f"so it stays down. Replug it.")
        return
    raw_device = getattr(getattr(controller, "deck", None), "deck", None)
    if raw_device is None:
        return
    # The serial the controller read at bring-up, which a wedged deck no longer
    # answers for.
    vendor_id, product_id, serial = _identity(
        raw_device, getattr(controller, "_serial_number", None))
    if reset_usb_device(vendor_id, product_id, serial, label) is None:
        # Nothing was reset, so nothing is latched and nothing is re-armed.
        return
    _escalated.add(controller)
    # supervisor_for() belongs to the watchdog thread, which is the thread the
    # give-up latch calls this on.
    watchdog.supervisor_for(controller).allow_one_more_round()


def _label(controller: "DeckController") -> str:
    return cast(str, getattr(controller, "_serial_number", None) or "unknown")


def install_give_up_escalation(watchdog: "DeckReaderWatchdog") -> None:
    """
    Wire the reader supervisor's give-up latch to the USB reset.
    """
    global _escalation_watchdog

    watchdog_ref = weakref.ref(watchdog)

    def escalate(controller: "DeckController") -> None:
        live_watchdog = watchdog_ref()
        if live_watchdog is None:
            return
        _escalate(controller, live_watchdog)

    _escalation_watchdog = watchdog_ref
    set_give_up_escalation(escalate)


def clear_give_up_escalation(watchdog: "DeckReaderWatchdog") -> None:
    """
    Take the hook back out, where this watchdog stops.
    """
    global _escalation_watchdog

    installed = _escalation_watchdog() if _escalation_watchdog is not None else None
    if installed is not watchdog:
        return
    _escalation_watchdog = None
    set_give_up_escalation(None)
