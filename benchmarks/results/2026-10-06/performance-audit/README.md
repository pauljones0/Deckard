# Measurement audit · 6 October 2026

[Findings and limits](../../../../docs/performance-audit.md) · [Summary](summary.json) · [Provenance](provenance.json).

- `common-actions`: all 18 uninstrumented timing trials, metadata and dependencies; these are the only included CPU/PSS table rows.
- `threads-normal` / `threads-slow`: actual callback thread starts; the slow case injects a 20-second sleep. Diagnostic timings are excluded.
- `graph-profile`: diagnostic process CPU attribution and actual plugin CPU readings, with per-thread IDs.
- `graph-frames`: separate JPEG dimensions/pixel-change validation, plus native changed-tile checks; timings excluded.
- `accounting.json`: synthetic counter regression tests, including detached descendants; not application performance.
- `endpoint-smoke`: new sampler and 100 FPS frame-counter smoke check; not a comparative result.
- `ui`: actual configured showcase and monitoring/graph captures; loaded monitoring includes independent host CPU samples. Live values differ between capture times.

Large thread traces and application logs are losslessly compressed as `.json.gz` / `.log.gz`; read with `gzip -dc`. Recompute the summary:

```sh
.venv/bin/python benchmarks/audit_report.py benchmarks/results/2026-10-06/performance-audit
```

The upstream revisions, executable checksum and unchanged application source are recorded in provenance. Tooling changed during the audit; application code did not. The host had unrelated GPU load. There is no hardware USB/LCD, ARM, multi-device, input-pressure or long-duration soak result here.
