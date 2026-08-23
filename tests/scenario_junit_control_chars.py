"""The JUnit writer must survive control characters in captured output.

A failing scenario that prints ANSI colour (or any other C0 control byte)
put those bytes straight into the XML text nodes; XML 1.0 forbids them, so
minidom's parse of the serialized tree raised and the whole report was
lost for every scenario, not just the noisy one. This drives write_junit
with such output and asserts it produces a valid, parseable report.
"""
import fixtures  # noqa: F401  (isolated --data tempdir; import first)

import tempfile
from pathlib import Path
from xml.dom import minidom

import run_all


def test_junit_survives_control_chars_in_output() -> None:
    noisy = "row \x1b[31mRED\x1b[0m\nbell\x07 esc\x1b vt\x0b nul\x00 keep\ttab"
    results = [
        ("scenario_ok.py", "PASS", 0.10, "plain \x1b[32mgreen\x1b[0m out"),
        ("scenario_bad.py", "FAIL", 0.20, noisy),
        ("scenario_xf.py", "XFAIL", 0.00, "x \x1b[1mbold\x1b[0m y"),
    ]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "report.xml"
        # Before the fix this raised ExpatError and wrote nothing.
        run_all.write_junit(path, results)
        assert path.exists(), "the report file must be written"
        dom = minidom.parse(str(path))  # must be well-formed XML
        text = dom.toxml()

    for forbidden in ("\x1b", "\x07", "\x00", "\x0b"):
        assert forbidden not in text, f"forbidden control char {forbidden!r} reached the report"
    # The visible content survives, minus the escape codes.
    for keep in ("scenario_bad.py", "RED", "green", "bold"):
        assert keep in text, f"expected {keep!r} to survive sanitisation"
    # tab, newline and carriage return are legal XML and must not be stripped.
    assert run_all._xml_safe("a\tb\nc\rd") == "a\tb\nc\rd"


def main() -> None:
    test_junit_survives_control_chars_in_output()
    print("scenario_junit_control_chars: OK")


if __name__ == "__main__":
    main()
