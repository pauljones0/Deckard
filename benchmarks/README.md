# Whole-application CPU and RAM comparison

These scripts run the real applications, including their interfaces and device-rendering workers. Python here is **measurement/test tooling only** and is never bundled into native Deckard.

[Recorded results, summary and frame validation](results/2026-10-06). Regenerate the README table with:

```sh
.venv/bin/python benchmarks/summarize.py \
  benchmarks/results/2026-10-06/visible \
  benchmarks/results/2026-10-06/background
```

## What is compared

- Original StreamController: `0f439967a16a8bfdc859439b0ae2b1370185aa06`.
- Direct upstream, nazbert/Deckard: `a3609c7de63347dbc7e031926826898950600c83`.
- Native Deckard: Rust release executable; each result records its SHA-256, source commit and working-tree state.

Each application runs alone, in a fresh private HOME/data directory and a private D-Bus session without desktop service activation. Tests use one fake Stream Deck Plus with **eight labelled 120×120 output keys**, 14-point DejaVu Sans, the same colored backgrounds, brightness and disabled screensaver/session locking. The animated workload uses a 96×96, 100-frame GIF with **100 ms frame delays**, looping at a requested 10 FPS. No installed plugins or USB devices are used. The interface is visible for one set of measurements; all three applications use `-b` for the other set, while deck animation remains active.

The two Python applications use the **same Python 3.13 environment and dependency set**, including StreamDeck 0.2.1, Pillow 11.1.0 and the host GTK/Adwaita. This controls dependency differences but is not a comparison of their separately distributed Flatpaks or original dependency locks. `dependencies.txt` records that shared environment. GTK uses its default renderer; Rust 0.5.0 uses egui/wgpu Vulkan with memory-focused device allocation. The archived 0.4.1 comparison used egui/OpenGL. Applications retain their default key JPEG quality: 100 in the original SDK path, 90 in the direct upstream and Rust. These full-application measurements include each implementation’s rendering choices. The hardware-GPU runs use a private 1280×800 Weston headless Wayland compositor; all application windows have the same fullscreen output size. The recorded GPU is NVIDIA RTX 5070, driver 610.57.04, on an AMD Ryzen 7 9800X3D with eight exposed logical CPUs. Host GTK is 4.22.4 and Adwaita 1.9.3. CPU/RAM figures exclude the shared compositor and private D-Bus daemon, but include application descendants.

Each trial has 30 seconds of warmup and 30 seconds of one-second samples. There are three independent trials per application/workload/mode; application order rotates between trials. Reported CPU is the median of the three trial means. Reported RAM is the median of each trial's median PSS. PSS counts private memory plus a proportional share of shared pages; RSS is also retained in the raw results. **100% CPU means one logical core**, rather than the whole machine. Child CPU includes reaped short-lived helpers. Linux CPU accounting has 10 ms granularity; tiny static CPU differences should not be marketed as meaningful speedups.

No application source is patched for timing runs. Startup/page-load failures abort measurement. Instrumentation is used only in separate render-validation runs and is marked `instrumented: true`; those timings are excluded from performance tables. The observer confirms actual fake-device JPEG dimensions and changing frames for the Python applications. Native validation checks changing identities on all eight output tiles, rather than counting worker polling iterations. Separate visible/background checks observed original output at 9.1–10 FPS, direct output at 8.9–9.1 FPS and Rust at 9.95 FPS. A clean direct-upstream repeat confirmed approximately 9 FPS with no frame errors and correct dimensions. All applications receive the identical 100 ms GIF; differences in actual scheduling/deduplication are retained rather than patching or normalizing upstream output. Instrumentation timings are excluded.

## Reproduce

Use a Linux desktop with working GPU drivers, Python 3.13, GTK 4, libadwaita, the Python upstream dependencies, psutil, Pillow and `dbus-daemon`. PyGObject also needs its system development libraries when creating a new environment. See the pinned dependency file alongside the recorded results. Keep benchmark output outside your real application data. Do not compile packages or run another benchmark during sampling.

Fetch/check out the two source revisions above into separate directories. For existing local Git refs, export without modifying them:

```sh
mkdir -p target/benchmarks/sources target/benchmarks/direct
git archive streamcontroller/main | tar -x -C target/benchmarks/sources
git archive deckard-upstream/main | tar -x -C target/benchmarks/direct
cargo build --release --locked
```

Verify those refs match the recorded SHA values; otherwise use fresh clones checked out at the recorded commits. The source archives must include each application's complete assets and locales.

Run a private GPU-backed Weston session, without changing your desktop configuration:

```sh
mkdir -p /tmp/deckard-benchmark-wayland
chmod 700 /tmp/deckard-benchmark-wayland
cat > /tmp/deckard-benchmark-weston.ini <<'INI'
[core]
shell=kiosk-shell.so
idle-time=0
INI
XDG_RUNTIME_DIR=/tmp/deckard-benchmark-wayland weston \
  --backend=headless --renderer=gl --socket=deckard-benchmark \
  --width=1280 --height=800 --config=/tmp/deckard-benchmark-weston.ini
```

Check Weston's log for the actual hardware renderer. The recorded session used Weston 15.0.1, with its modules from an extracted official Arch package; the compositor was outside the application process trees. A bare Xvfb display uses software rendering and is a different workload. For X11/software comparisons, supply an Xvfb display with a running window manager via `--display` and omit the Wayland arguments; do not compare those values to hardware-GPU results.

In another terminal:

```sh
.venv/bin/python benchmarks/compare.py \
  --wayland-runtime /tmp/deckard-benchmark-wayland \
  --trials 3 --warmup 30 --duration 30 --output /tmp/deckard-visible
.venv/bin/python benchmarks/compare.py \
  --wayland-runtime /tmp/deckard-benchmark-wayland --background \
  --trials 3 --warmup 30 --duration 30 --output /tmp/deckard-background
```

Use `--python`, `--original`, `--direct` and `--native` to select other paths. `--apps` and `--workloads` select subsets for diagnostics. Run `--observe-legacy-frames --workloads animated --trials 1` separately to validate output; **never publish its timing numbers**.

## Investigated measurement and implementation issues

Before accepting results we ruled out failed startup, the wrong executable, compositor errors, mismatched GIF delays and missing child CPU. Original StreamController interprets GIF delays below 50 ms differently, so using a 40 ms GIF did not produce equivalent playback. The final 100 ms workload is validated on all three applications. Original playback also starts only after its initial caching/check interval; the warmup covers that delay.

Initial bare-Xvfb trials lacked a window manager and exercised software Mesa. Those diagnostic timings are excluded. Instrumented source-import wrappers were fixed to respect the direct upstream's rebranding initialization; the application sources themselves remain unchanged. The performance process sampler now includes child CPU that would otherwise disappear when a shell helper exits between samples.

Each current run records the exact native executable SHA-256 and source commit. Source is frozen before timing; documentation-only commits may follow. The [archived 0.4.1 comparison](results/2026-10-05) used native source `ffc2c9f3624cc1c6dd8b964e187cb2a03a8e039c` and is retained for before/after comparisons.

The Rust investigations found a static editor repaint timer, redundant composition of unchanged GIF frames, repeated resizing of shared media and per-frame GPU texture creation. The implementation now repaints after state or pixel changes, reuses unchanged composed frames, shares resized media within a render and updates existing GPU textures. Playback sampling remains anchored to its clock. Render validation and regression tests check that these savings retain changing pixels, timing, labels, rotation and settings invalidation.

These are local full-application measurements, not a universal promise for every controller, desktop, plugin or animation. Plugin artwork/continuous status integrations are outside the tested workload. CPU is not a latency measure; a “CPU reduction” or CPU-budget ratio must not be described as an input-response speedup. Physical USB throughput, end-to-end input latency, GPU utilization and ARM performance require separate measurements.

## Native action integration

With `pulseaudio`, `pactl` and `paplay` installed, this command creates its own temporary null-sink server, runs the native integration checks and cleans up the server and stream. It does not connect to your desktop audio server:

```sh
python3 benchmarks/audio_fixture.py --native target/release/deckard
```

This also runs inside both architecture builds. The fixture validates the actual bundled-helper baseline: older `pactl` interprets decimal percentage arguments differently, so native volume control sends unambiguous integer PulseAudio units.

## Native CPU/memory optimization (0.5.0)

The native editor prefers Vulkan, with wgpu's `MemoryUsage` device hint. This avoids the observed NVIDIA OpenGL buffer-swap polling while limiting allocation growth. If Vulkan initialization fails, Deckard restarts once into its OpenGL fallback; `DECKARD_RENDERER=glow` selects OpenGL explicitly. GPU model/backend are logged at startup. Results depend on the graphics driver and backend.

Other changes remove repeat work: renderer snapshots and live worker lists are shared; action discovery runs only on document/device/state changes; MPRIS reuses one connection and batches property reads; OBS shares bounded status caches and invalidates them on events; unchanged keys and strips retain their pixel buffers; keys share a preview texture atlas; system fonts are scanned through temporary memory maps and only used glyphs are rasterized. Glyph caching has a 2 MiB bound. Artwork retains small source sizes, uses bounded least-recently-used eviction, refreshes changed local covers and retries transient failures.

A Weston 15 kiosk-shell crash was reproduced in `weston_view_move_to_layer` when the final client closed. The current harness supports `--weston-bundle /path/to/extracted/weston`: every trial gets a fresh private GPU compositor, terminated before client teardown. The compositor stays outside the application process tree. This makes the test sequence repeatable without changing the user's desktop.

Use short output paths because native control sockets have a Unix path-length limit. Never run compilation or package builds during timed trials. Profiler runs, graphics captures and frame observers are diagnostic/validation runs and must be kept separate from timings.

## Native endpoint optimization (0.6.0)

`endpoints.py` compares the frozen host-built 0.5.0 executable with the 0.6.0 native renderer. It runs the real application and its descendants with a fresh HOME/data directory and private D-Bus session. Models select their own native output dimensions and number of keys. All keys have the same 14-point DejaVu Sans labels in both versions. GIF fixtures contain 200 distinct 120×120 source frames at 10, 50 or 100 FPS; the video fixture is a 120×120, 60 FPS FFV1 clip. Both versions receive an explicit 120 FPS media limit, exercising their maximum software paths. The old version internally clamps to 60 and polls rendering at approximately 30 FPS; it also stretches one-centisecond GIF delays to two centiseconds. The new version preserves valid source delays and rates.

Each case has three independent trials, with application order reversed in the middle trial, 15 seconds warmup and 15 seconds timing. CPU/PSS use the same process-tree sampler as the upstream comparison. The timing phase contains no frame observer. Frame fidelity is checked afterward for four seconds, independently of CPU sampling. 0.5.0 is observed through changing output-tile identities; its approximately 30 FPS renderer is below the control socket's approximately 50-poll/s limit. 0.6.0 uses two snapshots of actual changed-tile counters over the engine's monotonic clock, avoiding both polling aliasing and a continuous observer. Every key must change, have the correct model dimensions, and produce no engine errors. These are fake-device checks; USB write counts are zero.

The summary reports medians of trial CPU means, trial median PSS and each trial's minimum per-key FPS. It retains raw one-second CPU/PSS samples, per-key dimensions and frame rates, executable hashes, source state and CPU ranges. CPU milliseconds per complete device frame equal `CPU percent × 10 / FPS`; a device frame covers all keys, rather than one key. Comparing this cost distinguishes efficiency from simply reducing work. GIF encoded caches fill during warmup/sample according to the source's loop duration and each implementation's scheduling; the records specify the full measurement window rather than claiming an unlimited steady-state soak. A faster source can use more total CPU while costing less per delivered frame.

Reproduce the background comparison, using separate release executables from the two versions:

```sh
.venv/bin/python benchmarks/endpoints.py \
  --before /path/to/deckard-0.5.0 --after /path/to/deckard-0.6.0 \
  --cases plus10 plus100 plus60video xl50 plusxl50 \
  --trials 3 --warmup 15 --duration 15 --validate-seconds 4 \
  --output /tmp/deckard-endpoints
```

For visible editor checks, add `--visible --weston-bundle /path/to/extracted/weston`. This starts a fresh private GPU-backed 1280×800 Weston compositor per trial. The 0.6.0 preview wakes on pixel changes and uses desktop vsync; the old preview watcher polls at 20 FPS. Renderer FPS counts controller pixels, rather than monitor presentations. Optional cases cover Mini, MK.2, Studio, Mirabox and Ulanzi; Studio's corrected geometry should be checked with `--versions after`, since the old fallback has different dimensions. Keep Unix socket data paths short and run one benchmark at a time, without compilation or packaging during sampling.

The recorded host build is x86-64, on the same Ryzen 7 9800X3D / NVIDIA RTX 5070 machine as the upstream comparison. CPU uses percent of one logical core; RAM is MiB PSS. These timings do not measure the ARM64 builds, separately distributed Python packages, physical USB transfer, or LCD scanout. [Endpoint specifications and runtime diagnostics](../docs/rendering-endpoints.md) explain those limits.

[Recorded 0.6.0 endpoint results](results/2026-10-06/endpoints) contain separate background and visible samples, plus model fidelity checks. [Provenance](results/2026-10-06/endpoints/provenance.json) identifies the frozen 0.5.0/0.6.0 runtime source commits and both executable hashes. The visible/model metadata correctly records a dirty tree: only this methodology document was being edited; Rust sources and measured executables remained unchanged. Host-built binaries differ from the Ubuntu 22.04 release bundles, whose packaging checks run independently on both architectures.
