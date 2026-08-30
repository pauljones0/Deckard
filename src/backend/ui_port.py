"""Define the stdlib-only synchronous boundary from the render engine to optional UI.
Engine code cannot import widgets or read GTK state; the null port forces later recomposition."""
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.InputIdentifier import InputIdentifier
    from src.backend.DeckManagement.deck_controller.input_latency import LatencySample


class UIPort:
    """Accept any-thread calls without blocking GTK; use idle_add, never run_on_main.
    push_input_image admits off-thread, and a main-loop drain paints the newest frame."""

    # Render mirror on the hot path. The media thread calls it up to keys x
    # fps a second.

    def push_input_image(self, controller: "DeckController",
                         identifier: "InputIdentifier",
                         image: "Image.Image",
                         latency_sample: "LatencySample | None" = None) -> "bool | InputImageDropRecorded":
        """Mirror images: True accepts, False needs dirty-marking; recorded drops are pre-counted.
        Never raise; the adapter dirty-marks later unmap or rebuild drops after acceptance."""
        return False

    # Per-deck sync. The caller does not wait; the adapter coalesces.

    def on_page_changed(self, controller: "DeckController") -> None:
        """A page finished loading on this deck; the sidebar may need to
        re-render for the new page's actions."""

    def on_input_visuals_changed(self, controller: "DeckController",
                                 identifier: "InputIdentifier",
                                 state: int, aspect: str) -> None:
        """Report labels, layout, or background changes for one input state."""

    def on_input_states_changed(self, controller: "DeckController",
                                identifier: "InputIdentifier",
                                n_states: int) -> None:
        """The number of states on an input changed."""

    def on_input_state_selected(self, controller: "DeckController",
                                identifier: "InputIdentifier",
                                state: int) -> None:
        """The active state of an input changed."""

    def set_low_fps_warning(self, controller: "DeckController",
                            shown: bool) -> None:
        """Show/hide this deck's low-FPS banner."""

    def on_deck_layout_changed(self, controller: "DeckController") -> None:
        """Report a rotated key layout and rebuild synchronously when on main.
        The caller reloads immediately, so an idled rebuild can receive stale-grid paints."""

    def query_input_widget(self, controller: "DeckController",
                           identifier: "InputIdentifier") -> "object | None":
        """Return the live widget for deprecated ControllerKey plugin compatibility.
        This in-process method cannot cross a process boundary."""
        return None

    def query_deck_widget(self, controller: "DeckController",
                          part: str) -> "object | None":
        """Return deck_stack_child or key_grid for deprecated DeckController compatibility.
        This in-process method has the same boundary limit as query_input_widget."""
        return None

    # App level. The USB monitor, the boot rescan and the flatpak poll call
    # these.

    def on_deck_added(self, controller: "DeckController") -> None:
        """A deck was registered and needs a UI page."""

    def on_deck_removed(self, controller: "DeckController") -> None:
        """Queue UI detach before returning from deck unregistration.
        This prevents a fast replug from racing a late detach against the fresh add."""

    def refresh_deck_availability(self) -> None:
        """Re-evaluate the "no decks connected" error screen."""

    def on_page_list_changed(self) -> None:
        """The set of pages changed; refresh any page selector."""

    def notify_plugin_problem(self, plugin_id: str, kind: str) -> None:
        """Show a plugin problem to the user. kind is "outdated" or
        "missing"."""

    def notify_user(self, body: str, title: str) -> None:
        """Show a notice through the UI hook rather than the notification backend.
        Headless runs and the null port drop it silently."""


@dataclass(frozen=True, slots=True)
class InputImageDropRecorded:
    """A push failure whose adapter already accounted for the frame drop."""

    recorded_drop: bool = True


# Process-wide null port; install(None) restores this exact instance.
_NULL_PORT = UIPort()

_port: UIPort = _NULL_PORT


def get() -> UIPort:
    """The currently installed port. Never None."""
    return _port


def install(port: "UIPort | None") -> None:
    """Install port as the process-wide engine-to-UI port. None restores the
    null port on window teardown, on quit, and in tests."""
    global _port
    _port = _NULL_PORT if port is None else port
