"""Let GApplication.register make the sole name claim before exclusive work; probes own no names.
Do not import main; a legacy build can overlap only during its pre-registration boot window."""
from __future__ import annotations

import time
from enum import Enum
from typing import cast, Callable, Protocol

# This GI-dependent module stays off the floor import list and runs after toolkit load.
from gi.repository import Gio, GLib

from loguru import logger as log

import appinfo
from src.backend.cli_forward import DBUS_CALL_TIMEOUT_MS

# Give --close-running 10 seconds for page flush and plugin-backend teardown.
# Poll every 0.2 seconds so a stuck instance cannot hang the launch.
CLOSE_GRACE_SECONDS = 10.0
RELEASE_POLL_SECONDS = 0.2

# Bound each repeated dispatch probe at one second.
DISPATCH_PROBE_TIMEOUT_MS = 1000


class Decision(Enum):
    """What this launch is."""

    #: This process owns the application name, so it boots.
    PRIMARY = "primary"
    #: No usable session bus, so nothing owns anything. Boot degraded.
    PRIMARY_UNREGISTERED = "primary-unregistered"
    #: Another process owns the name. Hand off to it and exit.
    REMOTE = "remote"


class LaunchAborted(Exception):
    """Abort before startup with a caller-reported message and nonzero exit."""


class CloseRunningFailed(LaunchAborted):
    """Report that --close-running left the instance alive.
    The new launch must not report success or boot beside it."""


class HandoffFailed(LaunchAborted):
    """Report failure to join the instance that owns the name.
    Never boot locally after the bus timeout because that would share its decks."""


class Application(Protocol):
    """Structural application interface required by the instance gate."""

    def get_application_id(self) -> str | None: ...

    def register(self) -> bool: ...

    def get_is_remote(self) -> bool: ...

    def get_flags(self) -> Gio.ApplicationFlags: ...

    def set_flags(self, flags: Gio.ApplicationFlags, /) -> None: ...


def object_path_for(app_id: str) -> str:
    """Derive the GApplication action object path from app_id."""
    return "/" + app_id.replace(".", "/")


def name_has_owner(session_bus: Gio.DBusConnection, name: str) -> bool:
    """Probe whether the session bus name has an owner without activating it."""
    return cast(bool, session_bus.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        GLib.Variant("(s)", (name,)),
        GLib.VariantType("(b)"),
        Gio.DBusCallFlags.NO_AUTO_START,
        DBUS_CALL_TIMEOUT_MS,
        None
    ).unpack()[0])


def activate_action(session_bus: Gio.DBusConnection, name: str, object_path: str,
                    action: str, parameter: GLib.Variant | None = None) -> None:
    """Invoke one of the running instance's GActions over org.gtk.Actions."""
    session_bus.call_sync(
        name,
        object_path,
        "org.gtk.Actions",
        "Activate",
        GLib.Variant("(sava{sv})", (action, [] if parameter is None else [parameter], {})),
        None,
        Gio.DBusCallFlags.NO_AUTO_START,
        DBUS_CALL_TIMEOUT_MS,
        None
    )


def is_no_reply(error: GLib.Error) -> bool:
    """Match both remote NoReply and client-side timeout errors."""
    if Gio.DBusError.get_remote_error(error) == "org.freedesktop.DBus.Error.NoReply":
        return True
    return cast(bool, error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT))


def _session_bus() -> Gio.DBusConnection | None:
    """Return the session connection used for application registration and API publication.
    Return None when no session bus is available."""
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as e:
        log.warning(f"No session bus available ({e}); this launch cannot tell "
                    f"whether another instance is running")
        return None


def _wait_for_release(session_bus: Gio.DBusConnection, name: str,
                      grace_seconds: float) -> bool:
    """Poll until name is free within the grace, using monotonic time.
    Wall-clock steps at login must not change the grace."""
    deadline = time.monotonic() + grace_seconds
    while True:
        try:
            if not name_has_owner(session_bus, name):
                return True
        except GLib.Error as e:
            # Treat one failed release probe as no owner instead of refusing launch.
            log.debug(f"Could not probe {name}: {e}")
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(RELEASE_POLL_SECONDS)


def _is_dispatching(session_bus: Gio.DBusConnection, app_id: str) -> bool:
    """Probe the owner's action group to confirm its main loop is dispatching.
    Peer Ping and Introspect can answer on a worker before the quit action is ready."""
    try:
        session_bus.call_sync(
            app_id,
            object_path_for(app_id),
            "org.gtk.Actions",
            "DescribeAll",
            None,
            None,
            Gio.DBusCallFlags.NO_AUTO_START,
            DISPATCH_PROBE_TIMEOUT_MS,
            None
        )
        return True
    except GLib.Error as e:
        log.debug(f"The running instance is not dispatching yet: {e}")
        return False


def _wait_until_dispatching(session_bus: Gio.DBusConnection, app_id: str) -> bool:
    """Poll until the owner answers, or the grace runs out."""
    deadline = time.monotonic() + CLOSE_GRACE_SECONDS
    while True:
        if _is_dispatching(session_bus, app_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(RELEASE_POLL_SECONDS)


def _close_running_instance(session_bus: Gio.DBusConnection, app_id: str) -> None:
    """Wait one grace for dispatch, ask the owner to quit, then wait a new grace for release.
    Leave a non-dispatching owner unasked; otherwise raise CloseRunningFailed if it remains."""
    log.info("Checking if another instance is running")
    try:
        running = name_has_owner(session_bus, app_id)
    except GLib.Error as e:
        log.debug(f"Could not probe for a running instance: {e}")
        return
    if not running:
        log.info("No other instance running, continuing")
        return

    if not _wait_until_dispatching(session_bus, app_id):
        # A booting instance and a blocked main loop both fail this probe.
        # Leave either unasked and report both possible causes.
        raise CloseRunningFailed(
            f"The running instance did not answer within "
            f"{CLOSE_GRACE_SECONDS:.0f}s -- it is still starting up, or it is "
            f"stuck; it was left running and nothing was started"
        )

    log.info("Closing running instance")
    try:
        activate_action(session_bus, app_id, object_path_for(app_id), "quit")
    except GLib.Error as e:
        if is_no_reply(e):
            # Quitting inside the handler gives no reply; only release confirms success.
            log.debug(f"The running instance did not answer the quit request: {e}")
        else:
            log.warning(f"Could not ask the running instance to quit: {e}")

    if not _wait_for_release(session_bus, app_id, CLOSE_GRACE_SECONDS):
        raise CloseRunningFailed(
            f"The running instance was asked to quit and had not exited "
            f"{CLOSE_GRACE_SECONDS:.0f}s later; nothing was started"
        )


def _shoo_pre_rename_instance(session_bus: Gio.DBusConnection) -> None:
    """After primary registration, stop an owner of the old name before opening decks.
    Probe first to avoid D-Bus activation; remote launches must not send this request."""
    try:
        if not name_has_owner(session_bus, appinfo.OLD_APP_ID):
            return
    except GLib.Error as e:
        log.debug(f"Could not probe the pre-rename bus name: {e}")
        return
    log.warning("Pre-rename StreamController instance detected on the session bus; asking it to quit")
    try:
        activate_action(session_bus, appinfo.OLD_APP_ID, appinfo.OLD_DBUS_OBJECT_PATH, "quit")
    except GLib.Error as e:
        if not is_no_reply(e):
            log.error(f"Could not close the pre-rename instance: {e}")
            return
        # An instance that quits inside the handler never replies. That is
        # what success looks like here, and the reason to run the poll below.
        log.debug(f"The pre-rename instance did not answer the quit request: {e}")
    _wait_for_release(session_bus, appinfo.OLD_APP_ID, CLOSE_GRACE_SECONDS)


def _registration_failed(app: Application, session_bus: Gio.DBusConnection | None,
                         app_id: str | None, error: GLib.Error) -> Decision:
    """Abort when registration failed to join an owner; never guess primary.
    If no owner exists, set NON_UNIQUE and allow a degraded boot."""
    owner = False
    if session_bus is not None and app_id is not None:
        try:
            owner = name_has_owner(session_bus, app_id)
        except GLib.Error as probe_error:
            log.debug(f"Could not probe for the name's owner: {probe_error}")
    if owner:
        raise HandoffFailed(
            f"Another instance is running but did not answer in time ({error}); "
            f"nothing was started"
        )
    log.error(f"Could not register the application on the session bus ({error}); "
              f"continuing without single-instance handling")
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    return Decision.PRIMARY_UNREGISTERED


def establish(app: Application, *, publish: Callable[[], None],
              close_running: bool) -> Decision:
    """Close if requested, publish once, register once, then return remote or prepare the primary.
    publish contains its failures; registration fails open only without an owner."""
    session_bus = _session_bus()
    app_id = app.get_application_id()

    if close_running and session_bus is not None and app_id is not None:
        _close_running_instance(session_bus, app_id)

    if session_bus is None:
        # Set NON_UNIQUE before registration, which otherwise asserts and keeps old flags.
        # GApplication reports successful primary registration even without a bus.
        app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)

    # Publish before the name grant so every exported object already answers.
    # GDBus permits the application and API interfaces on the same object path.
    publish()

    # register emits toolkit startup before globals exist; overrides must preserve that.
    # A remote waits for main-loop dispatch under the bus timeout while this process boots.
    try:
        app.register()
    except GLib.Error as e:
        return _registration_failed(app, session_bus, app_id, e)

    if session_bus is None:
        log.warning("Started without a session bus: this launch cannot be "
                    "reached by the CLI and does not exclude a second instance")
        return Decision.PRIMARY_UNREGISTERED

    if app.get_is_remote():
        # Remote-only objects use an unaddressed unique name and leave with this connection.
        log.info("Another instance owns the application name; handing off to it")
        return Decision.REMOTE

    log.info("This launch owns the application name")
    # Only a registered primary checks the old name.
    # A degraded primary with a working but refusing daemon can share those decks.
    _shoo_pre_rename_instance(session_bus)
    return Decision.PRIMARY
