import os
import threading
import traceback
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypedDict, TypeVar, cast

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


def stop_device_read_thread(device: "StreamDeck.StreamDeck", timeout: float = 1.0) -> None:
    """Stops the library reader thread on a raw device handle.

    This takes the raw handle, not the BetterDeck around it. BetterDeck has no
    __getattr__ passthrough, so a write of run_read_thread on the wrapper sets
    a dead attribute, while the reader polls the wrapped object's own flag
    (StreamDeck.py:_read_with_resume_from_suspend).

    Both flags go down. On a transport error the reader clears run_read_thread
    itself and then enters a resume loop that only reconnect_after_suspend
    gates, and that loop re-opens the device for up to 10 s after close()
    returned (StreamDeck.py:209-262). A later open(True) re-arms both, so a
    reopened handle keeps its reader and its resume behaviour.
    """
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
        try:
            read_thread.join(timeout)
        except RuntimeError:
            pass


def release_device_handle(device: "StreamDeck.StreamDeck", timeout: float = 1.0) -> None:
    """Stops the reader thread on a raw device handle, then closes it.

    For a caller that holds the raw handle, such as the deck-open retry. A
    caller that holds the wrapper uses BetterDeck.release_handle, which closes
    under the device lock.
    """
    stop_device_read_thread(device, timeout)
    device.close()


class BetterDeck():
    def __init__(self, deck: StreamDeck.StreamDeck, rotation: int = 0):
        self.deck: StreamDeck.StreamDeck = deck
        self.rotation: int = rotation # [0, 90, 180, 270]
        # Serializes device I/O. hidapi is not thread-safe, and several
        # threads write to the deck. Reentrant for nested wrapped calls.
        self._lock = threading.RLock()

        # The owner assertion detects a device write from any thread other
        # than the registered writer. It only logs and never raises, because it is
        # a harness and dev detector. The RLock above is the real defense.
        # The env read happens once, so the hot path is one attribute test.
        self._assert_owner: bool = bool(os.environ.get("DECKARD_ASSERT_DEVICE_OWNER"))
        self._expected_writer: threading.Thread | None = None
        self.owner_violations: list[tuple[str, str, str]] = []

    def set_expected_writer(self, thread: threading.Thread | None) -> None:
        """Registers the thread that performs all device writes.

        DeckController registers its media player thread. _check_owner logs a
        warning when another thread writes to the device.
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
        with self._lock:
            self.deck.open()

    def close(self) -> None:
        """
        Closes the device for input/output.

        .. seealso:: See :func:`~StreamDeck.open` for the corresponding open method.
        """
        with self._lock:
            self.deck.close()

    def stop_read_thread(self, timeout: float = 1.0) -> None:
        """Stops the library reader thread on the wrapped device, and leaves
        the handle open. See stop_device_read_thread."""
        stop_device_read_thread(self.deck, timeout)

    def release_handle(self, timeout: float = 1.0) -> None:
        """Stops the reader thread, then closes the device.

        This is how a live handle is given back. A bare close() leaves the
        reader running, and its resume loop re-opens what the close released.
        The close takes the device lock, so it cannot land inside another
        thread's multi-chunk write.
        """
        stop_device_read_thread(self.deck, timeout)
        self.close()

    def is_open(self) -> bool:
        """
        Indicates if the StreamDeck device is currently open and ready for use.

        :rtype: bool
        :return: `True` if the deck is open, `False` otherwise.
        """
        # This takes no BetterDeck lock. A status probe must not stall behind
        # a multi-chunk image write, and the transport's per-chunk mutex
        # covers close() races.
        return cast(bool, self.deck.is_open())

    def connected(self) -> bool:
        """
        Indicates if the physical StreamDeck device this instance is attached to
        is still connected to the host.

        :rtype: bool
        :return: `True` if the deck is still connected, `False` otherwise.
        """
        # This takes no BetterDeck lock, see is_open(). The transport mutex
        # covers close() races.
        return cast(bool, self.deck.connected())

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
        rows, cols = self.deck.key_layout()
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
            logical_key = self.get_logical_index(key)
            if logical_key is None:
                # Only a rotation outside the four the mapper handles
                # answers None. Report it rather than forward None into the
                # consumer's index math. An index past the key grid, such as
                # the Neo's touch buttons, still maps to an in-grid number
                # here, as it always did.
                log.warning(f"Dropping key event {key}: rotation {self.rotation!r} maps no keys")
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
            logical_key = self.get_logical_index(key)
            if logical_key is None:
                # See the sync remapper above.
                log.warning(f"Dropping key event {key}: rotation {self.rotation!r} maps no keys")
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
        self.deck.set_dial_callback(callback)

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
        # Delegate to the wrapped deck. A self-call recurses forever.
        # Dials need no index remap (see set_dial_callback).
        self.deck.set_dial_callback_async(async_callback, loop)

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
        self.deck.set_touchscreen_callback(callback)

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
        # Delegate to the wrapped deck. A self-call recurses forever.
        # The touchscreen needs no index remap.
        self.deck.set_touchscreen_callback_async(async_callback, loop)

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
            return cast("list[bool]", self.deck.dial_states())

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

    def set_rotation(self, value: int) -> None:
        if not value in [0, 90, 180, 270]:
            value = 0
        self.rotation = value

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
        """Maps a physical-indexed list into logical indexing.

        The device reports physical indexes, e.g. key_states(). The mapping
        under the current rotation is out[get_logical_index(p)] = orig[p]. Do
        not invert it to out[p] = orig[get_logical_index(p)]. That form
        applies the inverse rotation, which is self-inverse only at 0 and 180.
        It scrambles key_states() at 90 and 270, and ControllerKey.__init__
        then reads the wrong key's press state.
        """
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