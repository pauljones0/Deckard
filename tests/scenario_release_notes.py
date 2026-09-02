"""Verify the What's New page picks the running release's changelog entry as libadwaita markup."""
import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

import os
import re
import tempfile

from src.backend.release_notes import load_release_notes, release_notes_for, to_markup

ALLOWED_TAGS = {"p", "ul", "ol", "li", "em", "code"}

SAMPLE = """# Changelog

Intro text before any release.

## [Unreleased]

### Added

- Not shipped yet.

## [0.3.0] - 2026-09-01

### Added

- Pan and zoom the wallpaper. Drag the box to pan and set the zoom
  factor to size it; a & b <c> stay escaped.
- Read the state with `--json`; it prints one object.

### Changed
- Bot-style heading with no blank line, see [the docs](https://example.invalid/x).
  - An indented sub-item.

A closing paragraph.

## [0.2.1] - 2026-08-09

### Fixed

- Older fix.
"""


def tags_in(markup: str) -> set[str]:
    return set(re.findall(r"</?([a-z]+)>", markup))


def test_exact_version_renders_its_section() -> None:
    notes = release_notes_for(SAMPLE, "0.3.0")
    assert notes is not None
    assert notes.version == "0.3.0"
    m = notes.markup
    assert tags_in(m) <= ALLOWED_TAGS, tags_in(m)
    assert "<p><em>Added</em></p>" in m
    assert "<li>Pan and zoom the wallpaper. Drag the box to pan and set the zoom factor to size it; a &amp; b &lt;c&gt; stay escaped.</li>" in m
    assert "<code>--json</code>" in m
    assert "<p><em>Changed</em></p>\n<ul><li>Bot-style heading with no blank line, see the docs.</li><li>An indented sub-item.</li></ul>" in m
    assert "<p>A closing paragraph.</p>" in m
    assert "Not shipped yet" not in m and "Older fix" not in m
    assert "#" not in m and "`" not in m and "](" not in m


def test_unknown_version_falls_back_to_newest_release() -> None:
    notes = release_notes_for(SAMPLE, "dev")
    assert notes is not None and notes.version == "0.3.0"
    assert "Not shipped yet" not in notes.markup, "Unreleased is never the fallback"

    prefixed = release_notes_for(SAMPLE, "v0.2.1")
    assert prefixed is not None and prefixed.version == "0.2.1"
    assert "Older fix" in prefixed.markup


def test_empty_matching_section_falls_back() -> None:
    text = "## [0.4.0] - 2026-10-01\n\n## [0.3.0] - 2026-09-01\n\n- Real entry.\n"
    notes = release_notes_for(text, "0.4.0")
    assert notes is not None and notes.version == "0.3.0"

    assert release_notes_for("# Changelog\n\nno sections here\n", "0.3.0") is None
    assert release_notes_for("## [Unreleased]\n\n- pending\n", "0.3.0") is None


def test_missing_file_is_none() -> None:
    with tempfile.TemporaryDirectory() as directory:
        assert load_release_notes(os.path.join(directory, "CHANGELOG.md"), "0.3.0") is None


def test_markup_shapes() -> None:
    assert to_markup([]) == ""
    assert to_markup(["", "   ", ""]) == ""
    assert to_markup(["Line one", "line two"]) == "<p>Line one line two</p>"
    assert to_markup(["- a", "- b", "", "- c"]) == "<ul><li>a</li><li>b</li></ul>\n<ul><li>c</li></ul>"
    assert to_markup(["Para", "- item"]) == "<p>Para</p>\n<ul><li>item</li></ul>"
    assert to_markup(["#### Deep heading"]) == "<p><em>Deep heading</em></p>"


def test_shipped_changelog_loads_for_the_stamped_version() -> None:
    path = os.path.join(gl.top_level_dir, "CHANGELOG.md")
    notes = load_release_notes(path, gl.deckard_version)
    assert notes is not None, "the tracked CHANGELOG.md must yield an entry"
    assert tags_in(notes.markup) <= ALLOWED_TAGS, tags_in(notes.markup)
    assert "<li>" in notes.markup
    if gl.deckard_version != "dev":
        assert notes.version == gl.deckard_version, (notes.version, gl.deckard_version)
    # Every list item and paragraph carries text, so no entry collapsed to an empty tag.
    assert "<li></li>" not in notes.markup and "<p></p>" not in notes.markup


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_release_notes")
    test_exact_version_renders_its_section()
    test_unknown_version_falls_back_to_newest_release()
    test_empty_matching_section_falls_back()
    test_missing_file_is_none()
    test_markup_shapes()
    test_shipped_changelog_loads_for_the_stamped_version()
    print("scenario_release_notes: PASS")


if __name__ == "__main__":
    main()
