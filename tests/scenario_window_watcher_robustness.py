"""Guard active_page during startup or hotplug so one deck cannot abort routing.
Pass the GNOME shell extension UUID as the bare string required by D-Bus."""

import fixtures  # noqa: F401  (must be imported first: isolates DATA_PATH)

import globals as gl
from src.backend.WindowGrabber.WindowGrabber import WindowGrabber
from src.backend.WindowGrabber.Window import Window


# Stubs. Exactly what on_active_window_changed dereferences

class StubPage:
    def __init__(self, json_path: str):
        self.json_path = json_path


class StubPageManager:
    def __init__(self, pages: dict[str, dict]):
        self._pages = pages

    def get_pages(self) -> list[str]:
        return list(self._pages)

    def get_auto_change_settings(self, path: str) -> dict:
        return self._pages.get(path, {})

    def get_page(self, path: str, deck_controller) -> StubPage:
        return StubPage(path)


class StubDeck:
    def __init__(self, serial: str):
        self._serial = serial

    def is_open(self) -> bool:
        return True

    def get_serial_number(self) -> str:
        return self._serial


class StubWGDeckController:
    def __init__(self, serial: str, active_page: StubPage | None,
                 page_auto_loaded: bool = False):
        self.deck = StubDeck(serial)
        self._serial = serial
        self.active_page = active_page
        self.page_auto_loaded = page_auto_loaded
        self.last_manual_loaded_page_path = None
        self.loaded_pages: list[str] = []

    def serial_number(self) -> str:
        return self._serial

    def load_page(self, page: StubPage, allow_reload: bool = True) -> None:
        self.loaded_pages.append(page.json_path)
        self.active_page = page


# Part 1. Pageless deck must not abort routing for the other decks

def check_pageless_deck_routing() -> None:
    deck_manager = fixtures.install_stub_globals()

    # Put a pageless, auto-loaded deck first so an unguarded stay-on-page branch
    # dereferences active_page before routing the healthy deck.
    pageless = StubWGDeckController("HOTPLUG", active_page=None,
                                    page_auto_loaded=True)
    healthy = StubWGDeckController("GOOD",
                                   active_page=StubPage("/pages/other.json"))
    deck_manager.deck_controller.extend([pageless, healthy])

    gl.page_manager = StubPageManager({
        "/pages/match.json": {
            "wm-class": "firefox",
            "title": ".*",
            "enable": True,
            "decks": ["GOOD"],
        },
    })

    grabber = WindowGrabber.__new__(WindowGrabber)  # routing needs no integration

    try:
        grabber.on_active_window_changed(Window("firefox", "Mozilla Firefox"))
    except Exception as e:
        raise AssertionError(
            f"a deck without an active_page must be skipped, not raise: {e!r}"
        )

    assert healthy.loaded_pages == ["/pages/match.json"], (
        f"the healthy deck must still auto-switch when a pageless deck "
        f"precedes it, got {healthy.loaded_pages}"
    )
    assert pageless.loaded_pages == [], (
        "a pageless deck must not have pages loaded onto it by the watcher"
    )


# Part 1b. The None-guard itself, isolated from the per-deck try/except

def check_pageless_guard_is_noop() -> None:
    """Call the per-deck body directly so only its None guard can make a no-op."""
    # Direct invocation avoids the outer per-deck exception handler that would
    # hide a missing None guard.
    deck_manager = fixtures.install_stub_globals()

    pageless = StubWGDeckController("HOTPLUG", active_page=None,
                                    page_auto_loaded=True)
    deck_manager.deck_controller.append(pageless)

    gl.page_manager = StubPageManager({
        "/pages/match.json": {
            "wm-class": "firefox",
            "title": ".*",
            "enable": True,
            "decks": ["HOTPLUG"],
        },
    })

    grabber = WindowGrabber.__new__(WindowGrabber)

    try:
        # Exercise both active_page.json_path branches without an outer handler.
        grabber._apply_auto_change(pageless, Window("firefox", "Mozilla Firefox"))
    except Exception as e:
        raise AssertionError(
            f"_apply_auto_change must skip a pageless deck without the "
            f"None-guard's protection, not raise: {e!r}"
        )

    assert pageless.loaded_pages == [], (
        "the None-guard must make a pageless deck a no-op, loading nothing"
    )


def check_gnome_install_extension_uuid() -> None:
    """Use a bare UUID for the GNOME InstallRemoteExtension '(s)' signature.
    The same string must match entries returned by get_installed_extensions."""
    # The method does not use self, so this check builds no D-Bus proxy.
    import types

    from src.backend.WindowGrabber.Integrations.Gnome import Gnome

    class RecordingExtensions:
        def __init__(self, installed):
            self.installed = installed
            self.requested = []

        def get_installed_extensions(self):
            return list(self.installed)

        def request_installation(self, uuid):
            self.requested.append(uuid)
            return True

    real = getattr(gl, "gnome_extensions", None)
    try:
        gl.gnome_extensions = RecordingExtensions([])
        Gnome.install_extension(types.SimpleNamespace())
        requested = gl.gnome_extensions.requested
        assert len(requested) == 1, f"expected one install request, got {requested}"
        uuid = requested[0]
        assert isinstance(uuid, str), (
            f"the extension uuid must be the string the D-Bus \"(s)\" "
            f"signature takes, got {type(uuid).__name__}: {uuid!r}"
        )

        # Already installed. The same value must match what the shell lists.
        gl.gnome_extensions = RecordingExtensions([uuid])
        Gnome.install_extension(types.SimpleNamespace())
        assert gl.gnome_extensions.requested == [], (
            "an already-installed extension must not be requested again -- "
            f"the short-circuit compares {uuid!r} against the shell's list"
        )
    finally:
        gl.gnome_extensions = real

    print("ok: the GNOME integration installs its extension by uuid string")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_window_watcher_robustness")
    check_pageless_deck_routing()
    check_pageless_guard_is_noop()
    check_gnome_install_extension_uuid()
    print("PASS: scenario_window_watcher_robustness")


if __name__ == "__main__":
    main()
