"""Focused contract checks for the Python comment inventory."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import io
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from comment_inventory_core import InventoryError, Unit, attribute_unit, extract_units
from comment_inventory_git import blame_arguments, parse_blame
from comment_inventory_partitions import assign_partitions, validate_coverage
from comment_inventory_report import write_partition


def expect_inventory_error(action, text: str) -> None:
    try:
        action()
    except InventoryError as error:
        assert text in str(error), str(error)
    else:
        raise AssertionError(f"expected InventoryError containing {text!r}")


def check_extraction_and_types() -> None:
    source = '''#!/usr/bin/env python3
"""Copyright Example Project."""
# first line
# second line
value = 1  # noqa: reason
# disabled = 2
class Example:
    """Class summary."""
    def method(self):
        """Method summary."""
        return value
'''
    units = extract_units("sample.py", source)
    types = [unit.type_name for unit in units]
    assert types.count("comment:shebang") == 1
    assert types.count("docstring:license") == 1
    assert types.count("comment:ordinary") == 1
    assert types.count("comment:pragma") == 1
    assert types.count("comment:commented_code") == 1
    assert types.count("docstring:ordinary") == 2
    ordinary = next(unit for unit in units if unit.type_name == "comment:ordinary")
    assert (ordinary.start, ordinary.end, ordinary.text) == (3, 4, "# first line\n# second line")
    expect_inventory_error(lambda: extract_units("broken.py", "def broken(:\n"), "cannot parse")
    print("PASS: tokenizer and syntax tree extraction classify all unit types")


def check_attribution_and_blame_footing() -> None:
    unit = Unit("sample.py", 2, 3, "comment", "ordinary", False, "# old\n# new", (5, 5))
    attributed = attribute_unit(unit, {2: (True, "a" * 40), 3: (False, "b" * 40)})
    assert attributed.boundary_lines == (2,)
    assert attributed.authored_lines == (3,)
    assert attributed.commits == ("b" * 40,)
    assert attributed.text == "# old\n# new"
    expect_inventory_error(
        lambda: attribute_unit(unit, {2: (True, "a" * 40)}),
        "blame did not return",
    )

    porcelain = (
        f"{'a' * 40} 1 1 1\nboundary\n\told\n"
        f"{'b' * 40} 2 2 1\n\tnew\n"
    ).encode()
    assert parse_blame("sample.py", porcelain, 2) == {
        1: (True, "a" * 40),
        2: (False, "b" * 40),
    }
    expect_inventory_error(lambda: parse_blame("sample.py", porcelain, 3), "incomplete blame")
    args = blame_arguments("base", "tip", "sample.py")
    assert args[2:6] == ("-M", "-C", "-C", "base..tip")
    print("PASS: blame is move-aware, copy-aware, mixed-unit-safe, and fail-closed")


def check_partition_coverage() -> None:
    units = (
        Unit("main.py", 1, 1, "comment", "ordinary", False, "# root", (6,), (1,)),
        Unit(
            "src/backend/PluginManager/PluginBase.py",
            1,
            1,
            "docstring",
            "ordinary",
            False,
            '"""API."""',
            (10,),
            (1,),
        ),
        Unit(
            "tests/scenario_store_example.py",
            1,
            1,
            "comment",
            "pragma",
            False,
            "# noqa",
            (6,),
            (1,),
        ),
    )
    partitions = assign_partitions(units)
    assert [len(partitions[name]) for name in ("P00", "P04", "T11")] == [1, 1, 1]
    expect_inventory_error(lambda: validate_coverage(2, [(0, "P00")]), "missing")
    expect_inventory_error(
        lambda: validate_coverage(2, [(0, "P00"), (0, "P01"), (1, "P00")]),
        "duplicate",
    )
    print("PASS: every unit receives one valid campaign partition")


def check_bounded_review_output() -> None:
    units = [
        Unit("main.py", line, line, "comment", "ordinary", False, f"# {line}", (3,), (line,))
        for line in range(1, 6)
    ]
    output = io.StringIO()
    write_partition(output, "P00", units, 2, None)
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert records[0]["files"] == ["main.py"]
    assert records[0]["unit_count"] == 5
    chunks = records[1:]
    assert [len(record["units"]) for record in chunks] == [2, 2, 1]
    assert all("authored_lines" in unit for record in chunks for unit in record["units"])
    expect_inventory_error(lambda: write_partition(io.StringIO(), "P00", units, 101, None), "chunk size")
    invalid_output = io.StringIO()
    expect_inventory_error(lambda: write_partition(invalid_output, "P00", units, 2, 4), "outside")
    assert invalid_output.getvalue() == ""
    print("PASS: review output preserves exact fields in bounded chunks")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_comment_inventory")
    check_extraction_and_types()
    check_attribution_and_blame_footing()
    check_partition_coverage()
    check_bounded_review_output()
    print("PASS: scenario_comment_inventory")


if __name__ == "__main__":
    main()
