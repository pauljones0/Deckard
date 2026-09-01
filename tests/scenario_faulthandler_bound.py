"""Check in-place faulthandler log bounds and preservation of live file descriptors."""
import fixtures  # must be first; isolates DATA_PATH before any src import

import faulthandler
import getpass
import os
import tempfile

from src.backend import log_hooks

HOME = os.path.expanduser("~")
USER = getpass.getuser()

TRIM_NOTICE = "===== trimmed "
BOOT_MARKER = "===== boot "


def seed_multi_boot(path: str, sessions: int) -> None:
    """Write tagged boot records that expose the trim boundary."""
    with open(path, "w") as f:
        for i in range(1, sessions + 1):
            f.write(f"{BOOT_MARKER}2026-01-{i:02d}T00:00:00 pid={1000 + i} =====\n")
            f.write("Fatal Python error: Segmentation fault\n")
            f.write("\nCurrent thread 0x00007f0a1c2b3c00 (most recent call first):\n")
            f.write(f'  File "{HOME}/dev/App/main.py", line {i} in <module>  session{i}payload\n')


def check_bound_unit() -> None:
    log_hooks._FAULT_LOG_MAX_BYTES = 600

    with tempfile.TemporaryDirectory(prefix="fh-bound-") as d:
        path = os.path.join(d, "faulthandler.log")

        # A small file is left byte-untouched: the trim is a no-op under the cap.
        seed_multi_boot(path, 1)
        with open(path, "rb") as f:
            small = f.read()
        assert len(small) <= log_hooks._FAULT_LOG_MAX_BYTES, "fixture: seed too big"
        log_hooks._bound_fault_log(path)
        with open(path, "rb") as f:
            assert f.read() == small, "a file under the cap must not be rewritten"

        # A file over the cap is trimmed to the most recent content.
        seed_multi_boot(path, 8)
        before = os.path.getsize(path)
        assert before > log_hooks._FAULT_LOG_MAX_BYTES, "fixture: seed must exceed the cap"
        ino_before = os.stat(path).st_ino

        # A running instance's registered faulthandler fd: raw, append, kept
        # open across the trim.
        live_fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        try:
            log_hooks._bound_fault_log(path)

            after = os.path.getsize(path)
            assert after <= log_hooks._FAULT_LOG_MAX_BYTES, (
                f"trim left the file over the cap: {after} > "
                f"{log_hooks._FAULT_LOG_MAX_BYTES}"
            )
            assert after < before, "trim did not shrink an over-cap file"
            assert os.stat(path).st_ino == ino_before, (
                "trim replaced the inode -- a running instance's registered "
                "faulthandler fd is stranded on the unlinked old file"
            )

            with open(path) as f:
                content = f.read()
            assert "session8payload" in content, "most recent session was dropped"
            assert "session1payload" not in content, "oldest session survived the trim"
            assert content.lstrip().startswith(TRIM_NOTICE), "no trim notice at the head"
            # Keep content from a complete boot marker instead of a partial dump.
            body = content.split("\n", 1)[1]  # drop the notice line
            assert body.startswith(BOOT_MARKER), (
                "kept content must start at a whole boot marker, not mid-dump"
            )

            # A write through the old fd must remain visible at the original path.
            os.write(live_fd, b"LIVE_DUMP_AFTER_TRIM\n")
            with open(path) as f:
                assert "LIVE_DUMP_AFTER_TRIM" in f.read(), (
                    "a dump through the pre-trim fd is invisible -- fd stranded"
                )

            # Idempotence: once under the cap the next boot's trim is a no-op.
            with open(path, "rb") as f:
                snapshot = f.read()
            assert len(snapshot) <= log_hooks._FAULT_LOG_MAX_BYTES
            log_hooks._bound_fault_log(path)
            with open(path, "rb") as f:
                assert f.read() == snapshot, "a second trim under the cap rewrote the file"
        finally:
            os.close(live_fd)

        # A read-only file must not crash the trim or block boot.
        seed_multi_boot(path, 8)
        os.chmod(path, 0o444)
        try:
            log_hooks._bound_fault_log(path)  # logs and returns, never raises
        finally:
            os.chmod(path, 0o644)

        # An absent file is a silent no-op and is not created.
        missing = os.path.join(d, "absent.log")
        log_hooks._bound_fault_log(missing)
        assert not os.path.exists(missing), "trimming an absent file must not create it"


def check_bound_integration() -> None:
    """Check that startup bounds and redacts retained fault-log content."""
    log_hooks._FAULT_LOG_MAX_BYTES = 600
    log_dir = os.path.join(fixtures.DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "faulthandler.log")

    seed_multi_boot(path, 12)
    seeded = os.path.getsize(path)
    assert seeded > log_hooks._FAULT_LOG_MAX_BYTES

    log_hooks._fault_file = None  # a fresh process has no attached file yet
    log_hooks.redirect_faulthandler(log_dir)

    with open(path) as f:
        content = f.read()

    # The retained tail is smaller and redacted before the new marker is appended.
    assert len(content) < seeded, "redirect_faulthandler did not bound the file"
    assert HOME not in content, "kept content was not scrubbed after the trim"
    assert 'File "~/' in content, "frame paths must survive as ~-relative"
    assert TRIM_NOTICE in content, "the trim notice must be present"
    # This boot's marker is appended after the bounded, scrubbed content.
    assert content.rstrip("\n").endswith("====="), "boot marker must end the file"
    assert faulthandler.is_enabled(), "faulthandler must end up enabled"
    assert log_hooks._fault_file is not None
    assert os.fstat(log_hooks._fault_file.fileno()).st_ino == os.stat(path).st_ino, (
        "faulthandler fd must point at the bounded file's inode"
    )

    # No trim/scrub temp file is left behind.
    leftovers = [n for n in os.listdir(log_dir) if n.startswith("faulthandler.log.")]
    assert not leftovers, f"temp files left behind: {leftovers}"


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_faulthandler_bound")
    check_bound_unit()
    check_bound_integration()
    print("PASS: scenario_faulthandler_bound")


if __name__ == "__main__":
    main()
