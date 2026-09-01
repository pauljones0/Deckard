"""Check typed gl accessors for argument forwarding and live slot reads.

Optional accessors must preserve each None branch.
"""

# The accessors know nothing about GTK, and neither does this scenario.
import fixtures  # noqa: F401  (isolates DATA_PATH before src imports)

import ast
import os
import sys
from types import SimpleNamespace

import globals as gl  # noqa: E402

from locales.LocaleManager import LocaleManager  # noqa: E402
from src.backend import services  # noqa: E402
from src.backend.SettingsManager import AppSettings  # noqa: E402

WATCHDOG_SECONDS = 30

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(_REPO_ROOT, "src", "backend", "services.py")

# Two locales, one row per shape the reader below exercises. The delimiter and
# the header layout are the real file's (locales/locales.csv).
LOCALE_CSV = (
    "key;de_DE;en_US\n"
    "go-back;Zurück;Go back\n"
    "amp-key;A & B;A & B\n"
    "german-only;Nur Deutsch;\n"
)


def build_locale_manager() -> LocaleManager:
    """Build a real LocaleManager over a two-locale temporary CSV.

    Compare tr() with production get() rather than a recording stub.
    """
    path = os.path.join(gl.DATA_PATH, "services_accessors_locales.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write(LOCALE_CSV)
    lm = LocaleManager(csv_path=path)
    lm.set_language("de_DE")
    lm.set_fallback_language("en_US")
    # CSV values are strings, so inject None to reach the fallback parameter.
    lm.locale_data["fallback-only"] = {"de_DE": None, "en_US": None}
    return lm


class FakeApp:
    """Stands in for gl.app, with no main_win. App.on_activate binds that
    attribute and nothing earlier does."""


class FakeWindow:
    pass


class FakePageManager:
    pass


def check_tr_forwards_both_shapes() -> None:
    lm = build_locale_manager()
    gl.lm = lm

    # A hit in the active language.
    assert services.tr("go-back") == "Zurück", services.tr("go-back")
    assert services.tr("go-back") == lm.get("go-back")

    # On a miss get() resolves to the key itself, before it consults the
    # fallback parameter, so a fallback must not change the result.
    assert services.tr("no-such-key") == "no-such-key"
    assert services.tr("no-such-key", "FALLBACK") == "no-such-key"
    assert services.tr("no-such-key", "FALLBACK") == lm.get("no-such-key", "FALLBACK")

    # An empty value in the active language falls through to the fallback
    # locale, which is empty here too, so the empty string comes back.
    assert services.tr("german-only") == "Nur Deutsch"
    lm.set_language("en_US")
    assert services.tr("german-only") == "" == lm.get("german-only")
    lm.set_language("de_DE")

    # The fallback parameter, on the only path that reaches it. A dropped
    # argument inside tr() is invisible above and shows up right here.
    assert services.tr("fallback-only", "FALLBACK") == "FALLBACK"
    assert services.tr("fallback-only", "FALLBACK") == lm.get("fallback-only", "FALLBACK")
    assert services.tr("fallback-only") == "fallback-only"
    assert services.tr("fallback-only") == lm.get("fallback-only")

    # get() returns plain text, and tr() must not add or remove a layer. The
    # escaping lives in get_markup, which tr() does not reach.
    assert services.tr("amp-key") == "A & B" == lm.get("amp-key")
    assert lm.get_markup("amp-key") == "A &amp; B"

    # A per-call slot read means a rebound gl.lm is the one that answers.
    other = build_locale_manager()
    other.locale_data["go-back"] = {"de_DE": "Anders", "en_US": "Other"}
    gl.lm = other
    assert services.tr("go-back") == "Anders", "tr() cached gl.lm instead of reading it"
    gl.lm = lm

    print("PASS: tr() forwards to gl.lm.get in both shapes, fallback included")


def check_tr_before_locale_manager() -> None:
    gl.lm = None  # the real pre-boot value
    try:
        services.tr("go-back")
    except RuntimeError as e:
        message = str(e)
    else:
        raise AssertionError("tr() before gl.lm exists must raise RuntimeError")
    assert "gl.lm" in message, message
    assert "create_global_objects" in message, message
    assert "go-back" in message, "the message must name the key that was asked for"

    print("PASS: tr() pre-boot raises a named RuntimeError, not AttributeError on None")


def check_app_and_require_app() -> None:
    gl.app = None
    assert services.app() is None

    try:
        services.require_app()
    except RuntimeError as e:
        message = str(e)
    else:
        raise AssertionError("require_app() must raise while gl.app is None")
    assert "gl.app" in message, message

    running = FakeApp()
    gl.app = running  # stand-in for the real App
    assert services.app() is running, "app() must be the raw read, identity included"
    assert services.require_app() is running

    print("PASS: app() reads honestly, require_app() names the boot phase")


def check_main_window_covers_both_absences() -> None:
    gl.app = None
    assert services.main_window() is None, "no app means no window"

    running = FakeApp()
    gl.app = running
    assert not hasattr(running, "main_win"), "the fixture's premise: main_win is unbound"
    assert services.main_window() is None, (
        "an app without main_win must read as no window -- the raw "
        "gl.app.main_win raises AttributeError here"
    )

    window = FakeWindow()
    running.main_win = window
    assert services.main_window() is window

    # _destroy_main_window leaves the destroyed widget bound, so it remains present.
    window.destroyed = True
    assert services.main_window() is window, (
        "a destroyed-but-bound main_win still comes back: nothing unbinds it"
    )

    # A slot rebound to None is seen on the next call, not cached.
    running.main_win = None
    assert services.main_window() is None

    print("PASS: main_window() covers both absences and does not invent a post-quit None")


def check_require_main_window() -> None:
    gl.app = None
    try:
        services.require_main_window()
    except RuntimeError as e:
        message = str(e)
    else:
        raise AssertionError("require_main_window() must raise while gl.app is None")
    assert "gl.app.main_win" in message, message

    running = FakeApp()
    gl.app = running
    try:
        services.require_main_window()
    except RuntimeError as e:
        message = str(e)
    else:
        raise AssertionError(
            "require_main_window() must raise while main_win is unbound -- the "
            "window between the App being published and on_activate building it"
        )
    assert "on_activate" in message, message

    window = FakeWindow()
    running.main_win = window
    got = services.require_main_window()
    assert got is window, "require_main_window() must hand back the window itself"
    assert got is not None, "the whole point of the require_* form"

    print("PASS: require_main_window() names both absences and returns the window")


def check_sidebar_and_deck_stack_accessors() -> None:
    # No app: both read None, not an AttributeError on the missing chain.
    gl.app = None
    assert services.sidebar() is None, "no app means no sidebar"
    assert services.deck_stack() is None, "no app means no deck stack"

    # An app without a window stays None instead of dereferencing a missing chain.
    running = FakeApp()
    gl.app = running
    assert not hasattr(running, "main_win"), "the fixture's premise: main_win is unbound"
    assert services.sidebar() is None, "an app without a window has no sidebar"
    assert services.deck_stack() is None, "an app without a window has no deck stack"

    # A window present: each accessor forwards to the window's own typed
    # method and hands back exactly what it returns, identity included.
    sentinel_sidebar = object()
    sentinel_stack = object()
    running.main_win = SimpleNamespace(
        get_sidebar=lambda: sentinel_sidebar,
        get_deck_stack=lambda: sentinel_stack,
    )
    assert services.sidebar() is sentinel_sidebar, "sidebar() must forward the window's result"
    assert services.deck_stack() is sentinel_stack, "deck_stack() must forward the window's result"

    # The window can answer None while it is still building; the accessor
    # forwards that None rather than invent a value.
    running.main_win = SimpleNamespace(get_sidebar=lambda: None, get_deck_stack=lambda: None)
    assert services.sidebar() is None
    assert services.deck_stack() is None

    print("PASS: sidebar()/deck_stack() forward the window accessor and stay None-safe")


def check_settings_accessors_pass_through() -> None:
    manager = fixtures.StubSettingsManager(app_settings={"general": {"hold-time": 0.75}})
    gl.settings_manager = manager

    assert services.settings_manager() is manager, (
        "settings_manager() must return the manager itself"
    )

    view = services.app_settings()
    assert isinstance(view, AppSettings)
    assert view.data is manager.get_app_settings(), (
        "app_settings() must wrap the SHARED settings dict, not a copy"
    )
    assert view.hold_time == 0.75

    deck = services.deck_settings("DECK-SERIAL")
    assert deck == manager.get_deck_settings("DECK-SERIAL")
    deck["written-through"] = True
    assert manager.get_deck_settings("DECK-SERIAL").get("written-through") is True, (
        "deck_settings() must hand back what the manager hands back, unwrapped"
    )

    # Per-call slot read.
    replacement = fixtures.StubSettingsManager(app_settings={"general": {"hold-time": 0.25}})
    gl.settings_manager = replacement
    assert services.settings_manager() is replacement
    assert services.app_settings().hold_time == 0.25

    print("PASS: settings()/app_settings()/deck_settings() pass through unwrapped")


def check_page_manager_pair() -> None:
    gl.page_manager = None
    assert services.page_manager() is None, "page_manager() stays honestly Optional"

    try:
        services.require_page_manager()
    except RuntimeError as e:
        message = str(e)
    else:
        raise AssertionError("require_page_manager() must raise while the slot is None")
    assert "gl.page_manager" in message, message

    manager = FakePageManager()
    gl.page_manager = manager
    assert services.page_manager() is manager
    assert services.require_page_manager() is manager

    print("PASS: page_manager() is Optional, require_page_manager() names the absence")


def check_runtime_imports_are_globals_only() -> None:
    """Require runtime imports to stay limited to globals and the standard library.

    First-party imports risk cycles and toolkit imports in the engine closure.
    """
    tree = ast.parse(open(MODULE_PATH, encoding="utf-8").read(), MODULE_PATH)

    type_checking_bodies: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            named = (test.id if isinstance(test, ast.Name)
                     else test.attr if isinstance(test, ast.Attribute) else None)
            if named == "TYPE_CHECKING":
                for stmt in node.body:
                    for child in ast.walk(stmt):
                        type_checking_bodies.add(id(child))

    roots: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in type_checking_bodies:
            continue
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import can only reach first-party code and has
                # no root to resolve, so record it verbatim and fail below.
                roots.add("." * node.level + (node.module or ""))
            elif node.module:
                roots.add(node.module.split(".")[0])

    assert roots, f"no runtime imports found in {MODULE_PATH}: this check would pass vacuously"
    first_party = {r for r in roots if r not in sys.stdlib_module_names and r != "globals"}
    assert not first_party, (
        f"services.py must import nothing first-party but globals -- every "
        f"layer imports it: {sorted(first_party)}"
    )

    print("PASS: runtime imports are globals + stdlib")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_services_accessors")

    saved = (gl.lm, gl.app, gl.settings_manager, gl.page_manager)
    try:
        check_tr_forwards_both_shapes()
        check_tr_before_locale_manager()
        check_app_and_require_app()
        check_main_window_covers_both_absences()
        check_require_main_window()
        check_sidebar_and_deck_stack_accessors()
        check_settings_accessors_pass_through()
        check_page_manager_pair()
        check_runtime_imports_are_globals_only()
    finally:
        gl.lm, gl.app, gl.settings_manager, gl.page_manager = saved

    print("ALL PASS: scenario_services_accessors")


if __name__ == "__main__":
    main()
