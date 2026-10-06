# Rust GTK editor · 6 October 2026

Current source: `88171a62` / Deckard 0.9.1. [Published results](../../../../docs/performance.md) · [Method](../../../README.md).

- `rust-091`: final visible, hidden and daemon measurements, 100 FPS endpoint trials and validation-only frame records.
- `upstream-visible`, `upstream-daemon`: fresh original/direct baselines from the same session. The visible folder also retains earlier Rust 0.9.0 entries; they are excluded from the final table.
- `diagnostics-090`: released CachyOS 0.9.0 package renderer and GLX-vendor investigations. Auto/Cairo 100 FPS use three trials; the other cases are single-trial diagnostics.
- `diagnostics-080`: frozen egui 0.8.0 package, 100 FPS baseline only.
- `summary.json`: medians and ranges for the final CPU/RAM table.
- `validation-summary.json`: steady output rates, dimensions and errors; these observer runs supply no CPU timing to the table.
- `provenance.json`: hardware, GPU load, source state, exclusions and successful five-target release verification.

Every timing result retains per-second samples and executable/source metadata. Native output checks confirm 120×120 pixels on all eight Plus keys. GPU renderer verification and actual published-package checks are separate from CPU timing. No USB/LCD or ARM throughput was measured.
