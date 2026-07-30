from __future__ import annotations

import json
from pathlib import Path

from serena_light.build_identity import PUBLIC_TOOL_SCHEMA_VERSION, compute_build_identity

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_compatibility_revision_matches_public_tool_schema_and_build_identity() -> None:
    compatibility = json.loads((REPOSITORY_ROOT / "docs" / "compatibility.json").read_text())

    assert compatibility["schema_version"] == 3
    assert PUBLIC_TOOL_SCHEMA_VERSION == "3"
    assert compatibility["semantic_contract"]["coordinates"] == {
        "line": "0-based decoded-text line",
        "column": "0-based Unicode code-point column",
        "text_offset": "decoded-text offset in the exact verified snapshot",
        "byte_offset": "physical UTF-8 file offset in the same snapshot, including any BOM",
    }
    assert compute_build_identity(REPOSITORY_ROOT) != compute_build_identity(
        REPOSITORY_ROOT,
        public_tool_schema_version="2",
    )


def test_compact_candidate_passes_fresh_clients_and_remains_on_dual_audit_hold() -> None:
    compatibility = json.loads((REPOSITORY_ROOT / "docs" / "compatibility.json").read_text())
    acceptance = compatibility["acceptance"]

    assert acceptance["current_candidate_fresh_session_accepted"] is True
    assert acceptance["edit_reacceptance_pending"] is False
    assert acceptance["dual_audit_hold"] is True
