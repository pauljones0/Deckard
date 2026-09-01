"""Verify that load_page stamps every input under one page-generation lock."""
import fixtures
import globals as gl


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_config_gen_stamp")
    controller = fixtures.make_headless_controller(serial="cfggen-1")
    try:
        # Disable the async rebuild path to isolate load_page's lock-held stamps.
        controller.load_all_inputs = lambda *a, **k: None

        seed_path = fixtures.seed_page("CfgGenPage")
        page = gl.page_manager.get_page(seed_path, controller)

        prev_gen = controller._page_load_generation
        controller.load_page(page, allow_reload=True)
        gen = controller._page_load_generation
        assert gen == prev_gen + 1, f"expected a generation bump, got {prev_gen} -> {gen}"

        total = 0
        stale = []
        for input_type in controller.inputs:
            for inp in controller.inputs[input_type]:
                total += 1
                if inp.config_gen != gen:
                    stale.append((str(inp.identifier), inp.config_gen))

        assert not stale, (
            f"{len(stale)}/{total} input(s) still carry a stale config_gen after "
            f"load_page returned (current gen={gen}): {stale[:8]} -- a paint "
            f"reading this would be dropped by the present-boundary judge as "
            f"stale-gen, blanking the new page's own keys"
        )
        print(f"PASS: all {total} inputs stamped config_gen={gen} synchronously at the gen bump")
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_config_gen_stamp")


if __name__ == "__main__":
    main()
