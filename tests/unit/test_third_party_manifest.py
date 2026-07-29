"""Regression checks for the Serena copied-source and census admission gates."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REFERENCE_COMMIT = "9a9d07e83d8c1cba3458992707f440c624446c6d"
REQUIRED_COPY_FIELDS = {
    "source_path",
    "source_symbol",
    "license",
    "copied_sha256",
    "local_owner",
}
FORBIDDEN_CLOSURE_PATHS = (
    "src/serena/agent.py",
    "src/serena/project_server.py",
    "src/serena/memories/",
    "src/serena/resources/config/modes/",
    "src/serena/resources/dashboard/",
    "src/serena/dashboard.py",
    "src/serena/gui_log_viewer.py",
    "src/serena/jetbrains/",
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def test_copied_source_manifest_has_pinned_reference_and_entry_contract() -> None:
    manifest = json.loads((ROOT / "third_party" / "copied_sources.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["reference"] == {
        "repository": "/data/CoordExp/external/serena",
        "commit": REFERENCE_COMMIT,
        "license": "MIT",
    }
    assert set(manifest["required_entry_fields"]) == REQUIRED_COPY_FIELDS

    for copy in manifest["copies"]:
        assert copy.keys() >= REQUIRED_COPY_FIELDS
        assert copy["license"] == manifest["reference"]["license"]
        assert SHA256.fullmatch(copy["copied_sha256"])
        assert all(copy[field] for field in REQUIRED_COPY_FIELDS - {"copied_sha256"})


def test_notices_name_the_same_reference_and_manifest() -> None:
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert REFERENCE_COMMIT in notices
    assert "MIT" in notices
    assert "third_party/copied_sources.json" in notices


def test_census_selected_closure_excludes_forbidden_subsystems() -> None:
    census = json.loads((ROOT / "third_party" / "serena_source_census.json").read_text(encoding="utf-8"))

    assert census["schema_version"] == 1
    assert census["commit"] == REFERENCE_COMMIT
    selected_paths = {entry["source_path"] for entry in census["files"]}
    assert not {
        selected_path
        for selected_path in selected_paths
        for forbidden_path in FORBIDDEN_CLOSURE_PATHS
        if selected_path.startswith(forbidden_path)
    }
    assert set(census["excluded_subsystems"]) == set(FORBIDDEN_CLOSURE_PATHS)
