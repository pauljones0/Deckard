#!/usr/bin/env python3
"""Verify that release and tag writers cannot bypass static CI gates.
Normalize quoted keys, flow mappings, aliases, and merges with duplicate-safe YAML; reject external includes that an offline check cannot verify."""
from __future__ import annotations

import copy
import sys
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import Never, cast, override

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_CONFIG = REPO_ROOT / ".gitlab-ci.yml"
REQUIRED_GATES = frozenset({
    "test:compile", "lint:ruff", "lint:types", "test:scenario-smoke", "build:flatpak",
})
MUTATION_JOBS = frozenset({"release:gitlab", "release:github", "auto-release", "tag-release"})
RELEASE_GATE = "release:gate"
PROTECTED_JOBS = REQUIRED_GATES | MUTATION_JOBS | {RELEASE_GATE}
GLOBAL_KEYS = frozenset({
    "after_script", "before_script", "cache", "default", "hooks", "image", "include",
    "services", "stages", "variables", "workflow",
})
INHERITED_CONTROL_KEYS = frozenset({"allow_failure", "needs", "rules", "when"})
SAFE_RELEASE_WHEN = frozenset({"manual", "never", "on_success"})
SAFE_STATIC_WHEN = frozenset({"manual", "on_success"})
LOCAL_NEED_KEYS = frozenset({"artifacts", "job", "optional"})


class ContractFailure(Exception):
    """The CI dependency graph does not meet the release contract."""


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a duplicate mapping key after merge expansion."""

    @override
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[object, object]:
        self.flatten_mapping(node)
        mapping: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, Hashable):
                raise ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "found an unhashable mapping key", key_node.start_mark,
                )
            if key in mapping:
                raise ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"found duplicate key {key!r}", key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=True)
        return mapping


@dataclass(frozen=True)
class Need:
    job: str
    artifacts: bool
    optional: bool


def fail(message: str) -> Never:
    raise ContractFailure(message)


def require_mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        fail(f"{context} must be a mapping")
    raw = cast(dict[object, object], value)
    for key in raw:
        if not isinstance(key, str):
            fail(f"{context} has a non-string mapping key: {key!r}")
    return cast(dict[str, object], raw)


def require_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        fail(f"{context} must be a list")
    return cast(list[object], value)


def require_bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        fail(f"{context} must be true or false")
    return value


def require_string(value: object, context: str) -> str:
    if not isinstance(value, str):
        fail(f"{context} must be a string")
    return value


def load_config(text: str) -> dict[str, object]:
    """Load one root CI file, rejecting duplicate keys and custom YAML tags."""
    try:
        loaded = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as error:
        fail(f"CI YAML cannot be safely normalized: {error}")
    return require_mapping(loaded, "root CI config")


def job_mapping(config: dict[str, object], name: str) -> dict[str, object]:
    if name not in config:
        fail(f"missing required job/template: {name}")
    return require_mapping(config[name], f"job/template {name}")


def job_mappings(config: dict[str, object]) -> dict[str, dict[str, object]]:
    jobs: dict[str, dict[str, object]] = {}
    for name, value in config.items():
        if name in GLOBAL_KEYS or name.startswith("."):
            continue
        if isinstance(value, dict):
            jobs[name] = require_mapping(value, f"job {name}")
    return jobs


def parse_need(item: object, context: str, *, local_only: bool) -> Need:
    if isinstance(item, str):
        return Need(item, artifacts=True, optional=False)
    data = require_mapping(item, context)
    if local_only:
        unsupported = sorted(set(data) - LOCAL_NEED_KEYS)
        if unsupported:
            fail(f"{context} has unsupported non-local need key(s): {', '.join(unsupported)}")
    if "job" not in data:
        fail(f"{context} has no job")
    job = require_string(data["job"], f"{context}.job")
    artifacts = require_bool(data["artifacts"], f"{context}.artifacts") if "artifacts" in data else True
    optional = require_bool(data["optional"], f"{context}.optional") if "optional" in data else False
    return Need(job, artifacts, optional)


def parse_needs(job: str, data: dict[str, object]) -> list[Need]:
    if "needs" not in data:
        return []
    raw = data["needs"]
    items = [raw] if isinstance(raw, dict) else require_list(raw, f"{job}.needs")
    local_only = job in PROTECTED_JOBS
    return [
        parse_need(item, f"{job}.needs[{index}]", local_only=local_only)
        for index, item in enumerate(items)
    ]


def dependency_graph(jobs: dict[str, dict[str, object]]) -> dict[str, list[Need]]:
    return {name: parse_needs(name, data) for name, data in jobs.items()}


def check_known_jobs(graph: dict[str, list[Need]]) -> None:
    required = REQUIRED_GATES | MUTATION_JOBS | {RELEASE_GATE}
    missing = sorted(required - graph.keys())
    if missing:
        fail(f"missing required job(s): {', '.join(missing)}")
    unknown = sorted(
        f"{source} -> {need.job}"
        for source, needs in graph.items()
        for need in needs
        if need.job not in graph
    )
    if unknown:
        fail(f"needs references unknown job(s): {', '.join(unknown)}")


def check_no_cycles(graph: dict[str, list[Need]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(job: str, trail: list[str]) -> None:
        if job in visiting:
            cycle = trail[trail.index(job):] + [job]
            fail(f"needs graph contains a cycle: {' -> '.join(cycle)}")
        if job in visited:
            return
        visiting.add(job)
        for need in graph[job]:
            visit(need.job, [*trail, job])
        visiting.remove(job)
        visited.add(job)

    for job in graph:
        visit(job, [])


def matching_needs(needs: list[Need], name: str) -> list[Need]:
    return [need for need in needs if need.job == name]


def check_release_barrier(graph: dict[str, list[Need]]) -> None:
    gate_needs = graph[RELEASE_GATE]
    for required in sorted(REQUIRED_GATES):
        matching = matching_needs(gate_needs, required)
        if not matching:
            fail(f"{RELEASE_GATE} bypasses required gate: {required}")
        if any(need.optional for need in matching):
            fail(f"{RELEASE_GATE} makes required gate optional: {required}")
        if any(need.artifacts for need in matching):
            fail(f"{RELEASE_GATE} must not download artifacts from {required}")

    for job in sorted(MUTATION_JOBS):
        matching = matching_needs(graph[job], RELEASE_GATE)
        if not matching:
            fail(f"{job} does not depend directly on {RELEASE_GATE}")
        if any(need.optional for need in matching):
            fail(f"{job} makes {RELEASE_GATE} optional")


def check_bundle_artifact_flow(graph: dict[str, list[Need]]) -> None:
    for job in ("release:gitlab", "release:github"):
        matching = matching_needs(graph[job], "build:flatpak")
        if not matching or any(not need.artifacts or need.optional for need in matching):
            fail(f"{job} must keep a direct artifact dependency on build:flatpak")


def extends_names(job: str, data: dict[str, object]) -> list[str]:
    if "extends" not in data:
        return []
    raw = data["extends"]
    if isinstance(raw, str):
        return [raw]
    return [require_string(parent, f"{job}.extends") for parent in require_list(raw, f"{job}.extends")]


def check_inherited_controls(job: str, config: dict[str, object], trail: tuple[str, ...] = ()) -> None:
    data = job_mapping(config, job)
    for parent in extends_names(job, data):
        if parent in trail or parent == job:
            fail(f"extends graph contains a cycle at {parent}")
        inherited = job_mapping(config, parent)
        controls = sorted(INHERITED_CONTROL_KEYS & inherited.keys())
        if controls:
            fail(f"{job} extends {parent}, whose inherited control(s) are unsupported: {', '.join(controls)}")
        check_inherited_controls(parent, config, (*trail, job))


def allow_failure(data: dict[str, object], context: str) -> bool | None:
    if "allow_failure" not in data:
        return None
    return require_bool(data["allow_failure"], f"{context}.allow_failure")


def when(data: dict[str, object], context: str) -> str | None:
    if "when" not in data:
        return None
    return require_string(data["when"], f"{context}.when")


def rules(data: dict[str, object], context: str) -> list[dict[str, object]]:
    if "rules" not in data:
        return []
    return [
        require_mapping(rule, f"{context}.rules[{index}]")
        for index, rule in enumerate(require_list(data["rules"], f"{context}.rules"))
    ]


def merge_request_only(rule: dict[str, object]) -> bool:
    return rule.get("if") == '$CI_PIPELINE_SOURCE == "merge_request_event"'


def check_execution(job: str, data: dict[str, object]) -> None:
    release_context = job == RELEASE_GATE or job in MUTATION_JOBS
    static_gate = job in REQUIRED_GATES
    direct_allow_failure = allow_failure(data, job)
    if direct_allow_failure is True:
        fail(f"{job} declares optional execution")

    direct_when = when(data, job)
    if direct_when == "manual" and direct_allow_failure is not False:
        fail(f"{job} is a top-level manual job, which is optional by default")
    if release_context and direct_when is not None and direct_when not in SAFE_RELEASE_WHEN:
        fail(f"{job} uses unsupported release execution mode: {direct_when}")
    if static_gate and direct_when is not None and direct_when not in SAFE_STATIC_WHEN:
        fail(f"{job} can skip a successful release path: {direct_when}")

    for index, rule in enumerate(rules(data, job)):
        context = f"{job}.rules[{index}]"
        if release_context and "needs" in rule:
            fail(f"{context} can replace the validated top-level needs")
        rule_when = when(rule, context)
        if release_context and rule_when is not None and rule_when not in SAFE_RELEASE_WHEN:
            fail(f"{context} uses unsupported release execution mode: {rule_when}")
        if static_gate and rule_when is not None and rule_when not in SAFE_STATIC_WHEN:
            fail(f"{context} can skip a successful release path: {rule_when}")
        if allow_failure(rule, context) is True and not merge_request_only(rule):
            fail(f"{context} declares optional execution")


def check_smoke_selection(config: dict[str, object]) -> None:
    # The blocking smoke job must use the tracked scenario list.
    # The needs graph cannot detect a cleared or retargeted SCENARIO_LIST.
    smoke = job_mapping(config, "test:scenario-smoke")
    variables = require_mapping(
        smoke.get("variables", {}), "test:scenario-smoke variables")
    selection = variables.get("SCENARIO_LIST")
    if selection != "tests/scenario-smoke.txt":
        fail(
            "test:scenario-smoke must select SCENARIO_LIST="
            f"tests/scenario-smoke.txt, found {selection!r}")


def check_contract(config: dict[str, object]) -> None:
    if "include" in config:
        fail("root CI config includes external configuration")
    jobs = job_mappings(config)
    graph = dependency_graph(jobs)
    check_known_jobs(graph)
    check_no_cycles(graph)
    check_release_barrier(graph)
    check_bundle_artifact_flow(graph)
    check_smoke_selection(config)
    for job in sorted(PROTECTED_JOBS):
        data = job_mapping(config, job)
        check_inherited_controls(job, config)
        check_execution(job, data)


def check_config_text(text: str) -> dict[str, object]:
    config = load_config(text)
    check_contract(config)
    return config


def assert_rejected(config: dict[str, object], expected: str) -> None:
    try:
        check_contract(config)
    except ContractFailure:
        return
    fail(f"self-test did not reject {expected}")


def assert_rejected_text(text: str, expected: str) -> None:
    try:
        check_config_text(text)
    except ContractFailure:
        return
    fail(f"self-test did not reject {expected}")


def assert_accepted(config: dict[str, object], expected: str) -> None:
    try:
        check_contract(config)
    except ContractFailure as error:
        fail(f"self-test rejected {expected}: {error}")



def find_need(data: dict[str, object], name: str) -> dict[str, object]:
    for item in require_list(data["needs"], "self-test needs"):
        candidate = require_mapping(item, "self-test need")
        if candidate.get("job") == name:
            return candidate
    fail(f"self-test could not find need {name}")


def first_rule(data: dict[str, object]) -> dict[str, object]:
    parsed = rules(data, "self-test")
    if not parsed:
        fail("self-test could not find a rule")
    return parsed[0]


def self_test(config: dict[str, object], text: str) -> None:
    without_writer_barrier = copy.deepcopy(config)
    job_mapping(without_writer_barrier, "tag-release")["needs"] = []
    assert_rejected(without_writer_barrier, "a tag writer that bypasses the barrier")

    without_type_gate = copy.deepcopy(config)
    gate = job_mapping(without_type_gate, RELEASE_GATE)
    gate["needs"] = [
        item for item in require_list(gate["needs"], "self-test gate needs")
        if require_mapping(item, "self-test gate need").get("job") != "lint:types"
    ]
    assert_rejected(without_type_gate, "a release barrier that bypasses the type gate")

    without_scenario_smoke = copy.deepcopy(config)
    gate = job_mapping(without_scenario_smoke, RELEASE_GATE)
    gate["needs"] = [
        item for item in require_list(gate["needs"], "self-test gate needs")
        if require_mapping(item, "self-test gate need").get("job") != "test:scenario-smoke"
    ]
    assert_rejected(without_scenario_smoke, "a release barrier that bypasses scenario smoke")

    retargeted_smoke = copy.deepcopy(config)
    smoke_vars = require_mapping(
        job_mapping(retargeted_smoke, "test:scenario-smoke")["variables"],
        "self-test smoke variables")
    smoke_vars["SCENARIO_LIST"] = ""
    assert_rejected(retargeted_smoke, "a smoke job with its tracked list cleared")

    without_bundle = copy.deepcopy(config)
    publisher = job_mapping(without_bundle, "release:gitlab")
    publisher["needs"] = [
        item for item in require_list(publisher["needs"], "self-test publisher needs")
        if require_mapping(item, "self-test publisher need").get("job") != "build:flatpak"
    ]
    assert_rejected(without_bundle, "a release publisher without the bundle artifact")

    cyclic = copy.deepcopy(config)
    require_list(job_mapping(cyclic, "test:compile")["needs"], "self-test compile needs").append({
        "job": RELEASE_GATE,
        "artifacts": False,
    })
    assert_rejected(cyclic, "a cyclic needs graph")

    optional_static_gate = copy.deepcopy(config)
    job_mapping(optional_static_gate, "lint:ruff")["allow_failure"] = True
    assert_rejected(optional_static_gate, "an optional static gate")

    manual_static_gate = copy.deepcopy(config)
    job_mapping(manual_static_gate, "lint:ruff")["when"] = "manual"
    assert_rejected(manual_static_gate, "a top-level manual static gate")

    failed_static_gate = copy.deepcopy(config)
    job_mapping(failed_static_gate, "lint:ruff")["when"] = "on_failure"
    assert_rejected(failed_static_gate, "a failure-triggered static gate")

    skipped_static_gate = copy.deepcopy(config)
    job_mapping(skipped_static_gate, "lint:ruff")["rules"] = [{
        "if": "$CI_COMMIT_TAG",
        "when": "never",
    }]
    assert_rejected(skipped_static_gate, "a rule-skipped static gate")

    skipped_release_build = copy.deepcopy(config)
    first_rule(job_mapping(skipped_release_build, "build:flatpak"))["when"] = "never"
    assert_rejected(skipped_release_build, "a rule-skipped release build")

    optional_release_gate_rule = copy.deepcopy(config)
    first_rule(job_mapping(optional_release_gate_rule, RELEASE_GATE))["allow_failure"] = True
    assert_rejected(optional_release_gate_rule, "an optional release barrier rule")

    blocking_manual_release_gate = copy.deepcopy(config)
    gate = job_mapping(blocking_manual_release_gate, RELEASE_GATE)
    gate["when"] = "manual"
    gate["allow_failure"] = False
    assert_accepted(blocking_manual_release_gate, "a blocking top-level manual release barrier")

    always_release_gate = copy.deepcopy(config)
    job_mapping(always_release_gate, RELEASE_GATE)["when"] = "always"
    assert_rejected(always_release_gate, "an always-running release barrier")

    always_release_gate_rule = copy.deepcopy(config)
    first_rule(job_mapping(always_release_gate_rule, RELEASE_GATE))["when"] = "always"
    assert_rejected(always_release_gate_rule, "an always-running release barrier rule")

    optional_tag_writer_rule = copy.deepcopy(config)
    first_rule(job_mapping(optional_tag_writer_rule, "tag-release"))["allow_failure"] = True
    assert_rejected(optional_tag_writer_rule, "an optional tag writer rule")

    failed_tag_writer = copy.deepcopy(config)
    job_mapping(failed_tag_writer, "tag-release")["when"] = "on_failure"
    assert_rejected(failed_tag_writer, "a failure-triggered tag writer")

    failed_tag_writer_rule = copy.deepcopy(config)
    first_rule(job_mapping(failed_tag_writer_rule, "tag-release"))["when"] = "on_failure"
    assert_rejected(failed_tag_writer_rule, "a failure-triggered tag writer rule")

    conditional_gate_needs = copy.deepcopy(config)
    first_rule(job_mapping(conditional_gate_needs, RELEASE_GATE))["needs"] = []
    assert_rejected(conditional_gate_needs, "a conditional release barrier needs override")

    conditional_tag_needs = copy.deepcopy(config)
    first_rule(job_mapping(conditional_tag_needs, "tag-release"))["needs"] = []
    assert_rejected(conditional_tag_needs, "a conditional tag writer needs override")

    optional_static_dependency = copy.deepcopy(config)
    find_need(job_mapping(optional_static_dependency, RELEASE_GATE), "lint:types")["optional"] = True
    assert_rejected(optional_static_dependency, "an optional required static dependency")

    inherited_optional_release = copy.deepcopy(config)
    job_mapping(inherited_optional_release, ".release")["allow_failure"] = True
    assert_rejected(inherited_optional_release, "an optional inherited release template")

    inherited_rules_needs = copy.deepcopy(config)
    job_mapping(inherited_rules_needs, ".release")["rules"] = [{"if": "$CI_COMMIT_BRANCH", "needs": []}]
    assert_rejected(inherited_rules_needs, "an inherited release template with rules needs")

    assert_rejected_text("include: child.yml\n" + text, "an external CI include")
    assert_rejected_text(
        text.replace('needs: ["release:gate"]', 'needs: {job: release:gate, optional: true}', 1),
        "an optional unquoted flow need",
    )
    assert_rejected_text(
        text.replace(
            'needs: ["release:gate"]',
            'needs: [{job: release:gate, project: example/deckard}]',
            1,
        ),
        "a cross-pipeline release-barrier need",
    )
    assert_rejected_text(
        text.replace(
            "  rules:\n    - if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH'\n",
            "  rules: [{if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH', needs: []}]\n",
            1,
        ),
        "an unquoted flow rules needs override",
    )
    assert_rejected_text(
        text.replace("lint:ruff:\n  stage:", 'lint:ruff:\n  "allow_failure": true\n  stage:', 1),
        "a quoted static-gate control key",
    )
    assert_rejected_text("'include': child.yml\n" + text, "a quoted include key")
    assert_rejected_text(
        "release-controls: &release_controls\n  when: always\n"
        + text.replace("tag-release:\n", "tag-release:\n  <<: *release_controls\n", 1),
        "an anchored template merged into a tag writer",
    )
    assert_rejected_text(
        "release-mode: &release_mode on_failure\n"
        + text.replace("tag-release:\n", "tag-release:\n  when: *release_mode\n", 1),
        "a scalar alias in a tag writer",
    )
    assert_rejected_text(text + "\nself-test: !reference [job, needs]\n", "a !reference custom tag")
    assert_rejected_text(
        text + "\nself-test-rule:\n  rules:\n    - if: condition\n      when: on_success\n      when: always\n",
        "a duplicate key within a rule",
    )
    assert_rejected_text(
        text + "\nself-test-needs:\n  needs:\n    - job: test:compile\n      optional: false\n      optional: true\n",
        "a duplicate key within a needs item",
    )
    assert_rejected_text(text + "\nworkflow: {}\n", "a duplicate root key")


def main(arguments: list[str]) -> int:
    if arguments not in ([], ["--self-test"]):
        fail("usage: scripts/check_ci_release_gates.py [--self-test]")
    if not CI_CONFIG.is_file():
        fail(".gitlab-ci.yml is missing")
    text = CI_CONFIG.read_text(encoding="utf-8")
    config = check_config_text(text)
    if arguments:
        self_test(config, text)
    print("release-gate contract: static and smoke gates block every release and tag writer.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ContractFailure as error:
        print(f"release-gate contract: {error}", file=sys.stderr)
        raise SystemExit(1) from None
