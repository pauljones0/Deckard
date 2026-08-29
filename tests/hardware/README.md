# Hardware verification harness

Runs the real engine against the physical Stream Deck. The software suite
(`tests/run_all.py`) covers everything a fake deck can show; this harness
covers what only real transport can: USB pacing, encode cost on device
writes, reader recovery, and quit timing.

## Layout

- `orchestrator.py` — runs the gated suite unattended. It takes the deck
  from the running system instance over D-Bus (refusing, never forcing),
  runs every `gated/*.py` in its own process group with a timeout, relaunches
  the system instance, and exits nonzero on any failure.
- `hw_verify.py` — the shared library: instance detection and quit, scratch
  data-dir construction, capture parsing, report evaluators. Also a CLI for
  its own capture scenarios; `--selftest` runs its evaluators with no
  hardware.
- `gated/` — unattended-safe scripts. Tracked. See the contract below.
- `manual/` — interactive and visual tools: scripts that need a key press,
  a visual judgment, or a physical replug. Run by hand.
- `manual/archive/` — historical one-off verifiers from past MRs. Untracked
  on purpose: they carry machine-specific paths and verified branches that
  merged long ago. Kept locally for reference.

## Running

```
tests/hardware/orchestrator.py --dry-run     # what would run, and the claim
tests/hardware/orchestrator.py               # the full gated suite
tests/hardware/orchestrator.py --only smoke  # a subset
tests/hardware/orchestrator.py --no-deck     # only the no-deck-ok scripts
```

The orchestrator refuses to start when the running instance does not answer
the D-Bus quit within 45 s, and it refuses with no deck on USB. It relaunches
`/usr/bin/deckard -b` afterwards whatever the results were. Taking the deck
interrupts whoever is using it: coordinate before running, and never run two
orchestrators at once.

Some capture scenarios need a worst-case test video; point `DECKARD_HW_VIDEO`
at one, or pass `--video` to `hw_verify.py` directly.

## The gated contract

A script belongs in `gated/` only when all of this holds:

- Non-interactive. No `input()`, no visual judgment. The exit code is the
  whole verdict.
- Self-contained data. It never reads or writes the real data directory;
  it builds a scratch copy (`hw_verify.make_scratch_data`) or a temp dir.
- Bounded. It finishes inside the orchestrator's per-script timeout, and it
  cleans up its own engine processes on the normal path (the orchestrator's
  group kill is the backstop, not the plan).
- One deck cycle. It must not reset or power-cycle USB: stacked resets can
  wedge the deck until a physical replug. Replug stress stays in `manual/`.
- No personal paths. The repository mirrors publicly; machine-specific
  locations come from the environment.

A script that also works with no deck attached declares it with
`# hw: no-deck-ok` in its first 10 lines. `--no-deck` runs only those and
skips the claim choreography, so it is safe anywhere, including CI.
