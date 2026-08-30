#!/usr/bin/env python3
"""
Runner for the scenario harness (docs/presenter-migration-plan.md).

Runs each tests/scenario_*.py in its own subprocess and interpreter, so one
crash or hang cannot corrupt the next scenario.

Usage:
    .venv/bin/python tests/run_all.py [-k SUBSTRING] [--timeout SECONDS]
                                      [--scenario-list PATH] [--junit PATH] [--jobs N]
"""
import argparse
from contextlib import suppress
import os
import re
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.dom import minidom

# Each scenario gets an isolated temp data dir from fixtures.py. The run
# prints a PASS and FAIL table, and exits 1 when a scenario fails.
TESTS_DIR = Path(__file__).resolve().parent

# Scenarios that assert behavior the current code does not have yet. Add an
# entry here with a one-line reason instead of weakening its assertions.
EXPECTED_FAIL_UNTIL_M1: dict[str, str] = {
    # "scenario_example.py": "needs the M1 control queue",
}

_TERM_GRACE_SECONDS = 3.0
_KILL_GRACE_SECONDS = 3.0


def discover_scenarios() -> list[Path]:
    return sorted(TESTS_DIR.glob("scenario_*.py"))


def _decode(output: str | bytes | None) -> str:
    if output is None:
        return ""
    return output.decode(errors="replace") if isinstance(output, bytes) else output


def _combine_output(previous: str | bytes | None, current: str | bytes | None) -> str:
    previous_text = _decode(previous)
    current_text = _decode(current)
    if current_text.startswith(previous_text):
        return current_text
    return previous_text + current_text


def _terminate_process_group(proc: subprocess.Popen[str]) -> tuple[str, str]:
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)

    try:
        stdout, stderr = proc.communicate(timeout=_TERM_GRACE_SECONDS)
        return stdout, stderr
    except subprocess.TimeoutExpired as error:
        term_stdout, term_stderr = error.stdout, error.stderr

    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)

    try:
        stdout, stderr = proc.communicate(timeout=_KILL_GRACE_SECONDS)
        return (
            _combine_output(term_stdout, stdout),
            _combine_output(term_stderr, stderr),
        )
    except subprocess.TimeoutExpired as error:
        return (
            _combine_output(term_stdout, error.stdout),
            _combine_output(term_stderr, error.stderr),
        )


def load_scenario_list(path: Path) -> list[Path]:
    """Load an ordered, tracked list of scenario filenames.

    A list is intentionally filenames only: it cannot escape tests/, and each
    selected scenario must exist when the harness starts. This makes a CI
    subset reviewable while preserving the normal discovery path for the full
    suite.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"cannot read scenario list {path}: {error}") from error

    scenarios: list[Path] = []
    names: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        name = line.partition("#")[0].strip()
        if not name:
            continue
        if Path(name).name != name or not name.startswith("scenario_") or not name.endswith(".py"):
            raise ValueError(
                f"{path}:{line_number}: expected a scenario_*.py filename, got {name!r}"
            )
        if name in names:
            raise ValueError(f"{path}:{line_number}: duplicate scenario {name}")
        scenario = TESTS_DIR / name
        if scenario.is_symlink():
            raise ValueError(f"{path}:{line_number}: scenario must not be a symbolic link: {name}")
        if not scenario.is_file():
            raise ValueError(f"{path}:{line_number}: scenario does not exist: {name}")
        try:
            scenario.resolve(strict=True).relative_to(TESTS_DIR.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise ValueError(
                f"{path}:{line_number}: scenario resolves outside tests/: {name}"
            ) from error
        names.add(name)
        scenarios.append(scenario)

    if not scenarios:
        raise ValueError(f"{path}: contains no scenarios")
    return scenarios


def run_one(path: Path, timeout: float) -> tuple[bool, str, float]:
    start = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, str(path)],
        cwd=str(TESTS_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        elapsed = time.monotonic() - start
        ok = proc.returncode == 0
        output = stdout + stderr
        return ok, output, elapsed
    except subprocess.TimeoutExpired as error:
        elapsed = time.monotonic() - start
        stdout, stderr = _terminate_process_group(proc)
        output = (
            _combine_output(error.stdout, stdout)
            + _combine_output(error.stderr, stderr)
            + f"\n[TIMED OUT after {timeout}s]"
        )
        return False, output, elapsed


def _classify(name: str, ok: bool) -> tuple[str, bool]:
    """Map a scenario result to (status, counts_as_hard_failure) through the
    expected-fail list. One function keeps the serial and parallel paths
    identical."""
    expected_fail_reason = EXPECTED_FAIL_UNTIL_M1.get(name)
    if ok:
        return "PASS", False
    if expected_fail_reason is not None:
        return "XFAIL", False  # an expected failure does not fail the run
    return "FAIL", True


# Captured scenario output can carry ANSI colour and other control bytes. XML
# 1.0 forbids the C0 control characters bar tab, newline and carriage return,
# so minidom's parse of the serialized tree raises on them and the whole
# report is lost. Strip ANSI escape sequences, then any remaining forbidden
# control character.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_FORBIDDEN_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _xml_safe(text: str) -> str:
    return _FORBIDDEN_CONTROL.sub("", _ANSI_ESCAPE.sub("", text or ""))


def write_junit(path: Path, results: list) -> None:
    """Write a JUnit XML report with one testsuite and one testcase each.

    A FAIL case carries a failure child and an XFAIL case a skipped child. A
    PASS case attaches the captured stdout and stderr as system-out."""
    total_time = sum(elapsed for _, _, elapsed, _ in results)
    n_fail = sum(1 for _, s, _, _ in results if s == "FAIL")
    n_skip = sum(1 for _, s, _, _ in results if s == "XFAIL")

    suite = ET.Element("testsuite", {
        "name": "deckard-harness",
        "tests": str(len(results)),
        "failures": str(n_fail),
        "skipped": str(n_skip),
        "errors": "0",
        "time": f"{total_time:.3f}",
    })
    for name, status, elapsed, output in results:
        output = _xml_safe(output)
        case = ET.SubElement(suite, "testcase", {
            "classname": "scenarios",
            "name": name,
            "time": f"{elapsed:.3f}",
        })
        if status == "FAIL":
            failure = ET.SubElement(case, "failure", {
                "message": f"{name} failed (non-zero exit)",
                "type": "ScenarioFailure",
            })
            failure.text = output
        elif status == "XFAIL":
            skipped = ET.SubElement(case, "skipped", {
                "message": EXPECTED_FAIL_UNTIL_M1.get(name, "expected failure"),
            })
            skipped.text = output
        else:
            system_out = ET.SubElement(case, "system-out")
            system_out.text = output

    xml_bytes = ET.tostring(suite, encoding="utf-8")
    pretty = minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")
    path.write_bytes(pretty)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-k", dest="substring", default=None,
                         help="only run scenarios whose filename contains this substring")
    parser.add_argument("--scenario-list", type=Path, default=None,
                        help="run the ordered scenario_*.py filenames listed in this file")
    parser.add_argument("--timeout", type=float, default=90.0,
                         help="per-scenario timeout in seconds (default: 90)")
    parser.add_argument("-v", "--verbose", action="store_true",
                         help="print each scenario's captured output even on success")
    parser.add_argument("--junit", type=Path, default=None,
                         help="also write a JUnit XML report to this path")
    parser.add_argument("--jobs", type=int, default=1,
                         help="run up to N scenarios in parallel (default: 1 = serial). "
                              "Each scenario is an isolated subprocess with its own temp "
                              "data dir, so this is safe; the output table and exit code "
                              "are identical to serial.")
    args = parser.parse_args()

    if args.substring and args.scenario_list is not None:
        parser.error("--scenario-list cannot be combined with -k")

    if args.scenario_list is not None:
        try:
            scenarios = load_scenario_list(args.scenario_list)
        except ValueError as error:
            parser.error(str(error))
    else:
        scenarios = discover_scenarios()
    if args.substring:
        scenarios = [s for s in scenarios if args.substring in s.name]

    if not scenarios:
        print("No scenario_*.py files found/matched.")
        return 1

    # Collect (name, status, elapsed, output). The table follows discovery
    # order, not completion order, so serial and parallel runs print the same.
    results_by_name: dict[str, tuple] = {}

    def _record(path: Path, ok: bool, output: str, elapsed: float) -> None:
        status, _ = _classify(path.name, ok)
        results_by_name[path.name] = (path.name, status, elapsed, output)
        if args.verbose or status == "FAIL":
            print(f"----- {path.name} output -----")
            print(output.rstrip())
            print(f"----- end {path.name} -----")

    jobs = max(1, args.jobs)
    if jobs == 1:
        for path in scenarios:
            ok, output, elapsed = run_one(path, args.timeout)
            _record(path, ok, output, elapsed)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            # Submit every scenario, then collect in discovery order, so the
            # table and the verbose output match the serial run.
            future_for = {path: pool.submit(run_one, path, args.timeout) for path in scenarios}
            for path in scenarios:
                ok, output, elapsed = future_for[path].result()
                _record(path, ok, output, elapsed)

    results = [results_by_name[path.name] for path in scenarios]
    any_hard_failure = any(status == "FAIL" for _, status, _, _ in results)

    print()
    print(f"{'SCENARIO':<32} {'STATUS':<8} {'TIME':>8}")
    print("-" * 50)
    for name, status, elapsed, _ in results:
        extra = f"  ({EXPECTED_FAIL_UNTIL_M1[name]})" if status == "XFAIL" else ""
        print(f"{name:<32} {status:<8} {elapsed:>6.2f}s{extra}")

    n_pass = sum(1 for _, s, _, _ in results if s == "PASS")
    n_xfail = sum(1 for _, s, _, _ in results if s == "XFAIL")
    n_fail = sum(1 for _, s, _, _ in results if s == "FAIL")
    print("-" * 50)
    print(f"{n_pass} passed, {n_xfail} expected-fail, {n_fail} failed (of {len(results)})")

    if args.junit is not None:
        write_junit(args.junit, results)
        print(f"JUnit XML written to {args.junit}")

    return 1 if any_hard_failure else 0


if __name__ == "__main__":
    sys.exit(main())
