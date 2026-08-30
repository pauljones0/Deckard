import contextlib
import os
import threading
import traceback
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar, cast

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


class _ReleasedOpen:
    """Instance-level stand-in for open() on a released handle. The library's reader re-opens a device from inside the except arm of its read loop, where it reads neither run_read_thread nor reconnect_after_suspend (StreamDeck.py:209-262).
    A reader already in that arm therefore takes the handle back after close() returned, and its open() re-arms both flags and starts a second reader. A released handle that answers open() with nothing cannot be taken back."""

    def __init__(self, device_name: str) -> None:
        self.device_name = device_name

    def __call__(self, *args: object, **kwargs: object) -> None:
        log.debug(f"Ignoring a reopen of the released {self.device_name} handle")


def _install_release_shadow(device: "Any") -> None:
    """Shadows open() on this device instance, so nothing re-opens it. An attribute on the library's own object, as _install_fair_transport_lock does with the transport mutex.
    open_device_handle lifts it, so a handle released by a failed attempt can still be taken up again."""
    if isinstance(getattr(device, "open", None), _ReleasedOpen):
        return
    try:
        device.open = _ReleasedOpen(type(device).__name__)
    except (AttributeError, TypeError):
        # A handle that refuses the attribute keeps its own open(), and the
        # two flags are then the only defense against the resume loop.
        log.warning(f"Could not shadow open() on {type(device).__name__}; "
                    f"its reader can still re-open the released handle")


def open_device_handle(device: "Any", resume_from_suspend: bool = True) -> None:
    """Opens a device handle, and lifts any release shadow first. Every deliberate open runs through here.
    The deck-open retry re-uses the handle of an attempt that released it, and only this makes that handle take an open() again."""
    if isinstance(getattr(device, "open", None), _ReleasedOpen):
        del device.open
    device.open(resume_from_suspend)


def stop_device_read_thread(device: "StreamDeck.StreamDeck", timeout: "float | None" = None) -> None:
    """Stops the library reader thread on a raw device handle. This takes the raw handle, not the BetterDeck around it. BetterDeck has no __getattr__ passthrough, so a write of run_read_thread on the wrapper sets a dead attribute, while the reader polls the wrapped object's own flag (StreamDeck.py:_read_with_resume_from_suspend).
    Both flags go down, which stops a reader that is still reading and keeps one that hits a transport error next out of the resume loop. It does not reach a reader already inside that loop, which reads neither flag: only the release shadow stops that one. A later open(True) re-arms both, so a reopened handle keeps its reader and its resume behaviour."""
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
    """Stops the reader thread on a raw device handle, shadows its open(), then closes it. For a caller that holds the raw handle: the deck-open retry, a constructor that failed before it wrapped the handle, and the device listing. A caller that holds the wrapper uses BetterDeck.release_handle, which closes under the device lock.
    This closes the device with no check of its own that a media writer stopped first. A caller that has a writer makes that check itself, as DeckController._teardown_failed_init does, because a writer wedged mid-frame holds the device lock."""
    _install_release_shadow(device)
    stop_device_read_thread(device, timeout)
    device.close()


def device_is_on_bus(deck: "Any") -> bool:
    """Whether this deck's device is still enumerable on the HID bus. The library answers this with an unfiltered hid_enumerate. On Linux it binds the libusb backend, where that call opens every USB device on the system to read its manufacturer, product and serial string descriptors, and it runs under the process-wide hidapi mutex that every deck read and write also waits on. The liveness poll asks the question every two seconds per deck. Passing the deck's own vendor and product id to the same call makes hidapi skip every device that does not match, so only this model of deck is opened. The filter comes from the device the poll is about and is not a fixed vendor, so a deck of any supported make narrows to its own kind. The path is what identifies the device, the same key the library compares, and it stays valid for as long as the device stays on its port.
    This takes no per-device transport lock, which the library's own answer does take. Nothing here reads or writes the handle, and the device identity it does read is fixed at enumeration, so the probe has nothing to be serialized against; holding that lock would only park a status question behind an image write in the queue every deck write shares. The filtered call still runs under the process-wide hidapi mutex, and it still opens this deck itself; what it spares is every other device on the bus and this deck's write queue. A transport that does not carry both the loader and the enumeration entry gets the library's own answer: a fake deck or a remote deck in practice, since a renamed attribute would break the library's own probe too. A call the loader refuses degrades the same way, so drift never raises: under flatpak this answer is the only disconnect detection there is."""
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

        # The owner assertion detects a device write from any thread other than the registered writer. It only logs and never raises, because it is a harness and dev detector.
        # The RLock above is the real defense. The env read happens once, so the hot path is one attribute test.
        self._assert_owner: bool = bool(os.environ.get("DECKARD_ASSERT_DEVICE_OWNER"))
        self._expected_writer: threading.Thread | None = None
        self.owner_violations: list[tuple[str, str, str]] = []

    def set_expected_writer(self, thread: threading.Thread | None) -> None:
        """Registers the thread that performs all device writes. DeckController registers its media player thread.
        _check_owner logs a warning when another thread writes to the device."""
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
        """Opens the device for input/output. This must be called prior to setting or retrieving any device state. It delegates to open_handle(), which lifts a release shadow and refuses an open that would wait on a reader still running.
        A plain open() of the wrapped handle does neither, and a released handle ignores it outright. .. seealso:: See :func:`~StreamDeck.close` for the corresponding close method."""
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
        """Stops the reader thread and shadows open(), then closes the device. This is how a live handle is given back, and after it nothing re-opens the device: a bare close() leaves the reader running, and the reader's resume loop takes back what the close released. The close takes the device lock, so it cannot land inside another thread's multi-chunk write. The whole transition runs under self._lock, so it cannot interleave with open_handle, which holds the same lock across its own decision and open.
        Without it a reopen could slip between the reader stop and the close here: it would pass open_handle's reader-alive check, because the stop just joined the reader, lift the shadow and open, and then the close below would close the handle it had just opened. self._lock is an RLock, so the self.close() call re-enters it. The join inside stop_device_read_thread runs under the lock, as the library's own join inside open_handle already does."""
        with self._lock:
            _install_release_shadow(self.deck)
            stop_device_read_thread(self.deck, timeout)
            self.close()

    def open_handle(self, resume_from_suspend: bool = True,
                    guard: "Callable[[], bool] | None" = None) -> bool:
        """Takes the device back: lifts any release shadow, then opens. Returns whether it opened. This is the deliberate reopen for a caller that holds the wrapper, the counterpart of release_handle(). Two conditions are settled under the device lock, immediately before the open, so neither can change between the decision and the open itself. guard() is the caller's own reason to reopen, asked one last time.
        A teardown that starts while a reopen is queued must win: a reopen that ran after it would lift the shadow the teardown installed and hand the next process a busy device. A reader thread that is still alive refuses the open. The library's open() joins the previous reader with no timeout of its own, while release_handle() joins with a bound, so a reader that outlived that bound would wedge this call for as long as it runs. It is a caller error, and it says so rather than waiting."""
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
        # This takes no BetterDeck lock.
        # A status probe must not stall behind a multi-chunk image write, and the transport's per-chunk mutex covers close() races.
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
        """Maps a physical key-callback index to its logical grid index, or None when the event must not dispatch a grid key. The library fires the key callback for every physical index a device reports. On the Stream Deck Neo that includes its two touch buttons, which the driver reports at indexes past the key grid (key_count plus touch_key_count).
        Those buttons have no grid position, and there is no touch-button input path to route them to, so a press of one is dropped here: get_logical_index would fold an out-of-grid index back into the rotation arithmetic and fire a real grid key's action, hand the controller a negative index, or, at rotation 0, pass it straight through as an out-of-grid index. Grid indexes (0 to rows*cols-1) pass through get_logical_index unchanged in mapping at every rotation. Both key-callback remappers route through here, so the guard is applied in one place."""
        rows, cols = self.deck.key_layout()
        if not 0 <= physical_index < rows * cols:
            # A touch or extra button past the grid, or a stray index. Drop it rather than remap it.
            # This is a per-press event on hardware that has such buttons, so it logs at debug.
            log.debug(f"Dropping key event {physical_index}: past the "
                      f"{rows}x{cols} key grid (no grid position)")
            return None
        logical_key = self.get_logical_index(physical_index)
        if logical_key is None:
            # An in-grid index that still maps to None means a rotation outside the four the mapper handles.
            # Report it rather than forward None into the consumer's index math.
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

    # ---- Rotation ------------------------------------------------------- rotation is the quarter turn the user gave the physical deck, clockwise. The key map fixes what that means, and every other surface follows it. At 90, get_physical_index sends the logical top-left key to the physical bottom-left one, which is the key that comes to lie top-left once the device is turned a quarter turn clockwise.
    # A composite is therefore turned counter-clockwise by rotation to reach the device upright, which is the direction PIL's Image.rotate takes. Everything below states the logical view. A caller of this wrapper never converts between the two, and nothing above it holds a second copy of these rules.

    def set_rotation(self, value: int) -> None:
        if not value in [0, 90, 180, 270]:
            # Reachable from persisted deck settings, where a hand edit or a half-written file can leave anything.
            # An unhandled value makes every key write raise, so a deck comes up unrotated instead.
            log.warning(f"Deck rotation {value!r} is not 0, 90, 180 or 270; using 0")
            value = 0
        self.rotation = value

    def _touchscreen_size(self) -> "tuple[int, int] | None":
        """The device's own strip size, or None for a deck that has no
        touchscreen or reports no size for it."""
        image_format = getattr(self.deck, "touchscreen_image_format", None)
        if image_format is None:
            return None
        try:
            size = image_format().get("size")
        except (AttributeError, KeyError, TypeError):
            return None
        if size is None or len(size) != 2 or None in size:
            return None
        return int(size[0]), int(size[1])

    def _strip_is_mirrored(self) -> bool:
        """Whether the strip goes to the device turned end for end. One answer decides both halves of that mirror, the image and the touch positions, so the two can never disagree.
        A deck that reports no strip size has nothing to mirror a touch position against, and a turned image with unturned positions puts every touch at the far end of what the user sees. Such a deck therefore keeps both as they are."""
        return self.rotation == 180 and self._touchscreen_size() is not None

    def touchscreen_image_rotation(self) -> int:
        """Counter-clockwise degrees to turn a composed strip by, so that it reaches the device in the device's own orientation. At 180 the strip lies end for end under the user's hand, so the composite is turned through half a circle. At 90 and 270 the strip stands on its side, and an upright composite would have to be as tall as the strip is wide. The device takes a fixed 800 by 100 buffer, so there is nothing to turn such a composite into: the strip keeps the device's own orientation there, and its content reads sideways, which is what a strip of fixed shape on a deck laid on its side does.
        Presenting it upright needs a composite of the transposed size, which reaches the dial slots, the strip background and the window's own strip preview, and is not this. This turns the composite the strip's own inputs drew. A background image that extends onto the strip is cut from the band below the key grid, and at 180 the band the user sees is the one above it, so that content still comes off the wrong edge. It is a separate crop, on the background's own geometry, and it is tracked separately."""
        return 180 if self._strip_is_mirrored() else 0

    def logical_touch_value(self, value: "dict[str, int]") -> "dict[str, int]":
        """A touch event's positions, moved from where the device reports them to where the strip was composed. The device reports a position in its own frame. At 180 the composite was turned end for end before the write, so the pixel the user touches is reported from the opposite corner, and both ends of a drag move with it.
        At 0, 90 and 270 the strip goes to the device in the device's own orientation (see touchscreen_image_rotation), so a reported position already names the pixel the composite drew there. A position that does not lie on the strip stays where it is; see _mirror_position. The event's dict is copied and never edited in place, because the library hands one object to every consumer of that event."""
        if not self._strip_is_mirrored() or not isinstance(value, dict):
            return value
        size = self._touchscreen_size()
        if size is None:
            # Unreachable while _strip_is_mirrored() answers on the same size.
            # It stays because this reads the size a second time, and a None here would mirror against nothing.
            return value
        width, height = size
        mapped = dict(value)
        for key in ("x", "x_out"):
            if key in mapped:
                mapped[key] = self._mirror_position(mapped[key], width)
        for key in ("y", "y_out"):
            if key in mapped:
                mapped[key] = self._mirror_position(mapped[key], height)
        return mapped

    @staticmethod
    def _mirror_position(position: int, extent: int) -> int:
        """position measured from the other end of extent, or position unchanged when it does not lie on the strip at all. The library reports what the device sends and clamps nothing.
        A mirror applied to a position past the end lands back on the strip: an x one past the right edge comes out as -1, and the consumer's slot arithmetic reads that as the first slot, so a touch off the end of a deck held upside down would drive a dial. Leaving such a position where it is keeps it past the end, which is where every other rotation leaves it, and the consumer drops it there as it does then."""
        if not 0 <= position < extent:
            return position
        return extent - 1 - position

    def _dials_are_reversed(self) -> bool:
        """Whether logical dial order runs against physical dial order. The dials sit in one row along the strip, so they follow the strip. At 180 the composite is turned end for end, which puts the slot drawn first over the last knob.
        At 90 and 270 the strip is written in the device's own orientation, so slot and knob still line up one for one, whichever way round the row reads to the user."""
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
        """Maps a physical-indexed list into logical indexing. The device reports physical indexes, e.g. key_states(). The mapping under the current rotation is out[get_logical_index(p)] = orig[p]. Do not invert it to out[p] = orig[get_logical_index(p)].
        That form applies the inverse rotation, which is self-inverse only at 0 and 180. It scrambles key_states() at 90 and 270, and ControllerKey.__init__ then reads the wrong key's press state."""
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
