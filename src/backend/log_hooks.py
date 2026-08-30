"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

Central exception hooks.

A function decorated with @log.catch feeds its exceptions into loguru. An
uncaught exception on any other path reaches stderr alone, and a detached run
under autostart or flatpak loses it. install_exception_hooks() closes the four
surfaces that leak one, and _log_exc() rate-limits what they report.

Every hook routes through loguru, so a record reaches every sink that
config_logger() installed, which are logs/logs.log, stderr and the gl.logs
ring behind the About dialog. Loguru's default stderr sink catches a record
emitted before that call. Each hook resolves the sinks at call time, so no
re-install follows.

This module stays importable before globals, which the fixtures.py contract
of the test harness needs, so it imports stdlib and loguru only. log_redaction
is the one allowed sibling import, on the same contract, and
install_exception_hooks() installs its scrubbing patcher, so no hook routes an
unredacted traceback into a sink.
"""
import asyncio
import atexit
import contextlib
import faulthandler
import fcntl
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import cast, Any, TextIO

from loguru import logger as _LOG

from src.backend.log_redaction import install_log_redaction, scrub

# SC_NO_ERROR_HOOKS=1 disables process-global hooks and faulthandler redirection for this run.
# Read once at import; every other value keeps the safety net enabled.
_HOOKS_DISABLED = os.environ.get("SC_NO_ERROR_HOOKS") == "1"
_announced_disabled = False

_installed = False
# The same shape as sys.excepthook, which install() stores here.
_ExceptHook = Callable[[type[BaseException], BaseException, TracebackType | None], object]
_prev_sys_hook: _ExceptHook = None  # ty: ignore[invalid-assignment]  # late-init: install(); only read from _sys_hook, which install() wires up
# Keep this file alive because faulthandler stores only its raw descriptor.
# Closing it can redirect a fatal-signal dump into a recycled descriptor.
_fault_file: TextIO | None = None

# Cap accumulated boot markers and native dumps at one million bytes.
# _bound_fault_log keeps the newest content, and callers can lower this value.
_FAULT_LOG_MAX_BYTES = 1_000_000


# Rate-limit per exception type and innermost frame; first hits log and later hits count per window.
# Flush counts on prune, terminal records, and exit; same-line failures share one budget.
RATE_LIMIT_WINDOW_S = 5.0  # Module-level, so a scenario can shrink the window
_RATE_LIMIT_MAX_KEYS = 256
# Hooks update this state from worker, GLib, GC, and asyncio threads.
# Use RLock because a raising finalizer can re-enter during allocation; release before logging.
_rate_lock = threading.RLock()
# Bound every crash-path acquire at 0.5 seconds.
# On contention, log without throttling instead of wedging the failing thread.
_RATE_LOCK_TIMEOUT_S = 0.5
# Failure site: type name plus innermost file and line, or message without traceback.
# _exc_site is the sole constructor.
SiteKey = tuple[str, tuple[str, "int | str"]]

# Map each site to [window start, suppressed count, last hit].
# Allowed records refresh the window; all hits refresh the eviction timestamp.
_rate_state: dict[SiteKey, list[Any]] = {}


def _exc_site(exc_type: type[BaseException] | None, exc_value: BaseException | None,
              exc_tb: TracebackType | None) -> tuple[SiteKey, str]:
    """Return a rate-limit key and label from type name and innermost traceback frame.
    Store no type object; without traceback, use message text to separate budgets."""
    type_name: str = getattr(exc_type, "__name__", None) or str(exc_type)
    tb = exc_tb
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    # Two shapes, as the docstring states: a file and a line number when
    # there is a traceback, and the message text when there is not.
    where: tuple[str, str | int]
    if tb is not None:
        where = (tb.tb_frame.f_code.co_filename, tb.tb_lineno)
    else:
        where = ("<no-traceback>", str(exc_value)[:120])
    return (type_name, where), _label_for_key((type_name, where))


def _label_for_key(key: SiteKey) -> str:
    """Build a printable site from the key without retaining exception objects."""
    type_name, where = key
    return f"{where[0]}:{where[1]} [{type_name}]"


def _emit_pending(key: SiteKey, count: int, reason: str) -> None:
    """Report an orphaned pending count through loguru, then stderr, without raising.
    Keep the Uncaught exception prefix used to find incident records."""
    message = (
        f"Uncaught exception [rate-limit]: {count} further failures at "
        f"{_label_for_key(key)} since the last record ({reason})"
    )
    try:
        _LOG.warning(message)
    except Exception:
        with contextlib.suppress(Exception):
            print(message, file=sys.__stderr__)


def _prune_locked(now: float) -> list[tuple[SiteKey, int]]:
    """Under _rate_lock, evict idle sites then the least recently hit half and return pending counts.
    Snapshot items for GC re-entry safety; last-hit order keeps active storms throttled."""
    dropped: list[tuple[SiteKey, int]] = []

    def evict(key: SiteKey) -> None:
        entry = _rate_state.pop(key, None)
        if entry is not None and entry[1]:
            dropped.append((key, entry[1]))

    for key, entry in list(_rate_state.items()):
        if now - entry[2] >= RATE_LIMIT_WINDOW_S:
            evict(key)
    if len(_rate_state) > _RATE_LIMIT_MAX_KEYS:
        by_last_hit = sorted(list(_rate_state.items()), key=lambda kv: kv[1][2])
        for key, _entry in by_last_hit[: len(by_last_hit) // 2]:
            evict(key)
    return dropped


def _flush_pending_counts() -> None:
    """At exit, flush pending counts without a new thread.
    Skip after a bounded lock failure because diagnostics must not block shutdown."""
    if not _rate_state:
        return
    if not _rate_lock.acquire(timeout=2.0):
        return
    try:
        pending = [(key, entry[1]) for key, entry in list(_rate_state.items()) if entry[1]]
        _rate_state.clear()
    finally:
        _rate_lock.release()
    for key, count in pending:
        _emit_pending(key, count, "process exiting")


atexit.register(_flush_pending_counts)


def _rate_limit_bypass(key: SiteKey) -> int:
    """Clear and return pending count without throttling a terminal record.
    The final record must carry failures that have no later report."""
    if not _rate_lock.acquire(timeout=_RATE_LOCK_TIMEOUT_S):
        return 0
    try:
        entry = _rate_state.get(key)
        if entry is None:
            return 0
        pending, entry[1] = entry[1], 0
        entry[2] = time.monotonic()
        return cast(int, pending)
    finally:
        _rate_lock.release()


def _rate_limit(key: SiteKey) -> tuple[bool, int]:
    """Return whether to suppress this hit and the count since the previous record.
    Always log a site's first hit; throttle only later occurrences."""
    now = time.monotonic()
    dropped: list[tuple[SiteKey, int]] = []
    if not _rate_lock.acquire(timeout=_RATE_LOCK_TIMEOUT_S):
        # Never block a crash handler on throttling state.
        # Log this occurrence if the bounded acquire fails.
        return False, 0
    try:
        entry = _rate_state.get(key)
        if entry is not None and now - entry[0] < RATE_LIMIT_WINDOW_S:
            entry[1] += 1
            entry[2] = now
            suppress, suppressed = True, 0
        else:
            suppress = False
            suppressed = entry[1] if entry is not None else 0
            _rate_state[key] = [now, 0, now]
            if len(_rate_state) > _RATE_LIMIT_MAX_KEYS:
                dropped = _prune_locked(now)
    finally:
        _rate_lock.release()
    # Report outside the lock. A report is I/O, and a sink must not stall
    # every other thread's hook.
    for dropped_key, count in dropped:
        _emit_pending(dropped_key, count, "rate-limit state pruned")
    return suppress, cast(int, suppressed)


def _announce_disabled() -> None:
    """Announce the disabled hooks once after persistent log sinks exist.
    Installing hooks happens too early for detached runs to retain this line."""
    global _announced_disabled
    if _announced_disabled:
        return
    _announced_disabled = True
    with contextlib.suppress(Exception):
        _LOG.warning(
            "SC_NO_ERROR_HOOKS=1: the crash-logging exception hooks and the "
            "faulthandler redirection are DISABLED for this run -- uncaught "
            "exceptions reach stderr only, and native crash dumps are not "
            "written to logs/faulthandler.log (log redaction is unaffected)"
        )


def _is_terminal(exc_tb: TracebackType | None) -> bool:
    """Distinguish a terminal __main__ module traceback from a PyGObject callback traceback.
    Terminal errors bypass throttling; frequent GLib and GTK callback errors do not."""
    frame = getattr(exc_tb, "tb_frame", None)
    if frame is None:
        return False
    try:
        return cast(
            bool,
            frame.f_code.co_name == "<module>"
            and frame.f_globals.get("__name__") == "__main__",
        )
    except Exception:
        return False


def _log_exc(kind: str, exc_type: type[BaseException] | None, exc_value: BaseException | None,
             exc_tb: TracebackType | None, extra: str = "",
             rate_limit: bool = True) -> None:
    # Apply one rate limiter to all four hook surfaces.
    # Guard failures fall through and can suppress no original.
    try:
        key, label = _exc_site(exc_type, exc_value, exc_tb)
        if rate_limit:
            suppress, suppressed = _rate_limit(key)
            if suppress:
                return
        else:
            suppressed = _rate_limit_bypass(key)
        if suppressed:
            # Counts cover the unbounded gap since the last record, not one window.
            extra = (
                f"{extra} ({suppressed} further failures at {label} "
                f"since the last record)"
            )
    except Exception:
        pass
    # Hooks must not raise or recurse; use stderr if loguru fails.
    # Swallow stderr failure rather than crash inside the crash handler.
    try:
        _LOG.opt(exception=(exc_type, exc_value, exc_tb)).critical(
            f"Uncaught exception [{kind}]{extra}"
        )
    except Exception:
        try:
            import traceback
            traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.__stderr__)
        except Exception:
            pass


def _sys_hook(exc_type: type[BaseException], exc_value: BaseException,
              exc_tb: TracebackType | None) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        # Keep Ctrl-C quiet. Delegate to the hook installed before this one.
        _prev_sys_hook(exc_type, exc_value, exc_tb)
        return
    # Bypass throttling only for terminal errors, which cannot flood.
    # Callback-shaped calls on this hook remain rate-limited.
    _log_exc("main", exc_type, exc_value, exc_tb,
             rate_limit=not _is_terminal(exc_tb))


def _thread_hook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    name = getattr(args.thread, "name", "?")
    _log_exc(
        "thread", args.exc_type, args.exc_value, args.exc_traceback,
        extra=f" in thread {name!r}",
    )


# sys.UnraisableHookArgs exists only in typeshed, not the runtime module.
# Keep it quoted because Python 3.13 evaluates annotations eagerly.
def _unraisable_hook(unraisable: "sys.UnraisableHookArgs") -> None:
    _log_exc(
        "unraisable", unraisable.exc_type, unraisable.exc_value,
        unraisable.exc_traceback,
        extra=f" ({unraisable.err_msg or 'in __del__/GC'})",
    )


def asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Route unread task and call_soon failures from the long-lived asyncio loop."""
    if _HOOKS_DISABLED:
        # The opt-out must cover this hook, which event_dispatch installs separately.
        # Delegate to preserve asyncio's unhooked stderr behavior.
        loop.default_exception_handler(context)
        return
    exc = context.get("exception")
    if exc is not None:
        _log_exc("asyncio", type(exc), exc, getattr(exc, "__traceback__", None))
    else:
        message = context.get("message") or "asyncio error"
        _log_exc("asyncio", RuntimeError, RuntimeError(message), None)


def install_exception_hooks() -> None:
    """Idempotently install redaction plus sys, threading, unraisable, and asyncio support before risky work.
    The opt-out skips hooks but not redaction; executor futures still need done-callbacks."""
    global _installed, _prev_sys_hook
    install_log_redaction()
    if _HOOKS_DISABLED or _installed:
        return
    _prev_sys_hook = sys.excepthook
    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    sys.unraisablehook = _unraisable_hook
    _installed = True


def _bound_fault_log(path: str) -> None:
    """At boot, keep whole recent sections under the size cap by rewriting the same inode.
    Skip nonblocking-lock contention; concurrent appends can be lost, and failures must not block startup."""
    max_bytes = _FAULT_LOG_MAX_BYTES
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= max_bytes:
            return
        with open(path, "r+b") as log_file:
            try:
                fcntl.flock(log_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Another boot bounds or scrubs this file right now. Never wait.
                return
            size = os.fstat(log_file.fileno()).st_size
            if size <= max_bytes:
                return
            notice = (
                b"===== trimmed "
                + datetime.now().isoformat().encode("ascii", "replace")
                + b": older faulthandler entries removed to bound file size =====\n"
            )
            # Read the tail with room for the notice, so the rewrite lands at or
            # under the cap and the next boot is a no-op.
            budget = max(0, max_bytes - len(notice))
            log_file.seek(size - budget)
            tail = log_file.read()
            # Start at a boot marker or line boundary so the kept dump remains whole.
            marker = tail.find(b"\n===== boot ")
            if marker != -1:
                kept = tail[marker + 1:]
            else:
                newline = tail.find(b"\n")
                kept = tail[newline + 1:] if newline != -1 else tail
            log_file.seek(0)
            log_file.write(notice + kept)
            log_file.truncate()
    except Exception as e:
        with contextlib.suppress(Exception):
            _LOG.warning(f"could not bound faulthandler.log ({e}); continuing boot")


def _scrub_fault_log(path: str) -> None:
    """At boot, stream-scrub old C-level dumps back onto the same inode; current dumps stay raw until next boot.
    Skip lock contention and read-only files; concurrent appends can be lost, and failures must not block startup."""
    if not os.path.exists(path):
        return
    tmp_path = None
    try:
        with open(path, "r+", encoding="utf-8", errors="replace") as log_file:
            try:
                fcntl.flock(log_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Another boot scrubs this file right now, to the same
                # result. Never wait.
                return
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(path), prefix="faulthandler.", suffix=".scrub"
            )
            changed = False
            with os.fdopen(fd, "w+", encoding="utf-8") as dst:
                for line in log_file:
                    scrubbed = scrub(line)
                    if scrubbed != line:
                        changed = True
                    dst.write(scrubbed)
                if changed:
                    dst.seek(0)
                    log_file.seek(0)
                    shutil.copyfileobj(dst, log_file)
                    log_file.truncate()
        os.unlink(tmp_path)
        tmp_path = None
    except Exception as e:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        with contextlib.suppress(Exception):
            _LOG.warning(f"could not scrub faulthandler.log ({e}); continuing boot")


def redirect_faulthandler(directory: str) -> None:
    """Idempotently redirect native and SIGQUIT dumps after the data directory resolves.
    The opt-out keeps stderr and touches no fault log; any setup failure must not block startup."""
    global _fault_file
    if _HOOKS_DISABLED:
        _announce_disabled()
        return
    if _fault_file is not None:
        return
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "faulthandler.log")
        # Bound old sessions first while preserving the inode for other registered fds.
        _bound_fault_log(path)
        # Scrub prior C-level dumps before opening append, without replacing the inode.
        # Dumps from this session remain raw until the next boot.
        _scrub_fault_log(path)
        f = open(path, "a", buffering=1)
        # Append a boot marker without truncating previous crash evidence.
        f.write(f"\n===== boot {datetime.now().isoformat()} pid={os.getpid()} =====\n")
        faulthandler.enable(file=f, all_threads=True)
        faulthandler.register(signal.SIGQUIT, file=f, all_threads=True, chain=False)
        _fault_file = f
    except (AttributeError, ValueError, OSError):
        pass
