"""Integration contracts used by scenario_flatpak_python_manifest."""

from contextlib import redirect_stderr
import io
from pathlib import Path

import yaml

from flatpak.ci import generate_python_manifest as generator


def check_generator_commands(directory: Path) -> None:
    calls: list[list[str]] = []
    original_run = generator._run

    def fake_run(command: list[str]) -> None:
        calls.append(command)
        if "piptools" in command:
            output = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--output-file="))
            Path(output).write_text("demo==1\n", encoding="utf-8")
            return
        output = command[command.index("--outfile") + 1]
        Path(output).write_text(
            "name: generated\nbuild-commands:\n- install\nsources: []\n",
            encoding="utf-8",
        )

    generator._run = fake_run
    try:
        lock_path = directory / "runtime.lock"
        lock = generator.resolve_lock(
            generator.REQUIREMENTS, generator.LOCK, lock_path, upgrade=False
        )
        assert "demo==1" in lock
        partial = generator._generate_partial_manifest(lock_path, directory / "partial.yaml")
        assert partial["name"] == "generated"
    finally:
        generator._run = original_run

    compile_call, req2flatpak_call = calls
    assert compile_call[-1] == "requirements.txt"
    assert req2flatpak_call[-2:] == ["313-aarch64", "313-x86_64"]
    print("PASS: generator resolves a lock and requests both target architectures")


def check_complete_drift_mode(directory: Path) -> None:
    paths = {
        "LOCK": directory / "runtime.lock",
        "BUILD_LOCK": directory / "build.lock",
        "MANIFEST": directory / "manifest.yaml",
    }
    expected = {
        paths["LOCK"]: "runtime\n",
        paths["BUILD_LOCK"]: "build\n",
        paths["MANIFEST"]: "manifest\n",
    }
    for path, text in expected.items():
        path.write_text(text, encoding="utf-8")

    original_paths = {name: getattr(generator, name) for name in paths}
    original_resolve = generator.resolve_lock
    original_manifest = generator.generate_manifest
    original_version_check = generator.require_python_313

    def fake_resolve(
        requirements: Path, committed_lock: Path, destination: Path, *, upgrade: bool
    ) -> str:
        return expected[committed_lock]

    def fake_manifest(runtime_lock: Path, build_lock: Path, temp: Path) -> str:
        return expected[paths["MANIFEST"]]

    for name, path in paths.items():
        setattr(generator, name, path)
    generator.resolve_lock = fake_resolve
    generator.generate_manifest = fake_manifest
    generator.require_python_313 = lambda: None
    try:
        assert generator.generate(check=True, upgrade=False) == 0
        for path, text in expected.items():
            path.write_text("stale\n", encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                assert generator.generate(check=True, upgrade=False) == 1
            path.write_text(text, encoding="utf-8")
    finally:
        for name, path in original_paths.items():
            setattr(generator, name, path)
        generator.resolve_lock = original_resolve
        generator.generate_manifest = original_manifest
        generator.require_python_313 = original_version_check
    print("PASS: check mode covers both locks and the generated manifest")


def check_ci_contract(root: Path) -> None:
    config = yaml.safe_load((root / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    build = config["build:flatpak"]
    manifest_test = config["test:flatpak-python"]
    changed_paths = {
        path
        for rule in manifest_test["rules"]
        for path in rule.get("changes", [])
    }
    assert {"requirements.txt", "pypi-requirements.yaml", "flatpak/**/*"} <= changed_paths
    assert manifest_test["image"] == "python:3.13-slim"
    assert manifest_test["rules"] == build["rules"][: len(manifest_test["rules"])]

    script = manifest_test["script"]
    system_deps = next(i for i, command in enumerate(script) if "libgirepository" in command)
    tool_install = next(i for i, command in enumerate(script) if "pip install" in command)
    generation = next(i for i, command in enumerate(script) if "--check" in command)
    payload = next(i for i, command in enumerate(script) if "check_python_payload.py" in command)
    assert system_deps < tool_install < generation < payload

    build_need = next(need for need in build["needs"] if need["job"] == "test:flatpak-python")
    assert build_need == {
        "job": "test:flatpak-python",
        "artifacts": False,
        "optional": True,
    }

    release_needs = {need["job"] for need in config["release:gate"]["needs"]}
    assert "build:flatpak" in release_needs
    print("PASS: manifest inputs trigger the Python 3.13 gate before the release build")
