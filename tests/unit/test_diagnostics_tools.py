from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.lsp.state import DiagnosticsSnapshot, DiagnosticsState
from serena_light.tools.diagnostics import (
    DiagnosticDocumentInput,
    DiagnosticEngineFacts,
    DiagnosticsReadiness,
    DiagnosticsService,
    ExternalRootMetadata,
    get_diagnostics_for_file,
    get_diagnostics_for_symbol,
)
from serena_light.tools.envelopes import AdapterMetadata, GenerationMetadata, RetryMetadata, WorkspaceMetadata
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
    text: str = "# 😀\r\ndef outer():\r\n    return bad\r\n\ndef second():\r\n    return nope\r\n",
    generation: int = 2,
    raw_symbols: Sequence[Mapping[str, object]] | None = None,
) -> DocumentSymbolInput:
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
        WorkspaceMetadata("/repo", "git", "/repo"),
        AdapterMetadata("typescript" if mjs else "pyright", "typescript" if mjs else "python"),
        GenerationMetadata(trust=1, program=2, document=generation, index=4, scope="path"),
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
    generation: int = 2,
    readiness: DiagnosticsReadiness = DiagnosticsReadiness.READY,
    mjs: bool = False,
) -> DiagnosticDocumentInput:
    document = _document(mjs=mjs)
    publication = None
    if readiness is DiagnosticsReadiness.READY:
        publication = DiagnosticsSnapshot(
            state,
            document.uri,
            Path("/repo") / document.relative_path,
            1,
            generation,
            8,
            tuple(diagnostics or ()),
        )
    return DiagnosticDocumentInput(
        document,
        2,
        _engine(mjs=mjs),
        publication,
        readiness,
        "cold" if readiness is DiagnosticsReadiness.NOT_READY else None,
        RetryMetadata(True, retry_after_seconds=0.1) if readiness is DiagnosticsReadiness.NOT_READY else None,
        0.2,
    )


def test_empty_current_publication_is_clean_but_missing_and_stale_are_timed_out() -> None:
    clean = get_diagnostics_for_file(_input([], state=DiagnosticsState.CLEAN)).to_dict()
    missing = get_diagnostics_for_file(_input([], state=DiagnosticsState.MISSING)).to_dict()
    stale = get_diagnostics_for_file(_input([], state=DiagnosticsState.STALE, generation=1)).to_dict()
    timeout = get_diagnostics_for_file(_input(readiness=DiagnosticsReadiness.TIMED_OUT)).to_dict()
    not_ready = get_diagnostics_for_file(_input(readiness=DiagnosticsReadiness.NOT_READY)).to_dict()

    assert clean["data"]["state"] == "clean"
    assert clean["data"]["diagnostics_generation"] == 8
    for value in (missing, stale, timeout):
        assert value["error"]["code"] == "TIMED_OUT"
        assert value["error"]["details"]["state"] == "timed_out"
    assert not_ready["error"] == {
        "code": "NOT_READY",
        "message": "requested state is not ready",
        "retry": {"retryable": True, "retry_after_seconds": 0.1},
        "details": {
            "state": "not_ready",
            "phase": "cold",
            "engine": {
                "name": "pyright",
                "version": "1.1.403",
                "authority": "engine",
                "interpreter": "/root/miniconda3/envs/ms/bin/python",
                "external_root": {
                    "kind": "read_only_external",
                    "path": "/root/miniconda3/envs/ms/lib/python3.12/site-packages",
                },
            },
        },
    }


def test_default_severity_grouping_and_utf16_crlf_positions_are_deterministic() -> None:
    diagnostics = [
        {"severity": 3, "message": "info", "range": _range(2, 11, 2, 14)},
        {"severity": 2, "message": "outer warning", "source": "pyright", "range": _range(2, 11, 2, 14)},
        {"severity": 1, "message": "parse", "range": _range(0, 0, 0, 1)},
        {"severity": 1, "message": "second error", "range": _range(5, 11, 5, 15)},
    ]
    value = get_diagnostics_for_file(_input(diagnostics)).to_dict()

    assert value["data"]["state"] == "findings"
    assert [group["name_path"] for group in value["data"]["groups"]] == ["<file>", "outer", "second"]
    warning = value["data"]["groups"][1]["findings"][0]
    assert warning["severity"] == "warning"
    # The initial astral character consumes two UTF-16 units but only one text
    # character; CRLF remains two physical bytes.
    assert warning["range"]["start"] == {"line": 3, "column": 12, "text_offset": 30, "byte_offset": 33}
    engine = value["data"]["engine"]
    assert engine["version"] == "1.1.403"
    assert engine["interpreter"] == "/root/miniconda3/envs/ms/bin/python"
    assert engine["external_root"]["kind"] == "read_only_external"
    with_information = get_diagnostics_for_file(_input(diagnostics), maximum_severity=3).to_dict()
    assert any(
        finding["message"] == "info" for group in with_information["data"]["groups"] for finding in group["findings"]
    )


def test_truncation_is_deterministic_and_typescript_is_explicitly_advisory() -> None:
    diagnostics = [
        {"severity": 1, "message": "a", "range": _range(2, 11, 2, 12)},
        {"severity": 1, "message": "b", "range": _range(5, 11, 5, 12)},
    ]
    full = get_diagnostics_for_file(_input(diagnostics, mjs=True)).to_dict()
    bound = len(json.dumps(full["data"], ensure_ascii=False, separators=(",", ":"), sort_keys=True)) - 1
    truncated = get_diagnostics_for_file(_input(diagnostics, mjs=True), max_answer_chars=bound).to_dict()

    assert full["data"]["engine"]["authority"] == "advisory"
    assert full["data"]["engine"]["repository_authority"] == "repository-native typecheck or CI"
    assert full["data"]["engine"]["pinned_engine"] == {"name": "typescript", "version": "5.9.3"}
    assert full["data"]["engine"]["native_typecheck"] == {
        "authority": "authoritative",
        "command": "npm run typecheck",
    }
    assert truncated["ok"] is True
    assert truncated["truncation"]["truncated"] is True
    assert truncated["truncation"]["omitted_count"] >= 1
    assert truncated == get_diagnostics_for_file(_input(diagnostics, mjs=True), max_answer_chars=bound).to_dict()


def test_symbol_filter_uses_the_same_file_snapshot_and_rejects_ambiguity() -> None:
    diagnostics = [
        {"severity": 2, "message": "outer warning", "range": _range(2, 11, 2, 14)},
        {"severity": 1, "message": "second error", "range": _range(5, 11, 5, 15)},
    ]
    value = get_diagnostics_for_symbol(_input(diagnostics), "outer").to_dict()
    assert value["data"]["symbol"] == "outer"
    assert [item["message"] for item in value["data"]["groups"][0]["findings"]] == ["outer warning"]

    ambiguous_document = _document()
    raw = list(ambiguous_document.raw_symbols or ())
    raw.append({"name": "outer", "kind": 12, "range": _range(4, 0, 6, 0)})
    input_value = _input(diagnostics)
    ambiguous = DiagnosticDocumentInput(
        DocumentSymbolInput(
            ambiguous_document.relative_path,
            ambiguous_document.uri,
            ambiguous_document.snapshot,
            raw,
            ambiguous_document.position_encoding,
            ambiguous_document.workspace,
            ambiguous_document.adapter,
            ambiguous_document.generations,
        ),
        input_value.requested_generation,
        input_value.engine,
        input_value.publication,
    )
    assert get_diagnostics_for_symbol(ambiguous, "outer").to_dict()["error"]["code"] == "AMBIGUOUS_SYMBOL"


def test_service_uses_one_injected_file_provider_without_project_wide_lookup() -> None:
    @dataclass
    class Provider:
        calls: list[tuple[str, float]]

        def load_diagnostics(self, relative_path: str, *, timeout_seconds: float) -> DiagnosticDocumentInput:
            self.calls.append((relative_path, timeout_seconds))
            return _input([])

    provider = Provider([])
    result = DiagnosticsService(provider).get_diagnostics_for_symbol("src/example.py", "outer", timeout_seconds=0.5)
    assert result.to_dict()["data"]["state"] == "clean"
    assert provider.calls == [("src/example.py", 0.5)]


def test_typescript_args_divergence_remains_visible_but_advisory_for_every_terminal_state() -> None:
    diagnostics = [
        {
            "severity": 1,
            "message": f"TypeScript 5.9 divergence {index}",
            "range": _range(0, index, 0, index + 1),
        }
        for index in range(3)
    ]
    document = _document(
        mjs=True,
        relative_path="runtime/args.mjs",
        text="const values = [1, 2, 3];\n",
        raw_symbols=(),
    )
    publication = DiagnosticsSnapshot(
        DiagnosticsState.FINDINGS,
        document.uri,
        Path("/repo/runtime/args.mjs"),
        1,
        2,
        9,
        tuple(diagnostics),
    )
    clean_publication = DiagnosticsSnapshot(
        DiagnosticsState.CLEAN,
        document.uri,
        Path("/repo/runtime/args.mjs"),
        1,
        2,
        10,
        (),
    )
    findings = get_diagnostics_for_file(DiagnosticDocumentInput(document, 2, _engine(mjs=True), publication)).to_dict()
    clean = get_diagnostics_for_file(
        DiagnosticDocumentInput(document, 2, _engine(mjs=True), clean_publication)
    ).to_dict()
    not_ready = get_diagnostics_for_file(
        DiagnosticDocumentInput(
            document,
            2,
            _engine(mjs=True),
            readiness=DiagnosticsReadiness.NOT_READY,
            phase="cold",
        )
    ).to_dict()
    timed_out = get_diagnostics_for_file(
        DiagnosticDocumentInput(
            document,
            2,
            _engine(mjs=True),
            readiness=DiagnosticsReadiness.TIMED_OUT,
        )
    ).to_dict()

    assert findings["data"]["relative_path"] == "runtime/args.mjs"
    assert sum(len(group["findings"]) for group in findings["data"]["groups"]) == 3
    assert findings["data"]["engine"]["authority_distinction"] == {
        "pinned_lsp_diagnostics": "advisory",
        "repository_native_typecheck": "authoritative",
    }
    expected_engine = findings["data"]["engine"]
    assert clean["data"]["engine"] == expected_engine
    assert not_ready["error"]["details"]["engine"] == expected_engine
    assert timed_out["error"]["details"]["engine"] == expected_engine


def test_current_generation_transformers_import_clear_records_pinned_python_context() -> None:
    document = _document(
        relative_path="src/importer.py",
        text="from transformers import GenerationConfig\n",
        generation=7,
        raw_symbols=(),
    )
    engine = DiagnosticEngineFacts(
        "pyright",
        "python",
        "1.1.403",
        "/root/miniconda3/envs/ms/bin/python",
        external_root=ExternalRootMetadata(
            "read_only_external",
            "/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers",
        ),
    )

    @dataclass
    class Provider:
        calls: list[tuple[str, float]]

        def load_diagnostics(self, relative_path: str, *, timeout_seconds: float) -> DiagnosticDocumentInput:
            self.calls.append((relative_path, timeout_seconds))
            publication = DiagnosticsSnapshot(
                DiagnosticsState.CLEAN,
                document.uri,
                Path("/repo/src/importer.py"),
                1,
                7,
                12,
                (),
            )
            return DiagnosticDocumentInput(document, 7, engine, publication)

    provider = Provider([])
    result = DiagnosticsService(provider).get_diagnostics_for_file("src/importer.py", timeout_seconds=0.5).to_dict()

    assert result["data"]["state"] == "clean"
    assert result["generations"]["document"] == 7
    assert result["data"]["engine"] == {
        "name": "pyright",
        "version": "1.1.403",
        "authority": "engine",
        "interpreter": "/root/miniconda3/envs/ms/bin/python",
        "external_root": {
            "kind": "read_only_external",
            "path": "/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers",
        },
    }
    assert provider.calls == [("src/importer.py", 0.5)]


@pytest.mark.parametrize("maximum_severity", [0, 5])
def test_invalid_severity_is_rejected(maximum_severity: int) -> None:
    value = get_diagnostics_for_file(_input([]), maximum_severity=maximum_severity).to_dict()
    assert value["error"]["code"] == "INVALID_INPUT"
