# Whole-application CPU and RAM comparison

These scripts run the real applications, including their interfaces and device-rendering workers. Python here is **measurement/test tooling only** and is never bundled into native Deckard.

[Current Rust GTK results](results/2026-10-06/gtk-editor) · [Historical results](results/2026-10-06). The following command summarizes the historical 0.5.0 table:

```sh
.venv/bin/python benchmarks/summarize.py \
  benchmarks/results/2026-10-06/visible \
  benchmarks/results/2026-10-06/background
```

## Rust GTK editor (0.9.1)

The [current results](results/2026-10-06/gtk-editor) use source `88171a62`, a frozen host release binary and GTK's automatic renderer. Three trials use 15 seconds warmup and 30 seconds sampling. The upstream fixtures and Python revisions listed below are unchanged; fresh upstream timings from the same session supply the comparison. Their native 0.9.0 entries are retained as provenance, and the README uses the separate final 0.9.1 runs. Requested DejaVu Sans is unavailable on this host; the same requested family and fallback apply to all three apps.

Each trial uses a private D-Bus session without service activation and `GTK_A11Y=none`. This avoids launching host accessibility services while measuring. The timing phase has no frame observer. A separate, longer upstream observer covers the original's delayed playback startup. GPU-backed 1280×800 Wayland sessions are fresh per trial. An unrelated compute job used 97–100% GPU at spot checks; it was left running. These are measurements of this session, not quiet-lab or universal estimates.

The modes are distinct: `--background` never opens a window; `DECKARD_UI_HIDE_AFTER_MS=3000` opens and then closes the real GTK window before warmup ends. Hidden trials verify resource release after using the editor. Do not describe them as identical workloads.

```sh
.venv/bin/python benchmarks/compare.py --native target/release/deckard \
  --apps rust --workloads static animated --trials 3 --warmup 15 --duration 30 \
  --wayland-runtime /tmp/deckard-gtk --wayland-socket deckard-gtk \
  --weston-bundle target/benchmarks/weston --output target/gtk-visible
```

Repeat with a fresh output directory and the close environment variable for hidden measurements, or `--background` for the daemon. Select `--apps original direct` for upstream baselines. Run `--observe-legacy-frames --workloads animated --warmup 30 --duration 5 --trials 1` separately for validation; its timing results must be excluded.

The 100 FPS diagnostic uses `endpoints.py`, 200 distinct native 120×120 source frames and installed Liberation Sans. That differs from the 10 FPS upstream comparison fixture, so do not treat them as a matched pair.

```sh
.venv/bin/python benchmarks/endpoints.py --after target/release/deckard \
  --versions after --cases plus100 --visible --gtk-renderer auto \
  --font-family 'Liberation Sans' --trials 3 --warmup 15 --duration 30 \
  --validate-seconds 4 --output target/gtk-100
```

`--gtk-renderer cairo --disable-gpu` tests software painting; `--gtk-renderer gl` tests OpenGL. `--clear-glx-vendor` removes only a forced GLX vendor hint from the child environment. These are diagnostics rather than application defaults. Memory mappings are collected after CPU sampling. The raw records distinguish three-trial comparisons from single-trial vendor investigations, retain executable hashes and mark excluded measurements in their provenance.

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

## Native rendering and cache optimization (0.7.0)

The 0.7.0 comparison freezes both host-built executables before timing and uses the same native resolution, source rate, page, labels and JPEG quality in each version. The existing release profile remains optimized with thin LTO and one code-generation unit. A separate 0.6.0 PMU profile identified RGBA composition (about 53% of sampled user-space cycles), pixel hashing (14%) and generic RGBA-to-RGB conversion (9%) as large costs. Those sampled percentages are diagnostics, not benchmark speedups. The optimized paths preserve byte-for-byte reference RGBA blending/RGB extraction and reuse encoded images according to playback needs. Hash changes affect pixel identity; cryptographic download verification is unchanged.

All keys have 14-point **Liberation Sans**, which is installed on the measured host. Both versions load the same `/usr/share/fonts/liberation/LiberationSans-Regular.ttf`. This intentionally avoids relying on renderer-specific fallback for the historical fixture's requested DejaVu Sans family. Results here compare only these two executables and do not pool timings with previous tables. Missing-font lookup caching is implemented, but the new CPU comparison uses an available font.

Background cases cover Plus GIFs at 10 and 100 FPS, an XL GIF at 50 FPS, and a Plus single-pass 60 FPS video. GIF fixtures have 200 distinct 120×120 source frames. `plus60once` generates 7,200 FFV1 frames, 120 seconds at 60 FPS, and sets `loop: false`; it does not finish or loop during the measured window. Both versions receive an explicit 120 FPS limit and retain the detected source timing. The visible case animates all eight Plus keys at 100 FPS in a fresh private GPU-backed 1280×800 Weston session. There are three trials per version/case; execution order reverses in trial two. Each has 15 seconds warmup, 15 seconds of process-tree CPU/PSS sampling and four seconds of frame validation afterward. The CPU windows have no frame observer. Counter-window quantization and scheduling account for the small deviations around nominal FPS; all keys must change at the correct native dimensions without engine errors. CPU milliseconds per complete device frame remain in the summaries.

The video RAM figures are medians within that measurement window, including the FFmpeg child. The older large encoded cache fills with single-pass history during playback, while the new short cache retains recent repeated pixels. This is a bounded-window comparison, not a multi-day steady-state memory claim. The new cache stays within the existing configured byte budget. Looping GIF and editor PSS differences are small, slightly positive, and retained rather than marketed as savings. The raw samples show their variation. A separate Plus 100 FPS `/proc/PID/smaps` diagnostic found mapped application-image PSS higher by 0.39 MiB, anonymous heap lower by 0.04 MiB, and both stable between 15 and 30 seconds. This explains a small code footprint cost without claiming a long-duration leak test. [Diagnostic methods](results/2026-10-06/native-0.7/diagnostics.json) and [memory maps](results/2026-10-06/native-0.7/memory-maps.json) are excluded from CPU timings.

```sh
.venv/bin/python benchmarks/endpoints.py \
  --before /path/to/deckard-0.6.0 --after /path/to/deckard-0.7.0 \
  --cases plus10 plus100 xl50 plus60once --font-family 'Liberation Sans' \
  --trials 3 --warmup 15 --duration 15 --validate-seconds 4 \
  --output /tmp/deckard-native-bg
.venv/bin/python benchmarks/endpoints.py \
  --before /path/to/deckard-0.6.0 --after /path/to/deckard-0.7.0 \
  --cases plus100 --font-family 'Liberation Sans' --visible \
  --weston-bundle /path/to/extracted/weston \
  --trials 3 --warmup 15 --duration 15 --validate-seconds 4 \
  --output /tmp/deckard-native-ui
```

Use an available shared font, short absolute output paths and run one benchmark at a time. Do not compile or package software during timing. Source, compiler, host, executable hashes and measurement limits are recorded in [provenance](results/2026-10-06/native-0.7/provenance.json); [raw results](results/2026-10-06/native-0.7) retain all 30 runs. Both metadata files record a clean source tree. Ubuntu 22.04 distribution builds and their ARM64 variants are checked separately; these host timings do not measure their performance or physical LCD scanout.

## Native SIMD and allocation optimization (0.8.0)

The frozen 0.7.0 and 0.8.0 executables use identical nominal source rates, native dimensions, labels, JPEG quality and fixtures. The unchanged release profile uses opt-level 3, thin LTO and one code-generation unit. A separate 0.7.0 PMU profile attributed about 25% of sampled user-space cycles to `Renderer::encode`; annotation located nearly all of that function's sampled cost in scalar RGB channel packing. RGBA composition accounted for about 16%. These diagnostics are not benchmark speedups.

The new RGB writer selects SSSE3 on capable x86-64 CPUs and NEON on capable ARM64 CPUs, with a scalar fallback. Each SIMD block reads exactly 64 RGBA bytes and writes exactly 48 RGB bytes; all tails are handled by the scalar path. A final uninitialized Arc buffer is exposed as initialized only after every byte has been written. Tests check every channel, zero/small/native sizes, all 16 alignment offsets, surrounding-byte guards, extra source storage, color spaces and the scalar reference. Shared-image opacity uses a bounded 64-entry list of weak references, cleared for each composition and on device release. Partial transparency and clipping match the reference compositor. Borrowed decoder frames remove a GIF copy without changing disposal/timing. Both architectures run these checks in CI; performance is measured only on x86-64.

There are three trials for each of the four background cases and the visible Plus 100 FPS case, reversing version order in trial two. Each trial has 15 seconds warmup, 15 seconds of process-tree sampling and four seconds of changed-tile validation afterward. All keys must change at native dimensions, with no engine errors. The timing window has no frame observer. Both versions use installed 14-point Liberation Sans. The fixtures and sampler are the same as the 0.7.0 comparison: 200-frame 120×120 GIFs, a 120-second 60 FPS FFV1 single-pass video, fresh HOME/data/private D-Bus sessions, and a private 1280×800 GPU-backed Weston session for the editor. CPU/PSS summaries retain trial ranges and raw samples.

High-rate GIF CPU reductions are clear across trial ranges. The 10 FPS median is unchanged at the accounting floor. Video and editor ranges overlap; their small median reductions are retained as estimates rather than promised speedups. RAM is effectively flat, with small increases in the GIF/editor medians retained. A separate Plus 100 FPS memory-map pair finds the same 6.36 MiB anonymous heap in both versions at 15 and 30 seconds, with stable mapped-code PSS higher by 0.03 MiB in 0.8.0. This rules out a growing heap within that diagnostic window without claiming a multi-day leak test. A separate 0.8.0 video PMU capture, attached to the app and FFmpeg child, identifies at least 56.8% of sampled user-space cycles in JPEG routines and 18.8% in libavcodec, while RGB packing is 1.31%. This explains why video benefits less; profile overhead is excluded from the table. The temporary-copy removal reduces allocations; it is not presented as a measured RAM saving.

```sh
.venv/bin/python benchmarks/endpoints.py \
  --before /path/to/deckard-0.7.0 --after /path/to/deckard-0.8.0 \
  --cases plus10 plus100 xl50 plus60once --font-family 'Liberation Sans' \
  --trials 3 --warmup 15 --duration 15 --validate-seconds 4 \
  --output /tmp/deckard-simd-bg
.venv/bin/python benchmarks/endpoints.py \
  --before /path/to/deckard-0.7.0 --after /path/to/deckard-0.8.0 \
  --cases plus100 --font-family 'Liberation Sans' --visible \
  --weston-bundle /path/to/extracted/weston \
  --trials 3 --warmup 15 --duration 15 --validate-seconds 4 \
  --output /tmp/deckard-simd-ui
```

Use an available shared font and short output paths; run one benchmark at a time without host compilation or packaging. [Provenance](results/2026-10-06/native-0.8/provenance.json) records frozen source and executable hashes. Both timing metadata files report a clean source tree. [Diagnostics](results/2026-10-06/native-0.8/diagnostics.json) and [memory maps](results/2026-10-06/native-0.8/memory-maps.json) are separate from performance timing. Physical USB/panel refresh, ARM performance and long-duration soaks remain outside these measurements.

## Direct-upstream UI captures (0.10.0)

`ui_parity.py` captures both running applications in a fresh private Weston desktop-shell session with GTK Cairo, a 1400×900 main window and the same Liberation Sans/Plus fixture. Python is developer reference tooling only. Use the reference dependencies and source exports described above, plus Pillow and an extracted Weston bundle with its desktop shell.

```sh
.venv/bin/python benchmarks/ui_parity.py --case main --output target/ui-proof-main
.venv/bin/python benchmarks/ui_parity.py --case pages --output target/ui-proof-pages
```

Other cases cover no devices, settings pages, assets, the action form, dials, touchstrip, labels, deck settings and the common-plugin chooser. Output includes both PNGs, mapped-widget JSON, logs and a comparison report tied to the native executable hash. A matched screenshot is not proof of an interaction or physical USB behavior; the native GTK workflow and engine tests cover edits separately.

For `chooser-populated`, cache all five audited plugin repositories under `target/benchmarks/{OSPlugin,DeckPlugin,MediaPlugin,OBSPlugin,VolumeMixer}`. The reference Python environment additionally needs the OBS backend requirements. The script starts a private PulseAudio null-sink server in the cached CachyOS builder image, without connecting to your desktop audio server, and rejects captures where any reference plugin failed to initialize. No source application is modified.
