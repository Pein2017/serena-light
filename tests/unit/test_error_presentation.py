from __future__ import annotations

import json
from typing import cast

import pytest
from mcp import types

from serena_light.tools.envelopes import (
    AdapterMetadata,
    ErrorCode,
    GenerationMetadata,
    RetryMetadata,
    WorkspaceMetadata,
    error,
)
from serena_light.tools.presentation import render_error_result


def _payload(result: types.CallToolResult) -> dict[str, object]:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    assert result.structuredContent is not None
    assert block.text == json.dumps(
        result.structuredContent,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return dict(result.structuredContent)


def test_deterministic_symbol_miss_omits_runtime_authority() -> None:
    envelope = error(
        ErrorCode.SYMBOL_NOT_FOUND,
        details={"relative_path": "src/example.py", "name_path": "missing"},
        workspace=WorkspaceMetadata("/data/example", "git", "/data/example"),
        adapter=AdapterMetadata("pyright", "python"),
        generations=GenerationMetadata(1, 2, 3, 4),
    )

    payload = _payload(render_error_result(envelope.to_dict()))

    assert payload == {
        "ok": False,
        "error": {
            "code": "SYMBOL_NOT_FOUND",
            "message": "symbol was not found",
            "details": {"relative_path": "src/example.py", "name_path": "missing"},
        },
        "workspace": "/data/example",
    }


def test_deterministic_symbol_miss_is_bounded_and_drops_engine_authority() -> None:
    envelope = error(
        ErrorCode.SYMBOL_NOT_FOUND,
        details={
            "relative_path": "src/example.py",
            "name_path": "missing" * 200,
            "engine": {
                "adapter": "pyright",
                "interpreter": "/service/python",
                "external_root": "/service/site-packages",
            },
        },
        workspace=WorkspaceMetadata("/data/example", "git", "/data/example"),
        adapter=AdapterMetadata("pyright", "python"),
        generations=GenerationMetadata(1, 2, 3, 4),
    )

    result = render_error_result(envelope.to_dict(), max_answer_chars=512)
    payload = _payload(result)
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    assert len(block.text) <= 512
    assert "adapter" not in payload and "generations" not in payload
    details = payload["error"]["details"]  # type: ignore[index]
    assert "engine" not in details
    assert details["relative_path"] == "src/example.py"
    assert "name_path" not in details


def test_invalid_path_retains_active_workspace_and_closed_recovery_action_at_budget() -> None:
    envelope = error(
        ErrorCode.INVALID_PATH,
        details={
            "path": "wrong-root/" + "x" * 800,
            "relative_path": "wrong-root/" + "x" * 800,
            "next_action": "activate_workspace_if_other_root",
        },
        workspace=WorkspaceMetadata("/data/example", "git", "/data/example"),
        adapter=AdapterMetadata("pyright", "python"),
        generations=GenerationMetadata(1, 2, 3, 4),
    )

    result = render_error_result(envelope.to_dict(), max_answer_chars=512)
    payload = _payload(result)
    assert len(result.content[0].text) <= 512  # type: ignore[union-attr]
    assert payload["workspace"] == "/data/example"
    error_payload = payload["error"]
    assert isinstance(error_payload, dict)
    error_data = cast(dict[str, object], error_payload)
    assert error_data["details"] == {
        "next_action": "activate_workspace_if_other_root",
    }


def test_unknown_recovery_action_is_rejected_at_presentation_boundary() -> None:
    envelope = error(
        ErrorCode.INVALID_PATH,
        details={"next_action": "guess_a_root"},
        workspace=WorkspaceMetadata("/data/example", "git", "/data/example"),
    )

    with pytest.raises(ValueError, match="recovery action"):
        render_error_result(envelope.to_dict())


def test_generic_invalid_path_does_not_invent_query_recovery() -> None:
    envelope = error(
        ErrorCode.INVALID_PATH,
        details={"path": "activation-target"},
        workspace=WorkspaceMetadata("/data/example", "git", "/data/example"),
    )

    payload = _payload(render_error_result(envelope.to_dict()))

    error_payload = payload["error"]
    assert isinstance(error_payload, dict)
    error_data = cast(dict[str, object], error_payload)
    details = error_data["details"]
    assert isinstance(details, dict)
    assert "next_action" not in cast(dict[str, object], details)


def test_operational_error_retains_rich_recovery_authority() -> None:
    envelope = error(
        ErrorCode.NOT_READY,
        retry=RetryMetadata(True, retry_after_seconds=0.2, target_generation=4),
        details={"phase": "global_warming"},
        workspace=WorkspaceMetadata("/data/example", "git", "/data/example"),
        adapter=AdapterMetadata("pyright", "python"),
        generations=GenerationMetadata(1, 2, 3, 4),
    )

    payload = _payload(render_error_result(envelope.to_dict()))

    assert payload["adapter"] == {"name": "pyright", "language": "python"}
    assert payload["generations"] == {
        "trust": 1,
        "program": 2,
        "document": 3,
        "index": 4,
    }
    assert payload["error"]["retry"]["retryable"] is True  # type: ignore[index]


def test_ambiguous_candidates_are_bounded_without_runtime_authority() -> None:
    envelope = error(
        ErrorCode.AMBIGUOUS_SYMBOL,
        details={
            "relative_path": "src/example.py",
            "name_path": "item",
            "engine": "pyright",
            "interpreter": "/service/python",
            "candidates": [f"Container{index:03d}/item" for index in range(80)],
        },
        workspace=WorkspaceMetadata("/data/example", "git", "/data/example"),
        adapter=AdapterMetadata("pyright", "python"),
        generations=GenerationMetadata(1, 2, 3, 4),
    )

    result = render_error_result(envelope.to_dict(), max_answer_chars=512)
    payload = _payload(result)
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    assert len(block.text) <= 512
    assert "adapter" not in payload and "generations" not in payload
    details = payload["error"]["details"]  # type: ignore[index]
    assert "next_action" not in details
    assert details["candidates"] == [
        f"Container{index:03d}/item" for index in range(len(details["candidates"]))
    ]
    assert details["truncated"] is True
    assert details["omitted_count"] == 80 - len(details["candidates"])


def test_ambiguous_symbol_candidates_keep_only_correction_coordinates() -> None:
    envelope = error(
        ErrorCode.AMBIGUOUS_SYMBOL,
        details={
            "relative_path": "src/example.py",
            "name_path": "render",
            "candidates": [
                {
                    "name": "render",
                    "name_path": "Renderer/render",
                    "kind": 6,
                    "range": {
                        "start": {"line": 4, "column": 8, "text_offset": 41, "byte_offset": 44},
                        "end": {"line": 7, "column": 9, "text_offset": 84, "byte_offset": 87},
                    },
                    "info": {"detail": "(self) -> str"},
                }
            ],
        },
        workspace=WorkspaceMetadata("/data/example", "git", "/data/example"),
        adapter=AdapterMetadata("pyright", "python"),
        generations=GenerationMetadata(1, 2, 3, 4),
    )

    payload = _payload(render_error_result(envelope.to_dict()))

    assert payload["error"]["details"]["candidates"] == [  # type: ignore[index]
        {
            "name_path": "Renderer/render",
            "kind": "method",
            "range": ((4, 8), (7, 9)),
        }
    ]


def test_candidate_free_ambiguity_is_bounded_without_transport_failure() -> None:
    envelope = error(
        ErrorCode.AMBIGUOUS_SYMBOL,
        details={
            "relative_path": "src/example.py",
            "regex": "(def)" + "(?:)" * 300,
            "occurrence_count": 12,
        },
        workspace=WorkspaceMetadata("/data/example", "git", "/data/example"),
        adapter=AdapterMetadata("pyright", "python"),
        generations=GenerationMetadata(1, 2, 3, 4),
    )

    result = render_error_result(envelope.to_dict(), max_answer_chars=512)
    payload = _payload(result)
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    assert len(block.text) <= 512
    assert payload["error"]["code"] == "AMBIGUOUS_SYMBOL"  # type: ignore[index]
    details = payload["error"]["details"]  # type: ignore[index]
    assert details["occurrence_count"] == 12
    assert "regex" not in details
