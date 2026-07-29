import hashlib
from typing import Any

import pytest

from tests.admission.ts_scope_probe import SCHEMA_VERSION, SCOPE_INCOMPATIBLE, run_probe

pytestmark = [
    pytest.mark.timeout(90),
    pytest.mark.external_repo(
        root="/data/CoordExp/cc-plugin-codex",
        snapshot_env="SERENA_LIGHT_CC_PLUGIN_CODEX_SNAPSHOT",
    ),
]


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    return run_probe()


def _digest(paths: list[str]) -> str:
    return hashlib.sha256("\0".join(paths).encode()).hexdigest()


def test_program_is_attributed_to_the_native_config(report: dict[str, Any]) -> None:
    for check in report["checks"].values():
        assert check["project_kind"] == "configured"
        assert check["selected_config_path"] == "tsconfig.json"
        assert check["comparison_basis"] == "normalized_path_sets"
        assert check["count_only_equivalence_rejected"] is True
        assert check["overlay_generated"] is False

        for name in (
            "git_inventory",
            "tsserver_program",
            "trusted_not_in_configured_program",
            "configured_program_outside_trust",
        ):
            evidence = check["path_set_evidence"][name]
            assert evidence == {"count": len(check[name]), "sha256": _digest(check[name])}

    actual = report["checks"]["cc_plugin_codex"]
    assert actual["tsserver_program"]
    assert all(path.startswith("runtime/") and path.endswith(".mjs") for path in actual["tsserver_program"])
    assert all(path.endswith(".ts") for path in report["checks"]["ignored_subtree_fixture"]["tsserver_program"])


def test_real_root_allows_native_config_omissions(report: dict[str, Any]) -> None:
    actual = report["checks"]["cc_plugin_codex"]
    assert actual["configured_program_outside_trust"] == []
    assert actual["scope_compatible"] is True
    assert actual["error"] is None
    assert actual["status"] == "pass"

    omitted = actual["trusted_not_in_configured_program"]
    assert omitted
    assert sorted(set(actual["tsserver_program"]) | set(omitted)) == actual["git_inventory"]
    assert actual["difference_reasons"]["trusted_not_in_configured_program"] == [
        {"path": path, "reason": "excluded_by_native_config"} for path in omitted
    ]
    assert actual["difference_reasons"]["configured_program_outside_trust"] == []


def test_fixture_detects_program_file_outside_trust(report: dict[str, Any]) -> None:
    fixture = report["checks"]["ignored_subtree_fixture"]
    assert fixture["git_inventory"] == ["src/helper.ts", "src/main.ts"]
    assert fixture["configured_program_outside_trust"] == ["ignored-generated/hidden.ts"]
    assert fixture["trusted_not_in_configured_program"] == []
    assert fixture["scope_compatible"] is False
    assert fixture["error"]["code"] == SCOPE_INCOMPATIBLE
    assert fixture["error"]["paths"] == ["ignored-generated/hidden.ts"]
    assert fixture["status"] == "fail"
    assert fixture["difference_reasons"]["configured_program_outside_trust"] == [
        {"path": "ignored-generated/hidden.ts", "reason": "absent_from_git_trust_inventory"}
    ]


def test_deleted_and_symlink_paths_never_enter_accepted_sets(report: dict[str, Any]) -> None:
    fixture = report["checks"]["ignored_subtree_fixture"]
    rejected_inventory = {item["path"]: item["reason"] for item in fixture["git_inventory_rejected"]}
    assert rejected_inventory == {
        "tracked-deleted.ts": "tracked_deleted",
        "tracked-inside-link.ts": "symlink",
        "tracked-outside-link.ts": "symlink_escape",
    }
    rejected_program = {item["path"]: item["reason"] for item in fixture["configured_program_rejected"]}
    assert rejected_program == {
        "tracked-inside-link.ts": "symlink",
        "tracked-outside-link.ts": "symlink_escape",
    }
    rejected_paths = set(rejected_inventory) | set(rejected_program)
    assert rejected_paths.isdisjoint(fixture["git_inventory"])
    assert rejected_paths.isdisjoint(fixture["tsserver_program"])
    assert fixture["rejected_path_counts"] == {"git_inventory": 3, "configured_program": 2}


def test_omitted_mjs_is_served_by_engine_owned_inferred_project_without_scope_expansion(
    report: dict[str, Any],
) -> None:
    actual = report["checks"]["cc_plugin_codex"]
    proof = actual["path_scoped_omission_probe"]
    assert proof["path"].endswith(".mjs")
    assert proof["path"] in actual["trusted_not_in_configured_program"]
    assert proof["operation"] == "navtree"
    assert proof["service_supported"] is True
    assert proof["engine_owned"] is True
    assert proof["project_kind"] == "inferred"
    assert proof["selected_config_path"] is None
    assert proof["read_only"] is True
    assert proof["configured_program_unchanged"] is True
    assert proof["global_scope_expanded"] is False
    assert proof["configured_program_before_sha256"] == actual["path_set_evidence"]["tsserver_program"]["sha256"]
    assert proof["configured_program_after_sha256"] == actual["path_set_evidence"]["tsserver_program"]["sha256"]
    assert proof["status"] == "pass"
    assert proof["error"] is None


def test_probe_passes_and_preserves_cleanup(report: dict[str, Any]) -> None:
    assert report["schema_version"] == SCHEMA_VERSION
    assert all(check["cleanup_ok"] for check in report["checks"].values())
    assert report["overlay_generated"] is False
    assert report["status"] == "pass"
