"""The scenario runner must reap a timed-out scenario's whole process tree."""

import fixtures  # noqa: F401  (isolated --data tempdir; import first)

from contextlib import suppress
import os
import signal
import tempfile
import time
from pathlib import Path

import run_all


def _has_exited(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _wait_for(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_timeout_reaps_scenario_and_descendant() -> None:
    pids: list[int] = []
    with tempfile.TemporaryDirectory() as directory:
        marker = Path(directory) / "pids"
        scenario = Path(directory) / "sleeper.py"
        scenario.write_text(
            f"""import os
import subprocess
import sys
import time

marker = {str(marker)!r}
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(marker, "w", encoding="utf-8") as file:
    print(os.getpid(), file=file, flush=True)
    print(child.pid, file=file, flush=True)
time.sleep(60)
""",
            encoding="utf-8",
        )
        try:
            ok, output, _ = run_all.run_one(scenario, timeout=1.0)
            assert not ok, "the sleeper scenario must time out"
            assert "TIMED OUT" in output, "the timeout result must retain its marker"
            pids = [int(pid) for pid in marker.read_text(encoding="utf-8").splitlines()]
            assert len(pids) == 2, "the scenario must record itself and its descendant"
            assert _wait_for(lambda: all(_has_exited(pid) for pid in pids), 2.0), (
                "a timed-out scenario left a process alive: "
                + ", ".join(str(pid) for pid in pids if not _has_exited(pid))
            )
        finally:
            for pid in pids:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


def main() -> None:
    test_timeout_reaps_scenario_and_descendant()
    print("PASS: scenario timeout reaps descendants")


if __name__ == "__main__":
    main()
