import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

from tests.admission.lsp_probe import profiles as lsp_profiles

SCRIPT = Path(__file__).parents[2] / "scripts" / "render_admission_report.py"
SPEC = importlib.util.spec_from_file_location("render_admission_report", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
admission_report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admission_report)


def test_codexui_probe_profile_matches_the_rendered_admission_contract() -> None:
    profile = lsp_profiles()["codexui"]

    assert profile.root == Path("/data/CoordExp/external/codexUI")
    assert profile.language == "typescript"
    assert profile.symbol == "normalizeCodexApiError"
    assert profile.expected_relative_path == Path("src/api/codexErrors.ts")
    assert admission_report.PROFILE_CONTRACTS["codexui"] == (
        str(profile.root),
        profile.language,
        profile.symbol,
        str(profile.expected_relative_path),
    )


def _documents() -> tuple[dict, dict, dict, dict]:
    profiles = admission_report.PROFILE_CONTRACTS
    results = []
    for profile in profiles:
        language = profiles[profile][1]
        scope = None
        if language == "python":
            selected_config = "pyrightconfig.json" if profile == "coordexp" else None
            scope = {
                "selected_config_path": selected_config,
                "project_kind": "configured" if selected_config else "workspace_default",
                "configured_source_count": 1,
                "engine_version": "1.1.403",
                "configured_program_paths": ["src/main.py"],
                "configured_program_count": 1,
                "configured_program_digest": admission_report._path_digest(["src/main.py"]),
                "trust_inventory_kind": "git",
                "trust_inventory_paths": ["omitted.py", "src/main.py"],
                "trust_inventory_count": 2,
                "trust_inventory_digest": admission_report._path_digest(["omitted.py", "src/main.py"]),
                "configured_program_evidence": {
                    "kind": "pinned_pyright_analyzer_service_owned_files",
                    "comparison_basis": "normalized_path_sets",
                    "count_only_equivalence_rejected": True,
                    "projection_complete": True,
                    "evidence_reasons": [],
                },
                "trusted_not_in_configured_program": ["omitted.py"],
                "configured_program_outside_trust": [],
                "difference_reasons": {
                    "trusted_not_in_configured_program": [
                        {"path": "omitted.py", "reason": "omitted_by_native_config_or_engine_program"}
                    ],
                    "configured_program_outside_trust": [],
                },
                "scope_compatible": True,
                "error": None,
                "status": "pass",
                "projection_seconds": 0.2,
                "overlay_generated": False,
            }
        for run in range(1, 6):
            results.append(
                {
                    "profile": profile,
                    "run": run,
                    "root": profiles[profile][0],
                    "symbol": profiles[profile][2],
                    "language": language,
                    "initialize_seconds": 0.1,
                    "global_ready_seconds": 1.0 + run / 10,
                    "query_seconds": 1.0,
                    "result_count": 1,
                    "matched_names": [profiles[profile][2]],
                    "matched_uri": f"file://{profiles[profile][0]}/{profiles[profile][3]}",
                    "inventory_count": 2 if scope else 10,
                    "inventory_digest": scope["trust_inventory_digest"] if scope else "b" * 64,
                    "inventory_stable": True,
                    "server_source_count": 1 if scope else None,
                    "effective_scope_ok": True if scope else None,
                    "scope_attribution": scope,
                    "cleanup_ok": True,
                    "position_encoding": "utf-16",
                    "position_encoding_source": "LSP default",
                    "status": "pass",
                }
            )
    readiness = {"schema_version": 1, "timeout_seconds": 30, "results": results, "status": "pass"}
    actual_scope_check = {
        "status": "pass",
        "selected_config_path": "tsconfig.json",
        "project_kind": "configured",
        "git_inventory": ["runtime/main.mjs", "tests/main.test.mjs"],
        "tsserver_program": ["runtime/main.mjs"],
        "trusted_not_in_configured_program": ["tests/main.test.mjs"],
        "configured_program_outside_trust": [],
        "difference_reasons": {
            "trusted_not_in_configured_program": [
                {"path": "tests/main.test.mjs", "reason": "excluded_by_native_config"}
            ],
            "configured_program_outside_trust": [],
        },
        "path_set_evidence": {
            "git_inventory": {
                "count": 2,
                "sha256": admission_report._path_digest(["runtime/main.mjs", "tests/main.test.mjs"]),
            },
            "tsserver_program": {"count": 1, "sha256": admission_report._path_digest(["runtime/main.mjs"])},
            "trusted_not_in_configured_program": {
                "count": 1,
                "sha256": admission_report._path_digest(["tests/main.test.mjs"]),
            },
            "configured_program_outside_trust": {"count": 0, "sha256": admission_report._path_digest([])},
        },
        "configured_program_rejected": [],
        "comparison_basis": "normalized_path_sets",
        "count_only_equivalence_rejected": True,
        "overlay_generated": False,
        "path_scoped_omission_probe": {
            "status": "pass",
            "service_supported": True,
            "engine_owned": True,
            "project_kind": "inferred",
            "configured_program_unchanged": True,
            "global_scope_expanded": False,
            "read_only": True,
        },
        "cleanup_ok": True,
        "scope_compatible": True,
        "error": None,
    }
    fixture_scope_check = {
        "status": "fail",
        "selected_config_path": "tsconfig.json",
        "project_kind": "configured",
        "git_inventory": ["src/helper.ts", "src/main.ts"],
        "tsserver_program": ["ignored-generated/hidden.ts", "src/helper.ts", "src/main.ts"],
        "trusted_not_in_configured_program": [],
        "configured_program_outside_trust": ["ignored-generated/hidden.ts"],
        "difference_reasons": {
            "trusted_not_in_configured_program": [],
            "configured_program_outside_trust": [
                {"path": "ignored-generated/hidden.ts", "reason": "absent_from_git_trust_inventory"}
            ],
        },
        "path_set_evidence": {
            "git_inventory": {
                "count": 2,
                "sha256": admission_report._path_digest(["src/helper.ts", "src/main.ts"]),
            },
            "tsserver_program": {
                "count": 3,
                "sha256": admission_report._path_digest(
                    ["ignored-generated/hidden.ts", "src/helper.ts", "src/main.ts"]
                ),
            },
            "trusted_not_in_configured_program": {"count": 0, "sha256": admission_report._path_digest([])},
            "configured_program_outside_trust": {
                "count": 1,
                "sha256": admission_report._path_digest(["ignored-generated/hidden.ts"]),
            },
        },
        "configured_program_rejected": [],
        "comparison_basis": "normalized_path_sets",
        "count_only_equivalence_rejected": True,
        "overlay_generated": False,
        "cleanup_ok": True,
        "scope_compatible": False,
        "error": {"code": "SCOPE_INCOMPATIBLE", "paths": ["ignored-generated/hidden.ts"]},
    }
    scope = {
        "schema_version": 3,
        "status": "pass",
        "overlay_generated": False,
        "checks": {
            "ignored_subtree_fixture": fixture_scope_check,
            "external_typescript_root": actual_scope_check,
        },
    }
    runtime = "/data/CoordExp/.codex/runtime/serena-light/deps/" + "a" * 64
    bootstrap = {
        "lock_digest": "a" * 64,
        "runtime": runtime,
        "paths": {
            name: f"{runtime}/{name}"
            for name in ("node", "npm", "pyright-langserver", "typescript-language-server", "tsserver", "python")
        },
        "versions": {
            "node": "22.22.0",
            "npm": "11.13.0",
            "pyright": "1.1.403",
            "typescript-language-server": "5.1.3",
            "typescript": "5.9.3",
            "python": "Python 3.12.0",
        },
        "python_imports": [f"{runtime}/mcp.py"],
    }
    budget = {
        "status": "pass",
        "reference_commit": "abc",
        "selected_upstream_lines": 100,
        "owned_code_estimate_lines": 100,
        "expected_production_lines": 200,
        "current_local_production_lines": 150,
        "maximum_production_lines": None,
    }
    return readiness, scope, bootstrap, budget


def test_render_is_deterministic_and_includes_required_evidence() -> None:
    documents = _documents()
    first = admission_report.render_report(*documents)
    second = admission_report.render_report(*documents)

    assert first == second
    assert "**Overall: PASS**" in first
    assert "Lock digest: `" + "a" * 64 + "`" in first
    assert "## Source census and budget" in first
    assert "## Five-run readiness" in first
    assert "## TypeScript trust and configured-program scope" in first
    assert "## Position encodings" in first
    assert first.count("| transformers |") == 10


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda docs: docs[0]["results"].pop(), "fewer than five"),
        (
            lambda docs: docs[0]["results"].__setitem__(0, {**docs[0]["results"][0], "global_ready_seconds": 30.1}),
            "exceeds",
        ),
        (
            lambda docs: docs[0]["results"].__setitem__(0, {**docs[0]["results"][0], "position_encoding": "unknown"}),
            "unknown position",
        ),
        (
            lambda docs: docs[1]["checks"]["external_typescript_root"].update(
                {
                    "tsserver_program": ["runtime/main.mjs", "ignored.ts"],
                    "configured_program_outside_trust": ["ignored.ts"],
                    "scope_compatible": False,
                    "error": {"code": "SCOPE_INCOMPATIBLE", "paths": ["ignored.ts"]},
                }
            ),
            "invalid reasons",
        ),
        (lambda docs: docs[2].update({"lock_digest": "invalid"}), "SHA-256"),
        (lambda docs: docs[2].update({"status": "fail"}), "bootstrap status"),
        (lambda docs: docs[3].update({"maximum_production_lines": 12000}), "must be null"),
    ],
)
def test_render_fails_closed_for_each_required_gate(mutate, message: str) -> None:
    documents = list(deepcopy(_documents()))
    mutate(documents)
    with pytest.raises(admission_report.AdmissionError, match=message):
        admission_report.render_report(*documents)
