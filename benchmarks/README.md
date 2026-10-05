# Whole-application CPU and RAM comparison

These scripts run the real applications, including their interfaces and device-rendering workers. Python here is **measurement/test tooling only** and is never bundled into native Deckard.

## What is compared

- Original StreamController: `0f439967a16a8bfdc859439b0ae2b1370185aa06`.
- Direct upstream, nazbert/Deckard: `a3609c7de63347dbc7e031926826898950600c83`.
- Native Deckard: Rust release executable; each result records its SHA-256, source commit and working-tree state.

Each application runs alone, in a fresh private HOME/data directory and a private D-Bus session without desktop service activation. Tests use one fake Stream Deck Plus with **eight labelled 120×120 output keys**, 14-point DejaVu Sans, the same colored backgrounds, brightness and disabled screensaver/session locking. The animated workload uses a 96×96, 100-frame GIF with **100 ms frame delays**, looping at a requested 10 FPS. No installed plugins or USB devices are used. The interface is visible for one set of measurements; all three applications use `-b` for the other set, while deck animation remains active.

The two Python applications use the **same Python 3.13 environment and dependency set**, including StreamDeck 0.2.1, Pillow 11.1.0 and the host GTK/Adwaita. This controls dependency differences but is not a comparison of their separately distributed Flatpaks or original dependency locks. `dependencies.txt` records that shared environment. GTK uses its default renderer; Rust uses egui/OpenGL. The hardware-GPU runs use a private 1280×800 Weston headless Wayland compositor; all application windows have the same fullscreen output size. The recorded GPU is NVIDIA RTX 5070, driver 610.57.04, on an AMD Ryzen 7 9800X3D with eight exposed logical CPUs. Host GTK is 4.22.4 and Adwaita 1.9.3. CPU/RAM figures exclude the shared compositor and private D-Bus daemon, but include application descendants.

Each trial has 30 seconds of warmup and 30 seconds of one-second samples. There are three independent trials per application/workload/mode; application order rotates between trials. Reported CPU is the median of the three trial means. Reported RAM is the median of each trial's median PSS. PSS counts private memory plus a proportional share of shared pages; RSS is also retained in the raw results. **100% CPU means one logical core**, rather than the whole machine. Child CPU includes reaped short-lived helpers. Linux CPU accounting has 10 ms granularity; tiny static CPU differences should not be marketed as meaningful speedups.

No application source is patched for timing runs. Startup/page-load failures abort measurement. Instrumentation is used only in separate render-validation runs and is marked `instrumented: true`; those timings are excluded from performance tables. The observer confirms actual fake-device JPEG dimensions and changing frames for the Python applications. Native validation checks changing identities on all eight output tiles, rather than counting worker polling iterations.

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

The Rust investigations found a static editor repaint timer, redundant composition of unchanged GIF frames, repeated resizing of shared media and per-frame GPU texture creation. The implementation now repaints after state or pixel changes, reuses unchanged composed frames, shares resized media within a render and updates existing GPU textures. Playback sampling remains anchored to its clock. Render validation and regression tests check that these savings retain changing pixels, timing, labels, rotation and settings invalidation.

These are local full-application measurements, not a universal promise for every controller, desktop, plugin or animation. Plugin artwork/continuous status integrations are outside the tested workload. CPU is not a latency measure; a “CPU reduction” or CPU-budget ratio must not be described as an input-response speedup. Physical USB throughput, end-to-end input latency, GPU utilization and ARM performance require separate measurements.
