from __future__ import annotations

import collections
import json
from typing import TextIO

from comment_inventory_core import InventoryError, Unit
from comment_inventory_git import Inventory
from comment_inventory_partitions import PARTITION_IDS, PARTITION_NAMES


def summary(inventory: Inventory, partitions: dict[str, list[Unit]]) -> dict[str, object]:
    units = inventory.attributed_units
    partition_sum = sum(len(partitions[partition]) for partition in PARTITION_IDS)
    if partition_sum != len(units):
        raise InventoryError(
            f"partition sum {partition_sum} does not equal attributed corpus {len(units)}"
        )
    type_counts = collections.Counter(unit.type_name for unit in units)
    return {
        "revision": inventory.revision,
        "boundary": inventory.boundary,
        "tracked_python_files": len(inventory.tracked_files),
        "parsed_python_files": len(inventory.tracked_files),
        "blamed_python_files": len(inventory.tracked_files),
        "files_with_any_units": len({unit.path for unit in inventory.all_units}),
        "pre_attribution_units": len(inventory.all_units),
        "attributed_units": len(units),
        "attributed_files": len({unit.path for unit in units}),
        "mixed_units": sum(bool(unit.boundary_lines) for unit in units),
        "partition_count": len(PARTITION_IDS),
        "partition_sum": partition_sum,
        "types": dict(sorted(type_counts.items())),
        "partitions": [
            {
                "id": partition,
                "name": PARTITION_NAMES[partition],
                "files": len({unit.path for unit in partitions[partition]}),
                "units": len(partitions[partition]),
            }
            for partition in PARTITION_IDS
        ],
    }


def _unit_record(unit: Unit) -> dict[str, object]:
    return {
        "path": unit.path,
        "start": unit.start,
        "end": unit.end,
        "type": unit.type_name,
        "inline": unit.inline,
        "attribution": "mixed" if unit.boundary_lines else "deckard",
        "authored_lines": list(unit.authored_lines),
        "boundary_lines": list(unit.boundary_lines),
        "commits": list(unit.commits),
        "text": unit.text,
    }


def write_partition(
    output: TextIO,
    partition: str,
    units: list[Unit],
    chunk_size: int,
    selected_chunk: int | None,
) -> None:
    if not 1 <= chunk_size <= 100:
        raise InventoryError("chunk size must be from 1 through 100")
    chunks = [units[index : index + chunk_size] for index in range(0, len(units), chunk_size)]
    files = sorted({unit.path for unit in units})
    if selected_chunk is not None and not 1 <= selected_chunk <= len(chunks):
        raise InventoryError(
            f"chunk {selected_chunk} is outside partition range 1..{len(chunks)}"
        )
    metadata = {
        "kind": "partition",
        "id": partition,
        "name": PARTITION_NAMES[partition],
        "files": files,
        "unit_count": len(units),
        "chunk_size": chunk_size,
        "chunk_count": len(chunks),
    }
    output.write(json.dumps(metadata, sort_keys=True) + "\n")
    indexes = [selected_chunk - 1] if selected_chunk is not None else range(len(chunks))
    for index in indexes:
        record = {
            "kind": "chunk",
            "partition": partition,
            "index": index + 1,
            "chunk_count": len(chunks),
            "units": [_unit_record(unit) for unit in chunks[index]],
        }
        output.write(json.dumps(record, sort_keys=True) + "\n")
