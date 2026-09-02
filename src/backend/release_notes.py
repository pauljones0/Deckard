"""Pick the running release's CHANGELOG.md entry and render it as release-notes markup.
libadwaita accepts only <p>, <ul>, <ol>, <li>, <em> and <code>, with no nesting, so the body is flattened."""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape

_SECTION_RE = re.compile(r"^## \[(?P<version>[^\]]+)\]")
_SUBHEADING_RE = re.compile(r"^#{3,6} +(?P<title>.+?)\s*$")
_BULLET_RE = re.compile(r"^[-*+] +(?P<text>.*)$")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")

_UNRELEASED = "unreleased"


@dataclass(frozen=True)
class ReleaseNotes:
    version: str
    markup: str


def load_release_notes(path: str, version: str) -> ReleaseNotes | None:
    """Read the changelog at path; None when it is missing or has no released entry."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    return release_notes_for(text, version)


def release_notes_for(text: str, version: str) -> ReleaseNotes | None:
    """The entry for version, else the newest released entry, else None."""
    sections = split_sections(text)
    wanted = _normalize(version)
    for name, body in sections:
        if _normalize(name) == wanted:
            markup = to_markup(body)
            if markup:
                return ReleaseNotes(name, markup)
    for name, body in sections:
        if _normalize(name) == _UNRELEASED:
            continue
        markup = to_markup(body)
        if markup:
            return ReleaseNotes(name, markup)
    return None


def split_sections(text: str) -> list[tuple[str, list[str]]]:
    """Split on `## [version]` headings into (version, body lines) pairs, in file order."""
    sections: list[tuple[str, list[str]]] = []
    body: list[str] | None = None
    for line in text.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            body = []
            sections.append((match.group("version").strip(), body))
        elif body is not None:
            body.append(line)
    return sections


def to_markup(lines: list[str]) -> str:
    """Render one section body: sub-headings and paragraphs as <p>, bullets as one flat <ul>."""
    blocks: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()
        if items:
            blocks.append("<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + "</ul>")
            items.clear()

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            flush()
            continue
        heading = _SUBHEADING_RE.match(stripped)
        if heading:
            flush()
            blocks.append(f"<p><em>{_inline(heading.group('title'))}</em></p>")
            continue
        bullet = _BULLET_RE.match(stripped)
        if bullet:
            # An indented bullet flattens to a sibling item: nested lists are unsupported.
            if paragraph:
                flush()
            items.append(bullet.group("text"))
            continue
        if items:
            items[-1] += " " + stripped
        else:
            paragraph.append(stripped)
    flush()
    return "\n".join(blocks)


def _inline(text: str) -> str:
    """Escape the text, drop link targets, and turn backtick spans into <code>."""
    text = _LINK_RE.sub(r"\1", text)
    text = escape(text, quote=False)
    return _CODE_SPAN_RE.sub(r"<code>\1</code>", text)


def _normalize(version: str) -> str:
    return version.strip().casefold().removeprefix("v")
