"""Pre-compaction regressions for ``compact-success-schema``.

These tests intentionally describe the verbose success contract that the compact
change replaces.  They do not assert a future compact shape; their job is to
make the old duplication and the inner-fragment budgeting failure reproducible
under the pinned MCP SDK.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from mcp import types
from mcp.server.fastmcp import FastMCP

from serena_light.tools.envelopes import (
    AdapterMetadata,
    GenerationMetadata,
    SuccessEnvelope,
    TruncationMetadata,
    WorkspaceMetadata,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_ARCHIVED_EVIDENCE = (
    _REPOSITORY_ROOT
    / "openspec"
    / "changes"
    / "archive"
    / "2026-07-30-fix-position-and-coverage-contract"
    / "evidence"
)
_WORKSPACE = "/data/CoordExp/serena-light"
_PATH = "src/serena_light/lsp/positions.py"
_SHA256 = "d55c51c75b3e6ec87558bf79e58d9a2d2d66d36163f07c8f819fc1a7bb49c9e7"
_COMPACT_FIXTURE = _REPOSITORY_ROOT / "tests" / "integration" / "fixtures" / "compact_navigation"


def test_fixed_compact_workspace_covers_every_baseline_query_category() -> None:
    files = {path.name for path in _COMPACT_FIXTURE.iterdir() if path.is_file()}
    assert files == {
        "empty.py",
        "large_nested.py",
        "pyrightconfig.json",
        "python_symbols.py",
        "python_uncovered.py",
        "python_usage.py",
        "tsconfig.json",
        "typescript_symbols.ts",
        "typescript_usage.ts",
    }
    python_symbols = (_COMPACT_FIXTURE / "python_symbols.py").read_text()
    typescript_symbols = (_COMPACT_FIXTURE / "typescript_symbols.ts").read_text()
    assert "ANSWER: int = 42" in python_symbols
    assert "GenerationConfig" in python_symbols
    assert "interface Runner" in typescript_symbols
    assert "implements Runner" in typescript_symbols
    assert (_COMPACT_FIXTURE / "empty.py").read_text().startswith('"""')


def test_archived_mcp_baseline_exposes_verbose_metadata_repetition() -> None:
    """The archived content-text fixtures expose the legacy schema, not an internal dump."""

    navigation = _load_archived_json("baseline-navigation.json")
    references = _load_archived_json("baseline-references.json")

    assert navigation["workspace"] == {
        "root": _WORKSPACE,
        "kind": "git",
        "working_subdirectory": _WORKSPACE,
    }
    assert navigation["adapter"] == {"name": "pyright", "language": "python"}
    assert navigation["generations"] == {
        "trust": 0,
        "program": 1,
        "document": 697,
        "index": 1,
        "scope": "path",
    }
    assert navigation["data"]["relative_path"] == _PATH
    assert navigation["data"]["sha256"] == _SHA256

    reference_data = cast(dict[str, Any], references["data"])
    same_file_references = [
        reference
        for reference in cast(list[dict[str, Any]], reference_data["references"])
        if reference["path"] == reference_data["relative_path"]
    ]
    assert reference_data["relative_path"] == _PATH
    assert len(same_file_references) >= 2
    assert all(reference["read_only_external"] is False for reference in same_file_references)


def test_legacy_success_envelope_snapshot_repeats_file_identity_per_result() -> None:
    """Capture the pre-compact owner types and their duplicated success shape."""

    envelope = SuccessEnvelope(
        {
            "relative_path": "src",
            "symbols": [
                {"relative_path": _PATH, "sha256": _SHA256, "symbol": {"name": "first", "kind": 6}},
                {"relative_path": _PATH, "sha256": _SHA256, "symbol": {"name": "second", "kind": 6}},
            ],
        },
        WorkspaceMetadata(_WORKSPACE, "git", _WORKSPACE),
        AdapterMetadata("pyright", "python"),
        GenerationMetadata(trust=2, program=3, document=714, index=1, scope="global"),
        TruncationMetadata(False, 0),
    )

    assert envelope.to_dict() == {
        "ok": True,
        "data": {
            "relative_path": "src",
            "symbols": [
                {"relative_path": _PATH, "sha256": _SHA256, "symbol": {"name": "first", "kind": 6}},
                {"relative_path": _PATH, "sha256": _SHA256, "symbol": {"name": "second", "kind": 6}},
            ],
        },
        "workspace": {"root": _WORKSPACE, "kind": "git", "working_subdirectory": _WORKSPACE},
        "adapter": {"name": "pyright", "language": "python"},
        "generations": {"trust": 2, "program": 3, "document": 714, "index": 1, "scope": "global"},
        "truncation": {"truncated": False, "omitted_count": 0},
    }


def test_fastmcp_text_overflows_when_legacy_budget_prices_only_inner_data() -> None:
    """FastMCP's text rendering can exceed a 50k inner-fragment budget."""

    max_answer_chars = 50_000
    data = {
        "symbols": [
            {"name": f"symbol_{index:04d}", "kind": "function"}
            for index in range(1_000)
        ]
    }
    inner_fragment = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    assert len(inner_fragment) <= max_answer_chars

    content, structured_content = asyncio.run(_fastmcp_legacy_result(data))
    result = types.CallToolResult(content=list(content), structuredContent=structured_content)

    assert len(result.content) == 1
    text = result.content[0]
    assert isinstance(text, types.TextContent)
    assert result.structuredContent == {"ok": True, "data": data}
    assert len(text.text) > max_answer_chars
    assert json.loads(text.text) == result.structuredContent


async def _fastmcp_legacy_result(
    data: dict[str, list[dict[str, str]]],
) -> tuple[tuple[types.ContentBlock, ...], dict[str, object]]:
    """Exercise the pinned FastMCP dictionary-to-text conversion directly."""

    mcp = FastMCP("compact-baseline-overflow")

    @mcp.tool(structured_output=True)
    async def overview() -> dict[str, object]:
        return {"ok": True, "data": data}

    content, structured_content = await mcp.call_tool("overview", {})
    assert isinstance(structured_content, dict)
    return cast(tuple[types.ContentBlock, ...], content), structured_content


def _load_archived_json(name: str) -> dict[str, Any]:
    value = json.loads((_ARCHIVED_EVIDENCE / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)
