# Native endpoint comparison: Rust 0.5.0 → 0.6.0

`background/` contains three trials for each version and each of five workloads. `visible/` contains three trials per version for the Plus 100 FPS GIF with the editor open. Each directory retains metadata, raw timing samples, separate per-key pixel validation and the median summary. Read [the methodology](../../../README.md#native-endpoint-optimization-060) before comparing them.

`fidelity/models.json` retains only short native-resolution/pixel checks for Mini, MK.2, Studio, Ulanzi and Mirabox. Its diagnostic timing samples are excluded from performance comparisons.

[provenance.json](provenance.json) ties each measured executable to its frozen runtime sources and SHA-256. The dirty-tree flags in visible/model metadata refer only to methodology documentation edits. Runtime sources and executables remained unchanged. These are host-built x86-64 application measurements without physical USB hardware, rather than ARM benchmarks or LCD refresh measurements.

FPS counts actual changing rendered pixels on every key. CPU is percent of one logical core and RAM is MiB PSS, including application descendants. CPU milliseconds per complete device frame equal `CPU percent × 10 / FPS`. Higher total CPU at higher FPS can coexist with lower CPU cost per frame. Visible measurements exclude the private compositor and count controller rendering, rather than monitor presentations.
