# Rust GTK 0.10.0 · 6 October 2026

[Measured medians and ranges](summary.json) · [Provenance and exclusions](provenance.json) · [Frame checks](validation-summary.json) · [UI comparison](ui/README.md) · [Final screenshot provenance](screenshot-verification.json)

The table uses original/direct entries from `upstream-visible` and `upstream-daemon`, and final Rust entries from `visible`, `daemon` and `endpoints`. Candidate Rust rows in upstream datasets are retained for provenance and excluded. All instrumented `validation-*` timings are excluded; their frame records validate dimensions, changing pixels and actual source rates.

Final application source: `2bd27218dbec3c52bd2519c21939ad5d974621fc`.
Host executable SHA-256: `b5032a0c8a734914c41cdff6c968ba68173f39baa75bc78c098dbc15dc9f2dae`.

Three trials, 15 seconds warmup and 30 seconds sampling. CPU means percent of one core; RAM is process-tree PSS. The 10 FPS comparison uses eight Plus keys with 120×120 output, a 96×96 GIF source and shared label requests. The 100 FPS endpoint fixture uses 200 native-size frames and installed Liberation Sans; it is a different workload. Applications keep their own font fallbacks, encoders and scheduling.

9800X3D / RTX 5070, fresh GPU-backed Wayland sessions, GTK 4.22.4 / libadwaita 1.9.3. An unrelated compute job was at 99% GPU in a spot check and was left running. The forced NVIDIA GLX vendor hint is retained. Compositor, private D-Bus daemon and GPU VRAM are excluded. USB/LCD, ARM performance, input latency and long-duration soak are unmeasured.

[Reproduce](../../../README.md#direct-upstream-gtk-layout-0100) · [Readable performance report](../../../../docs/performance.md)
