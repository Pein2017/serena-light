"""Render and gate the deterministic Section-1 serena-light admission report.

This command consumes only recorded evidence.  It does not run a bootstrap,
readiness probe, or source census itself, so its output is reproducible from
the four explicit JSON inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

REQUIRED_PROFILES = ("transformers", "coordexp", "ms-swift", "cc-plugin-codex")
PROFILE_CONTRACTS = {
    "transformers": (
        "/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers",
        "python",
        "Qwen2VLForConditionalGeneration",
        "models/qwen2_vl/modeling_qwen2_vl.py",
    ),
    "coordexp": ("/data/CoordExp", "python", "PipelinePlanner", "public_data/pipeline/planner.py"),
    "ms-swift": ("/data/ms-swift", "python", "SwiftPipeline", "swift/pipelines/base.py"),
    "cc-plugin-codex": (
        "/data/CoordExp/cc-plugin-codex",
        "typescript",
        "createAgentStore",
        "runtime/agent-store.mjs",
    ),
}
KNOWN_POSITION_ENCODINGS = frozenset({"utf-8", "utf-16", "utf-32"})
MAX_READY_SECONDS = 30.0
LOCK_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class AdmissionError(ValueError):
    """Raised when an admission input is malformed or fails a required gate."""


def _path_digest(paths: list[str]) -> str:
    return hashlib.sha256("\0".join(paths).encode()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdmissionError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdmissionError(f"{path}: expected a JSON object")
    return value


def _require_pass(document: dict[str, Any], label: str) -> None:
    if document.get("status") != "pass":
        raise AdmissionError(f"{label} status is not pass: {document.get('status')!r}")


def _require_no_failed_gates(value: Any, label: str) -> None:
    """Reject explicit gate fields that are false or non-passing.

    The probes currently expose their gate via ``status``.  This also keeps the
    report fail-closed if a later schema adds a named ``*_gate`` result.
    """

    if isinstance(value, dict):
        for key, child in value.items():
            if (key == "gate" or key.endswith("_gate")) and child is not True and child != "pass":
                raise AdmissionError(f"{label} {key} failed: {child!r}")
            _require_no_failed_gates(child, label)
    elif isinstance(value, list):
        for child in value:
            _require_no_failed_gates(child, label)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AdmissionError(f"{label} must be a number")
    return float(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AdmissionError(f"{label} must be a non-empty string")
    return value


def _validate_bootstrap(bootstrap: dict[str, Any]) -> None:
    if "status" in bootstrap:
        _require_pass(bootstrap, "bootstrap")
    _require_no_failed_gates(bootstrap, "bootstrap")
    digest = _required_string(bootstrap.get("lock_digest"), "bootstrap lock_digest")
    if not LOCK_DIGEST_RE.fullmatch(digest):
        raise AdmissionError("bootstrap lock_digest is not a SHA-256 digest")
    _required_string(bootstrap.get("runtime"), "bootstrap runtime")
    paths = bootstrap.get("paths")
    versions = bootstrap.get("versions")
    if not isinstance(paths, dict) or not paths:
        raise AdmissionError("bootstrap paths must be a non-empty object")
    if not isinstance(versions, dict) or not versions:
        raise AdmissionError("bootstrap versions must be a non-empty object")
    for name, path in paths.items():
        _required_string(name, "bootstrap path name")
        _required_string(path, f"bootstrap path {name}")
    for name, version in versions.items():
        _required_string(name, "bootstrap version name")
        _required_string(version, f"bootstrap version {name}")
    expected_versions = {
        "node": "22.22.0",
        "npm": "11.13.0",
        "pyright": "1.1.403",
        "typescript-language-server": "5.1.3",
        "typescript": "5.9.3",
    }
    for name, expected in expected_versions.items():
        if versions.get(name) != expected:
            raise AdmissionError(f"bootstrap {name} version drift: {versions.get(name)!r} != {expected!r}")
    for name in ("node", "npm", "pyright-langserver", "typescript-language-server", "tsserver"):
        path = _required_string(paths.get(name), f"bootstrap path {name}")
        if not path.startswith(str(bootstrap["runtime"]) + "/") or path.startswith("/root/.nvm/"):
            raise AdmissionError(f"bootstrap path {name} is not runtime-owned: {path}")


def _validate_source_budget(source_budget: dict[str, Any]) -> None:
    _require_pass(source_budget, "source budget")
    _require_no_failed_gates(source_budget, "source budget")
    expected = _number(source_budget.get("expected_production_lines"), "expected_production_lines")
    maximum = _number(source_budget.get("maximum_production_lines"), "maximum_production_lines")
    current = _number(source_budget.get("current_local_production_lines"), "current_local_production_lines")
    if expected > maximum or current > maximum:
        raise AdmissionError("source budget exceeds its maximum production-line budget")


def _validate_ts_scope(scope: dict[str, Any]) -> None:
    _require_pass(scope, "TypeScript scope")
    _require_no_failed_gates(scope, "TypeScript scope")
    if scope.get("schema_version") != 3:
        raise AdmissionError("TypeScript scope must use schema_version 3")
    if scope.get("overlay_generated") is not False:
        raise AdmissionError("TypeScript scope generated an overlay")
    checks = scope.get("checks")
    if not isinstance(checks, dict) or set(checks) != {"ignored_subtree_fixture", "cc_plugin_codex"}:
        raise AdmissionError("TypeScript scope must contain both schema-v3 checks")
    for name, raw_check in checks.items():
        if not isinstance(raw_check, dict):
            raise AdmissionError(f"TypeScript scope check {name} must be an object")
        inventory = raw_check.get("git_inventory")
        program = raw_check.get("tsserver_program")
        omitted = raw_check.get("trusted_not_in_configured_program")
        outside = raw_check.get("configured_program_outside_trust")
        if not all(isinstance(value, list) for value in (inventory, program, omitted, outside)):
            raise AdmissionError(f"TypeScript scope check {name} inventories must be lists")
        inventory_set = set(map(str, inventory))
        program_set = set(map(str, program))
        if sorted(inventory_set - program_set) != sorted(map(str, omitted)):
            raise AdmissionError(f"TypeScript scope check {name} has an invalid trusted omission projection")
        if sorted(program_set - inventory_set) != sorted(map(str, outside)):
            raise AdmissionError(f"TypeScript scope check {name} has an invalid outside-trust projection")
        reasons = raw_check.get("difference_reasons")
        if not isinstance(reasons, dict):
            raise AdmissionError(f"TypeScript scope check {name} lacks difference reasons")
        for key, paths in (
            ("trusted_not_in_configured_program", omitted),
            ("configured_program_outside_trust", outside),
        ):
            raw_reasons = reasons.get(key)
            if not isinstance(raw_reasons, list) or sorted(
                str(item.get("path")) for item in raw_reasons if isinstance(item, dict)
            ) != sorted(map(str, paths)):
                raise AdmissionError(f"TypeScript scope check {name} has invalid reasons for {key}")
        evidence = raw_check.get("path_set_evidence")
        if not isinstance(evidence, dict):
            raise AdmissionError(f"TypeScript scope check {name} lacks path-set evidence")
        for key, paths in (
            ("git_inventory", inventory),
            ("tsserver_program", program),
            ("trusted_not_in_configured_program", omitted),
            ("configured_program_outside_trust", outside),
        ):
            if evidence.get(key) != {"count": len(paths), "sha256": _path_digest(list(map(str, paths)))}:
                raise AdmissionError(f"TypeScript scope check {name} has invalid {key} evidence")
        rejected_program = raw_check.get("configured_program_rejected")
        if not isinstance(rejected_program, list):
            raise AdmissionError(f"TypeScript scope check {name} lacks rejected-program evidence")
        if (
            raw_check.get("comparison_basis") != "normalized_path_sets"
            or raw_check.get("count_only_equivalence_rejected") is not True
            or raw_check.get("overlay_generated") is not False
        ):
            raise AdmissionError(f"TypeScript scope check {name} lacks normalized path-set proof")
        compatible = not outside and not rejected_program
        if raw_check.get("scope_compatible") is not compatible:
            raise AdmissionError(f"TypeScript scope check {name} has inconsistent compatibility")
        error = raw_check.get("error")
        if compatible and error is not None:
            raise AdmissionError(f"TypeScript scope check {name} has an unexpected error")
        if not compatible and (not isinstance(error, dict) or error.get("code") != "SCOPE_INCOMPATIBLE"):
            raise AdmissionError(f"TypeScript scope check {name} lacks SCOPE_INCOMPATIBLE")
        if raw_check.get("project_kind") not in {"configured", "inferred"}:
            raise AdmissionError(f"TypeScript scope check {name} has an invalid project kind")
        if raw_check.get("project_kind") == "configured":
            _required_string(raw_check.get("selected_config_path"), f"TypeScript scope check {name} config path")
        if raw_check.get("cleanup_ok") is not True:
            raise AdmissionError(f"TypeScript scope check {name} did not clean up tsserver")

    fixture = checks["ignored_subtree_fixture"]
    actual = checks["cc_plugin_codex"]
    if fixture.get("configured_program_outside_trust") != ["ignored-generated/hidden.ts"]:
        raise AdmissionError("TypeScript scope fixture did not detect its ignored source")
    if fixture.get("scope_compatible") is not False:
        raise AdmissionError("TypeScript scope fixture must be incompatible")
    if actual.get("scope_compatible") is not True:
        raise AdmissionError("TypeScript real root contains configured files outside trust")
    path_scoped = actual.get("path_scoped_omission_probe")
    if not isinstance(path_scoped, dict) or (
        path_scoped.get("status") != "pass"
        or path_scoped.get("service_supported") is not True
        or path_scoped.get("engine_owned") is not True
        or path_scoped.get("project_kind") != "inferred"
        or path_scoped.get("configured_program_unchanged") is not True
        or path_scoped.get("global_scope_expanded") is not False
        or path_scoped.get("read_only") is not True
    ):
        raise AdmissionError("TypeScript omitted-file path-scoped proof did not pass")


def _validate_readiness(readiness: dict[str, Any]) -> list[dict[str, Any]]:
    _require_pass(readiness, "readiness")
    _require_no_failed_gates(readiness, "readiness")
    timeout = _number(readiness.get("timeout_seconds"), "readiness timeout_seconds")
    if timeout > MAX_READY_SECONDS:
        raise AdmissionError(f"readiness timeout_seconds exceeds {MAX_READY_SECONDS:g}")
    results = readiness.get("results")
    if not isinstance(results, list):
        raise AdmissionError("readiness results must be a list")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    typed_results: list[dict[str, Any]] = []
    for index, raw_result in enumerate(results):
        if not isinstance(raw_result, dict):
            raise AdmissionError(f"readiness result {index} must be an object")
        result = cast("dict[str, Any]", raw_result)
        if result.get("status") != "pass":
            raise AdmissionError(f"readiness result {index} did not pass")
        profile = _required_string(result.get("profile"), f"readiness result {index} profile")
        run = result.get("run")
        if isinstance(run, bool) or not isinstance(run, int) or run < 1:
            raise AdmissionError(f"readiness result {index} run must be a positive integer")
        _number(result.get("initialize_seconds"), f"readiness result {index} initialize_seconds")
        ready_seconds = _number(result.get("global_ready_seconds"), f"readiness result {index} global_ready_seconds")
        _number(result.get("query_seconds"), f"readiness result {index} query_seconds")
        _number(result.get("result_count"), f"readiness result {index} result_count")
        if ready_seconds > MAX_READY_SECONDS:
            raise AdmissionError(f"readiness result {index} exceeds {MAX_READY_SECONDS:g} seconds")
        encoding = result.get("position_encoding")
        if encoding not in KNOWN_POSITION_ENCODINGS:
            raise AdmissionError(f"readiness result {index} has unknown position encoding: {encoding!r}")
        for field in ("root", "symbol", "language", "position_encoding_source"):
            _required_string(result.get(field), f"readiness result {index} {field}")
        if profile not in PROFILE_CONTRACTS:
            raise AdmissionError(f"unexpected readiness profile: {profile}")
        expected_root, expected_language, expected_symbol, expected_relative = PROFILE_CONTRACTS[profile]
        if (result["root"], result["language"], result["symbol"]) != (
            expected_root,
            expected_language,
            expected_symbol,
        ):
            raise AdmissionError(f"readiness profile {profile} does not match its canonical contract")
        expected_uri = "file://" + expected_root + "/" + expected_relative
        if result.get("matched_names") != [expected_symbol] or result.get("matched_uri") != expected_uri:
            raise AdmissionError(f"readiness profile {profile} did not prove its exact symbol location")
        if result.get("inventory_stable") is not True or result.get("cleanup_ok") is not True:
            raise AdmissionError(f"readiness profile {profile} has unstable inventory or incomplete cleanup")
        digest = _required_string(result.get("inventory_digest"), f"readiness result {index} inventory_digest")
        if not LOCK_DIGEST_RE.fullmatch(digest):
            raise AdmissionError(f"readiness result {index} inventory_digest is invalid")
        if result.get("effective_scope_ok") is False:
            raise AdmissionError(f"readiness profile {profile} effective scope disagrees with inventory")
        if result["language"] == "python":
            scope = result.get("scope_attribution")
            if not isinstance(scope, dict):
                raise AdmissionError(f"readiness profile {profile} lacks Python file-level scope attribution")
            trust = scope.get("trust_inventory_paths")
            program = scope.get("configured_program_paths")
            omitted = scope.get("trusted_not_in_configured_program")
            outside = scope.get("configured_program_outside_trust")
            if not all(isinstance(value, list) for value in (trust, program, omitted, outside)):
                raise AdmissionError(f"readiness profile {profile} Python scope paths must be lists")
            trust_paths = sorted(set(map(str, trust)))
            program_paths = sorted(set(map(str, program)))
            if trust_paths != trust or program_paths != program:
                raise AdmissionError(f"readiness profile {profile} Python scope paths are not normalized sets")
            if sorted(set(trust_paths) - set(program_paths)) != sorted(map(str, omitted)):
                raise AdmissionError(f"readiness profile {profile} has an invalid Python trusted omission projection")
            if sorted(set(program_paths) - set(trust_paths)) != sorted(map(str, outside)):
                raise AdmissionError(f"readiness profile {profile} has an invalid Python outside-trust projection")
            if scope.get("trust_inventory_count") != len(trust_paths) or scope.get(
                "trust_inventory_digest"
            ) != _path_digest(trust_paths):
                raise AdmissionError(f"readiness profile {profile} has invalid Python trust evidence")
            if scope.get("configured_program_count") != len(program_paths) or scope.get(
                "configured_program_digest"
            ) != _path_digest(program_paths):
                raise AdmissionError(f"readiness profile {profile} has invalid Python program evidence")
            evidence = scope.get("configured_program_evidence")
            if not isinstance(evidence, dict) or (
                evidence.get("comparison_basis") != "normalized_path_sets"
                or evidence.get("count_only_equivalence_rejected") is not True
                or evidence.get("projection_complete") is not True
                or evidence.get("kind") != "pinned_pyright_analyzer_service_owned_files"
            ):
                raise AdmissionError(f"readiness profile {profile} lacks complete Python path-set evidence")
            _number(scope.get("projection_seconds"), f"readiness profile {profile} Python projection_seconds")
            reasons = scope.get("difference_reasons")
            if not isinstance(reasons, dict):
                raise AdmissionError(f"readiness profile {profile} lacks Python difference reasons")
            for key, paths in (
                ("trusted_not_in_configured_program", omitted),
                ("configured_program_outside_trust", outside),
            ):
                raw_reasons = reasons.get(key)
                if not isinstance(raw_reasons, list) or sorted(
                    str(item.get("path")) for item in raw_reasons if isinstance(item, dict)
                ) != sorted(map(str, paths)):
                    raise AdmissionError(f"readiness profile {profile} has invalid reasons for {key}")
            if (
                outside
                or scope.get("scope_compatible") is not True
                or scope.get("error") is not None
                or scope.get("overlay_generated") is not False
            ):
                raise AdmissionError(f"readiness profile {profile} Python native program is outside trust")
            if scope.get("project_kind") not in {"configured", "workspace_default"}:
                raise AdmissionError(f"readiness profile {profile} has invalid Python project kind")
            if scope.get("project_kind") == "configured":
                _required_string(scope.get("selected_config_path"), f"readiness profile {profile} Python config")
            elif scope.get("selected_config_path") is not None:
                raise AdmissionError(f"readiness profile {profile} default project unexpectedly selected a config")
            if scope.get("configured_source_count") != len(program_paths):
                raise AdmissionError(f"readiness profile {profile} Python engine source count lacks path attribution")
            if result.get("server_source_count") != len(program_paths):
                raise AdmissionError(f"readiness profile {profile} Python CLI/LSP programs disagree")
            if result.get("inventory_count") != len(trust_paths) or result.get("inventory_digest") != _path_digest(
                trust_paths
            ):
                raise AdmissionError(
                    f"readiness profile {profile} Python readiness inventory disagrees with scope evidence"
                )
        grouped[profile].append(result)
        typed_results.append(result)

    for profile in REQUIRED_PROFILES:
        if len(grouped[profile]) < 5:
            raise AdmissionError(f"readiness profile {profile!r} has fewer than five passing runs")
        observed_runs = {result["run"] for result in grouped[profile]}
        if not set(range(1, 6)).issubset(observed_runs):
            raise AdmissionError(f"readiness profile {profile!r} is missing one of runs 1 through 5")
        if PROFILE_CONTRACTS[profile][1] == "python":
            signatures = {
                (
                    result["scope_attribution"]["trust_inventory_digest"],
                    result["scope_attribution"]["configured_program_digest"],
                    result["scope_attribution"]["selected_config_path"],
                    result["scope_attribution"]["project_kind"],
                )
                for result in grouped[profile]
            }
            if len(signatures) != 1:
                raise AdmissionError(f"readiness profile {profile} changed Python scope across clean starts")

    def sort_key(result: dict[str, Any]) -> tuple[int, Any, str]:
        profile = result["profile"]
        profile_index = REQUIRED_PROFILES.index(profile) if profile in REQUIRED_PROFILES else len(REQUIRED_PROFILES)
        return profile_index, result.get("run", 0), result["root"]

    if len(typed_results) != 20:
        raise AdmissionError("readiness must contain exactly 20 canonical runs")
    return sorted(
        typed_results,
        key=sort_key,
    )


def _markdown_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def render_report(
    readiness: dict[str, Any], ts_scope: dict[str, Any], bootstrap: dict[str, Any], source_budget: dict[str, Any]
) -> str:
    """Validate the four documents and return a deterministic PASS report."""

    _validate_bootstrap(bootstrap)
    _validate_source_budget(source_budget)
    _validate_ts_scope(ts_scope)
    results = _validate_readiness(readiness)

    lines = [
        "# serena-light Section-1 scope-admission report",
        "",
        "**Overall: PASS**",
        "",
        "This report supersedes the historical failed `admission-report.md`; that report remains immutable evidence.",
        "",
    ]
    lines.extend(
        [
            "## Locked runtime",
            "",
            f"- Lock digest: `{bootstrap['lock_digest']}`",
            f"- Runtime: `{bootstrap['runtime']}`",
            "",
        ]
    )
    lines.extend(["### Exact paths", "", "| Component | Path |", "| --- | --- |"])
    lines.extend(
        f"| {_markdown_escape(name)} | `{_markdown_escape(path)}` |"
        for name, path in sorted(bootstrap["paths"].items())
    )
    lines.extend(["", "### Exact versions", "", "| Component | Version |", "| --- | --- |"])
    lines.extend(
        f"| {_markdown_escape(name)} | `{_markdown_escape(version)}` |"
        for name, version in sorted(bootstrap["versions"].items())
    )
    imports = bootstrap.get("python_imports", [])
    if isinstance(imports, list):
        lines.extend(["", "### Verified Python imports", ""])
        lines.extend(f"- `{_markdown_escape(path)}`" for path in sorted(map(str, imports)))

    lines.extend(["", "## Source census and budget", "", "| Measure | Value |", "| --- | --- |"])
    for key in (
        "reference_commit",
        "selected_upstream_lines",
        "owned_code_estimate_lines",
        "expected_production_lines",
        "current_local_production_lines",
        "maximum_production_lines",
    ):
        lines.append(f"| {_markdown_escape(key)} | {_markdown_escape(source_budget.get(key, ''))} |")
    lines.extend(
        [
            "",
            "## Five-run readiness",
            "",
            "| Profile | Run | Root | Symbol | Language | Initialize (s) | Global ready (s) | "
            "Query (s) | Results | Position encoding |",
            "| --- | ---: | --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for result in results:
        lines.append(
            (
                "| {profile} | {run} | `{root}` | `{symbol}` | {language} | {initialize:.3f} | "
                "{seconds:.3f} | {query:.3f} | {count} | {encoding} |"
            ).format(
                profile=_markdown_escape(result["profile"]),
                run=_markdown_escape(result.get("run", "")),
                root=_markdown_escape(result["root"]),
                symbol=_markdown_escape(result["symbol"]),
                language=_markdown_escape(result["language"]),
                initialize=_number(result["initialize_seconds"], "initialize_seconds"),
                seconds=float(result["global_ready_seconds"]),
                query=_number(result["query_seconds"], "query_seconds"),
                count=_markdown_escape(result["result_count"]),
                encoding=_markdown_escape(result["position_encoding"]),
            )
        )
    lines.extend(
        [
            "",
            "## Python trust and configured-program scope",
            "",
            "**PASS** — each native Pyright program was attributed by normalized path and stays inside trust.",
            "",
        ]
    )
    first_python_results = {result["profile"]: result for result in results if result["language"] == "python"}
    for profile in ("transformers", "coordexp", "ms-swift"):
        scope = first_python_results[profile]["scope_attribution"]
        lines.extend(
            [
                f"### {_markdown_escape(profile)}",
                "",
                f"- Selected config: `{_markdown_escape(scope.get('selected_config_path'))}`",
                f"- Project kind: `{_markdown_escape(scope.get('project_kind'))}`",
                f"- Projection time: `{_markdown_escape(scope.get('projection_seconds'))}` seconds",
                f"- Trust: `{scope['trust_inventory_count']}` paths, digest `{scope['trust_inventory_digest']}`",
                (
                    f"- Program: `{scope['configured_program_count']}` paths, "
                    f"digest `{scope['configured_program_digest']}`"
                ),
                f"- Trusted but omitted: `{len(scope['trusted_not_in_configured_program'])}`",
                "- Configured outside trust: `0`",
                "- Overlay generated: `False`",
                "",
                "<details>",
                "<summary>File-level trust/program projection and difference reasons</summary>",
                "",
                "```json",
                json.dumps(
                    {
                        "trust_inventory_paths": scope["trust_inventory_paths"],
                        "configured_program_paths": scope["configured_program_paths"],
                        "difference_reasons": scope["difference_reasons"],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                "```",
                "</details>",
                "",
            ]
        )
    lines.extend(
        [
            "",
            "## TypeScript trust and configured-program scope",
            "",
            "**PASS** — the real native program stays inside the Git trust inventory, "
            "and the ignored-source fixture is rejected.",
            "",
        ]
    )
    for name, check in sorted(ts_scope["checks"].items()):
        lines.extend(
            [
                f"### {_markdown_escape(name)}",
                "",
                f"- Selected config: `{_markdown_escape(check.get('selected_config_path'))}`",
                f"- Project kind: `{_markdown_escape(check.get('project_kind'))}`",
                f"- Scope compatible: `{_markdown_escape(check.get('scope_compatible'))}`",
                f"- Trusted but omitted: `{_markdown_escape(check.get('trusted_not_in_configured_program', []))}`",
                f"- Configured outside trust: `{_markdown_escape(check.get('configured_program_outside_trust', []))}`",
                f"- Difference reasons: `{_markdown_escape(check.get('difference_reasons', {}))}`",
                f"- Rejected configured paths: `{_markdown_escape(check.get('configured_program_rejected', []))}`",
                "",
                "| Git inventory | tsserver program |",
                "| --- | --- |",
            ]
        )
        inventory = sorted(map(str, check["git_inventory"]))
        program = sorted(map(str, check["tsserver_program"]))
        for index in range(max(len(inventory), len(program))):
            left = inventory[index] if index < len(inventory) else ""
            right = program[index] if index < len(program) else ""
            lines.append(f"| `{_markdown_escape(left)}` | `{_markdown_escape(right)}` |")
        path_scoped = check.get("path_scoped_omission_probe")
        if isinstance(path_scoped, dict):
            lines.extend(
                [
                    "",
                    "Path-scoped omitted-file proof:",
                    "",
                    "```json",
                    json.dumps(path_scoped, indent=2, sort_keys=True),
                    "```",
                ]
            )
    lines.extend(
        ["", "## Position encodings", "", "| Profile | Run | Encoding | Source |", "| --- | ---: | --- | --- |"]
    )
    for result in results:
        lines.append(
            f"| {_markdown_escape(result['profile'])} | {_markdown_escape(result.get('run', ''))} | "
            f"{_markdown_escape(result['position_encoding'])} | "
            f"{_markdown_escape(result['position_encoding_source'])} |"
        )
    return "\n".join(lines) + "\n"


def render_failure_report(
    reason: str,
    readiness: dict[str, Any],
    ts_scope: dict[str, Any],
    bootstrap: dict[str, Any],
    source_budget: dict[str, Any],
) -> str:
    """Preserve bounded failure evidence while keeping the gate fail-closed."""
    blockers = [reason]
    readiness_results = readiness.get("results", [])
    if isinstance(readiness_results, list):
        scope_mismatches = sorted(
            {
                str(result.get("profile"))
                for result in readiness_results
                if isinstance(result, dict) and result.get("effective_scope_ok") is False
            }
        )
        for profile in scope_mismatches:
            blockers.append(f"{profile} engine source count disagrees with its declared inventory projection")
    lines = [
        "# serena-light Section-1 admission report",
        "",
        "**Overall: FAIL — Section 2 is blocked.**",
        "",
        "## Blocking gates",
        "",
    ]
    lines.extend(f"- {_markdown_escape(blocker)}" for blocker in blockers)
    lines.extend(
        [
            "",
            "## Locked runtime",
            "",
            f"- Lock digest: `{_markdown_escape(bootstrap.get('lock_digest', 'unknown'))}`",
            f"- Runtime: `{_markdown_escape(bootstrap.get('runtime', 'unknown'))}`",
            "",
            "### Exact versions",
            "",
            "| Component | Version |",
            "| --- | --- |",
        ]
    )
    versions = bootstrap.get("versions", {})
    if isinstance(versions, dict):
        lines.extend(
            f"| {_markdown_escape(name)} | `{_markdown_escape(value)}` |" for name, value in sorted(versions.items())
        )
    lines.extend(["", "### Exact paths", "", "| Component | Path |", "| --- | --- |"])
    paths = bootstrap.get("paths", {})
    if isinstance(paths, dict):
        lines.extend(
            f"| {_markdown_escape(name)} | `{_markdown_escape(value)}` |" for name, value in sorted(paths.items())
        )

    lines.extend(["", "## Source census and budget", "", "```json"])
    lines.append(json.dumps(source_budget, indent=2, sort_keys=True))
    lines.extend(["```", "", "## Readiness runs", ""])
    lines.extend(
        [
            f"- Recorded runs: {len(readiness_results) if isinstance(readiness_results, list) else 0}",
            f"- Probe status: `{_markdown_escape(readiness.get('status', 'unknown'))}`",
            f"- Timeout: `{_markdown_escape(readiness.get('timeout_seconds', 'unknown'))}` seconds",
            "",
            "| Profile | Run | Global ready (s) | Symbol | URI | Inventory | Server sources | Cleanup | Scope |",
            "| --- | ---: | ---: | --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    if isinstance(readiness_results, list):
        for raw in readiness_results:
            if not isinstance(raw, dict):
                continue
            lines.append(
                (
                    "| {profile} | {run} | {ready} | `{symbol}` | `{uri}` | "
                    "{inventory} | {sources} | {cleanup} | {scope} |"
                ).format(
                    profile=_markdown_escape(raw.get("profile", "")),
                    run=_markdown_escape(raw.get("run", "")),
                    ready=_markdown_escape(raw.get("global_ready_seconds", "")),
                    symbol=_markdown_escape(raw.get("symbol", "")),
                    uri=_markdown_escape(raw.get("matched_uri", "")),
                    inventory=_markdown_escape(raw.get("inventory_count", "")),
                    sources=_markdown_escape(raw.get("server_source_count", "")),
                    cleanup=_markdown_escape(raw.get("cleanup_ok", "")),
                    scope=_markdown_escape(raw.get("effective_scope_ok", "")),
                )
            )

    lines.extend(["", "## TypeScript trust and configured-program scope", ""])
    checks = ts_scope.get("checks", {})
    if isinstance(checks, dict):
        for name, raw_check in sorted(checks.items()):
            if not isinstance(raw_check, dict):
                continue
            check = cast("dict[str, Any]", raw_check)
            lines.extend(
                [
                    f"### {_markdown_escape(name)} — {_markdown_escape(check.get('status', 'unknown')).upper()}",
                    "",
                    f"- Git inventory: {len(check.get('git_inventory', []))}",
                    f"- tsserver workspace program: {len(check.get('tsserver_program', []))}",
                    f"- Selected config: `{_markdown_escape(check.get('selected_config_path'))}`",
                    f"- Project kind: `{_markdown_escape(check.get('project_kind'))}`",
                    f"- Trusted but omitted: `{_markdown_escape(check.get('trusted_not_in_configured_program', []))}`",
                    "- Configured outside trust: "
                    f"`{_markdown_escape(check.get('configured_program_outside_trust', []))}`",
                    f"- Scope compatible: `{_markdown_escape(check.get('scope_compatible'))}`",
                    f"- Cleanup: `{_markdown_escape(check.get('cleanup_ok', False))}`",
                    "",
                ]
            )
    lines.extend(
        [
            "## Position encodings",
            "",
            "All recorded initialize responses omitted `positionEncoding`; per the LSP specification "
            "the selected default is UTF-16.",
            "",
            "## Stop decision",
            "",
            "Do not start Section 2. Revise the OpenSpec source-scope contract before implementation continues.",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readiness", type=Path, required=True)
    parser.add_argument("--ts-scope", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--source-budget", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    readiness = _load_json(args.readiness)
    ts_scope = _load_json(args.ts_scope)
    bootstrap = _load_json(args.bootstrap)
    source_budget = _load_json(args.source_budget)
    try:
        report = render_report(readiness, ts_scope, bootstrap, source_budget)
    except AdmissionError as exc:
        print(f"admission report failed: {exc}", file=sys.stderr)
        report = render_failure_report(str(exc), readiness, ts_scope, bootstrap, source_budget)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
