#!/usr/bin/env python3
"""Enforce per-architecture Flatpak Python archive payload budgets."""

from __future__ import annotations

import argparse
from contextlib import suppress
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
from typing import Any
import urllib.parse
import urllib.request
import zipfile

import yaml


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "pypi-requirements.yaml"
BUDGETS = ROOT / "flatpak" / "python-payload-budgets.json"
MIB = 1024 * 1024
ALLOWED_HOST = "files.pythonhosted.org"


def rounded_budget(baseline: int, headroom_percent: int) -> int:
    numerator = baseline * (100 + headroom_percent)
    with_headroom = (numerator + 99) // 100
    return ((with_headroom + MIB - 1) // MIB) * MIB


def load_budget_contract(path: Path) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("metric") != "sum-uncompressed-archive-file-bytes":
        raise ValueError("unsupported payload metric")
    headroom = data.get("headroom_percent")
    architectures = data.get("architectures")
    if (
        not isinstance(headroom, int)
        or headroom < 0
        or not isinstance(architectures, dict)
    ):
        raise ValueError("invalid payload budget contract")

    budgets: dict[str, int] = {}
    for arch in ("x86_64", "aarch64"):
        values = architectures.get(arch)
        if not isinstance(values, dict):
            raise ValueError(f"missing payload budget for {arch}")
        baseline = values.get("baseline_bytes")
        budget = values.get("budget_bytes")
        if (
            not isinstance(baseline, int)
            or baseline < 0
            or not isinstance(budget, int)
            or budget < 0
        ):
            raise ValueError(f"invalid payload budget for {arch}")
        expected = rounded_budget(baseline, headroom)
        if budget != expected:
            raise ValueError(f"{arch} budget must be {expected} bytes by policy")
        budgets[arch] = budget
    return budgets


def selected_sources(manifest: dict[str, Any], arch: str) -> list[dict[str, Any]]:
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        raise ValueError("manifest sources must be a list")
    selected: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict) or source.get("type") != "file":
            raise ValueError("Python manifest sources must be file mappings")
        arches = source.get("only-arches")
        if arches is not None and not isinstance(arches, list):
            raise ValueError("source only-arches must be a list")
        if arches is None or arch in arches:
            selected.append(source)
    return selected


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise ValueError(f"archive URL must use https://{ALLOWED_HOST}: {url}")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError(f"archive URL contains forbidden authority fields: {url}")


def verify_archive(path: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as archive:
        for block in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected_sha256:
        raise ValueError(f"SHA-256 mismatch for {path.name}")


def fetch_archive(source: dict[str, Any], cache_dir: Path) -> Path:
    url = source.get("url")
    expected = source.get("sha256")
    if not isinstance(url, str) or not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("manifest source requires url and SHA-256")
    try:
        int(expected, 16)
    except ValueError as error:
        raise ValueError("manifest source SHA-256 must be hexadecimal") from error
    validate_url(url)
    destination = cache_dir / expected
    if not destination.is_file():
        temporary: Path | None = None
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as output:
                    while block := response.read(1024 * 1024):
                        output.write(block)
                    temporary = Path(output.name)
            verify_archive(temporary, expected)
            temporary.replace(destination)
        finally:
            if temporary is not None:
                with suppress(FileNotFoundError):
                    temporary.unlink()
    verify_archive(destination, expected)
    return destination


def archive_unpacked_size(path: Path, url: str) -> int:
    if url.endswith((".whl", ".zip")):
        with zipfile.ZipFile(path) as archive:
            return sum(item.file_size for item in archive.infolist() if not item.is_dir())
    with tarfile.open(path, mode="r:*") as archive:
        return sum(item.size for item in archive.getmembers() if item.isfile())


def measure(manifest_path: Path, cache_dir: Path) -> dict[str, int]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Python manifest must be a mapping")
    cache_dir.mkdir(parents=True, exist_ok=True)
    sizes: dict[str, int] = {}
    for arch in ("x86_64", "aarch64"):
        total = 0
        sources = selected_sources(manifest, arch)
        for source in sources:
            archive = fetch_archive(source, cache_dir)
            total += archive_unpacked_size(archive, source["url"])
        sizes[arch] = total
        print(f"{arch}: {len(sources)} archives, {total} bytes ({total / MIB:.2f} MiB)")
    return sizes


def budget_errors(sizes: dict[str, int], budgets: dict[str, int]) -> list[str]:
    errors = []
    for arch, size in sizes.items():
        budget = budgets[arch]
        if size > budget:
            errors.append(f"{arch} payload {size} bytes exceeds budget {budget} bytes")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--budgets", type=Path, default=BUDGETS)
    parser.add_argument(
        "--cache-dir", type=Path, default=ROOT / ".flatpak-builder" / "python-archives"
    )
    args = parser.parse_args()
    try:
        budgets = load_budget_contract(args.budgets)
        sizes = measure(args.manifest, args.cache_dir)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"error: {error}")
        return 1

    errors = budget_errors(sizes, budgets)
    for error in errors:
        print(f"error: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
