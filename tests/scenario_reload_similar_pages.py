"""Reload each sibling controller with its own Page object."""

# Never pass one controller's Page to another controller.
# Snapshot active_page because connect or disconnect can clear it concurrently.
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import globals as gl
from fixtures import FaultyFakeDeck, seed_page, start_watchdog

from src.backend.PageManagement.Page import Page


class RecordingController:
    """Records which Page object load_page received."""

    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None
        self.loaded_pages = []
        self.loaded_inputs = []

    def serial_number(self) -> str:
        return self.deck.get_serial_number()

    def load_page(self, page, *args, **kwargs):
        self.loaded_pages.append(page)

    def load_input_from_identifier(self, identifier, page):
        self.loaded_inputs.append((identifier, page))


def main() -> int:
    start_watchdog(30, "reload_similar_pages")
    fixtures._install_integration_globals()

    path = seed_page("SharedPage")

    caller_controller = RecordingController("reload-a")
    sibling_controller = RecordingController("reload-b")

    caller_page = Page(json_path=path, deck_controller=caller_controller)
    sibling_page = Page(json_path=path, deck_controller=sibling_controller)
    caller_controller.active_page = caller_page
    sibling_controller.active_page = sibling_page

    gl.deck_manager.deck_controller = [caller_controller, sibling_controller]

    caller_page.reload_similar_pages()  # identifier=None, reload_self=False

    if caller_controller.loaded_pages:
        print(f"FAIL: caller's own controller was reloaded despite reload_self=False: {caller_controller.loaded_pages}")
        return 1
    if sibling_controller.loaded_pages != [sibling_page]:
        got = ["caller_page (the CALLER'S page)" if p is caller_page else
               ("sibling_page" if p is sibling_page else repr(p)) for p in sibling_controller.loaded_pages]
        print(f"FAIL: sibling controller received {got}, expected its own [sibling_page]")
        return 1

    # An active_page that flips to None mid-scan must not raise AttributeError.
    class FlippingController:
        """Return a page once and then None to model concurrent deck removal."""
        def __init__(self, serial, page):
            self.deck = FaultyFakeDeck(serial_number=serial)
            self._page = page
            self._reads = 0

        def serial_number(self):
            return self.deck.get_serial_number()

        @property
        def active_page(self):
            self._reads += 1
            return self._page if self._reads <= 1 else None

    probe_page = Page(json_path=path, deck_controller=caller_controller)
    flipping = FlippingController("reload-flip", probe_page)
    gl.deck_manager.deck_controller = [flipping]
    try:
        probe_page.get_pages_with_same_json(get_self=True)
    except AttributeError as e:
        print(f"FAIL: get_pages_with_same_json derefs a concurrently-cleared active_page: {e}")
        return 1

    print("PASS: reload_similar_pages loads each controller's own Page; "
          "get_pages_with_same_json survives active_page clearing mid-scan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
