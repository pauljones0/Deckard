#!/usr/bin/env python3
"""Capture real HID input latency while an externally claimed deck paints video.
Never inject events; the operator must claim the deck, close other instances, and press a physical key."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT_ROOT = ROOT / "tests" / "hardware" / "reports"
APP_ID = "io.github.nazbert.Deckard"
CLAIM_PHRASE = "I-OWN-THIS-DECK"


def die(message: str) -> None:
    raise SystemExit(f"FATAL: {message}")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def dbus_owner_pid() -> int | None:
    """Return the local session owner of Deckard's single-instance name."""
    try:
        result = run(["busctl", "--user", "--no-legend", "list"])
    except OSError as error:
        die(f"cannot inspect the session bus for a Deckard owner: {error}")
    for line in result.stdout.splitlines():
        columns = line.split()
        if columns and columns[0] == APP_ID and len(columns) > 1 and columns[1].isdigit():
            return int(columns[1])
    return None


def elgato_device_count() -> int:
    try:
        result = run(["lsusb"])
    except OSError as error:
        die(f"cannot inspect USB devices: {error}")
    return sum("0fd9:" in line.lower() for line in result.stdout.splitlines())


def preflight(*, take_deck: bool, claim_deck: str | None) -> None:
    """Refuse ambiguous ownership; cross-session claiming stays manual."""
    if not take_deck or claim_deck != CLAIM_PHRASE:
        die(
            "hardware access requires --take-deck and "
            f"--claim-deck {CLAIM_PHRASE} after the external deck-claim protocol"
        )
    owner = dbus_owner_pid()
    if owner is not None:
        die(
            f"Deckard pid {owner} owns {APP_ID}. Close it from its own session and "
            "re-run after verifying the deck is free; this driver never automates "
            "cross-session ownership transfer."
        )
    if elgato_device_count() == 0:
        die("no Elgato USB device is visible")


def make_run_directory(report_root: Path, run_id: str | None = None) -> tuple[str, Path]:
    """Create one fresh, non-overwritable directory for a report pair member."""
    if run_id is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"latency-{stamp}-{uuid.uuid4().hex[:12]}"
    output = report_root / run_id
    output.mkdir(parents=True, exist_ok=False)
    return run_id, output


def verify_reports(report_dir: Path, *, run_id: str, writer_pid: int,
                   expect_video: bool = True,
                   expected_decks: int) -> list[Path]:
    reports = sorted(report_dir.glob("*.json"))
    if len(reports) != expected_decks:
        die(
            f"expected {expected_decks} report(s) from this run, found {len(reports)} "
            f"in {report_dir}"
        )
    for report in reports:
        try:
            payload = json.loads(report.read_text())
        except (OSError, json.JSONDecodeError) as error:
            die(f"cannot read current-run report {report}: {error}")
        if payload.get("run_id") != run_id or payload.get("writer_pid") != writer_pid:
            die(f"report {report} was not written by this run")
        if payload.get("validity", {}).get("valid_for_comparison") is not True:
            die(f"report {report} has incomplete physical inputs and cannot support comparison")
        if payload.get("video_saturated") is not expect_video:
            die(f"report {report} attests video_saturated="
                f"{payload.get('video_saturated')!r}, expected {expect_video}: "
                f"a saturated and a quiet capture must never be compared as "
                f"siblings")
    return reports


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--take-deck", action="store_true",
                        help="state that this terminal has an external deck claim")
    parser.add_argument("--claim-deck", metavar="PHRASE",
                        help=f"must exactly be {CLAIM_PHRASE}")
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT,
                        help="parent directory for unique run output directories")
    parser.add_argument("--video-page",
                        help="the manually selected page that saturates the deck with video")
    parser.add_argument("--no-video", action="store_true",
                        help="capture a quiet-deck baseline on a page with no animated media")
    parser.add_argument("--expected-decks", type=int, default=1,
                        help="number of claimed decks expected to emit reports")
    parser.add_argument("--selftest", action="store_true",
                        help="validate report-directory and report-origin checks without hardware")
    return parser.parse_args()


def selftest() -> None:
    with tempfile.TemporaryDirectory(prefix="deckard-latency-selftest-") as temporary:
        root = Path(temporary)
        run_id, report_dir = make_run_directory(root, "known-run")
        report = report_dir / "deck.json"
        report.write_text(json.dumps({
            "run_id": run_id,
            "writer_pid": 123,
            "video_saturated": True,
            "validity": {"valid_for_comparison": True},
        }))
        assert verify_reports(report_dir, run_id=run_id, writer_pid=123,
                              expected_decks=1) == [report]
        quiet_run, quiet_dir = make_run_directory(root, "quiet-run")
        quiet_report = quiet_dir / "deck.json"
        quiet_report.write_text(json.dumps({
            "run_id": quiet_run,
            "writer_pid": 123,
            "video_saturated": False,
            "validity": {"valid_for_comparison": True},
        }))
        assert verify_reports(quiet_dir, run_id=quiet_run, writer_pid=123,
                              expected_decks=1, expect_video=False) == [quiet_report]
        try:
            verify_reports(quiet_dir, run_id=quiet_run, writer_pid=123,
                           expected_decks=1)
        except SystemExit:
            pass
        else:
            raise AssertionError(
                "a quiet report must not verify as a saturated one")
        try:
            make_run_directory(root, run_id)
        except FileExistsError:
            pass
        else:
            raise AssertionError("a run directory must never reuse stale evidence")
        try:
            verify_reports(report_dir, run_id=run_id, writer_pid=999,
                           expected_decks=1)
        except SystemExit:
            pass
        else:
            raise AssertionError("a report from another process must be rejected")
    print("PASS: hw_input_latency selftest")


def main() -> None:
    args = parse_args()
    if args.selftest:
        selftest()
        return
    if bool(args.video_page) == bool(args.no_video):
        die("pass exactly one of --video-page or --no-video: the report "
            "attests which load shape it measured")
    if args.expected_decks < 1:
        die("--expected-decks must be at least one")
    preflight(take_deck=args.take_deck, claim_deck=args.claim_deck)
    run_id, report_dir = make_run_directory(args.report_root.resolve())
    env = os.environ.copy()
    env["DECKARD_INPUT_LATENCY_REPORT_DIR"] = str(report_dir)
    env["DECKARD_INPUT_LATENCY_RUN_ID"] = run_id
    env["DECKARD_INPUT_LATENCY_VIDEO_SATURATED"] = "0" if args.no_video else "1"
    env["DECKARD_INPUT_LATENCY_VIDEO_PAGE"] = args.video_page or ""

    process = subprocess.Popen([str(ROOT / "scripts" / "Deckard"), "--devel"],
                               cwd=ROOT, env=env, start_new_session=True)
    try:
        prompt = (
            "Select a page with no animated media, then press a "
            if args.no_video else
            "Select the declared video page, confirm video saturation, then press a ")
        input(
            prompt +
            "responsive physical key repeatedly. Press Enter here to end the run. "
        )
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    reports = verify_reports(
        report_dir,
        run_id=run_id,
        writer_pid=process.pid,
        expected_decks=args.expected_decks,
    )
    print("Wrote current-run input latency reports:")
    for report in reports:
        print(report)


if __name__ == "__main__":
    main()
