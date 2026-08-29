#!/usr/bin/env python3
"""Automated hardware-verification driver for the fps-campaign MRs (!71/!72/!74).

Runs the real app against the real Stream Deck from a per-branch git worktree
and an ISOLATED scratch copy of the data dir (background swapped to a test
video, screensaver disabled), captures the env-gated media profiler output,
and evaluates the quantitative gates from the hardware-verification plans
posted on the MRs. The physical/subjective half (dial feel, touchscreen,
unplug/replug, suspend, visual smoothness) cannot be automated and is emitted
as a manual checklist in every report.

Scenarios:
  mr71         fair transport lock: install-line gate, violations, fps capture
  mr72         native tile cache: loop-2 encode-free criteria, RSS bound
  mr72-killswitch  fallback proof: encodes must PERSIST with the cache off
  mr74         A/B usb_write-share merge gate (runs two captures)
  soak         long capture with wedge/RSS-trend detection on any branch

Examples:
  hw_verify.py mr71
  hw_verify.py mr72 --duration 240 --video /path/to/worst-case.mp4
  hw_verify.py mr74 --duration 180
  hw_verify.py soak --branch perf/presenter-write-overlap --duration 7200
  hw_verify.py mr71 --post 71        # append the report as a note on MR !71
  hw_verify.py --selftest            # parser/evaluator self-test, no hardware

The app instance is started with `-b --devel --data <scratch>`; the real
config is never read or written. Preflight refuses to run if another app
instance is alive (detected via its D-Bus name — argv is masked, pgrep is
blind) or the deck is absent. With --take-deck, a running instance is
cleanly quit over D-Bus first and ALWAYS relaunched (/usr/bin/deckard -b)
after the captures, even on failure.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from statistics import median

APP_ID = "io.github.nazbert.Deckard"
DBUS_PATH = "/io/github/nazbert/Deckard"
# The packaged binary is lowercase since the Deckard rebrand landed in the
# AUR recipe (/usr/bin/deckard); the old capitalised path no longer exists,
# so the relaunch used to fail silently and leave the deck dark.
SYSTEM_LAUNCHER = ["/usr/bin/deckard", "-b"]

# The repository this file sits in, so a checkout anywhere works. A worktree
# links the primary .venv, so VENV_PY resolves in both.
REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
VENV_PY = os.path.join(REPO, ".venv", "bin", "python")
WORK_ROOT = os.path.expanduser("~/.cache/deckard-hwverify")
REAL_DATA = os.path.expanduser("~/.local/share/deckard/data")
# A worst-case test video for the capture scenarios. Machine-specific, so it
# comes from the environment; scenarios that need one refuse without it.
DEFAULT_VIDEO = os.environ.get("DECKARD_HW_VIDEO", "")

BRANCH = {
    "mr71": "perf/fair-transport-lock",
    "mr72": "perf/native-tile-cache",
    "mr72-killswitch": "perf/native-tile-cache",
    "mr74a": "perf/fair-transport-lock",
    "mr74b": "perf/presenter-write-overlap",
}

INSTALL_RE = re.compile(r"Installed the fair \(FIFO\) transport lock")
SKIP_RE = re.compile(r"skipping the fair transport lock")
VIOLATION_RE = re.compile(r"Device owner violation")
UNCAUGHT_RE = re.compile(r"Uncaught exception \[")
TRANSPORT_ERR_RE = re.compile(r"TransportError")
WINDOW_RE = re.compile(r"\[media-prof\] ([\d.]+)s window: (.*)")
SECTION_RE = re.compile(r"^(\w+) n=(\d+) tot=(\d+)ms p50=([\d.]+)ms$")
COUNTER_RE = re.compile(r"^(\w+)=(\d+)$")

MANUAL_CHECKLIST = """
## Manual residue (cannot be automated — fingers and eyes required)

- [ ] Dial hammer during video: crisp per-detent, no coalesced jumps (all 4 dials + HA hold)
- [ ] Touchscreen taps/swipes during video; scroll-label page jitter check
- [ ] Visual smoothness verdict on the deck itself
- [ ] USB unplug -> replug mid-video: teardown + re-register + resume
- [ ] Suspend -> resume mid-video: painting resumes, dials responsive
"""


# ------------------------------------------------------------------ parsing

def parse_log(text: str) -> dict:
    """Extract profiler windows and grep-gates from a captured app log."""
    windows = []
    for m in WINDOW_RE.finditer(text):
        w = {"elapsed": float(m.group(1)), "sections": {}, "counters": {}}
        for part in (p.strip() for p in m.group(2).split(" | ")):
            if part.startswith("loop_fps="):
                w["loop_fps"] = float(part.split("=", 1)[1])
                continue
            s = SECTION_RE.match(part)
            if s:
                w["sections"][s.group(1)] = {
                    "n": int(s.group(2)),
                    "tot_ms": int(s.group(3)),
                    "p50_ms": float(s.group(4)),
                }
                continue
            c = COUNTER_RE.match(part)
            if c:
                w["counters"][c.group(1)] = int(c.group(2))
        windows.append(w)
    return {
        "windows": windows,
        "install_line": bool(INSTALL_RE.search(text)),
        "install_skipped": bool(SKIP_RE.search(text)),
        "violations": len(VIOLATION_RE.findall(text)),
        "uncaught": len(UNCAUGHT_RE.findall(text)),
        "transport_errors": len(TRANSPORT_ERR_RE.findall(text)),
    }


def steady(windows: list) -> list:
    """The tail half of the capture (minimum 3 windows) = steady state."""
    if len(windows) < 3:
        return windows
    return windows[max(len(windows) // 2, len(windows) - max(3, len(windows) // 2)):]


def w_med(windows, getter, default=0.0):
    vals = [getter(w) for w in windows if getter(w) is not None]
    return median(vals) if vals else default


def sec(w, name, field):
    s = w["sections"].get(name)
    return s[field] if s else None


# ------------------------------------------------------------- orchestration

def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def dbus_owner_pid():
    """PID owning the app's session-bus name, or None. This is the same check
    the app's own single-instance gate uses; the process argv is masked to
    just "Deckard", so pgrep alone is blind to an installed instance."""
    r = sh(["busctl", "--user", "--no-legend", "list"])
    for line in r.stdout.splitlines():
        cols = line.split()
        if cols and cols[0] == APP_ID and len(cols) > 1 and cols[1].isdigit():
            return int(cols[1])
    return None


def dbus_quit():
    """Activate the app's own 'quit' action — the same clean path the tray
    uses (blank deck, bounded joins, close). Never boots a replacement,
    unlike `main.py --close-running`."""
    return sh(["gdbus", "call", "--session", "--dest", APP_ID,
               "--object-path", DBUS_PATH, "--method",
               "org.gtk.Actions.Activate", "quit", "[]", "{}"], timeout=30)


def wait_gone(pid, timeout=30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not os.path.exists(f"/proc/{pid}") and dbus_owner_pid() is None:
            return True
        time.sleep(0.5)
    return False


def preflight(video, take_deck=False):
    """Returns True if a pre-existing instance was stopped (caller must
    relaunch it afterwards)."""
    stopped = False
    pid = dbus_owner_pid()
    if pid is not None:
        if not take_deck:
            die(f"the deck app is running (pid {pid}, owns {APP_ID}); it holds the "
                "device exclusively.\nRe-run with --take-deck to quit it cleanly "
                "for the capture and relaunch it afterwards")
        print(f"  taking the deck: quitting running instance pid {pid} via D-Bus")
        dbus_quit()
        if not wait_gone(pid, 45):
            die(f"running instance pid {pid} did not exit within 45s — not forcing it")
        stopped = True
    usb = sh(["lsusb"])
    if "0fd9" not in usb.stdout:
        die("no Elgato device on USB — plug the deck in")
    if not os.path.isfile(video):
        die(f"test video not found: {video!r} — pass --video or set DECKARD_HW_VIDEO")
    if not os.path.isfile(VENV_PY):
        die(f"venv interpreter missing: {VENV_PY}")
    return stopped


def die(msg):
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(2)


def make_worktree(branch: str) -> str:
    """Fresh worktree for the branch under WORK_ROOT (recreated every run so
    it can never be stale — the recurring stale-base trap)."""
    slug = branch.replace("/", "-")
    path = os.path.join(WORK_ROOT, "wt", slug)
    if os.path.exists(path):
        sh(["git", "-C", REPO, "worktree", "remove", "--force", path])
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    r = sh(["git", "-C", REPO, "worktree", "add", "--detach", path, branch])
    if r.returncode != 0:
        die(f"worktree add failed for {branch}:\n{r.stderr}")
    tip = sh(["git", "-C", path, "rev-parse", "--short", "HEAD"]).stdout.strip()
    print(f"  worktree {path} @ {tip} ({branch})")
    return path


def find_serial() -> str:
    decks = os.path.join(REAL_DATA, "settings", "decks")
    serials = [f[:-5] for f in os.listdir(decks) if f.endswith(".json")]
    if len(serials) != 1:
        die(f"expected exactly one deck settings file, found {serials} — pass --serial")
    return serials[0]


def repoint_default_pages(scratch: str) -> dict:
    """Rewrite `settings/pages.json` default-page paths to the scratch copies.

    THIS IS WHAT MAKES A FULL-COPY SCRATCH ACTUALLY ISOLATED. `pages.json`
    stores ABSOLUTE page paths, and on this machine they read
    `~/.var/app/io.github.nazbert.Deckard/data/pages/...`, which is a SYMLINK
    to the live `~/.local/share/deckard/data`. Copying the tree without
    rewriting them leaves every scratch run pointed at the user's real page
    file — which the app then loads AND SAVES. Confirmed 2026-08-09: same
    inode on both paths.

    The rsync has already copied `pages/` into the scratch, so this normally
    just re-points at a file that is already there; it copies only if the
    target is missing. Idempotent, and a path already inside `scratch` is
    left alone. Returns {serial: new_path} for the entries it moved."""
    pages_file = os.path.join(scratch, "settings", "pages.json")
    if not os.path.isfile(pages_file):
        return {}
    with open(pages_file) as f:
        cfg = json.load(f)
    scratch_prefix = os.path.abspath(scratch) + os.sep
    moved = {}
    for serial, path in list(cfg.get("default-pages", {}).items()):
        if not path or os.path.abspath(path).startswith(scratch_prefix):
            continue
        local = os.path.join(scratch, "pages", os.path.basename(path))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        if not os.path.isfile(local) and os.path.isfile(path):
            shutil.copy2(path, local)
        cfg["default-pages"][serial] = local
        moved[serial] = local
    if moved:
        with open(pages_file, "w") as f:
            json.dump(cfg, f, indent=4)
    return moved


def make_scratch_data(video: str, serial: str, scenario: str,
                      blank_page: bool = False) -> str:
    """Isolated data dir: real config copied (minus logs/cache), background
    forced to the test video, screensaver disabled. With blank_page, the deck
    opens on an empty page so every key is on the tile_passthrough path —
    required to exercise the native tile cache at all (a fully-populated real
    page has zero passthrough keys and the identity path never engages).

    !!! FULL-COPY MODE IS ONLY ISOLATED BECAUSE OF repoint_default_pages() !!!
    A copied `settings/pages.json` carries ABSOLUTE page paths into the live
    data dir (via the ~/.var/app/... symlink), so without that rewrite every
    non-blank_page scenario reads and WRITES the user's real page file. That
    was live in this harness from its first commit until 2026-08-09 —
    blank_page mode happened to overwrite the entry and hid it. The rewrite
    is done HERE, for every caller, precisely so no scenario has to remember
    it; do not move it into the callers."""
    scratch = os.path.join(WORK_ROOT, "data", scenario)
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch, exist_ok=True)
    r = sh(["rsync", "-a", "--exclude", "logs/", "--exclude", "cache/",
            REAL_DATA + "/", scratch + "/"])
    if r.returncode != 0:
        die(f"rsync failed: {r.stderr}")
    deck_file = os.path.join(scratch, "settings", "decks", f"{serial}.json")
    with open(deck_file) as f:
        cfg = json.load(f)
    cfg["background"] = {"media-path": video, "enable": True, "loop": True,
                         "fps": 30, "extend-to-touchscreen": True}
    cfg.setdefault("screensaver", {})["enable"] = False
    with open(deck_file, "w") as f:
        json.dump(cfg, f, indent=4)
    repoint_default_pages(scratch)
    if blank_page:
        page_path = os.path.join(scratch, "pages", "HWVerify.json")
        with open(page_path, "w") as f:
            json.dump({"keys": {}}, f)
        pages_file = os.path.join(scratch, "settings", "pages.json")
        with open(pages_file) as f:
            pages_cfg = json.load(f)
        pages_cfg.setdefault("default-pages", {})[serial] = page_path
        with open(pages_file, "w") as f:
            json.dump(pages_cfg, f, indent=4)
    return scratch


def run_capture(worktree, scratch, env_extra, duration, tag):
    """Launch app, wait for boot, sample RSS for `duration`, clean shutdown.
    Returns (log_text, rss_samples, clean_close: bool)."""
    logdir = os.path.join(WORK_ROOT, "results", tag)
    os.makedirs(logdir, exist_ok=True)
    logpath = os.path.join(logdir, "run.log")
    env = dict(os.environ, DECKARD_MEDIA_PROFILE="1", **env_extra)
    with open(logpath, "w") as logf:
        proc = subprocess.Popen(
            [VENV_PY, "main.py", "-b", "--devel", "--data", scratch],
            cwd=worktree, env=env, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True)
        print(f"  app pid {proc.pid}, capturing {duration}s -> {logpath}")
        try:
            deadline = time.time() + 90
            booted = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    with open(logpath) as f:
                        if "Already running" in f.read():
                            die("bounced by the single-instance gate — another "
                                "instance owns the D-Bus name (use --take-deck)")
                    die(f"app exited during boot (rc={proc.returncode}) — see {logpath}")
                with open(logpath) as f:
                    if "[media-prof]" in f.read():
                        booted = True
                        break
                time.sleep(2)
            if not booted:
                die(f"no [media-prof] output within 90s — see {logpath}")
            rss = []
            t_end = time.time() + duration
            while time.time() < t_end:
                if proc.poll() is not None:
                    die(f"app died mid-capture (rc={proc.returncode}) — see {logpath}")
                try:
                    with open(f"/proc/{proc.pid}/status") as f:
                        for line in f:
                            if line.startswith("VmRSS:"):
                                rss.append(int(line.split()[1]) // 1024)  # MB
                                break
                except OSError:
                    pass
                time.sleep(10)
        finally:
            clean = shutdown(proc, worktree, scratch)
    with open(logpath) as f:
        return f.read(), rss, clean


def shutdown(proc, worktree, scratch) -> bool:
    """D-Bus 'quit' action (the real tray quit path — exercises shutdown
    ordering), then SIGTERM, SIGKILL. NOT `--close-running`: that flag quits
    the running instance and then boots ITSELF as a replacement."""
    if proc.poll() is not None:
        return False
    dbus_quit()
    for _ in range(20):
        if proc.poll() is not None:
            print("  clean close via D-Bus quit action")
            return True
        time.sleep(0.5)
    print("  D-Bus quit did not stop it; SIGTERM")
    os.killpg(proc.pid, signal.SIGTERM)
    for _ in range(20):
        if proc.poll() is not None:
            return False
        time.sleep(0.5)
    print("  SIGKILL")
    os.killpg(proc.pid, signal.SIGKILL)
    return False


# ------------------------------------------------------------- evaluation

def check(ok, label, detail=""):
    mark = "PASS" if ok else "FAIL"
    return ok, f"- **{mark}** {label}" + (f" — {detail}" if detail else "")


def eval_mr71(parsed, rss, clean):
    st = steady(parsed["windows"])
    fps = w_med(st, lambda w: w.get("loop_fps"))
    usb_tot = w_med(st, lambda w: sec(w, "usb_write", "tot_ms"))
    rows = [
        check(parsed["install_line"], "fair-lock install line present (hard gate)"),
        check(not parsed["install_skipped"], "no drift-guard skip warning"),
        check(parsed["violations"] == 0, "owner violations == 0", str(parsed["violations"])),
        check(fps >= 24, "steady loop_fps >= 24 (expect 26-30 pre-!72)", f"median {fps:.1f}"),
        check(clean, "clean shutdown via D-Bus quit"),
        check(parsed["uncaught"] == 0, "no uncaught exceptions", str(parsed["uncaught"])),
    ]
    stats = (f"steady windows: {len(st)} | loop_fps median {fps:.1f} | "
             f"usb_write tot/window {usb_tot:.0f}ms | RSS {rss[0] if rss else '?'}"
             f"->{rss[-1] if rss else '?'}MB | transport_errors {parsed['transport_errors']}")
    return rows, stats


def eval_mr72(parsed, rss, clean, killswitch=False):
    st = steady(parsed["windows"])
    fps = w_med(st, lambda w: w.get("loop_fps"))
    enc = w_med(st, lambda w: sec(w, "encode", "tot_ms") or 0)
    hits = sum(w["counters"].get("native_id_hit", 0) for w in st)
    misses = sum(w["counters"].get("native_id_miss", 0) for w in st)
    rss_delta = (rss[-1] - rss[0]) if len(rss) >= 2 else 0
    tail = rss[len(rss) * 2 // 3:] if len(rss) >= 6 else rss
    tail_growth = (tail[-1] - tail[0]) if len(tail) >= 2 else 0
    if killswitch:
        rows = [
            check(enc > 20, "encodes PERSIST with cache disabled (fallback proof)",
                  f"steady encode {enc:.0f}ms/window"),
            check(hits == 0, "no native_id_hit with cache off", f"{hits}"),
            check(clean, "clean shutdown"),
        ]
    else:
        rows = [
            check(enc <= 5, "steady encode ~0 (loop-2 claim)", f"{enc:.0f}ms/window"),
            check(hits > 0 and misses <= max(1, hits // 20),
                  "native_id_hit dominates steady state", f"hit {hits} / miss {misses}"),
            check(fps >= 29, "steady loop_fps >= 29", f"median {fps:.1f}"),
            check(rss_delta <= 120, "total RSS delta bounded (<=120MB)", f"{rss_delta}MB"),
            check(tail_growth <= 10, "RSS flat in tail (no loop-over-loop growth)",
                  f"{tail_growth}MB over tail"),
            check(parsed["uncaught"] == 0, "no uncaught exceptions", str(parsed["uncaught"])),
            check(clean, "clean shutdown via D-Bus quit"),
        ]
    stats = (f"steady windows: {len(st)} | loop_fps median {fps:.1f} | "
             f"encode {enc:.0f}ms/w | native_id {hits}/{misses} hit/miss | "
             f"RSS {rss[0] if rss else '?'}->{rss[-1] if rss else '?'}MB")
    return rows, stats


def eval_mr74(pa, pb, rss_b, clean_b):
    sta, stb = steady(pa["windows"]), steady(pb["windows"])
    def share(w):
        u, t = sec(w, "usb_write", "tot_ms"), sec(w, "tick", "tot_ms")
        return (u / t) if (u is not None and t) else None
    share_a = w_med(sta, share)
    tick_a = w_med(sta, lambda w: sec(w, "tick", "p50_ms"))
    tick_b = w_med(stb, lambda w: sec(w, "tick", "p50_ms"))
    fps_a = w_med(sta, lambda w: w.get("loop_fps"))
    fps_b = w_med(stb, lambda w: w.get("loop_fps"))
    drop = (1 - tick_b / tick_a) if tick_a else 0
    gate = share_a >= 0.25
    rows = [
        (gate, f"- **{'MET' if gate else 'NOT MET'}** MERGE GATE: usb_write share on "
               f"the in-tick topology >= 25% — measured {share_a:.0%}"
               + ("" if gate else "  ->  per the plan on #165 this argues WON'T-DO; "
                  "record on the issue rather than merging")),
        check(tick_b < tick_a, "render tick p50 dropped on presenter branch",
              f"{tick_a:.2f}ms -> {tick_b:.2f}ms ({drop:.0%}; A's share predicts ~{share_a:.0%})"),
        check(pa["violations"] == 0 and pb["violations"] == 0,
              "owner violations == 0 on both runs",
              f"A={pa['violations']} B={pb['violations']}"),
        check(pb["uncaught"] == 0, "no uncaught exceptions on B", str(pb["uncaught"])),
        check(clean_b, "clean shutdown via D-Bus quit on B"),
    ]
    stats = (f"A(!71-branch): loop_fps {fps_a:.1f}, tick p50 {tick_a:.2f}ms, usb share {share_a:.0%} | "
             f"B(!74-branch): loop_fps {fps_b:.1f}, tick p50 {tick_b:.2f}ms | "
             f"RSS B {rss_b[0] if rss_b else '?'}->{rss_b[-1] if rss_b else '?'}MB | "
             f"NOTE: !72 (native cache) NOT in either build")
    return rows, stats


def eval_soak(parsed, rss, clean, duration):
    wins = parsed["windows"]
    st = steady(wins)
    fps_first = w_med(wins[:max(3, len(wins) // 10)], lambda w: w.get("loop_fps"))
    fps_last = w_med(st, lambda w: w.get("loop_fps"))
    # a healthy capture emits a window every ~5s of ACTIVE loop; long gaps mean
    # idle (fine) or a wedge (not fine) — flag only a total absence at the tail
    rss_delta = (rss[-1] - rss[0]) if len(rss) >= 2 else 0
    rate_mb_h = rss_delta / (duration / 3600) if duration else 0
    rows = [
        check(len(wins) > 0, "profiler windows present through the soak", f"{len(wins)}"),
        check(fps_last >= fps_first * 0.9, "no fps degradation over soak",
              f"{fps_first:.1f} -> {fps_last:.1f}"),
        check(rate_mb_h <= 20, "RSS growth <= 20MB/h", f"{rate_mb_h:.1f}MB/h"),
        check(parsed["uncaught"] == 0, "no uncaught exceptions", str(parsed["uncaught"])),
        check(parsed["transport_errors"] == 0, "no transport errors",
              str(parsed["transport_errors"])),
        check(clean, "deck still alive at end (clean close worked)"),
    ]
    stats = f"windows {len(wins)} | RSS {rss[0] if rss else '?'}->{rss[-1] if rss else '?'}MB over {duration}s"
    return rows, stats


# ------------------------------------------------------------------ report

def report(scenario, rows, stats, video, extra=""):
    ok = all(r[0] for r in rows)
    lines = [f"# Automated hardware verification — {scenario}",
             "",
             f"Overall: **{'ALL AUTOMATED CHECKS PASS' if ok else 'FAILURES PRESENT'}**",
             "",
             f"Video: `{video}`  (worst-case high-entropy clip? verify — the sample "
             "clip understates encode load)", "",
             *(r[1] for r in rows), "",
             f"`{stats}`", extra, MANUAL_CHECKLIST]
    return "\n".join(lines), ok


def post_note(mr_iid, body):
    r = sh(["glab", "mr", "note", str(mr_iid), "-m", body])
    print(r.stdout.strip() or r.stderr.strip())


# ------------------------------------------------------------------ selftest

SYNTH = """
boot noise
INFO Installed the fair (FIFO) transport lock on Stream Deck +
[media-prof] 5.0s window: loop_fps=20.1 | composite n=100 tot=300ms p50=3.00ms | encode n=90 tot=400ms p50=4.20ms | tick n=100 tot=1500ms p50=15.00ms | usb_write n=800 tot=600ms p50=0.70ms | native_id_hit=5 | native_id_miss=200
[media-prof] 5.0s window: loop_fps=31.2 | tick n=156 tot=800ms p50=5.10ms | usb_write n=1200 tot=300ms p50=0.25ms | encode n=2 tot=3ms p50=1.50ms | native_id_hit=1200 | native_id_miss=3
[media-prof] 5.0s window: loop_fps=31.0 | tick n=155 tot=790ms p50=5.05ms | usb_write n=1190 tot=295ms p50=0.24ms | encode n=1 tot=2ms p50=1.90ms | native_id_hit=1190 | native_id_miss=2
[media-prof] 5.0s window: loop_fps=30.8 | tick n=154 tot=795ms p50=5.08ms | usb_write n=1180 tot=290ms p50=0.24ms | encode n=1 tot=2ms p50=1.80ms | native_id_hit=1180 | native_id_miss=1
"""


def _selftest_repoint():
    """Reproduces the exact 2026-08-09 hazard: pages.json holding an ABSOLUTE
    path into a SYMLINKED view of the live data dir, and proves a scratch
    write can no longer reach the live page."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        live = os.path.join(tmp, "live")
        os.makedirs(os.path.join(live, "pages"))
        real_page = os.path.join(live, "pages", "Real.json")
        with open(real_page, "w") as f:
            json.dump({"keys": {"0x0": "REAL"}}, f)
        # the ~/.var/app/... style symlinked view the stored path really uses
        linked = os.path.join(tmp, "linked")
        os.symlink(live, linked)
        stored = os.path.join(linked, "pages", "Real.json")

        scratch = os.path.join(tmp, "scratch")
        os.makedirs(os.path.join(scratch, "pages"))
        os.makedirs(os.path.join(scratch, "settings"))
        shutil.copy2(real_page, os.path.join(scratch, "pages", "Real.json"))
        pages_file = os.path.join(scratch, "settings", "pages.json")
        with open(pages_file, "w") as f:
            json.dump({"default-pages": {"SERIAL1": stored}}, f)

        moved = repoint_default_pages(scratch)
        assert moved == {"SERIAL1": os.path.join(scratch, "pages", "Real.json")}, moved
        with open(pages_file) as f:
            got = json.load(f)["default-pages"]["SERIAL1"]
        assert got.startswith(scratch + os.sep), got

        # what the app would do next: write its page. It must land in the
        # scratch, never in the live tree.
        with open(got, "w") as f:
            json.dump({"keys": {"0x0": "SCRATCH-WRITE"}}, f)
        with open(real_page) as f:
            assert json.load(f)["keys"]["0x0"] == "REAL", "live page was clobbered"

        assert repoint_default_pages(scratch) == {}, "not idempotent"
        assert repoint_default_pages(os.path.join(tmp, "nope")) == {}
    print("  repoint_default_pages: live page protected, idempotent")


def selftest():
    _selftest_repoint()
    p = parse_log(SYNTH)
    assert len(p["windows"]) == 4, p
    assert p["install_line"] and not p["install_skipped"]
    w0 = p["windows"][0]
    assert w0["loop_fps"] == 20.1 and w0["sections"]["usb_write"]["n"] == 800
    assert w0["counters"]["native_id_miss"] == 200
    st = steady(p["windows"])
    assert all(w["loop_fps"] > 30 for w in st), st
    rows, stats = eval_mr72(p, [500, 520, 540, 541, 542, 542], clean=True)
    assert all(r[0] for r in rows), "\n".join(r[1] for r in rows)
    rows71, _ = eval_mr71(p, [500, 540], clean=True)
    assert all(r[0] for r in rows71), "\n".join(r[1] for r in rows71)
    # mr74: A = high usb share in tick, B = tick collapsed
    pa = parse_log("""
[media-prof] 5.0s window: loop_fps=28.0 | tick n=140 tot=4000ms p50=28.00ms | usb_write n=1100 tot=1600ms p50=1.40ms
[media-prof] 5.0s window: loop_fps=28.2 | tick n=141 tot=4010ms p50=28.10ms | usb_write n=1110 tot=1620ms p50=1.41ms
[media-prof] 5.0s window: loop_fps=28.1 | tick n=140 tot=4005ms p50=28.05ms | usb_write n=1105 tot=1610ms p50=1.40ms
""")
    pb = parse_log("""
[media-prof] 5.0s window: loop_fps=33.0 | tick n=165 tot=1700ms p50=10.00ms | present n=160 tot=900ms p50=5.00ms | usb_write n=1300 tot=1600ms p50=1.20ms
[media-prof] 5.0s window: loop_fps=33.1 | tick n=165 tot=1690ms p50=10.10ms | present n=161 tot=905ms p50=5.10ms | usb_write n=1310 tot=1610ms p50=1.21ms
[media-prof] 5.0s window: loop_fps=33.2 | tick n=166 tot=1695ms p50=10.05ms | present n=162 tot=903ms p50=5.05ms | usb_write n=1305 tot=1605ms p50=1.20ms
""")
    rows74, stats74 = eval_mr74(pa, pb, [600, 610], clean_b=True)
    assert all(r[0] for r in rows74), "\n".join(r[1] for r in rows74)
    assert "40%" in stats74 or "usb share" in stats74
    print("selftest OK")


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", nargs="?",
                    choices=["mr71", "mr72", "mr72-killswitch", "mr74", "soak"])
    ap.add_argument("--duration", type=int, default=180,
                    help="capture seconds per run (default 180; soak: set hours' worth)")
    ap.add_argument("--video", default=DEFAULT_VIDEO)
    ap.add_argument("--serial", default=None)
    ap.add_argument("--branch", default=None, help="soak only: branch to soak")
    ap.add_argument("--post", type=int, metavar="MR_IID",
                    help="append the report as a note on this MR")
    ap.add_argument("--take-deck", action="store_true",
                    help="cleanly quit a running app instance for the capture "
                         "and relaunch it (/usr/bin/deckard -b) afterwards")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if not args.scenario:
        ap.error("scenario required (or --selftest)")

    stopped = preflight(args.video, take_deck=args.take_deck)
    serial = args.serial or find_serial()
    ts = time.strftime("%Y%m%d-%H%M%S")

    def one(tag, branch, env_extra, blank_page=False):
        wt = make_worktree(branch)
        scratch = make_scratch_data(args.video, serial, tag, blank_page=blank_page)
        return run_capture(wt, scratch, env_extra, args.duration, f"{ts}-{tag}")

    try:
        if args.scenario == "mr71":
            text, rss, clean = one("mr71", BRANCH["mr71"], {})
            rows, stats = eval_mr71(parse_log(text), rss, clean)
        elif args.scenario == "mr72":
            text, rss, clean = one("mr72", BRANCH["mr72"], {"DECKARD_VIDEO_WRITE_HZ": "30"},
                                   blank_page=True)
            rows, stats = eval_mr72(parse_log(text), rss, clean)
        elif args.scenario == "mr72-killswitch":
            text, rss, clean = one("mr72ks", BRANCH["mr72-killswitch"],
                                   {"DECKARD_VIDEO_WRITE_HZ": "30",
                                    "DECKARD_NATIVE_TILE_CACHE_MB": "0"},
                                   blank_page=True)
            rows, stats = eval_mr72(parse_log(text), rss, clean, killswitch=True)
        elif args.scenario == "mr74":
            text_a, _rss_a, _clean_a = one("mr74a", BRANCH["mr74a"],
                                           {"DECKARD_ASSERT_DEVICE_OWNER": "1"})
            text_b, rss_b, clean_b = one("mr74b", BRANCH["mr74b"],
                                         {"DECKARD_ASSERT_DEVICE_OWNER": "1"})
            rows, stats = eval_mr74(parse_log(text_a), parse_log(text_b), rss_b, clean_b)
        elif args.scenario == "soak":
            branch = args.branch or BRANCH["mr74b"]
            text, rss, clean = one("soak", branch, {})
            rows, stats = eval_soak(parse_log(text), rss, clean, args.duration)
    finally:
        if stopped:
            print("  relaunching the system instance:", " ".join(SYSTEM_LAUNCHER))
            subprocess.Popen(SYSTEM_LAUNCHER, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    body, ok = report(args.scenario, rows, stats, args.video)
    out = os.path.join(WORK_ROOT, "results", f"{ts}-{args.scenario}-report.md")
    with open(out, "w") as f:
        f.write(body)
    print("\n" + body)
    print(f"\nreport: {out}")
    if args.post:
        post_note(args.post, body)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
