#!/usr/bin/env python3
"""Run the gated hardware suite unattended, with the deck claim choreography.

The physical deck is held exclusively by the system instance, so a hardware
run has to take it and give it back. This driver does that once around the
whole batch instead of once per script:

  1. Preflight. The deck must be on USB, and the running instance must quit
     cleanly over D-Bus, or the run refuses to start. Nothing is ever
     force-killed: an instance that does not answer keeps the deck.
  2. Run every script in tests/hardware/gated/, each in its own process
     group with a timeout. A timeout kills the whole group, so a script's
     engine subprocesses die with it and cannot hold the deck hostage.
  3. Relaunch the system instance, whatever the results were.
  4. Print one summary and exit nonzero when any script failed.

Script contract (gated/*.py):
  - Non-interactive: no input(), no visual judgment, exit code is the verdict.
  - Self-contained data: never reads or writes the real data dir (use
    hw_verify.make_scratch_data or a temp dir).
  - A script that can run with NO deck attached declares it with a line
    `# hw: no-deck-ok` in its first 10 lines. --no-deck runs only those,
    skips the claim choreography entirely, and is safe on any machine.

Interactive tools and visual calibration live in manual/, and historical
one-off MR verifiers in manual/archive/ (untracked). See README.md.
"""
import argparse
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GATED = os.path.join(HERE, "gated")
sys.path.insert(0, HERE)

import hw_verify as hv  # noqa: E402  (the shared claim/preflight library)

SCRIPT_TIMEOUT_S = 900


def no_deck_ok(path: str) -> bool:
    """Whether the script declares it runs without hardware."""
    try:
        with open(path) as f:
            for _ in range(10):
                if "# hw: no-deck-ok" in f.readline():
                    return True
    except OSError:
        pass
    return False


def discover(only: "str | None", no_deck: bool) -> list[str]:
    scripts = sorted(
        os.path.join(GATED, entry) for entry in os.listdir(GATED)
        if entry.endswith(".py") and not entry.startswith("_"))
    if only:
        scripts = [s for s in scripts if only in os.path.basename(s)]
    if no_deck:
        scripts = [s for s in scripts if no_deck_ok(s)]
    return scripts


def run_script(path: str, timeout_s: int) -> "tuple[str, float, int]":
    """Run one gated script in its own process group. Returns
    (verdict, seconds, returncode)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    began = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, path], cwd=hv.REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True)
    try:
        output, _ = proc.communicate(timeout=timeout_s)
        verdict = "PASS" if proc.returncode == 0 else "FAIL"
    except subprocess.TimeoutExpired:
        # Kill the whole group: the script's engine subprocesses must die
        # with it, or they keep the deck open into the next script.
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            output, _ = proc.communicate()
        verdict = "TIMEOUT"
    seconds = time.monotonic() - began
    name = os.path.basename(path)
    print(f"--- {name}: {verdict} in {seconds:.1f}s (rc={proc.returncode}) ---")
    if verdict != "PASS" and output:
        print(output[-4000:])
    return verdict, seconds, proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="run only gated scripts whose name contains this")
    ap.add_argument("--timeout", type=int, default=SCRIPT_TIMEOUT_S,
                    help="per-script timeout in seconds")
    ap.add_argument("--no-deck", action="store_true",
                    help="run only the no-deck-ok scripts, with no claim choreography")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would run and the choreography, then exit")
    args = ap.parse_args()

    scripts = discover(args.only, args.no_deck)
    if not scripts:
        print("Nothing to run: no matching script in gated/")
        return 1

    if args.dry_run:
        print("Would run" + (" (no deck)" if args.no_deck else " (with deck claim)") + ":")
        for s in scripts:
            marker = " [no-deck-ok]" if no_deck_ok(s) else ""
            print(f"  {os.path.basename(s)}{marker}")
        if not args.no_deck:
            pid = hv.dbus_owner_pid()
            print(f"Claim: running instance pid={pid}; would quit it over D-Bus "
                  f"and relaunch {' '.join(hv.SYSTEM_LAUNCHER)} afterwards")
        return 0

    stopped = False
    if not args.no_deck:
        # The one claim for the whole batch. Refuses rather than forces:
        # an instance that does not answer the D-Bus quit keeps the deck.
        pid = hv.dbus_owner_pid()
        if pid is not None:
            print(f"Taking the deck: quitting the running instance (pid {pid}) over D-Bus")
            hv.dbus_quit()
            if not hv.wait_gone(pid, 45):
                print(f"REFUSED: instance pid {pid} did not exit within 45s; "
                      f"not forcing it, the deck stays with it")
                return 1
            stopped = True
        usb = hv.sh(["lsusb"])
        if "0fd9" not in usb.stdout:
            if stopped:
                subprocess.Popen(hv.SYSTEM_LAUNCHER, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("REFUSED: no Elgato device on USB")
            return 1

    results: "list[tuple[str, str, float, int]]" = []
    try:
        for path in scripts:
            verdict, seconds, rc = run_script(path, args.timeout)
            results.append((os.path.basename(path), verdict, seconds, rc))
    finally:
        if stopped:
            print(f"Relaunching the system instance: {' '.join(hv.SYSTEM_LAUNCHER)}")
            subprocess.Popen(hv.SYSTEM_LAUNCHER, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("\n=== gated hardware suite ===")
    failed = 0
    for name, verdict, seconds, _rc in results:
        print(f"  {name:<40} {verdict:>8}  {seconds:7.1f}s")
        if verdict != "PASS":
            failed += 1
    print(f"{len(results) - failed} passed, {failed} failed (of {len(results)})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
