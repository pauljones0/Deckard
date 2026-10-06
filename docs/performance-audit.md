# Performance audit

The README's general CPU/RAM savings claim is withdrawn. The old runs measured real applications with fake USB devices, eight animated keys and **no plugins**. They did not establish performance with common plugins, repeated input events, slow callbacks or real USB traffic.

## Thread creation

The pinned original starts a new `own_actions_tick` thread for **every input every second**, including empty inputs. Its tick dispatcher has no overlap guard. If a plugin callback takes longer than a tick, another callback can start before it finishes.

The direct fork preserves a per-deck worker pool and skips another tick while the previous callback for that input runs. Empty inputs do not submit work. Rust uses persistent service workers for its native system/media/OBS actions; the system readouts share one service worker. These are different callback implementations, so a slow Python callback reproducer is not a matched Rust workload.

Sources: [original tick loop](https://github.com/StreamController/StreamController/blob/0f439967a16a8bfdc859439b0ae2b1370185aa06/src/backend/DeckManagement/DeckController.py#L1522), [direct dispatch](https://github.com/nazbert/Deckard/blob/a3609c7de63347dbc7e031926826898950600c83/src/backend/DeckManagement/deck_controller/input_state_classes.py#L244), [Rust workers](../rust/core/src/live.rs). Upstreams are pinned to `0f439967a16a8bfdc859439b0ae2b1370185aa06` and `a3609c7de63347dbc7e031926826898950600c83`.

Recorded diagnostics: the empty Plus starts **12.99 new tick threads/second** in the original and none in the fork. With eight deliberately delayed monitoring actions, the original peaks at **161 concurrent tick threads**; the fork uses **eight persistent callback workers**. Sleeping callbacks did not create a CPU spike in this test. This proves overlap/backlog behavior, not inevitable CPU saturation on every page.

## Counter checks

Short-lived threads are included in Linux process CPU totals. The old sampler was checked with a single busy thread, 120 short-lived busy threads and nested children that are waited for. Those cases agreed with independent kernel accounting within the old counter's resolution.

A double-forked child that finishes between samples is different: it leaves the observed process tree and its CPU is never transferred to the app's waited-child counters. The regression fixture reproduces that loss. This proves a weakness in the sampler, **not that every archived application run was affected**. The old JSON did not retain sufficient per-process counters to reconstruct every historical value.

Current timing reads `cpu.stat usage_usec` from an isolated cgroup-v2 group containing only the application and its descendants. The kernel retains CPU usage after thread/process exit and reparenting. PSS, RSS, process counts and thread counts are sampled from that same group's live processes. The compositor, D-Bus daemon and measurement harness remain outside it. Missing cgroup delegation aborts a run instead of falling back to the old sampler.

```sh
.venv/bin/python benchmarks/accounting_audit.py --output target/accounting-audit.json
```

The fixtures test accounting mechanics; they are not application benchmarks.

## Fresh measurements · released Rust 0.10.0

Three trials per application and workload, 15 seconds warmup and 30 seconds timing. The editor is open on one fake Plus with eight 120×120 keys. Four actions read CPU and four read RAM. CPU is percent of **one logical core**; RAM is summed cgroup-process PSS. Each cell is a median; CPU trial ranges follow below. These are actual process measurements; USB traffic is omitted.

| Workload · CPU / RAM | Original StreamController | Direct fork | Rust GTK |
| --- | ---: | ---: | ---: |
| Readouts | 0.68% / 321 MiB | 0.52% / 305 MiB | 0.58% / 204 MiB |
| Graphs | 12.05% / 993 MiB | 17.25% / 800 MiB | 0.99% / 210 MiB |

| CPU trial ranges | Original | Direct fork | Rust |
| --- | ---: | ---: | ---: |
| Readouts | 0.57–0.76% | 0.33–0.72% | 0.24–0.90% |
| Graphs | 11.93–12.11% | 17.10–17.83% | 0.66–1.05% |

PSS ranges: readouts 320.4–322.1 / 303.1–307.0 / 203.3–207.4 MiB; graphs 982.5–993.6 / 795.7–806.0 / 207.0–212.9 MiB, in the same column order. The readout CPU ranges overlap: **no clear Rust CPU win is established there**. Graphs use nine processes in both reference applications, including eight real matplotlib graph workers; Rust uses one process.

### Why the fork's graph CPU is higher

A separate diagnostic attributes CPU to each live process and cross-checks their totals against cgroup accounting. The original uses about 2.33% in its application process and 9.86% in graph workers; the fork uses 6.76% and 11.00%. Independent totals agree within 0.12 percentage points. Most of the extra CPU is in the application process. This rules out missing child CPU as the explanation; it does not identify a single hot function.

The graphs also receive different values. After warmup, the trace records 116 CPU readings in each reference: the original has 29 previously unseen thread IDs, returning 26 zeros and three 100% readings; the fork has no new IDs and no zeros. Reused original IDs average about five seconds between calls; the fork's median is about one second. Nonblocking `psutil.cpu_percent()` keeps per-thread histories and requires a meaningful interval. Thread churn is the supported explanation for these first-sample artifacts. See the [psutil API](https://psutil.io/api/#psutil.cpu_percent); the tested dependency is 7.0.0.

The applications retain their defaults and actual bugs. These graph runs compare the same requested actions, **not identical sampled values, pixels or plotting implementations**. The table therefore reports resource use for these cases without general speedup percentages.

### Output checks

Separate frame observers verify eight changing 120×120 output keys with no frame errors in all three applications. They are excluded from timing. Matched real-editor captures show the monitoring and graph output; Rust adds numeric percentages over its graphs. A functional check with three temporary busy processes shows 61% in the running Rust editor against contemporaneous Linux counter readings around 61%. It is not a performance result. The busy processes were stopped after the check.

[Raw samples, summaries, compressed diagnostic logs and actual UI captures](../benchmarks/results/2026-10-06/performance-audit).

## Coverage

`compare.py` now supports `monitoring` (four CPU and four RAM readouts) and `graphs` (four CPU and four RAM graphs). The reference applications load the real OSPlugin, including its graph subprocesses. Rust uses the corresponding native actions. Their rendering and update behavior retain their own defaults; the graph implementations are not pixel-equivalent.

Thread starts are counted in separate `--observe-threads` runs. `--slow-tick-seconds 20` deliberately delays real CPU/RAM plugin callbacks to reproduce overlap. That synthetic delay and the observer's timings must never be used as ordinary performance claims.

```sh
.venv/bin/python benchmarks/compare.py --apps original direct --workloads monitoring \
  --observe-threads --slow-tick-seconds 20 --trials 1 --warmup 10 --duration 45 \
  --weston-bundle target/benchmarks/weston --wayland-runtime /tmp/deckard-audit \
  --wayland-socket audit --output target/slow-tick-audit
.venv/bin/python benchmarks/compare.py --workloads monitoring graphs \
  --trials 3 --warmup 15 --duration 30 \
  --weston-bundle target/benchmarks/weston --wayland-runtime /tmp/deckard-audit \
  --wayland-socket audit --output target/common-action-timings
```

The host has unrelated GPU load. These fixtures still omit physical USB/LCD traffic, multiple devices, a long-duration soak, OBS/MPRIS/volume service activity and sustained input/page-switch pressure. No general speedup is claimed from them. [Archived results](performance.md).
