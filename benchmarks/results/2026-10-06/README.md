# Deckard 0.5.0 measurements — 2026-10-06

The native executable was built from `b19036481e9a90a058d286595af8370b3fedc670`, SHA-256 `6494f201eb901bc31f23f67d43af9be035dce53c16380b9581fda6020e68e3c7`. Both timing modes used this unchanged host-built Rust 1.99 release executable. Release packages compile the same native source against Ubuntu 22.04 for distribution; their ELF checksums differ. Later commits add these results and documentation.

`visible/` and `background/` each contain 18 trials: three applications × two workloads × three repetitions. Every trial warms for 30 seconds, then samples for 30 seconds. `summary.json` includes medians and ranges. Timing runs have `instrumented: false`, rotated application order and a fresh private hardware-rendered Weston compositor per trial. No local compilation or package builds ran during timing.

Original StreamController is pinned at `0f439967a16a8bfdc859439b0ae2b1370185aa06`; direct upstream Deckard is pinned at `a3609c7de63347dbc7e031926826898950600c83`. Their sources were not patched. Both use the recorded shared Python environment. Native startup logs identify NVIDIA GeForce RTX 5070 / Vulkan. The device is a fake Plus with eight labelled keys; the workload has no plugin actions or physical USB IO.

`validation/` records separate instrumented checks of actual changing fake-device JPEGs for Python and output-tile identities for Rust. These timings are excluded from the table. GIF bytes match the timing workload. Upstream startup/caching needs a full warmup; a short diagnostic that saw only the initial previews was rejected before measurement.

Regenerate the comparison:

```sh
.venv/bin/python benchmarks/summarize.py \
  benchmarks/results/2026-10-06/visible \
  benchmarks/results/2026-10-06/background
```

[Full method, investigations and commands](../../README.md). [Previous Rust baseline](../2026-10-05).
