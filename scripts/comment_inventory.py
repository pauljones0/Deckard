#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from comment_inventory_core import InventoryError
from comment_inventory_git import GitRepository
from comment_inventory_partitions import PARTITION_IDS, assign_partitions
from comment_inventory_report import summary, write_partition


DEFAULT_BOUNDARY = "129bdb58829929ac943b37583ab77c83e8a3dd12"
REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory fork-authored Python comments and docstrings at a Git revision."
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--boundary", default=DEFAULT_BOUNDARY)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--partition", choices=PARTITION_IDS)
    parser.add_argument("--chunk-size", type=int, default=25)
    parser.add_argument("--chunk", type=int)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if args.chunk is not None and args.partition is None:
        parser.error("--chunk requires --partition")
    return args


def main() -> int:
    args = parse_args()
    try:
        inventory = GitRepository(args.root).inventory(args.revision, args.boundary, args.jobs)
        partitions = assign_partitions(inventory.attributed_units)
        if args.partition is None:
            print(json.dumps(summary(inventory, partitions), indent=2, sort_keys=True))
        else:
            write_partition(
                sys.stdout,
                args.partition,
                partitions[args.partition],
                args.chunk_size,
                args.chunk,
            )
    except InventoryError as error:
        print(f"comment inventory failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
