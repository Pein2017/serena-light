from __future__ import annotations

import json
from pathlib import Path

from serena_light.build_identity import PUBLIC_TOOL_SCHEMA_VERSION, compute_build_identity
from serena_light.instructions import AGENT_INSTRUCTIONS

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_compatibility_revision_matches_public_tool_schema_and_build_identity() -> None:
    compatibility = json.loads((REPOSITORY_ROOT / "docs" / "compatibility.json").read_text())

    assert compatibility["schema_version"] == 6
    assert PUBLIC_TOOL_SCHEMA_VERSION == "6"
    assert compatibility["semantic_contract"]["coordinates"] == {
        "line": "0-based decoded-text line",
        "column": "0-based Unicode code-point column",
        "text_offset": "decoded-text offset in the exact verified snapshot",
        "byte_offset": "physical UTF-8 file offset in the same snapshot, including any BOM",
    }
    assert compute_build_identity(REPOSITORY_ROOT) != compute_build_identity(
        REPOSITORY_ROOT,
        public_tool_schema_version="5",
    )


def test_compatibility_records_the_source_owned_current_interaction_contract() -> None:
    compatibility = json.loads((REPOSITORY_ROOT / "docs" / "compatibility.json").read_text())
    contract = compatibility["semantic_contract"]
    readme = (REPOSITORY_ROOT / "README.md").read_text()

    assert contract["initialize_guidance"]["source"] == "serena_light.instructions.AGENT_INSTRUCTIONS"
    assert contract["initialize_guidance"]["text"] == AGENT_INSTRUCTIONS
    assert f"> {AGENT_INSTRUCTIONS}" in readme
    assert contract["overview"]["default_max_depth"] == 0
    assert contract["reference_coverage"]["complete"]["exact_shape"] == {"complete": True}
    assert contract["reference_coverage"]["declaration_included"] is False
    assert contract["agent_surface_exclusions"] == [
        "lexical discovery tools",
        "diagnostics hooks or automatic diagnostics injection",
        "RTK integration",
    ]


def test_compact_and_freshness_candidates_have_final_acceptance() -> None:
    compatibility = json.loads((REPOSITORY_ROOT / "docs" / "compatibility.json").read_text())
    acceptance = compatibility["acceptance"]

    assert acceptance["compact_candidate_fresh_session_accepted"] is True
    assert acceptance["edit_reacceptance_pending"] is False
    assert acceptance["dual_audit_hold"] is False
    assert acceptance["strengthen_call_freshness"]["status"] == "pass"
    assert acceptance["strengthen_call_freshness"]["release_pass"] is True
