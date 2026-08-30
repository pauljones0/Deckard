"""GLib, timer-wheel, and deck-reader events update one quiescence flag without GTK.
Media loops read it lock-free; system-idle gates animations after lock grace or idle delay."""
import os
import threading
import time
from typing import cast, Any

from loguru import logger as log

import globals as gl
from src.backend import timer_wheel

from gi.repository import Gio, GLib


# The two performance.animation-pause-mode values. "screensaver" is the
# careful default, under which nothing new engages.
MODE_SCREENSAVER = "screensaver"
MODE_SYSTEM_IDLE = "system-idle"

# Match SettingsManager defaults while settings are unavailable during early startup.
# The unit-tier harness also uses these values with its stub manager.
FALLBACK_MODE = MODE_SCREENSAVER
FALLBACK_IDLE_MINUTES = 5

LOGIND_BUS_NAME = "org.freedesktop.login1"
LOGIND_MANAGER_PATH = "/org/freedesktop/login1"
LOGIND_MANAGER_IFACE = "org.freedesktop.login1.Manager"
LOGIND_SESSION_IFACE = "org.freedesktop.login1.Session"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"


def _settings_seed() -> tuple[str, int]:
    """Read persisted values or use conservative defaults while settings are unavailable.
    An unreadable setting must not enable the animation gate."""
    try:
        app = gl.settings_manager.app()
        return str(app.animation_pause_mode), int(app.animation_idle_minutes)
    except Exception:
        log.debug("PresenceMonitor: falling back to default pause mode "
                  "(app settings not readable yet)")
        return FALLBACK_MODE, FALLBACK_IDLE_MINUTES


class PresenceMonitor:
    """The quiescence signal. See the module docstring for the rule."""

    # Keep animations live after input when lock-on-lock-screen is off.
    # Tests can shorten this 30-second balance between active use and CPU savings.
    DECK_ACTIVITY_GRACE_S = 30.0

    def __init__(self, mode: str | None = None, minutes: int | None = None,
                 idle_detector: bool = True, bus: Gio.DBusConnection | None = None) -> None:
        # Media threads read this bool without the lock on every tick.
        # A stale read costs one animation tick, and a torn bool read cannot occur.
        self.quiescent: bool = False

        self._lock = threading.Lock()

        seed_mode, seed_minutes = _settings_seed()
        self._mode: str = mode if mode is not None else seed_mode
        self._minutes: int = max(1, int(minutes if minutes is not None else seed_minutes))

        self._idle_hint: bool = False
        # Wall clock (time.time() domain, same as logind's IdleSinceHint,
        # which is CLOCK_REALTIME microseconds).
        self._idle_since: float | None = None
        # 0.0 means no deck input; process start is not activity.
        # Seeding with now would postpone gating after a restart into an already-idle session.
        self._last_deck_activity: float = 0.0
        self._deadline: "timer_wheel.TimerHandle | None" = None

        # Build logind only for system-idle; each build seeds IdleHint before evaluation.
        # Capture the test opt-out and bus seam for that deferred build.
        self.idle_detector: "LogindIdleDetector | None" = None
        self._idle_detector_enabled: bool = bool(idle_detector)
        self._idle_detector_bus = bus
        self._detector_lock = threading.Lock()
        if self._mode == MODE_SYSTEM_IDLE:
            self._ensure_idle_detector()

        # Evaluate at construction so an already-locked or idle session gates
        # without waiting for its next state transition.
        self._evaluate()

    # Reads

    def is_quiescent(self) -> bool:
        """The media loop's question. Lock-free by contract."""
        return self.quiescent

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def idle_minutes(self) -> int:
        return self._minutes

    # Inputs

    def on_lock_changed(self, active: bool) -> None:
        """Handle a published lock even when lock-on-lock-screen keeps decks live.
        active records unlock activity; evaluation re-reads gl.screen_locked."""
        log.debug(f"PresenceMonitor: screen lock -> {active}")
        if not active:
            # Treat unlock as activity because some idle agents do not clear IdleHint.
            # This prevents a stale IdleSinceHint from keeping decks frozen until the next press.
            self._last_deck_activity = time.time()
        self._evaluate()

    def on_idle_hint_changed(self, idle_hint: bool, idle_since: float | None = None) -> None:
        """Handle a logind IdleHint change with its wall-clock IdleSinceHint.
        None means now when logind provides no usable timestamp."""
        with self._lock:
            self._idle_hint = bool(idle_hint)
            self._idle_since = idle_since if idle_hint else None
        self._evaluate()

    def notify_activity(self) -> None:
        """Record key, dial, or touch input that the compositor and lock state cannot observe.
        This activity outranks a locked screen for DECK_ACTIVITY_GRACE_S."""
        self._last_deck_activity = time.time()
        # The default mode never gates, so input needs no lock or timer-wheel work.
        # set_mode atomically replaces the mode and performs its own evaluation.
        if self._mode != MODE_SYSTEM_IDLE:
            return
        self._evaluate()

    def set_mode(self, mode: str, minutes: int | None = None) -> None:
        """Runtime push from the Settings dialog."""
        with self._lock:
            self._mode = mode if mode in (MODE_SCREENSAVER, MODE_SYSTEM_IDLE) else MODE_SCREENSAVER
            if minutes is not None:
                self._minutes = max(1, int(minutes))
            mode_now = self._mode
        if mode_now == MODE_SYSTEM_IDLE:
            # Build outside the lock because an injected bus calls back into _evaluate inline.
            # Keep the detector across later mode changes to avoid D-Bus teardown and rebuild churn.
            self._ensure_idle_detector()
        self._evaluate()

    def _ensure_idle_detector(self) -> None:
        """Build the logind idle detector once from any thread when the mode needs it.
        Do not hold self._lock because an injected bus calls _evaluate during the build."""
        if not self._idle_detector_enabled:
            return
        with self._detector_lock:
            if self.idle_detector is not None:
                return
            self.idle_detector = LogindIdleDetector(self, bus=self._idle_detector_bus)

    def stop(self) -> None:
        """Release the idle deadline and D-Bus subscription.
        The process-lifetime app does not call this, but scenarios use it to leave no timer."""
        with self._lock:
            self._cancel_deadline_locked()
        if self.idle_detector is not None:
            self.idle_detector.stop()

    # Evaluation

    def _cancel_deadline_locked(self) -> None:
        if self._deadline is not None:
            self._deadline.cancel()
            self._deadline = None

    def _on_deadline(self) -> None:
        """The idle delay elapsed. Runs on a timer_wheel dispatch thread."""
        self._evaluate()

    def _evaluate(self) -> None:
        """Recompute quiescence from any thread and arm its next automatic change.
        The deadline is the idle delay or the deck-activity grace under a locked screen."""
        with self._lock:
            was = self.quiescent
            now = time.time()
            quiescent = False
            rearm_in = None

            if self._mode == MODE_SYSTEM_IDLE:
                since_activity = now - self._last_deck_activity
                if bool(getattr(gl, "screen_locked", False)):
                    # A lock is the strongest away signal and is independent of lock-on-lock-screen.
                    # Recent input gets a grace; 0.0 makes locked startup gate at once.
                    if since_activity >= self.DECK_ACTIVITY_GRACE_S:
                        quiescent = True
                    else:
                        # Arm expiry because lock state and logind cannot report later input.
                        # Without this deadline, gating waits for an unrelated input.
                        rearm_in = self.DECK_ACTIVITY_GRACE_S - since_activity
                elif self._idle_hint:
                    since = self._idle_since if self._idle_since is not None else now
                    remaining = (max(since, self._last_deck_activity)
                                 + self._minutes * 60) - now
                    if remaining <= 0:
                        quiescent = True
                    else:
                        rearm_in = remaining

            self._cancel_deadline_locked()
            if rearm_in is not None:
                self._deadline = timer_wheel.schedule(
                    rearm_in, self._on_deadline, name="PresenceIdleDeadline"
                )

            self.quiescent = quiescent
            changed = quiescent != was

        if changed:
            log.info(f"Presence: deck animations {'gated' if quiescent else 'live'}")
            # Run outside the lock. The fan-out reaches every controller's
            # media thread, and that loop needs none of this module's state.
            self._wake_media_threads()

    def _wake_media_threads(self) -> None:
        """End every media loop's inter-tick wait so a presence transition applies on the next tick.
        This avoids up to half a second of gated cadence."""
        deck_manager = getattr(gl, "deck_manager", None)
        if deck_manager is None:
            return
        # Take a snapshot. remove_controller() mutates this list from unplug
        # and close threads during the loop.
        for controller in list(getattr(deck_manager, "deck_controller", None) or []):
            try:
                controller.media_player.wake()
            except Exception:
                log.opt(exception=True).warning(
                    "PresenceMonitor: failed to wake a deck's media thread"
                )


class LogindIdleDetector:
    """Feed the monitor from logind IdleHint and IdleSinceHint on the system bus.
    GNOME and KDE set it; Niri, Sway, and river need an agent or use lock-only gating."""

    def __init__(self, monitor: PresenceMonitor, bus: Gio.DBusConnection | None = None) -> None:
        self.monitor = monitor
        self.bus: Gio.DBusConnection | None = None
        self.session_path: str | None = None
        self._subscription_id: int | None = None
        if bus is not None:
            # An injected bus is an in-process double with no I/O to block on,
            # so wire it up inline and keep a scenario deterministic.
            self.setup_dbus(bus)
        else:
            # Open the real system bus on a daemon because bus_get_sync and GetSession can block.
            # This keeps a wedged logind off the application startup path.
            threading.Thread(target=self.setup_dbus, name="PresenceIdleSetup",
                             daemon=True).start()

    def setup_dbus(self, bus: Gio.DBusConnection | None = None) -> None:
        try:
            # Use the system bus unless the test seam supplies a connection.
            # Compare with None so a falsy valid double does not open the real bus.
            self.bus = bus if bus is not None else Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self.session_path = self.resolve_session_path()

            # The setup daemon has no thread-default context, so GDBus uses the
            # global default context and dispatches this callback on the GTK loop.
            self._subscription_id = self.bus.signal_subscribe(
                LOGIND_BUS_NAME,
                PROPERTIES_IFACE,
                "PropertiesChanged",
                self.session_path,
                LOGIND_SESSION_IFACE,
                Gio.DBusSignalFlags.NONE,
                self.on_properties_changed,
            )

            self.read_initial_state()
        except GLib.Error as e:
            # Report the failure once and leave idle detection inert.
            # Lock-based gating continues to work.
            log.info(f"Presence: logind IdleHint unavailable, idle gating inert ({e})")
        except Exception:
            log.opt(exception=True).warning(
                "Presence: unexpected failure wiring up the logind idle detector; "
                "idle gating inert"
            )

    def resolve_session_path(self) -> str:
        bus = self.bus
        if bus is None:
            # setup_dbus assigns the bus first; route an unusable connection
            # through its GLib.Error stay-inert branch.
            raise GLib.Error("logind system bus unavailable")

        session_id = os.getenv("XDG_SESSION_ID")
        if session_id:
            method = "GetSession"
            args = GLib.Variant("(s)", (session_id,))
        else:
            # PID 0 makes logind resolve the caller from D-Bus credentials.
            # A Flatpak namespace PID can name no host process or the wrong host session.
            method = "GetSessionByPID"
            args = GLib.Variant("(u)", (0,))

        reply = bus.call_sync(
            LOGIND_BUS_NAME,
            LOGIND_MANAGER_PATH,
            LOGIND_MANAGER_IFACE,
            method,
            args,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        return cast(str, reply.unpack()[0])

    def read_property(self, name: str) -> Any:
        bus = self.bus
        session_path = self.session_path
        if bus is None or session_path is None:
            # No completed setup means there is no session property source.
            # Both callers use GLib.Error as the logind-unavailable result.
            raise GLib.Error("logind session properties unavailable")

        reply = bus.call_sync(
            LOGIND_BUS_NAME,
            session_path,
            PROPERTIES_IFACE,
            "Get",
            GLib.Variant("(ss)", (LOGIND_SESSION_IFACE, name)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        return reply.unpack()[0]

    def read_initial_state(self) -> None:
        """Seed the monitor from current session properties.
        An already-idle startup might never receive another PropertiesChanged signal."""
        hint = bool(self.read_property("IdleHint"))
        self.monitor.on_idle_hint_changed(hint, self._read_idle_since() if hint else None)

    def _read_idle_since(self, changed: dict[str, Any] | None = None) -> float | None:
        """Return IdleSinceHint as wall-clock seconds, or None for zero or unusable values.
        Prefer the signal value, then read the property because logind can omit it."""
        raw = None
        if changed is not None:
            raw = changed.get("IdleSinceHint")
        if raw is None:
            try:
                raw = self.read_property("IdleSinceHint")
            except GLib.Error:
                return None
        try:
            usec = int(raw)
        except (TypeError, ValueError):
            return None
        return usec / 1_000_000 if usec > 0 else None

    def on_properties_changed(self, connection: Gio.DBusConnection, sender_name: str,
                              object_path: str, interface_name: str, signal_name: str,
                              parameters: GLib.Variant) -> None:
        try:
            iface, changed, _invalidated = parameters.unpack()
            if iface != LOGIND_SESSION_IFACE or "IdleHint" not in changed:
                return
            hint = bool(changed["IdleHint"])
            self.monitor.on_idle_hint_changed(
                hint, self._read_idle_since(changed) if hint else None
            )
        except Exception:
            # This runs on the GTK main context, where GLib swallows an
            # escaping exception and reports no useful origin.
            log.opt(exception=True).warning(
                "Presence: failed to handle a logind PropertiesChanged signal"
            )

    def stop(self) -> None:
        if self.bus is not None and self._subscription_id is not None:
            try:
                self.bus.signal_unsubscribe(self._subscription_id)
            except Exception:
                log.opt(exception=True).debug(
                    "Presence: failed to unsubscribe the logind idle signal"
                )
        self._subscription_id = None
