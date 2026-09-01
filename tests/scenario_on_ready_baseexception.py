"""Run the redraw and open the ready gate when on_ready raises BaseException."""

# fixtures must import first: it points argv at an isolated data dir.
import fixtures

import contextlib

from fixtures import make_headless_controller, start_watchdog

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PageManagement.Page import Page
from src.backend.PluginManager.ActionCore import ActionCore


class _ReadyAbort(BaseException):
    """A non-Exception failure, e.g. a plugin that raises something outside the
    Exception hierarchy. The except Exception in _run_ready_callbacks lets it by."""


class BaseExcReadyAction(ActionCore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ready_entered = False
        self.redrawn = False

    def on_ready(self):
        self.ready_entered = True
        raise _ReadyAbort("simulated non-Exception failure in on_ready")

    def on_update(self):
        # Overrides the compat default, so this never re-enters on_ready. It
        # only records that the redraw ran.
        self.redrawn = True


def _make_action(controller, page):
    return BaseExcReadyAction(
        action_id="dev_test::BaseExcReady",
        action_name="BaseExcReady",
        deck_controller=controller,
        page=page,
        plugin_base=None,
        state=0,
        input_ident=Input.Key("0x0"),
    )


def _prefix_run_ready_callbacks(self, action):
    """The pre-fix body: the redraw sits after the try/finally, so a
    BaseException from on_ready unwinds past it."""
    try:
        action.on_ready()
    except Exception:
        pass
    finally:
        action.on_ready_finished = True
    action.on_update()


def main() -> int:
    start_watchdog(60, "on_ready_baseexception")
    fixtures._install_integration_globals()

    controller = make_headless_controller(serial="baseexc-ready")
    try:
        page = controller.active_page
        assert page is not None, "controller loaded no page"

        # The fixed method: the BaseException still propagates, but the redraw
        # runs first.
        action = _make_action(controller, page)
        # Expected: the finally runs the redraw before _ReadyAbort unwinds.
        with contextlib.suppress(_ReadyAbort):
            page._run_ready_callbacks(action)

        assert action.ready_entered, "on_ready never ran -- setup is wrong"
        assert action.on_ready_finished, (
            "on_ready_finished was not set -- the gate must open even on a raise"
        )
        assert action.redrawn, (
            "the redraw (on_update) was skipped when on_ready raised a "
            "BaseException -- it must run inside the finally"
        )

        # Mutation proof: put the pre-fix ordering back and confirm the redraw
        # is lost. A leg that survives its own mutation pins nothing.
        saved = Page._run_ready_callbacks
        Page._run_ready_callbacks = _prefix_run_ready_callbacks
        try:
            prefix_action = _make_action(controller, page)
            with contextlib.suppress(_ReadyAbort):
                page._run_ready_callbacks(prefix_action)
            assert prefix_action.ready_entered, "pre-fix leg: on_ready never ran"
            assert prefix_action.on_ready_finished, "pre-fix leg: gate must still open"
            assert not prefix_action.redrawn, (
                "the pre-fix body ran the redraw anyway -- the fix is not "
                "load-bearing and this scenario proves nothing"
            )
        finally:
            Page._run_ready_callbacks = saved

        print("PASS: scenario_on_ready_baseexception")
    finally:
        fixtures.teardown(controller)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
