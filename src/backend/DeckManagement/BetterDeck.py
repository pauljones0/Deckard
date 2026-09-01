import contextlib
import os
import threading
import traceback
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar, cast

from src.backend.DeckManagement.strip_geometry import SlotOrder

if TYPE_CHECKING:
    import asyncio

_ElemT = TypeVar("_ElemT")


class ImageFormat(TypedDict):
    """The image format dict every StreamDeck device class returns for its
    keys, its touchscreen and its screen alike."""

    size: tuple[int, int]
    format: str
    flip: tuple[bool, bool]
    rotation: int

from loguru import logger as log
from StreamDeck.Devices import StreamDeck
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType


# How long a release waits for the library reader to leave its loop. A
# scenario raises it; the release order it asserts does not change with it.
READ_THREAD_JOIN_TIMEOUT_S = 1.0


class _BlockedOpen:
    """Ignore reader-loop reopen calls until a deliberate open removes this shadow."""

    def __init__(self, device_name: str) -> None:
        self.device_name = device_name

    def __call__(self, *args: object, **kwargs: object) -> None:
        log.debug(f"Ignoring a reopen of the released {self.device_name} handle")


def _install_release_shadow(device: "Any") -> None:
    """
    Shadows open() on this device instance, so nothing re-opens it. open_device_handle lifts it, so
    a handle released by a failed attempt can still be taken up again.
    """
    if isinstance(getattr(device, "open", None), _BlockedOpen):
        return
    try:
        device.open = _BlockedOpen(type(device).__name__)
    except (AttributeError, TypeError):
        # A handle that refuses the attribute keeps its own open(), and the
        # two flags are then the only defense against the resume loop.
        log.warning(f"Could not shadow open() on {type(device).__name__}; "
                    f"its reader can still re-open the released handle")


def open_device_handle(device: "Any", resume_from_suspend: bool = True) -> None:
    """
    Opens a device handle, and lifts any release shadow first. The deck-open retry re-uses the
    handle of an attempt that released it, and only this makes that handle take an open() again.
    """
    if isinstance(getattr(device, "open", None), _BlockedOpen):
        del device.open
    device.open(resume_from_suspend)


def stop_device_read_thread(device: "StreamDeck.StreamDeck", timeout: "float | None" = None) -> None:
    """Clear both reader flags and join the raw handle's reader with a bound.
    A reader already in its resume loop also needs a release shadow."""
    # FakeDeck and RemoteDeck have no read thread and no run_read_thread
    # attribute, so this guard returns early for them.
    if not hasattr(device, "run_read_thread"):
        return
    # Disarm the resume loop first, so a transport error raised between these
    # two writes cannot carry the reader into it.
    if hasattr(device, "reconnect_after_suspend"):
        device.reconnect_after_suspend = False
    device.run_read_thread = False
    read_thread = getattr(device, "read_thread", None)
    if read_thread is not None and read_thread is not threading.current_thread():
        with contextlib.suppress(RuntimeError):
            read_thread.join(READ_THREAD_JOIN_TIMEOUT_S if timeout is None else timeout)


def release_device_handle(device: "StreamDeck.StreamDeck", timeout: "float | None" = None) -> None:
    """Stop the reader, shadow open(), and close a raw handle.
    A caller that owns a media writer must stop it before this call."""
    _install_release_shadow(device)
    stop_device_read_thread(device, timeout)
    device.close()


def device_is_on_bus(deck: "Any") -> bool:
    """Check this device path through a vendor/product-filtered HID enumeration.
    Fall back to the library probe when the transport cannot provide that data."""
    device = getattr(deck, "device", None)
    hidapi = getattr(device, "hidapi", None)
    info = getattr(device, "device_info", None)
    if hidapi is None or not isinstance(info, dict):
        return cast(bool, deck.connected())

    path = info.get("path")
    vendor_id = info.get("vendor_id")
    product_id = info.get("product_id")
    if path is None or vendor_id is None or product_id is None:
        return cast(bool, deck.connected())

    # Positional, because the library spells these parameters vendor_id and
    # product_id on the loader and vid and pid on the transport above it.
    try:
        entries = hidapi.enumerate(vendor_id, product_id)
    except Exception:
        return cast(bool, deck.connected())
    return any(entry.get("path") == path for entry in entries)


class BetterDeck():
    def __init__(self, deck: StreamDeck.StreamDeck, rotation: int = 0):
        self.deck: StreamDeck.StreamDeck = deck
        self.rotation: int = rotation # [0, 90, 180, 270]
        # Serializes device I/O. hidapi is not thread-safe, and several
        # threads write to the deck. Reentrant for nested wrapped calls.
        self._lock = threading.RLock()

        # The owner assertion detects a device write from any thread other than the registered
        # writer. It only logs and never raises, because it is a harness and dev detector.
        self._assert_owner: bool = bool(os.environ.get("DECKARD_ASSERT_DEVICE_OWNER"))
        self._expected_writer: threading.Thread | None = None
        self.owner_violations: list[tuple[str, str, str]] = []

    def set_expected_writer(self, thread: threading.Thread | None) -> None:
        """
        Registers the thread that performs all device writes. _check_owner logs a warning when
        another thread writes to the device.
        """
        self._expected_writer = thread

    def _check_owner(self, method_name: str) -> None:
        if not self._assert_owner or self._expected_writer is None:
            return
        current = threading.current_thread()
        if current is self._expected_writer:
            return
        stack_summary = "".join(traceback.format_stack(limit=8))
        self.owner_violations.append((method_name, current.name, stack_summary))
        log.warning(
            f"Device owner violation: {method_name}() called from thread "
            f"'{current.name}', expected '{self._expected_writer.name}'"
        )


    def open(self) -> None:
        """
        Opens the device for input/output. This must be called prior to setting
        or retrieving any device state.

        .. seealso:: See :func:`~StreamDeck.close` for the corresponding close method.
        """
        # Use the guarded path because a released raw handle ignores a direct open().
        self.open_handle()

    def close(self) -> None:
        """
        Closes the device for input/output.

        .. seealso:: See :func:`~StreamDeck.open` for the corresponding open method.
        """
        with self._lock:
            self.deck.close()

    def stop_read_thread(self, timeout: "float | None" = None) -> None:
        """Stops the library reader thread on the wrapped device, and leaves
        the handle open. See stop_device_read_thread."""
        stop_device_read_thread(self.deck, timeout)

    def release_handle(self, timeout: "float | None" = None) -> None:
        """Under the device lock, shadow open(), stop the reader, then close.
        This serializes release against multi-chunk writes and deliberate opens."""
        with self._lock:
            _install_release_shadow(self.deck)
            stop_device_read_thread(self.deck, timeout)
            self.close()

    def open_handle(self, resume_from_suspend: bool = True,
                    guard: "Callable[[], bool] | None" = None) -> bool:
        """Under the device lock, recheck the guard, then lift the shadow and open.
        Refuse a live reader because the library would join it without a bound."""
        with self._lock:
            if guard is not None and not guard():
                return False
            read_thread = getattr(self.deck, "read_thread", None)
            if read_thread is not None and read_thread.is_alive():
                log.warning(f"Refusing to reopen the {type(self.deck).__name__} handle: "
                            f"its reader thread is still running, and the library's open "
                            f"would wait for that thread with no bound")
                return False
            open_device_handle(self.deck, resume_from_suspend)
            return True

    def is_open(self) -> bool:
        """
        Indicates if the StreamDeck device is currently open and ready for use.

        :rtype: bool
        :return: `True` if the deck is open, `False` otherwise.
        """
        # This takes no BetterDeck lock. A status probe must not stall behind a multi-chunk image
        # write, and the transport's per-chunk mutex covers close() races.
        return cast(bool, self.deck.is_open())

    def connected(self) -> bool:
        """
        Indicates if the physical StreamDeck device this instance is attached to
        is still connected to the host.

        :rtype: bool
        :return: `True` if the deck is still connected, `False` otherwise.
        """
        # This takes no BetterDeck lock, see is_open(). The enumeration it
        # runs reads no handle, so a close() during it changes no answer.
        return device_is_on_bus(self.deck)

    def vendor_id(self) -> int:
        """
        Retrieves the vendor ID attached StreamDeck. This can be used
        to determine the exact type of attached StreamDeck.

        :rtype: int
        :return: Vendor ID of the attached device.
        """
        return cast(int, self.deck.vendor_id())

    def product_id(self) -> int:
        """
        Retrieves the product ID attached StreamDeck. This can be used
        to determine the exact type of attached StreamDeck.

        :rtype: int
        :return: Product ID of the attached device.
        """
        return cast(int, self.deck.product_id())

    def id(self) -> str:
        """
        Retrieves the physical ID of the attached StreamDeck. This can be used
        to differentiate one StreamDeck from another.

        :rtype: str
        :return: Identifier for the attached device.
        """
        return cast(str, self.deck.id())

    def key_count(self) -> int:
        """
        Retrieves number of physical buttons on the attached StreamDeck device.

        :rtype: int
        :return: Number of physical buttons.
        """
        return cast(int, self.deck.key_count())

    def touch_key_count(self) -> int:
        """
        Retrieves number of touch buttons on the attached StreamDeck device.

        :rtype: int
        :return: Number of touch buttons.
        """
        return cast(int, self.deck.touch_key_count())

    def dial_count(self) -> int:
        """
        Retrieves number of physical dials on the attached StreamDeck device.

        :rtype: int
        :return: Number of physical dials
        """
        return cast(int, self.deck.dial_count())

    def deck_type(self) -> str:
        """
        Retrieves the model of Stream Deck.

        :rtype: str
        :return: String containing the model name of the StreamDeck device.
        """
        with self._lock:
            return cast(str, self.deck.deck_type())

    def is_visual(self) -> bool:
        """
        Returns whether the Stream Deck has a visual display output.

        :rtype: bool
        :return: `True` if the deck has a screen, `False` otherwise.
        """
        return cast(bool, self.deck.is_visual())

    def is_touch(self) -> bool:
        """
        Returns whether the Stream Deck can receive touch events

        :rtype: bool
        :return: `True` if the deck can receive touch events, `False` otherwise
        """
        return cast(bool, self.deck.is_touch())

    def key_layout(self) -> tuple[int, int]:
        """
        Retrieves the physical button layout on the attached StreamDeck device.

        :rtype: (int, int)
        :return (rows, columns): Number of button rows and columns.
        """
        rows, cols = cast("tuple[int, int]", self.deck.key_layout())
        if self.rotation in [0, 180]:
            return rows, cols
        else:
            return cols, rows

    def key_image_format(self) -> "ImageFormat":
        """
        Retrieves the image format accepted by the attached StreamDeck device.
        Images should be given in this format when setting an image on a button.

        .. seealso:: See :func:`~StreamDeck.set_key_image` method to update the
                     image displayed on a StreamDeck button.

        :rtype: dict()
        :return: Dictionary describing the various image parameters
                 (size, image format, image mirroring and rotation).
        """
        return cast("ImageFormat", self.deck.key_image_format())

    def touchscreen_image_format(self) -> "ImageFormat":
        """
        Retrieves the image format accepted by the touchscreen of the Stream
        Deck. Images should be given in this format when drawing on
        touchscreen.

        .. seealso:: See :func:`~StreamDeck.set_touchscreen_image` method to
                     draw an image on the StreamDeck touchscreen.

        :rtype: dict()
        :return: Dictionary describing the various image parameters
                 (size, image format).
        """
        return cast("ImageFormat", self.deck.touchscreen_image_format())

    def screen_image_format(self) -> "ImageFormat":
        """
        Retrieves the image format accepted by the screen of the Stream
        Deck. Images should be given in this format when drawing on
        screen.

        .. seealso:: See :func:`~StreamDeck.set_screen_image` method to
                     draw an image on the StreamDeck screen.

        :rtype: dict()
        :return: Dictionary describing the various image parameters
                 (size, image format).
        """
        return cast("ImageFormat", self.deck.screen_image_format())
    
    def set_poll_frequency(self, hz: float) -> None:
        """
        Sets the frequency of the button polling reader thread, determining how
        often the StreamDeck will be polled for button changes.

        A higher frequency will result in a higher CPU usage, but a lower
        latency between a physical button press and a event from the library.

        :param int hz: Reader thread frequency, in Hz (1-1000).
        """
        with self._lock:
            self.deck.set_poll_frequency(hz)

    def _remap_key_event_index(self, physical_index: int) -> "int | None":
        """
        Maps a physical key-callback index to its logical grid index, or None when the event must
        not dispatch a grid key.
        """
        rows, cols = self.deck.key_layout()
        if not 0 <= physical_index < rows * cols:
            # A touch or extra button past the grid, or a stray index. Drop it rather than remap it.
            # This is a per-press event on hardware that has such buttons, so it logs at debug.
            log.debug(f"Dropping key event {physical_index}: past the "
                      f"{rows}x{cols} key grid (no grid position)")
            return None
        logical_key = self.get_logical_index(physical_index)
        if logical_key is None:
            # An in-grid index that still maps to None means a rotation outside the four the mapper
            # handles.
            log.warning(f"Dropping key event {physical_index}: rotation "
                        f"{self.rotation!r} maps no keys")
        return logical_key

    def set_key_callback(self, callback: "Callable[[StreamDeck.StreamDeck, int, bool], None]") -> None:
        """
        Sets the callback function called each time a button on the StreamDeck
        changes state (either pressed, or released).

        .. note:: This callback will be fired from an internal reader thread.
                  Ensure that the given callback function is thread-safe.

        .. note:: Only one callback can be registered at one time.

        .. seealso:: See :func:`~StreamDeck.set_key_callback_async` method for
                     a version compatible with Python 3 `asyncio` asynchronous
                     functions.

        :param function callback: Callback function to fire each time a button
                                state changes.
        """
        def remapper_callback(deck: "StreamDeck.StreamDeck", key: int, state: bool) -> None:
            logical_key = self._remap_key_event_index(key)
            if logical_key is None:
                return
            callback(deck, logical_key, state)

        self.deck.set_key_callback(remapper_callback)

    def set_key_callback_async(self, async_callback: "Callable[[StreamDeck.StreamDeck, int, bool], Awaitable[None]]", loop: "asyncio.AbstractEventLoop | None" = None) -> None:
        """
        Sets the asynchronous callback function called each time a button on the
        StreamDeck changes state (either pressed, or released). The given
        callback should be compatible with Python 3's `asyncio` routines.

        .. note:: The asynchronous callback will be fired in a thread-safe
                  manner.

        .. note:: This will override the callback (if any) set by
                  :func:`~StreamDeck.set_key_callback`.

        :param function async_callback: Asynchronous callback function to fire
                                        each time a button state changes.
        :param asyncio.loop loop: Asyncio loop to dispatch the callback into
        """
        async def remapper_callback(deck: "StreamDeck.StreamDeck", key: int, state: bool) -> None:
            logical_key = self._remap_key_event_index(key)
            if logical_key is None:
                return
            await async_callback(deck, logical_key, state)

        # Delegate to the wrapped deck. A self-call recurses forever.
        self.deck.set_key_callback_async(remapper_callback, loop)

    def set_dial_callback(self, callback: "Callable[[StreamDeck.StreamDeck, int, DialEventType, int], None]") -> None:
        """
        Sets the callback function called each time there is an interaction
        with a dial on the StreamDeck.

        .. note:: This callback will be fired from an internal reader thread.
                  Ensure that the given callback function is thread-safe.

        .. note:: Only one callback can be registered at one time.

        .. seealso:: See :func:`~StreamDeck.set_dial_callback_async` method
                     for a version compatible with Python 3 `asyncio`
                     asynchronous functions.

        :param function callback: Callback function to fire each time a button
                                state changes.
        """
        def remapper_callback(deck: "StreamDeck.StreamDeck", dial: int,
                              event_type: DialEventType, value: int) -> None:
            callback(deck, self.get_logical_dial_index(dial), event_type, value)

        self.deck.set_dial_callback(remapper_callback)

    def set_dial_callback_async(self, async_callback: "Callable[[StreamDeck.StreamDeck, int, DialEventType, int], Awaitable[None]]", loop: "asyncio.AbstractEventLoop | None" = None) -> None:
        """
        Sets the asynchronous callback function called each time there is an
        interaction with a dial on the StreamDeck. The given callback should
        be compatible with Python 3's `asyncio` routines.

        .. note:: The asynchronous callback will be fired in a thread-safe
                  manner.

        .. note:: This will override the callback (if any) set by
                  :func:`~StreamDeck.set_dial_callback`.

        :param function async_callback: Asynchronous callback function to fire
                                        each time a button state changes.
        :param asyncio.loop loop: Asyncio loop to dispatch the callback into
        """
        async def remapper_callback(deck: "StreamDeck.StreamDeck", dial: int,
                                    event_type: DialEventType, value: int) -> None:
            await async_callback(deck, self.get_logical_dial_index(dial), event_type, value)

        # Delegate to the wrapped deck. A self-call recurses forever.
        self.deck.set_dial_callback_async(remapper_callback, loop)

    def set_touchscreen_callback(self, callback: "Callable[[StreamDeck.StreamDeck, TouchscreenEventType, dict[str, int]], None]") -> None:
        """
        Sets the callback function called each time there is an interaction
        with a touchscreen on the StreamDeck.

        .. note:: This callback will be fired from an internal reader thread.
                  Ensure that the given callback function is thread-safe.

        .. note:: Only one callback can be registered at one time.

        .. seealso:: See :func:`~StreamDeck.set_touchscreen_callback_async`
                     method for a version compatible with Python 3 `asyncio`
                     asynchronous functions.

        :param function callback: Callback function to fire each time a button
                                state changes.
        """
        def remapper_callback(deck: "StreamDeck.StreamDeck", event_type: TouchscreenEventType,
                              value: "dict[str, int]") -> None:
            callback(deck, event_type, self.logical_touch_value(value))

        self.deck.set_touchscreen_callback(remapper_callback)

    def set_touchscreen_callback_async(self, async_callback: "Callable[[StreamDeck.StreamDeck, TouchscreenEventType, dict[str, int]], Awaitable[None]]", loop: "asyncio.AbstractEventLoop | None" = None) -> None:
        """
        Sets the asynchronous callback function called each time there is an
        interaction with the touchscreen on the StreamDeck. The given callback
        should be compatible with Python 3's `asyncio` routines.

        .. note:: The asynchronous callback will be fired in a thread-safe
                  manner.

        .. note:: This will override the callback (if any) set by
                  :func:`~StreamDeck.set_touchscreen_callback`.

        :param function async_callback: Asynchronous callback function to fire
                                        each time a button state changes.
        :param asyncio.loop loop: Asyncio loop to dispatch the callback into
        """
        async def remapper_callback(deck: "StreamDeck.StreamDeck", event_type: TouchscreenEventType,
                                    value: "dict[str, int]") -> None:
            await async_callback(deck, event_type, self.logical_touch_value(value))

        # Delegate to the wrapped deck. A self-call recurses forever.
        self.deck.set_touchscreen_callback_async(remapper_callback, loop)

    def key_states(self) -> "list[bool]":
        """
        Retrieves the current states of the buttons on the StreamDeck.

        :rtype: list(bool)
        :return: List describing the current states of each of the buttons on
                 the device (`True` if the button is being pressed, `False`
                 otherwise).
        """
        with self._lock:
            # The rotation permutation is a bijection over the full grid, so
            # every logical slot receives a state.
            return cast("list[bool]", self.reorder_physical_for_rotation(self.deck.key_states()))

    def dial_states(self) -> "list[bool]":
        """
        Retrieves the current states of the dials (pressed or not) on the
        Stream Deck

        :rtype: list(bool)
        :return: List describing the current states of each of the dials on
                 the device (`True` if the dial is being pressed, `False`
                 otherwise).
        """
        with self._lock:
            states = cast("list[bool]", self.deck.dial_states())
            # Logical order, as key_states() is. The dial map is its own
            # inverse, so one reversal serves both directions.
            if self._dials_are_reversed():
                return list(reversed(states))
            return states

    def reset(self) -> None:
        """
        Resets the StreamDeck, clearing all button images and showing the
        standby image.
        """
        self._check_owner("reset")
        with self._lock:
            self.deck.reset()

    # The library reads a float as normalized 0.0-1.0 and an int as
    # 0-100, so 50.0 means full brightness while 50 means half.
    def set_brightness(self, percent: float) -> None:
        """
        Sets the global screen brightness of the StreamDeck, across all the
        physical buttons.

        :param int/float percent: brightness percent, from [0-100] as an `int`,
                                  or normalized to [0.0-1.0] as a `float`.
        """
        self._check_owner("set_brightness")
        with self._lock:
            self.deck.set_brightness(percent)

    def get_serial_number(self) -> str:
        """
        Gets the serial number of the attached StreamDeck.

        :rtype: str
        :return: String containing the serial number of the attached device.
        """
        with self._lock:
            return cast(str, self.deck.get_serial_number())

    def get_firmware_version(self) -> str:
        """
        Gets the firmware version of the attached StreamDeck.

        :rtype: str
        :return: String containing the firmware version of the attached device.
        """
        with self._lock:
            return cast(str, self.deck.get_firmware_version())

    def set_key_image(self, key: int, image: bytes) -> None:
        """
        Sets the image of a button on the StreamDeck to the given image. The
        image being set should be in the correct format for the device, as an
        enumerable collection of bytes.

        .. seealso:: See :func:`~StreamDeck.key_image_format` method for
                     information on the image format accepted by the device.

        :param int key: Index of the button whose image is to be updated.
        :param enumerable image: Raw data of the image to set on the button.
                                 If `None`, the key will be cleared to a black
                                 color.
        """
        self._check_owner("set_key_image")
        physical_key = self.get_physical_index(key)
        with self._lock:
            self.deck.set_key_image(physical_key, image)

    def set_touchscreen_image(self, image: bytes, x_pos: int = 0, y_pos: int = 0, width: int = 0, height: int = 0) -> None:
        """
        Draws an image on the touchscreen in a certain position. The image
        should be in the correct format for the devices, as an enumerable
        collection of bytes.

        .. seealso:: See :func:`~StreamDeck.touchscreen_image_format` method for
                     information on the image format accepted by the device.

        :param enumerable image: Raw data of the image to set on the button.
                                 If `None`, the touchscreen will be cleared.
        :param int x_pos: Position on x axis of the image to draw
        :param int y_pos: Position on y axis of the image to draw
        :param int width: width of the image
        :param int height: height of the image

        """
        self._check_owner("set_touchscreen_image")
        with self._lock:
            self.deck.set_touchscreen_image(image, x_pos, y_pos, width, height)

    def set_key_color(self, key: int, r: int, g: int, b: int) -> None:
        """
        Sets the color of the touch buttons. These buttons are indexed
        in order after the standard keys.

        :param int key: Index of the button
        :param int r: Red value
        :param int g: Green value
        :param int b: Blue value

        """
        self._check_owner("set_key_color")
        physical_key = self.get_physical_index(key)
        with self._lock:
            self.deck.set_key_color(physical_key, r, g, b)

    def set_screen_image(self, image: bytes) -> None:
        """
        Draws an image on the touchless screen of the StreamDeck.

        .. seealso:: See :func:`~StreamDeck.screen_image_format` method for
                     information on the image format accepted by the device.

        :param enumerable image: Raw data of the image to set on the button.
                                 If `None`, the screen will be cleared.
        """
        self._check_owner("set_screen_image")
        with self._lock:
            self.deck.set_screen_image(image)

    # Rotation is the user's clockwise physical turn. PIL applies the inverse turn to images.
    # All wrapper indexes and touch positions use the logical view.

    def set_rotation(self, value: int) -> None:
        if not value in [0, 90, 180, 270]:
            # Reachable from persisted deck settings, where a hand edit or a half-written file can
            # leave anything.
            log.warning(f"Deck rotation {value!r} is not 0, 90, 180 or 270; using 0")
            value = 0
        self.rotation = value

    def _touchscreen_size(self) -> "tuple[int, int] | None":
        """The device's own strip size, or None for a deck that has no
        touchscreen or reports no size or a zero size for it."""
        image_format = getattr(self.deck, "touchscreen_image_format", None)
        if image_format is None:
            return None
        try:
            size = image_format().get("size")
        except (AttributeError, KeyError, TypeError):
            return None
        if size is None or len(size) != 2 or None in size:
            return None
        if not size[0] or not size[1]:
            return None
        return int(size[0]), int(size[1])

    def _strip_turn(self) -> int:
        """The counter-clockwise turn from the strip the user sees to the device's strip.
        Every strip surface reads this one value, so no two can disagree; 0 with no strip."""
        if self._touchscreen_size() is None:
            return 0
        return self.rotation

    def _strip_is_mirrored(self) -> bool:
        """Whether the strip reaches the device turned end for end."""
        return self._strip_turn() == 180

    def strip_is_transposed(self) -> bool:
        """Whether the strip stands on its side, so the composite is as tall as the
        device's strip is wide."""
        return self._strip_turn() in (90, 270)

    def logical_touchscreen_size(self) -> "tuple[int, int] | None":
        """The strip size every strip composer draws at, or None for a deck with no strip.
        The device's own size at 0 and 180, its transpose at 90 and 270."""
        size = self._touchscreen_size()
        if size is None:
            return None
        return (size[1], size[0]) if self.strip_is_transposed() else size

    def dial_slot_order(self) -> SlotOrder:
        """How the dial slots divide the strip the user sees: across it, or down it at 90 and 270.
        Dial 0 stays at the end the turn carried it to: the top at 90, the bottom at 270."""
        turn = self._strip_turn()
        if turn == 90:
            return "y-down"
        if turn == 270:
            return "y-up"
        return "x"

    def touchscreen_image_rotation(self) -> int:
        """Counter-clockwise degrees that turn a composed strip into the device's orientation.
        At 90 and 270 the composite is the buffer's transpose; the turn runs with expansion."""
        return self._strip_turn()

    def logical_touch_value(self, value: "dict[str, int]") -> "dict[str, int]":
        """Map reported touch positions into the composed strip's frame, on a copy of the dict.
        180 mirrors both axes; 90 and 270 swap them with one mirrored, when both are present."""
        turn = self._strip_turn()
        if not turn or not isinstance(value, dict):
            return value
        size = self._touchscreen_size()
        if size is None:
            # Unreachable while _strip_turn() reads the same size; a second read, so guard it.
            return value
        width, height = size
        # The library hands one event dict to every consumer.
        mapped = dict(value)
        if turn == 180:
            for key in ("x", "x_out"):
                if key in mapped:
                    mapped[key] = self._mirror_position(mapped[key], width)
            for key in ("y", "y_out"):
                if key in mapped:
                    mapped[key] = self._mirror_position(mapped[key], height)
            return mapped
        for x_key, y_key in (("x", "y"), ("x_out", "y_out")):
            if x_key not in mapped or y_key not in mapped:
                # A quarter turn reads each axis off the other; a lone axis passes through unmapped.
                continue
            device_x, device_y = mapped[x_key], mapped[y_key]
            if turn == 90:
                mapped[x_key] = self._mirror_position(device_y, height)
                mapped[y_key] = device_x
            else:
                mapped[x_key] = device_y
                mapped[y_key] = self._mirror_position(device_x, width)
        return mapped

    @staticmethod
    def _mirror_position(position: int, extent: int) -> int:
        """
        position measured from the other end of extent, or position unchanged when it does not lie
        on the strip at all.
        """
        if not 0 <= position < extent:
            return position
        return extent - 1 - position

    def _dials_are_reversed(self) -> bool:
        """Whether the 180-degree strip makes logical dial order oppose physical order."""
        return self.rotation == 180

    def get_logical_dial_index(self, physical_index: int) -> int:
        """The logical dial that an event from physical_index belongs to."""
        if not self._dials_are_reversed():
            return physical_index
        return self.dial_count() - 1 - physical_index

    def get_physical_dial_index(self, logical_index: int) -> int:
        """The knob a logical dial's slot sits on. The map is its own
        inverse, so it shares the body above."""
        return self.get_logical_dial_index(logical_index)

    def get_physical_index(self, logical_index: int) -> int:
        physical_rows, physical_cols = self.deck.key_layout()
        if self.rotation == 0:
            return logical_index
        elif self.rotation == 90:
            return cast(int, ((physical_rows - 1 - (logical_index % physical_rows)) ) * physical_cols + (logical_index // physical_rows ))
        elif self.rotation == 180:
            return cast(int, (physical_rows * physical_cols) - logical_index - 1)
        elif self.rotation == 270:
            return cast(int, ((logical_index % physical_rows) * physical_cols ) + (physical_cols - 1 - (logical_index // physical_rows )))
    
        raise ValueError("Invalid rotation")
    
    def get_logical_index(self, physical_index: int) -> "int | None":
        rows, cols = self.deck.key_layout()
        if self.rotation == 0:
            return physical_index
        elif self.rotation == 90:
            return cast("int | None", (physical_index % cols) * rows + (rows - 1 - (physical_index // cols)))
        elif self.rotation == 180:
            return cast("int | None", rows * cols - physical_index - 1)
        elif self.rotation == 270:
            return cast("int | None", (cols - 1 - (physical_index % cols)) * rows + (physical_index // cols))
        else:
            return None
    
    def reorder_physical_for_rotation(self, original_list: "list[_ElemT]") -> "list[_ElemT | None]":
        """Map a physical-indexed list with out[logical(p)] = original[p].
        Reversing that assignment applies the wrong transform at 90 and 270 degrees."""
        pysical_rows, physical_cols = self.deck.key_layout()
        total = pysical_rows * physical_cols
        reordered: "list[_ElemT | None]" = [None] * total

        for physical_index in range(total):
            logical_index = self.get_logical_index(physical_index)
            if logical_index is not None and 0 <= logical_index < total:
                reordered[logical_index] = original_list[physical_index]

        return reordered
        
    def get_rotation(self) -> int:
        return self.rotation
