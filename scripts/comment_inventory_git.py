from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from comment_inventory_core import (
    InventoryError,
    Unit,
    attribute_unit,
    decode_python,
    extract_units,
)


HEADER_RE = re.compile(r"^([0-9a-f]{40,64}) \d+ (\d+)(?: \d+)?$")


@dataclass(frozen=True)
class Inventory:
    revision: str
    boundary: str
    tracked_files: tuple[str, ...]
    all_units: tuple[Unit, ...]
    attributed_units: tuple[Unit, ...]


def blame_arguments(boundary: str, revision: str, path: str) -> tuple[str, ...]:
    return (
        "blame",
        "--line-porcelain",
        "-M",
        "-C",
        "-C",
        f"{boundary}..{revision}",
        "--",
        path,
    )


def parse_blame(path: str, output: bytes, expected_lines: int) -> dict[int, tuple[bool, str]]:
    result: dict[int, tuple[bool, str]] = {}
    commit = ""
    final_line = 0
    boundary = False
    for text in output.decode("utf-8", "replace").splitlines():
        header = HEADER_RE.match(text)
        if header:
            commit = header.group(1)
            final_line = int(header.group(2))
            boundary = False
        elif text == "boundary":
            boundary = True
        elif text.startswith("\t"):
            if not commit or final_line < 1 or final_line in result:
                raise InventoryError(f"{path}: malformed or duplicate blame record")
            result[final_line] = (boundary, commit)

    expected = set(range(1, expected_lines + 1))
    if set(result) != expected:
        missing = sorted(expected - set(result))
        extra = sorted(set(result) - expected)
        raise InventoryError(f"{path}: incomplete blame; missing={missing[:5]} extra={extra[:5]}")
    return result


class GitRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _execute(
        self,
        args: tuple[str, ...],
        failure_message: Callable[[str], str],
    ) -> bytes:
        try:
            process = subprocess.run(
                ["git", "-C", str(self.root), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as error:
            raise InventoryError(f"cannot execute git: {error}") from error
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", "replace").strip()
            raise InventoryError(failure_message(detail))
        return process.stdout

    def _run(self, *args: str) -> bytes:
        return self._execute(
            args,
            lambda detail: f"git {' '.join(args)} failed: {detail}",
        )

    def resolve_commit(self, revision: str) -> str:
        return self._run("rev-parse", "--verify", f"{revision}^{{commit}}").decode().strip()

    def require_ancestor(self, boundary: str, revision: str) -> None:
        self._execute(
            ("merge-base", "--is-ancestor", boundary, revision),
            lambda detail: (
                f"fork boundary {boundary} is not an ancestor of {revision}: {detail}"
            ),
        )

    def tracked_python(self, revision: str) -> list[str]:
        output = self._run("ls-tree", "-r", "-z", "--name-only", revision)
        try:
            paths = [item.decode("utf-8") for item in output.split(b"\0") if item]
        except UnicodeError as error:
            raise InventoryError(f"{revision}: tracked path is not UTF-8: {error}") from error
        return sorted(path for path in paths if path.endswith(".py"))

    def read_blob(self, revision: str, path: str) -> bytes:
        return self._run("show", f"{revision}:{path}")

    def blame(self, boundary: str, revision: str, path: str, lines: int) -> dict[int, tuple[bool, str]]:
        output = self._run(*blame_arguments(boundary, revision, path))
        return parse_blame(path, output, lines)

    def inventory(self, revision: str, boundary: str, jobs: int = 8) -> Inventory:
        revision = self.resolve_commit(revision)
        boundary = self.resolve_commit(boundary)
        self.require_ancestor(boundary, revision)
        paths = self.tracked_python(revision)
        all_units: list[Unit] = []
        line_counts: dict[str, int] = {}
        for path in paths:
            source = decode_python(path, self.read_blob(revision, path))
            line_counts[path] = len(source.splitlines())
            all_units.extend(extract_units(path, source))

        with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
            blame_maps = dict(
                zip(
                    paths,
                    executor.map(
                        lambda path: self.blame(
                            boundary, revision, path, line_counts[path]
                        ),
                        paths,
                    ),
                    strict=True,
                )
            )
        attributed = [attribute_unit(unit, blame_maps[unit.path]) for unit in all_units]
        corpus = tuple(unit for unit in attributed if unit.authored_lines)
        return Inventory(revision, boundary, tuple(paths), tuple(all_units), corpus)
