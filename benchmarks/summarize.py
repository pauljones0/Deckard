#!/usr/bin/env python3
"""Summarize complete uninstrumented runs; print a README comparison table."""

import argparse
import json
import pathlib
import statistics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("visible", type=pathlib.Path)
    parser.add_argument("background", type=pathlib.Path)
    parser.add_argument("--json", type=pathlib.Path)
    args = parser.parse_args()
    summaries = []
    metadata = []
    for mode, directory in [
        ("Editor visible", args.visible),
        ("Background", args.background),
    ]:
        settings = json.loads((directory / "metadata.json").read_text())
        assert not settings["instrumented"], "observer timings must never be published"
        assert settings["trials"] >= 3 and settings["sample_seconds"] >= 30
        metadata.append(settings)
        results = json.loads((directory / "results.json").read_text())
        for workload in ["static", "animated"]:
            row = {"mode": mode, "workload": workload, "apps": {}}
            for app in ["original", "direct", "rust"]:
                trials = [
                    result
                    for result in results
                    if result["app"] == app and result["workload"] == workload
                ]
                assert len(trials) == settings["trials"]
                assert len({trial["trial"] for trial in trials}) == len(trials)
                row["apps"][app] = {
                    key: {
                        "median": statistics.median(trial[key] for trial in trials),
                        "min": min(trial[key] for trial in trials),
                        "max": max(trial[key] for trial in trials),
                    }
                    for key in ["cpu_percent", "pss_mib", "rss_mib"]
                }
            summaries.append(row)
    assert metadata[0]["native_sha256"] == metadata[1]["native_sha256"]
    assert metadata[0]["upstreams"] == metadata[1]["upstreams"]
    if args.json:
        args.json.write_text(
            json.dumps({"runs": metadata, "scenarios": summaries}, indent=2) + "\n"
        )
    print(
        "| Workload | Original StreamController | Direct upstream Deckard | Rust Deckard | vs original | vs direct |"
    )
    print("| --- | ---: | ---: | ---: | --- | --- |")
    for row in summaries:
        label = row["mode"] + (
            " · static" if row["workload"] == "static" else " · 8 animated keys"
        )
        cells = []
        for app in ["original", "direct", "rust"]:
            values = row["apps"][app]
            cells.append(
                f"{values['cpu_percent']['median']:.2f}% / {values['pss_mib']['median']:.0f} MiB"
            )
        deltas = []
        for app in ["original", "direct"]:
            native = row["apps"]["rust"]
            legacy = row["apps"][app]
            cpu = (
                100
                * (
                    native["cpu_percent"]["median"] / legacy["cpu_percent"]["median"]
                    - 1
                )
                if legacy["cpu_percent"]["median"]
                else None
            )
            ram = 100 * (native["pss_mib"]["median"] / legacy["pss_mib"]["median"] - 1)
            cpu_text = (
                "CPU: idle floor" if row["workload"] == "static" else f"CPU {cpu:+.0f}%"
            )
            deltas.append(f"{cpu_text}; RAM {ram:+.0f}%")
        print(
            f"| {label} | {cells[0]} | {cells[1]} | **{cells[2]}** | {deltas[0]} | {deltas[1]} |"
        )


if __name__ == "__main__":
    main()
