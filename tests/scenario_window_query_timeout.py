"""A stuck window-query helper is killed within its deadline.

The KDE window queries ran Popen.communicate() with no timeout, which parks
the caller forever on a stuck compositor helper. communicate_bounded gives
every one-shot query a deadline, terminates and kills a helper that overruns
it, reaps the child, and returns None. This drives it against a real hanging
subprocess and a normal one.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

from fixtures import start_watchdog  # noqa: E402
from src.backend.WindowGrabber.Integration import communicate_bounded  # noqa: E402


def main() -> int:
    start_watchdog(30, "window_query_timeout")
    failures: list[str] = []

    # --- A hanging helper is killed within the deadline and returns None.
    hung = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                            stdout=subprocess.PIPE)
    began = time.monotonic()
    out = communicate_bounded(hung, "test hang", timeout_s=0.5)
    elapsed = time.monotonic() - began
    if out is not None:
        failures.append(f"a hanging helper returned output instead of None: {out!r}")
    if elapsed > 5:
        failures.append(f"the bounded wait took {elapsed:.1f}s; the deadline did not bound it")
    # The child is reaped: poll returns a code, not None.
    if hung.poll() is None:
        hung.kill()
        failures.append("the hanging helper was not killed and reaped")

    # --- A normal helper returns its output.
    ok = subprocess.Popen([sys.executable, "-c", "print('hello')"],
                          stdout=subprocess.PIPE)
    out = communicate_bounded(ok, "test ok", timeout_s=5)
    if out is None or out.decode().strip() != "hello":
        failures.append(f"a normal helper did not return its output: {out!r}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: a stuck window-query helper is killed within its deadline; a "
          "normal one returns its output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
