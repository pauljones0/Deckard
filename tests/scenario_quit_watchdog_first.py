"""Arm the watchdog before UI, D-Bus, window, or AppQuit teardown can block.
Raise at the first teardown step to verify that the watchdog is already set."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

from src import app as app_module  # noqa: E402
from src.app import App  # noqa: E402
from fixtures import start_watchdog  # noqa: E402


class _FirstTeardownReached(Exception):
    pass


class _StubApp:
    """Carries only what on_quit reads before the first teardown step."""

    def __init__(self):
        self._quit_started = False
        self._ui_adapter = None

    def force_quit(self):  # the watchdog target; never invoked in this test
        pass


def main() -> int:
    start_watchdog(30, "quit_watchdog_first")

    scheduled: list[tuple] = []
    real_schedule = app_module.timer_wheel.schedule
    real_install = app_module.ui_port.install

    def recording_schedule(delay, callback, *args, **kwargs):
        scheduled.append((delay, kwargs.get("name")))
        return object()

    def exploding_install(_port):
        # The first teardown step. Raising here stops on_quit before it
        # reaches os._exit, so the ordering is all that this observes.
        raise _FirstTeardownReached()

    app_module.timer_wheel.schedule = recording_schedule
    app_module.ui_port.install = exploding_install
    try:
        try:
            App.on_quit(_StubApp())
        except _FirstTeardownReached:
            pass
        else:
            print("FAIL: the first teardown step never ran; test is inconclusive")
            return 1
    finally:
        app_module.timer_wheel.schedule = real_schedule
        app_module.ui_port.install = real_install

    watchdog = [s for s in scheduled if s[1] == "force_quit_timer"]
    if not watchdog:
        print("FAIL: the force-quit watchdog was not armed before the first "
              "teardown step")
        return 1
    if watchdog[0][0] != 6:
        print(f"FAIL: the watchdog delay changed unexpectedly: {watchdog[0][0]}")
        return 1

    print("PASS: on_quit arms the force-quit watchdog before any teardown step")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
