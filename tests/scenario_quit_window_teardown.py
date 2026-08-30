"""Do not destroy an unrealized main window because GTK 4.22 can segfault.
Background mode leaves it unrealized, and an abort would prevent backend termination."""

# GTK windows need a display, so this scenario skips cleanly without one.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import fixtures  # noqa: F401  (isolated data dir + watchdog; must precede app imports)

fixtures.start_watchdog(60, "quit_window_teardown")

if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
    print("SKIP: no display; GTK window construction needs one")
    raise SystemExit(0)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from src.app import App  # noqa: E402


class _Stub:
    """Carries just the attribute the real unbound method reads."""

    def __init__(self, main_win):
        self.main_win = main_win


def check_missing_window_noop():
    class _NoWin:
        pass

    App._destroy_main_window(_NoWin())
    print("PASS 1: no main_win attribute -> no-op")


def _run_in_app(body, app_id):
    """Run body(app) inside a GtkApplication.
    The abort requires a bound window and an active application loop."""
    app = Adw.Application(application_id=app_id)
    result = {}

    def on_activate(_a):
        body(app, result)
        app.quit()

    app.connect("activate", on_activate)
    app.run([])
    return result


def check_unrealized_window_survives():
    """Reach the final assertion without the interpreter receiving SIGSEGV.
    The failure has no Python exception to inspect."""
    for cls, label in ((Gtk.ApplicationWindow, "Gtk"), (Adw.ApplicationWindow, "Adw")):
        def body(app, result, cls=cls):
            win = cls(application=app)  # built exactly as on_activate does
            assert not win.get_realized(), "precondition: never presented"
            App._destroy_main_window(_Stub(win))
            result["survived"] = True

        res = _run_in_app(body, f"dev.deckard.scenario.qwt.unrealized{label}")
        assert res.get("survived"), f"{label}: teardown did not complete"
        print(f"PASS 2{'' if label == 'Gtk' else 'b'}: unrealized "
              f"{label}.ApplicationWindow -> survived teardown")


def check_realized_window_destroyed():
    """The guard must not turn the normal (windowed) path into a no-op."""
    app = Adw.Application(application_id="dev.deckard.scenario.quitteardown")
    result = {}

    def on_activate(_a):
        win = Adw.ApplicationWindow(application=app)
        win.connect("destroy", lambda *_: result.__setitem__("destroyed", True))
        win.present()
        result["realized_before"] = win.get_realized()
        App._destroy_main_window(_Stub(win))
        app.quit()

    app.connect("activate", on_activate)
    app.run([])

    assert result.get("realized_before"), "presented window should have realized"
    assert result.get("destroyed"), \
        "a realized window must still be destroyed, not skipped"
    print("PASS 3: realized window -> still destroyed")


def main():
    check_missing_window_noop()
    check_unrealized_window_survives()
    check_realized_window_destroyed()
    print("ALL PASS: scenario_quit_window_teardown")


if __name__ == "__main__":
    main()
