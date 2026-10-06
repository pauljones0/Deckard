# Performance results

## Under 1% of a CPU core at 100 FPS

**Eight native-resolution Plus keys at 100 FPS: 33% less CPU than 0.7.0. XL animation uses 29% less.** These are fresh, matched-rate Rust **0.7.0 → 0.8.0** measurements.

| Workload · all keys | FPS · 0.7 → 0.8 | CPU · 0.7 → 0.8 | Median CPU change | RAM MiB · 0.7 → 0.8 |
| --- | ---: | ---: | ---: | ---: |
| Plus · 8 × 120² · 10 FPS GIF · background | 10.0 → 10.0 | 0.33% → **0.33%** | ≈unchanged | 17.5 → 17.8 |
| Plus · 8 × 120² · 100 FPS GIF · background | 100.0 → 100.0 | 1.40% → **0.93%** | −33% | 17.7 → 17.8 |
| XL · 32 × 96² · 50 FPS GIF · background | 49.9 → 49.9 | 2.33% → **1.67%** | −29% | 28.0 → 28.1 |
| Plus · 8 × 120² · single-pass 60 FPS video · background | 59.8 → 59.8 | 4.00% → **3.87%** | −3%* | 54.2 → 54.1 |
| Plus · 8 × 120² · 100 FPS GIF · visible editor | 100.2 → 100.2 | 4.00% → **3.60%** | −10%* | 177.7 → 177.8 |

*Video/editor trial ranges overlap: their smaller median changes are estimates, not guaranteed savings. The 10 FPS case is at the CPU accounting floor. RAM is effectively flat; GIF/editor medians increase by 0.07–0.27 MiB, and the table retains those differences. A separate memory-map diagnostic finds identical, stable anonymous heap and a 0.03 MiB mapped-code increase in the Plus 100 FPS pair.

0.8.0 packs RGB previews with runtime-detected x86-64 SSSE3 or ARM64 NEON, with a scalar fallback. It writes the final shared buffer directly without preliminary zero filling. Shared-image opacity is checked once per composed render in a 64-entry metadata cache using weak references; it does not retain pixel buffers. GIF playback borrows the decoder's frame, removing a temporary allocation/copy. Channel bytes, partial transparency, source timing, native dimensions and quality-90 4:4:4 JPEG are preserved.

CPU means percent of one logical core; RAM is application-plus-descendants PSS, including FFmpeg. Each result is the median of three trials with 15 seconds warmup, 15 seconds timing and separate four-second per-key validation. Both versions use installed 14-point Liberation Sans, identical GIF/video fixtures and the same native endpoints. The visible editor uses Vulkan on the Ryzen 7 9800X3D / RTX 5070 host. These fake-device checks measure rendered pixels; physical USB/LCD and ARM performance remain unmeasured. Compare the paired values here rather than chaining medians from older tables. [Methodology](../benchmarks/README.md#native-simd-and-allocation-optimization-080), [all 30 runs, ranges and executable hashes](../benchmarks/results/2026-10-06/native-0.8).

## Same pixels, half the rendering CPU

**100 FPS with 53% less background CPU. Single-pass video uses 56% less RAM.** These are measured Rust **0.6.0 → 0.7.0** results at the same source rates and native key dimensions.

| Workload · all keys | FPS · 0.6 → 0.7 | CPU · 0.6 → 0.7 | CPU saved | RAM MiB · 0.6 → 0.7 |
| --- | ---: | ---: | ---: | ---: |
| Plus · 8 × 120² · 10 FPS GIF · background | 10.0 → 10.0 | 0.60% → **0.40%** | **−33%** | 17.3 → 17.6 |
| Plus · 8 × 120² · 100 FPS GIF · background | 99.9 → 100.0 | 3.27% → **1.53%** | **−53%** | 17.5 → 17.6 |
| XL · 32 × 96² · 50 FPS GIF · background | 50.1 → 50.0 | 4.60% → **2.47%** | **−46%** | 27.8 → 28.1 |
| Plus · 8 × 120² · single-pass 60 FPS video · background | 60.1 → 59.8 | 5.47% → **4.13%** | **−24%** | 123.3 → 54.5 |
| Plus · 8 × 120² · 100 FPS GIF · visible editor | 100.0 → 100.0 | 5.93% → **3.53%** | **−40%** | 177.2 → 177.3 |

The renderer copies opaque RGBA rows, allocates RGB previews directly in their shared buffer and hashes pixel identities with single-pass XXH3. Partial transparency matches the reference compositor. JPEG quality 90 and 4:4:4 chroma are retained. Non-looping GIF/video playback keeps a short reuse cache, up to 1 MiB within the existing total cache budget; looping animations retain the larger cache. Unused encoded bytes are cleared after single-pass playback or device release.

Looping GIF/editor RAM is essentially flat: measured increases are 0.06–0.33 MiB, and the table retains them. The video memory result covers the measured playback window, when 0.6.0 accumulates encoded history; it is not an unlimited-duration soak result. CPU is percent of one logical core; RAM is application-plus-descendants PSS, including FFmpeg. Each result is the median of three trials with 15 seconds warmup, 15 seconds timing and separate four-second per-key validation. GIFs have 200 distinct 120×120 source frames; the single-pass video is a 120-second 60 FPS FFV1 clip. Both versions use the installed 14-point Liberation Sans font. The visible editor uses Vulkan on the same Ryzen 7 9800X3D / RTX 5070 host. These fake-device checks measure rendered pixels; physical USB/LCD and ARM performance remain unmeasured. [Methodology](../benchmarks/README.md#native-rendering-and-cache-optimization-070), [raw samples, FPS and executable hashes](../benchmarks/results/2026-10-06/native-0.7).

## Faster animation, less work per frame (0.6.0)

**100 FPS at about 17 MiB: 3.4× the frames, with 34% less CPU work per device frame. At the same 10 FPS, background CPU falls 20%.** These are measured Rust 0.5.0 → 0.6.0 results, with every key animated at its native resolution.

| Workload · all keys | FPS · 0.5 → 0.6 | CPU · 0.5 → 0.6 | RAM MiB · 0.5 → 0.6 | CPU work per device frame |
| --- | ---: | ---: | ---: | ---: |
| Plus · 8 × 120² · 10 FPS GIF · background | 10.0 → **10.0** | 0.67% → **0.53%** | 17.8 → **16.9** | **−20%** |
| Plus · 8 × 120² · 100 FPS GIF · background | 29.7 → **100.1** | 1.47% → 3.27% | 17.9 → **17.1** | **−34%** |
| Plus · 8 × 120² · 60 FPS video · background | 29.7 → **60.1** | 2.40% → 2.93% | 66.9 → 66.5 | **−40%** |
| XL · 32 × 96² · 50 FPS GIF · background | 29.2 → **49.9** | 3.20% → 4.80% | 28.0 → 27.7 | **−12%** |
| Plus XL · 36 × 112² · 50 FPS GIF · background | 28.7 → **49.9** | 4.47% → 6.53% | 32.8 → 32.4 | **−16%** |
| Plus · 8 × 120² · 100 FPS GIF · visible editor | 29.6 → **100.0** | 3.07% → 5.33% | 178.3 → 177.7 | **−48%** |

Higher-rate sources use more total CPU because more frames are rendered; the final column compares CPU cost for a complete frame across all keys. CPU is percent of one logical core; RAM is application-plus-descendants PSS. Results are medians of three trials, each with 15 seconds warmup and 15 seconds sampling, on the Ryzen 7 9800X3D / RTX 5070 host. Separate pixel checks validate every key after timing; the visible editor uses Vulkan in a fresh private 1280×800 Wayland session. GIFs have 200 distinct 120×120 source frames; video is a 60 FPS FFV1 clip. These fake-device measurements establish rendering throughput, rather than physical USB/LCD limits or ARM performance. [Methodology](../benchmarks/README.md#native-endpoint-optimization-060), [raw samples, per-key FPS and executable hashes](../benchmarks/results/2026-10-06/endpoints).

## More room for your stream 🦀

**Published 0.5.0 comparison: editor animation uses 52% less CPU and 42% less RAM than the direct upstream. Background animation uses 80% less CPU and 92% less RAM than the original.**

The upstream table records the published **0.5.0** comparison. Each application cell shows **CPU / RAM**. CPU is percent of one logical core; RAM is proportional resident memory (PSS). Negative changes mean lower resource use.

| Workload | Original StreamController | Direct upstream Deckard | Rust 0.5.0 | vs original | vs direct |
| --- | ---: | ---: | ---: | --- | --- |
| Editor visible · static | 0.10% / 297 MiB | 0.07% / 266 MiB | **0.10% / 171 MiB** | CPU: idle floor; RAM -42% | CPU: idle floor; RAM -36% |
| Editor visible · 8 animated keys | 6.33% / 339 MiB | 3.80% / 303 MiB | **1.83% / 176 MiB** | CPU -71%; RAM -48% | CPU -52%; RAM -42% |
| Background · static | 0.10% / 167 MiB | 0.03% / 149 MiB | **0.10% / 13 MiB** | CPU: idle floor; RAM -92% | CPU: idle floor; RAM -91% |
| Background · 8 animated keys | 4.07% / 197 MiB | 2.23% / 195 MiB | **0.80% / 16 MiB** | CPU -80%; RAM -92% | CPU -64%; RAM -92% |

The Rust port itself also got leaner:

| Animated workload | Rust 0.4.1 | Rust 0.5.0 | Improvement |
| --- | ---: | ---: | --- |
| Editor visible · 8 keys | 4.90% / 187 MiB | **1.83% / 176 MiB** | **63% less CPU; 6% less RAM** |
| Background · 8 keys | 1.07% / 64 MiB | **0.80% / 16 MiB** | **25% less CPU; 75% less RAM** |

Static CPU values are near the accounting floor, so their tiny differences are not treated as speedups. CPU savings are resource savings; input-response latency was not measured.

Measured on a Ryzen 7 9800X3D / NVIDIA RTX 5070, using three trials per case, 30 seconds warmup and 30 seconds sampling, a fresh private 1280×800 GPU-backed Wayland session, one fake Plus and eight labelled keys. The GIF requests 10 FPS; separate frame checks observed **8.9–10 FPS upstream and 9.95 FPS in Rust**, on all eight keys in both modes. No installed plugins or physical USB hardware were used. The Python baselines share one dependency environment; applications retain their default renderers/encoders. Rust 0.5.0 uses Vulkan with memory-focused allocation; 0.4.1 used OpenGL. Results depend on hardware, drivers and workload. [Methodology and reproduction](../benchmarks/README.md), [raw samples and revisions](../benchmarks/results/2026-10-06), [frame validation](../benchmarks/results/2026-10-06/validation), [archived 0.4.1 baseline](../benchmarks/results/2026-10-05).
