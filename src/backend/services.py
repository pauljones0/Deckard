"""Provide typed access to late-bound process services without constructing them.
Each call reads the current globals slot, preserving rebinding and explicit None guards."""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

# Import only globals at runtime so all layers can use this without toolkit cycles.
# Deferred annotations keep Python 3.13 from evaluating type-only imports.
import globals as gl

if TYPE_CHECKING:
    from locales.LocaleManager import LocaleManager
    from src.app import App
    from src.backend.DeckManagement.DeckManager import DeckManager
    from src.backend.PageManagement.PageManagerBackend import PageManagerBackend
    from src.backend.SettingsManager import AppSettings, SettingsManager
    from src.windows.mainWindow.elements.DeckStack import DeckStack
    from src.windows.mainWindow.elements.Sidebar.Sidebar import Sidebar
    from src.windows.mainWindow.mainWindow import MainWindow


# Translations

def tr(key: str, fallback: str | None = None) -> str:
    """Return plain text; locale fallback comes first, so unknown keys return themselves.
    Raise before setup; None adds no fallback, and Pango callers must escape it."""
    # Widen the late-bound concrete slot so the live pre-boot branch remains typed.
    # A direct None check would otherwise narrow to an uninhabited type.
    lm: LocaleManager | None = gl.lm
    if lm is None:
        raise RuntimeError(
            f"translation requested for {key!r} before the locale manager "
            f"exists -- gl.lm is built by main.create_global_objects()"
        )
    if fallback is None:
        return lm.get(key)
    return lm.get(key, fallback)


# The application and its window

def app() -> App | None:
    """Return the App, or None before Main publishes it.
    Use require_app() only where absence is invalid."""
    return gl.app


def require_app() -> App:
    """Return the App or raise RuntimeError before publication.
    Use only where absence is invalid; do not replace a live None branch."""
    running = gl.app
    if running is None:
        raise RuntimeError(
            "the App does not exist yet -- gl.app is published in "
            "Main.__init__, before app.run(). Work that must wait for the "
            "running app belongs on src.backend.startup_queue instead."
        )
    return running


def main_window() -> MainWindow | None:
    """Return the bound main window, or None before App and on_activate create it.
    Destruction leaves it bound, so quit-path callers must not repaint or present it."""
    running = gl.app
    if running is None:
        return None
    window: MainWindow | None = getattr(running, "main_win", None)
    return window


def require_main_window() -> MainWindow:
    """Return the bound main window or raise before on_activate creates it.
    A destroyed window remains bound during teardown, so callers must not paint through it."""
    window = main_window()
    if window is None:
        raise RuntimeError(
            "the main window does not exist yet -- gl.app.main_win is bound "
            "by App.on_activate, and until it runs there is either no App or "
            "no window attribute on it."
        )
    return window


def sidebar() -> "Sidebar | None":
    """Return the sidebar or None for no App, no window, or an incomplete build."""
    window = main_window()
    if window is None:
        return None
    return window.get_sidebar()


def deck_stack() -> "DeckStack | None":
    """Return the deck stack or None for no App, no window, or an incomplete build.
    Call window.get_deck_stack() when the caller already holds the window."""
    window = main_window()
    if window is None:
        return None
    return window.get_deck_stack()


# Settings

def settings_manager() -> SettingsManager:
    """Return the late-bound settings manager with a concrete type.
    Before global initialization this preserves the raw None result despite the annotation."""
    return gl.settings_manager


def app_settings() -> AppSettings:
    """Return an uncopied view of shared app settings; build it per use.
    Before global initialization the raw settings dereference raises AttributeError."""
    return gl.settings_manager.app()


def deck_settings(serial_number: str) -> dict[str, Any]:
    """Return a deep copy of this deck's settings; mutations do not persist.
    Pair changes with save_deck_settings; pre-boot access raises AttributeError."""
    return gl.settings_manager.get_deck_settings(serial_number)


# Pages

def page_manager() -> PageManagerBackend | None:
    """Return the page manager or None before initialization and after teardown.
    D-Bus startup and controller teardown both use the None branches."""
    return gl.page_manager


def require_page_manager() -> PageManagerBackend:
    """Return the page manager or raise before it exists.
    Use only for loaded-page paths; preserve guards on live None branches."""
    manager = gl.page_manager
    if manager is None:
        raise RuntimeError(
            "the page manager backend does not exist yet -- gl.page_manager "
            "is built by main.create_global_objects(), after the settings "
            "manager it takes as an argument."
        )
    return manager


# Decks

def deck_manager() -> DeckManager | None:
    """Return the deck manager or None until main builds it.
    Other global services become available before this slot."""
    return gl.deck_manager


def require_deck_manager() -> DeckManager:
    """Return the deck manager or raise before main builds it.
    Use only after window construction; D-Bus and other live None branches keep guards."""
    manager = gl.deck_manager
    if manager is None:
        raise RuntimeError(
            "the deck manager does not exist yet -- gl.deck_manager is built "
            "in main.main(), after create_global_objects() returns and "
            "before app.run()."
        )
    return manager
