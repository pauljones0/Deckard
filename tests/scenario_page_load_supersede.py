"""Verify superseded loads stop at mutation boundaries and current loads finish."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import globals as gl  # noqa: F401, E402
from fixtures import start_watchdog  # noqa: E402

from src.backend.DeckManagement.InputIdentifier import Input  # noqa: E402
from src.backend.DeckManagement.deck_controller import input_state_classes  # noqa: E402


INPUT_CONFIG = {
    "states": {
        "0": {
            "labels": {"bottom": {"text": "OLD-PAGE"}},
            "media": {},
        }
    }
}


def main() -> int:
    start_watchdog(60, "page_load_supersede")
    controller = fixtures.make_headless_controller(serial="load-supersede-1")
    failures: list[str] = []
    try:
        key = controller.get_input(Input.Key("0x0"))

        # Supersede inside own_actions_update to model a blocked plugin callback
        # that resumes after the page switched.
        load_is_current = {"value": True}
        real_update = input_state_classes.ControllerKeyState.own_actions_update

        def superseding_update(self):
            load_is_current["value"] = False
            return real_update(self)

        input_state_classes.ControllerKeyState.own_actions_update = superseding_update
        try:
            key.load_from_input_dict(INPUT_CONFIG, update=False,
                                     still_current=lambda: load_is_current["value"])
        finally:
            input_state_classes.ControllerKeyState.own_actions_update = real_update

        state = key.get_active_state()
        bottom_label = state.label_manager.page_labels.get("bottom")
        if bottom_label is not None and bottom_label.text == "OLD-PAGE":
            failures.append("a superseded load still wrote the old page's label")

        # --- A current load applies everything.
        load_is_current["value"] = True
        key.load_from_input_dict(INPUT_CONFIG, update=False,
                                 still_current=lambda: load_is_current["value"])
        state = key.get_active_state()
        bottom_label = state.label_manager.page_labels.get("bottom")
        if bottom_label is None or bottom_label.text != "OLD-PAGE":
            failures.append(f"a current load did not apply the label: {bottom_label}")

        # A stale generation must not load; a current generation must pass a
        # live still_current probe that tracks later generation changes.
        page = controller.active_page
        if page is None:
            failures.append("no active page; the wiring leg would prove nothing")
        else:
            calls: list = []

            def recording_load(config, update=True, page=None, *, still_current=None):
                calls.append(still_current)

            real_load = key.load_from_input_dict
            key.load_from_input_dict = recording_load  # type: ignore[method-assign]
            try:
                stale_gen = controller._page_load_generation - 1
                controller._load_input_if_current(key, page, update=False, gen=stale_gen)
                if calls:
                    failures.append("a stale-generation load reached the input")

                current_gen = controller._page_load_generation
                controller._load_input_if_current(key, page, update=False, gen=current_gen)
                if len(calls) != 1 or calls[0] is None:
                    failures.append("the current load did not carry a still_current probe")
                elif not calls[0]():
                    failures.append("the probe answers False for the live generation")
                else:
                    # The probe tracks the generation: a bump flips it.
                    with controller._page_gen_lock:
                        controller._page_load_generation += 1
                    if calls[0]():
                        failures.append("the probe missed a generation bump")
                    with controller._page_gen_lock:
                        controller._page_load_generation -= 1
            finally:
                key.load_from_input_dict = real_load  # type: ignore[method-assign]
    finally:
        fixtures.teardown(controller)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: a superseded load stops at the mutation boundary; a current "
          "load applies everything")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
