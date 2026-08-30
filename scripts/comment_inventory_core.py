from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, replace


PRAGMA_RE = re.compile(
    r"^(?:noqa\b|nosec\b|type:\s*ignore\b|type:\s*[^ ]|ty:\s*ignore\b|"
    r"pyright:|ruff:|fmt:\s*(?:on|off|skip)\b|isort:|pylint:|"
    r"pragma:\s*no\s*cover\b|coverage:)",
    re.IGNORECASE,
)
ENCODING_RE = re.compile(r"coding[=:]\s*[-\w.]+")
LICENSE_RE = re.compile(
    r"(?:SPDX-License-Identifier|copyright|GNU General Public License|"
    r"free software foundation|this program is free software|"
    r"ABSOLUTELY NO WARRANTY|you should have received a copy)",
    re.IGNORECASE,
)
CODE_EVIDENCE_RE = re.compile(
    r"^(?:@[A-Za-z_]|(?:async\s+)?def\s+|class\s+|from\s+.+\s+import\s+|"
    r"import\s+[A-Za-z_]|(?:if|elif|for|while|with|match|case|try|except|else|finally)\b.*:"
    r"|(?:return|yield|raise|assert|del|global|nonlocal|await)\b|(?:break|continue|pass)\s*$"
    r"|(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[[^]]+\])*)\s*(?::[^=]+)?="
    r"|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*\()"
)


class InventoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Unit:
    path: str
    start: int
    end: int
    syntax: str
    subtype: str
    inline: bool
    text: str
    physical_lengths: tuple[int, ...]
    authored_lines: tuple[int, ...] = ()
    boundary_lines: tuple[int, ...] = ()
    commits: tuple[str, ...] = ()

    @property
    def type_name(self) -> str:
        return f"{self.syntax}:{self.subtype}"


def decode_python(path: str, data: bytes) -> str:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return data.decode(encoding)
    except (LookupError, SyntaxError, UnicodeError) as error:
        raise InventoryError(f"{path}: cannot decode Python source: {error}") from error


def _docstring_ranges(tree: ast.AST) -> list[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    node_types = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, node_types) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add((first.lineno, first.end_lineno or first.lineno))
    return sorted(found)


def _comment_subtype(text: str, line: int) -> str:
    value = text[1:].strip()
    if line == 1 and text.startswith("#!"):
        return "shebang"
    if line <= 2 and ENCODING_RE.search(value):
        return "pragma"
    if PRAGMA_RE.search(value):
        return "pragma"
    return "ordinary"


Comment = tuple[int, int, str, str, bool]


def _is_commented_code(group: list[Comment]) -> bool:
    if group[0][3] != "ordinary" or group[0][4]:
        return False
    source = "\n".join(item[2][1:].lstrip() for item in group)
    if not CODE_EVIDENCE_RE.search(source):
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return bool(tree.body) and not all(
        isinstance(node, ast.Expr)
        and isinstance(node.value, (ast.Name, ast.Attribute, ast.Constant))
        for node in tree.body
    )


def extract_units(path: str, source: str) -> list[Unit]:
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=path)
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (IndentationError, SyntaxError, ValueError, tokenize.TokenError) as error:
        raise InventoryError(f"{path}: cannot parse or tokenize Python source: {error}") from error

    units: list[Unit] = []
    for start, end in _docstring_ranges(tree):
        text = "\n".join(lines[start - 1 : end])
        units.append(
            Unit(
                path,
                start,
                end,
                "docstring",
                "license" if LICENSE_RE.search(text) else "ordinary",
                False,
                text,
                tuple(len(line) for line in lines[start - 1 : end]),
            )
        )

    comments: list[Comment] = []
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        line, column = token.start
        inline = bool(lines[line - 1][:column].strip())
        comments.append((line, column, token.string, _comment_subtype(token.string, line), inline))

    groups: list[list[Comment]] = []
    for item in comments:
        previous = groups[-1][-1] if groups else None
        if (
            previous is not None
            and not item[4]
            and not previous[4]
            and item[0] == previous[0] + 1
            and item[1] == previous[1]
            and item[3] == previous[3]
        ):
            groups[-1].append(item)
        else:
            groups.append([item])

    for group in groups:
        start, end = group[0][0], group[-1][0]
        subtype = "commented_code" if _is_commented_code(group) else group[0][3]
        units.append(
            Unit(
                path,
                start,
                end,
                "comment",
                subtype,
                group[0][4],
                "\n".join(item[2] for item in group),
                tuple(len(lines[item[0] - 1]) for item in group),
            )
        )
    return sorted(units, key=lambda unit: (unit.start, unit.end, unit.syntax))


def attribute_unit(unit: Unit, blame: dict[int, tuple[bool, str]]) -> Unit:
    authored: list[int] = []
    boundary: list[int] = []
    commits: set[str] = set()
    for line in range(unit.start, unit.end + 1):
        try:
            is_boundary, commit = blame[line]
        except KeyError as error:
            raise InventoryError(f"{unit.path}:{line}: blame did not return this line") from error
        if is_boundary:
            boundary.append(line)
        else:
            authored.append(line)
            commits.add(commit)
    return replace(
        unit,
        authored_lines=tuple(authored),
        boundary_lines=tuple(boundary),
        commits=tuple(sorted(commits)),
    )
