"""Contract checks for the tracked blocking scenario selection."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import tempfile
from pathlib import Path

import run_all
from run_all import TESTS_DIR, load_scenario_list


SMOKE_SCENARIOS = [
    "scenario_media_config.py",
    "scenario_atomic_settings.py",
    "scenario_corrupt_json_fallback.py",
    "scenario_move_page_no_stray_destination.py",
    "scenario_wallpaper_viewport_math.py",
    "scenario_event_dispatch_contract.py",
    "scenario_input_pipeline.py",
    "scenario_page_save_mutation.py",
    "scenario_shutdown.py",
    "scenario_background_wait_yield.py",
    "scenario_writer_survival.py",
    "scenario_runner_timeout_descendants.py",
    "scenario_floor_import.py",
    "scenario_comment_inventory.py",
    "scenario_scenario_list.py",
]


def check_tracked_membership() -> None:
    selected = load_scenario_list(TESTS_DIR / "scenario-smoke.txt")
    assert [path.name for path in selected] == SMOKE_SCENARIOS
    print("PASS: tracked smoke selection has the expected ordered membership")


def check_invalid_lists_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "scenarios.txt"
        path.write_text("scenario_media_config.py\nscenario_media_config.py\n")
        try:
            load_scenario_list(path)
        except ValueError as error:
            assert "duplicate scenario" in str(error)
        else:
            raise AssertionError("duplicate scenario list entries must fail")

        path.write_text("../scenario_media_config.py\n")
        try:
            load_scenario_list(path)
        except ValueError as error:
            assert "expected a scenario_*.py filename" in str(error)
        else:
            raise AssertionError("scenario lists must reject paths outside tests/")

        isolated_tests = Path(directory) / "tests"
        isolated_tests.mkdir()
        outside = Path(directory) / "outside.py"
        outside.write_text("raise SystemExit(0)\n")
        (isolated_tests / "scenario_escape.py").symlink_to(outside)
        path.write_text("scenario_escape.py\n")
        original_tests_dir = run_all.TESTS_DIR
        run_all.TESTS_DIR = isolated_tests
        try:
            try:
                load_scenario_list(path)
            except ValueError as error:
                assert "must not be a symbolic link" in str(error)
            else:
                raise AssertionError("scenario lists must reject symbolic links")
        finally:
            run_all.TESTS_DIR = original_tests_dir
    print("PASS: invalid scenario lists fail closed")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_scenario_list")
    check_tracked_membership()
    check_invalid_lists_fail_closed()
    print("PASS: scenario_scenario_list")


if __name__ == "__main__":
    main()
