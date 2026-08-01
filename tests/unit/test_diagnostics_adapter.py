from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from mcp import types

from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.lsp.state import DiagnosticsSnapshot, DiagnosticsState
from serena_light.tools.diagnostics import (
    DiagnosticDocumentInput,
    DiagnosticEngineFacts,
    ExternalRootMetadata,
    get_diagnostics_for_file,
    get_diagnostics_for_symbol,
)
from serena_light.tools.diagnostics_adapter import compact_diagnostics_result
from serena_light.tools.envelopes import AdapterMetadata, GenerationMetadata, WorkspaceMetadata
from serena_light.tools.navigation import DocumentSymbolInput


def _range(start_line: int, start_character: int, end_line: int, end_character: int) -> dict[str, dict[str, int]]:
    return {
        "start": {"line": start_line, "character": start_character},
        "end": {"line": end_line, "character": end_character},
    }


def _document(
    *,
    mjs: bool = False,
    relative_path: str | None = None,
    workspace_root: str = "/repo",
    text: str = "# module header\ndef outer():\n    return bad\n\ndef second():\n    return nope\n",
    raw_symbols: Sequence[Mapping[str, object]] | None = None,
):
    selected_path = relative_path or ("src/example.mjs" if mjs else "src/example.py")
    selected_symbols = raw_symbols
    if selected_symbols is None:
        selected_symbols = [
            {"name": "outer", "kind": 12, "range": _range(1, 0, 3, 0)},
            {"name": "second", "kind": 12, "range": _range(4, 0, 6, 0)},
        ]
    return DocumentSymbolInput(
        selected_path,
        f"file:///repo/{selected_path}",
        FileSnapshot.from_bytes(text.encode()),
        selected_symbols,
        PositionEncoding.UTF16,
        WorkspaceMetadata(workspace_root, "git", workspace_root),
        AdapterMetadata("typescript" if mjs else "pyright", "typescript" if mjs else "python"),
        GenerationMetadata(trust=1, program=2, document=2, index=4, scope="path"),
    )


def _engine(*, mjs: bool = False) -> DiagnosticEngineFacts:
    if mjs:
        return DiagnosticEngineFacts(
            "typescript-language-server",
            "typescript",
            "5.1.3",
            semantic_engine_name="typescript",
            semantic_engine_version="5.9.3",
            native_typecheck_command="npm run typecheck",
        )
    return DiagnosticEngineFacts(
        "pyright",
        "python",
        "1.1.403",
        "/root/miniconda3/envs/ms/bin/python",
        external_root=ExternalRootMetadata(
            "read_only_external", "/root/miniconda3/envs/ms/lib/python3.12/site-packages"
        ),
    )


def _input(
    diagnostics: Sequence[Mapping[str, object]] | None = None,
    *,
    state: DiagnosticsState = DiagnosticsState.FINDINGS,
    mjs: bool = False,
    document=None,
) -> DiagnosticDocumentInput:
    resolved_document = document or _document(mjs=mjs)
    publication = DiagnosticsSnapshot(
        state,
        resolved_document.uri,
        Path("/repo") / resolved_document.relative_path,
        1,
        2,
        8,
        tuple(diagnostics or ()),
    )
    return DiagnosticDocumentInput(resolved_document, 2, _engine(mjs=mjs), publication)


def _text(result: types.CallToolResult) -> str:
    return cast(types.TextContent, result.content[0]).text


def _structured(result: types.CallToolResult) -> dict[str, Any]:
    return cast(dict[str, Any], result.structuredContent)


def _assert_text_matches_structured(result: types.CallToolResult) -> dict[str, Any]:
    text = _text(result)
    payload = json.loads(text)
    assert result.structuredContent == payload
    assert text == json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return payload


def test_clean_publication_compacts_to_named_file_with_empty_diagnostics() -> None:
    envelope = get_diagnostics_for_file(_input([], state=DiagnosticsState.CLEAN)).to_dict()

    result = compact_diagnostics_result(envelope)
    payload = _assert_text_matches_structured(result)

    assert payload == {
        "ok": True,
        "data": {
            "workspace": "/repo",
            "files": [{"path": "src/example.py", "diagnostics": []}],
            "omitted": 0,
        },
    }


def test_file_level_and_symbol_findings_carry_severity_symbol_source_and_code() -> None:
    diagnostics = [
        {"severity": 1, "message": "parse error", "range": _range(0, 0, 0, 1)},
        {
            "severity": 2,
            "message": "outer warning",
            "source": "pyright",
            "code": "reportGeneralTypeIssues",
            "range": _range(1, 4, 1, 10),
        },
    ]
    envelope = get_diagnostics_for_file(_input(diagnostics)).to_dict()

    result = compact_diagnostics_result(envelope)
    payload = _assert_text_matches_structured(result)

    findings = payload["data"]["files"][0]["diagnostics"]
    assert findings[0] == {
        "severity": "error",
        "range": [[0, 0], [0, 1]],
        "message": "parse error",
    }
    assert findings[1] == {
        "severity": "warning",
        "range": [[1, 4], [1, 10]],
        "message": "outer warning",
        "symbol": "outer",
        "source": "pyright",
        "code": "reportGeneralTypeIssues",
    }
    assert "symbol" not in findings[0]


def test_no_state_hash_generation_adapter_engine_or_offsets_leak_into_success() -> None:
    diagnostics = [{"severity": 1, "message": "bad", "range": _range(1, 4, 1, 10)}]
    envelope = get_diagnostics_for_file(_input(diagnostics)).to_dict()

    payload = _assert_text_matches_structured(compact_diagnostics_result(envelope))

    blob = json.dumps(payload)
    for forbidden in (
        "state",
        "sha256",
        "diagnostics_generation",
        "adapter",
        "engine",
        "interpreter",
        "uri",
        "text_offset",
        "byte_offset",
    ):
        assert forbidden not in blob


def test_typescript_authority_is_advisory_once_per_file_and_python_omits_it() -> None:
    ts_diagnostics = [{"severity": 1, "message": "ts issue", "range": _range(0, 0, 0, 1)}]
    ts_envelope = get_diagnostics_for_file(_input(ts_diagnostics, mjs=True)).to_dict()
    py_envelope = get_diagnostics_for_file(_input([], state=DiagnosticsState.CLEAN)).to_dict()

    ts_payload = _assert_text_matches_structured(compact_diagnostics_result(ts_envelope))
    py_payload = _assert_text_matches_structured(compact_diagnostics_result(py_envelope))

    assert ts_payload["data"]["files"][0]["authority"] == "advisory"
    assert "authority" not in py_payload["data"]["files"][0]


def test_symbol_scoped_result_only_includes_findings_within_selected_symbol() -> None:
    diagnostics = [
        {"severity": 2, "message": "outer warning", "range": _range(1, 4, 1, 10)},
        {"severity": 1, "message": "second error", "range": _range(4, 4, 4, 9)},
    ]
    envelope = get_diagnostics_for_symbol(_input(diagnostics), "outer").to_dict()

    payload = _assert_text_matches_structured(compact_diagnostics_result(envelope))

    findings = payload["data"]["files"][0]["diagnostics"]
    assert [item["message"] for item in findings] == ["outer warning"]
    assert findings[0]["symbol"] == "outer"


def test_failure_input_is_rejected_as_malformed_diagnostics_success() -> None:
    document = _document()
    raw = list(document.raw_symbols or ())
    raw.append({"name": "outer", "kind": 12, "range": _range(3, 0, 5, 0)})
    ambiguous_document = _document(raw_symbols=raw)
    diagnostics = [{"severity": 2, "message": "outer warning", "range": _range(1, 4, 1, 10)}]
    envelope = get_diagnostics_for_symbol(_input(diagnostics, document=ambiguous_document), "outer").to_dict()

    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "AMBIGUOUS_SYMBOL"

    result = compact_diagnostics_result(envelope)
    compacted = json.loads(_text(result))
    assert compacted["ok"] is False
    assert compacted["error"]["code"] == "UNSUPPORTED"
    assert compacted["error"]["details"]["reason"] == "malformed_diagnostics_success"


def test_unicode_message_is_preserved_without_ascii_escaping() -> None:
    diagnostics = [{"severity": 1, "message": "bad emoji \U0001f600 usage", "range": _range(0, 0, 0, 1)}]
    envelope = get_diagnostics_for_file(_input(diagnostics)).to_dict()

    result = compact_diagnostics_result(envelope)
    payload = _assert_text_matches_structured(result)

    assert "\U0001f600" in _text(result)
    assert "\\u" not in _text(result)
    assert payload["data"]["files"][0]["diagnostics"][0]["message"] == "bad emoji \U0001f600 usage"


def test_budget_trims_whole_trailing_findings_deterministically_at_exact_512() -> None:
    line_count = 20
    document = _document(
        text="".join(f"line {index}\n" for index in range(line_count)),
        raw_symbols=(),
    )
    diagnostics = [
        {"severity": 1, "message": f"error number {index}", "range": _range(index, 0, index, 5)}
        for index in range(line_count)
    ]
    envelope = get_diagnostics_for_file(_input(diagnostics, document=document)).to_dict()

    full = compact_diagnostics_result(envelope, max_answer_chars=50_000)
    full_payload = _assert_text_matches_structured(full)
    assert len(full_payload["data"]["files"][0]["diagnostics"]) == 20

    bounded = compact_diagnostics_result(envelope, max_answer_chars=512)
    bounded_payload = _assert_text_matches_structured(bounded)
    text = _text(bounded)

    assert len(text) <= 512
    assert bounded_payload["ok"] is True
    kept = len(bounded_payload["data"]["files"][0]["diagnostics"])
    assert 0 < kept < 20
    assert bounded_payload["data"]["omitted"] == 20 - kept
    # Trimming removes only the trailing findings, never reorders or cuts one.
    assert bounded_payload["data"]["files"][0]["diagnostics"] == full_payload["data"]["files"][0]["diagnostics"][:kept]

    smaller = compact_diagnostics_result(envelope, max_answer_chars=len(text) - 1 if len(text) > 512 else 512)
    assert _structured(smaller)["ok"] is True


def test_impossible_budget_returns_bounded_invalid_input_with_truthful_minimum() -> None:
    long_path = "src/" + ("nested_directory/" * 40) + "example.py"
    document = _document(relative_path=long_path)
    diagnostics = [{"severity": 1, "message": "bad", "range": _range(0, 0, 0, 1)}]
    envelope = get_diagnostics_for_file(
        DiagnosticDocumentInput(
            document,
            2,
            _engine(),
            DiagnosticsSnapshot(
                DiagnosticsState.FINDINGS,
                document.uri,
                Path("/repo") / document.relative_path,
                1,
                2,
                8,
                tuple(diagnostics),
            ),
        ),
        max_answer_chars=50_000,
    ).to_dict()

    result = compact_diagnostics_result(envelope, max_answer_chars=512)
    payload = json.loads(_text(result))

    assert result.structuredContent == payload
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["error"]["details"]["field"] == "max_answer_chars"
    minimum_required = payload["error"]["details"]["minimum_required_chars"]
    assert minimum_required > 512

    fits = compact_diagnostics_result(envelope, max_answer_chars=minimum_required)
    assert _structured(fits)["ok"] is True
    assert len(_text(fits)) == minimum_required


def test_one_unfittable_finding_returns_truthful_minimum_for_both_diagnostics_tools() -> None:
    diagnostic = {
        "severity": 1,
        "message": "Argument of type X is not assignable to parameter of type Y. " * 8,
        "range": _range(2, 4, 2, 14),
    }
    input_value = _input([diagnostic])
    envelopes = (
        get_diagnostics_for_file(input_value, max_answer_chars=50_000).to_dict(),
        get_diagnostics_for_symbol(input_value, "outer", max_answer_chars=50_000).to_dict(),
    )

    for envelope in envelopes:
        result = compact_diagnostics_result(envelope, max_answer_chars=512)
        payload = _assert_text_matches_structured(result)
        assert len(_text(result)) <= 512
        assert payload["ok"] is False
        assert payload["error"]["code"] == "INVALID_INPUT"
        details = payload["error"]["details"]
        assert details["field"] == "max_answer_chars"
        minimum_required = details["minimum_required_chars"]
        assert minimum_required > 512

        fits = compact_diagnostics_result(envelope, max_answer_chars=minimum_required)
        fits_payload = _assert_text_matches_structured(fits)
        assert fits_payload["ok"] is True
        assert len(fits_payload["data"]["files"][0]["diagnostics"]) == 1
        assert len(_text(fits)) == minimum_required


def test_invalid_max_answer_chars_is_rejected_without_fabricated_minimum() -> None:
    envelope = get_diagnostics_for_file(_input([], state=DiagnosticsState.CLEAN)).to_dict()

    result = compact_diagnostics_result(envelope, max_answer_chars=10)
    payload = json.loads(_text(result))

    assert result.structuredContent == payload
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["error"]["details"]["field"] == "max_answer_chars"
    assert "minimum_required_chars" not in payload["error"]["details"]


def test_malformed_success_uses_typed_canonical_presentation_failure() -> None:
    result = compact_diagnostics_result({"ok": True}, max_answer_chars=512)
    payload = _assert_text_matches_structured(result)

    assert payload == {
        "ok": False,
        "error": {
            "code": "UNSUPPORTED",
            "message": "operation is unsupported",
            "retry": None,
            "details": {"reason": "malformed_diagnostics_success"},
        },
    }


def test_malformed_truncation_uses_typed_canonical_presentation_failure() -> None:
    for malformed in ({"omitted_count": "bad"}, {"omitted_count": -1}, []):
        envelope = get_diagnostics_for_file(_input([])).to_dict()
        envelope["truncation"] = malformed

        result = compact_diagnostics_result(envelope, max_answer_chars=512)
        payload = _assert_text_matches_structured(result)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "UNSUPPORTED"
        assert payload["error"]["details"]["reason"] == "malformed_diagnostics_success"
