from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from serena_light.lsp.adapter import RawLspProviders
from serena_light.lsp.positions import FileSnapshot, LspPosition, PositionEncoding
from serena_light.tools.declarations import (
    DEFINITION_METHOD,
    IMPLEMENTATION_METHOD,
    CapabilityMatrix,
    DeclarationNavigationService,
    SemanticDocumentInput,
)
from serena_light.tools.envelopes import AdapterMetadata, ErrorEnvelope, GenerationMetadata, WorkspaceMetadata
from serena_light.tools.navigation import DocumentSymbolInput


def _range(start_line: int, start_character: int, end_line: int, end_character: int) -> dict[str, Any]:
    return {
        "start": {"line": start_line, "character": start_character},
        "end": {"line": end_line, "character": end_character},
    }


def _capabilities(*, definition: bool, declaration: bool, implementation: bool) -> CapabilityMatrix:
    return CapabilityMatrix.from_raw(
        RawLspProviders(
            definition=definition,
            declaration=declaration,
            implementation=implementation,
            references=True,
            document_symbols=True,
            workspace_symbols=True,
        )
    )


def _document(
    text: str,
    symbols: list[dict[str, Any]],
    capabilities: CapabilityMatrix,
    *,
    relative_path: str = "src/example.ts",
) -> SemanticDocumentInput:
    language = "python" if relative_path.endswith(".py") else "typescript"
    return SemanticDocumentInput(
        DocumentSymbolInput(
            relative_path=relative_path,
            uri=f"file:///repo/{relative_path}",
            snapshot=FileSnapshot.from_bytes(text.encode()),
            raw_symbols=symbols,
            position_encoding=PositionEncoding.UTF16,
            workspace=WorkspaceMetadata("/repo", "git", "/repo"),
            adapter=AdapterMetadata("pyright" if language == "python" else "typescript", language),
            generations=GenerationMetadata(trust=1, program=2, document=3, index=4),
        ),
        capabilities,
    )


@dataclass
class FakeAdapter:
    semantic_document: SemanticDocumentInput
    raw_result: object = None
    normalized: list[dict[str, Any]] | ErrorEnvelope = field(default_factory=list)
    requests: list[tuple[str, str, LspPosition]] = field(default_factory=list)
    normalization_calls: list[tuple[object, bool, bool]] = field(default_factory=list)

    def load_semantic_document(self, relative_path: str) -> SemanticDocumentInput:
        assert relative_path == self.semantic_document.document.relative_path
        return self.semantic_document

    def request_locations(self, method: str, *, document_uri: str, position: LspPosition) -> object:
        self.requests.append((method, document_uri, position))
        return self.raw_result

    def normalize_and_classify_locations(
        self,
        raw_locations: object,
        *,
        include_body: bool,
        include_info: bool,
    ) -> list[dict[str, Any]] | ErrorEnvelope:
        self.normalization_calls.append((raw_locations, include_body, include_info))
        return self.normalized


def test_find_declaration_uses_definition_provider_and_method_when_declaration_provider_is_false() -> None:
    text = "// 😀\r\nconst value = api.\r\n  target();\r\n"
    adapter = FakeAdapter(
        _document(text, [], _capabilities(definition=True, declaration=False, implementation=True)),
        raw_result={"uri": "file:///repo/src/target.ts"},
        normalized=[
            {
                "relative_path": "src/target.ts",
                "kind": 12,
                "location_kind": "workspace",
            }
        ],
    )

    value = (
        DeclarationNavigationService(adapter)
        .find_declaration(
            "src/example.ts",
            r"api\.\s*(target)\(\)",
            include_body=True,
            include_info=True,
        )
        .to_dict()
    )

    assert value["ok"] is True
    assert adapter.requests == [(DEFINITION_METHOD, "file:///repo/src/example.ts", LspPosition(line=2, character=2))]
    assert all(method != "textDocument/declaration" for method, _, _ in adapter.requests)
    assert adapter.normalization_calls == [({"uri": "file:///repo/src/target.ts"}, True, True)]
    assert value["data"]["capabilities"]["raw"]["definitionProvider"] is True
    assert value["data"]["capabilities"]["raw"]["declarationProvider"] is False


def test_find_declaration_does_not_use_declaration_provider_as_a_fallback_gate() -> None:
    adapter = FakeAdapter(
        _document("target();\n", [], _capabilities(definition=False, declaration=True, implementation=False))
    )

    value = DeclarationNavigationService(adapter).find_declaration("src/example.ts", r"(target)\(\)").to_dict()

    assert value["error"]["code"] == "UNSUPPORTED"
    assert value["error"]["details"]["method"] == DEFINITION_METHOD
    assert value["error"]["details"]["capabilities"]["raw"] == {
        "definitionProvider": False,
        "declarationProvider": True,
        "implementationProvider": False,
        "referencesProvider": True,
        "documentSymbolProvider": True,
        "workspaceSymbolProvider": True,
    }
    assert adapter.requests == []


@pytest.mark.parametrize("regex", [r"target", r"(target)(alias)", "("])
def test_find_declaration_rejects_invalid_capture_contract_before_dispatch(regex: str) -> None:
    adapter = FakeAdapter(
        _document("target();\n", [], _capabilities(definition=True, declaration=True, implementation=False))
    )

    value = DeclarationNavigationService(adapter).find_declaration("src/example.ts", regex).to_dict()

    assert value["error"]["code"] == "INVALID_INPUT"
    assert adapter.requests == []


def test_find_declaration_restricts_multiline_dotall_regex_to_one_unique_container_body() -> None:
    text = "function first() {\n  api.target();\n}\nfunction second() {\n  api.\n    target();\n}\n"
    symbols = [
        {"name": "first", "kind": 12, "range": _range(0, 0, 3, 0)},
        {"name": "second", "kind": 12, "range": _range(3, 0, 7, 0)},
    ]
    adapter = FakeAdapter(
        _document(text, symbols, _capabilities(definition=True, declaration=False, implementation=True)),
        normalized=[{"relative_path": "src/api.ts", "kind": 12, "location_kind": "workspace"}],
    )
    service = DeclarationNavigationService(adapter)

    ambiguous = service.find_declaration("src/example.ts", r"api\.\s*(target)\(\)").to_dict()
    selected = service.find_declaration(
        "src/example.ts",
        r"api\.\s*(target)\(\)",
        containing_symbol_name_path="second",
    ).to_dict()

    assert ambiguous["error"]["code"] == "AMBIGUOUS_SYMBOL"
    assert selected["ok"] is True
    assert adapter.requests == [(DEFINITION_METHOD, "file:///repo/src/example.ts", LspPosition(5, 4))]


def test_find_declaration_reports_container_ambiguity_without_dispatch() -> None:
    text = "function run() { api.target(); }\nfunction run() { api.target(); }\n"
    symbols = [
        {"name": "run", "kind": 12, "range": _range(0, 0, 0, 32)},
        {"name": "run", "kind": 12, "range": _range(1, 0, 1, 32)},
    ]
    adapter = FakeAdapter(
        _document(text, symbols, _capabilities(definition=True, declaration=False, implementation=True))
    )

    value = (
        DeclarationNavigationService(adapter)
        .find_declaration("src/example.ts", r"api\.(target)", containing_symbol_name_path="run")
        .to_dict()
    )

    assert value["error"]["code"] == "AMBIGUOUS_SYMBOL"
    assert adapter.requests == []


def test_supported_definition_empty_result_is_symbol_not_found_after_normalization() -> None:
    adapter = FakeAdapter(
        _document("target();\n", [], _capabilities(definition=True, declaration=False, implementation=False)),
        raw_result=None,
        normalized=[],
    )

    value = DeclarationNavigationService(adapter).find_declaration("src/example.ts", r"(target)\(\)").to_dict()

    assert value["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert adapter.requests[0][0] == DEFINITION_METHOD
    assert adapter.normalization_calls == [(None, False, False)]


def test_python_external_definition_is_returned_from_injected_classification_seam() -> None:
    adapter = FakeAdapter(
        _document(
            "from transformers import GenerationConfig\n",
            [],
            _capabilities(definition=True, declaration=True, implementation=False),
            relative_path="src/main.py",
        ),
        raw_result={"uri": "file:///conda/site-packages/transformers/configuration_utils.py"},
        normalized=[
            {
                "absolute_path": "/conda/site-packages/transformers/configuration_utils.py",
                "kind": 5,
                "location_kind": "read_only_external",
                "read_only_external": True,
            }
        ],
    )

    value = (
        DeclarationNavigationService(adapter).find_declaration("src/main.py", r"import\s+(GenerationConfig)").to_dict()
    )

    assert value["ok"] is True
    assert value["data"]["locations"][0]["read_only_external"] is True
    assert adapter.requests[0][0] == DEFINITION_METHOD


def test_pyright_implementations_are_unsupported_with_raw_and_derived_matrices() -> None:
    symbols = [{"name": "Runner", "kind": 11, "range": _range(0, 0, 0, 16)}]
    adapter = FakeAdapter(
        _document(
            "class Runner: ...\n",
            symbols,
            _capabilities(definition=True, declaration=True, implementation=False),
            relative_path="src/main.py",
        )
    )

    value = DeclarationNavigationService(adapter).find_implementations("Runner", "src/main.py").to_dict()

    assert value["error"]["code"] == "UNSUPPORTED"
    matrices = value["error"]["details"]["capabilities"]
    assert matrices["raw"]["implementationProvider"] is False
    assert matrices["derived"]["find_implementations"] is False
    assert matrices["raw"]["declarationProvider"] is True
    assert adapter.requests == []


def test_typescript_implementations_use_implementation_method_filters_and_deterministic_order() -> None:
    symbols = [
        {
            "name": "Runner",
            "kind": 11,
            "range": _range(0, 0, 2, 1),
            "selectionRange": _range(0, 10, 0, 16),
        }
    ]
    adapter = FakeAdapter(
        _document(
            "interface Runner {\n  run(): void;\n}\n",
            symbols,
            _capabilities(definition=True, declaration=False, implementation=True),
        ),
        raw_result=[{"uri": "file:///repo/src/a.ts"}, {"uri": "file:///repo/src/z.ts"}],
        normalized=[
            {"relative_path": "src/z.ts", "name_path": "ZRunner", "kind": 5},
            {"relative_path": "src/a.ts", "name_path": "ARunner", "kind": 6},
            {"relative_path": "src/m.ts", "name_path": "MRunner", "kind": 5},
        ],
    )

    value = (
        DeclarationNavigationService(adapter)
        .find_implementations(
            "Runner",
            "src/example.ts",
            include_info=True,
            include_kinds=[5, 6],
            exclude_kinds=[5],
            max_answer_chars=2_000,
        )
        .to_dict()
    )

    assert value["ok"] is True
    assert adapter.requests == [
        (IMPLEMENTATION_METHOD, "file:///repo/src/example.ts", LspPosition(line=0, character=10))
    ]
    assert adapter.normalization_calls == [(adapter.raw_result, False, True)]
    assert value["data"]["locations"] == [{"relative_path": "src/a.ts", "name_path": "ARunner", "kind": 6}]
    assert value["truncation"] == {"truncated": False, "omitted_count": 0}


def test_typescript_implementation_location_without_symbol_kind_is_preserved() -> None:
    symbols = [{"name": "Runner", "kind": 11, "range": _range(0, 0, 0, 19)}]
    adapter = FakeAdapter(
        _document(
            "interface Runner {}\n",
            symbols,
            _capabilities(definition=True, declaration=False, implementation=True),
        ),
        raw_result=[
            {
                "uri": "file:///repo/src/runner.ts",
                "range": _range(0, 13, 0, 19),
            }
        ],
        normalized=[
            {
                "relative_path": "src/runner.ts",
                "location_kind": "workspace",
                "range": {"start": {"line": 1, "column": 14}, "end": {"line": 1, "column": 20}},
            }
        ],
    )

    value = DeclarationNavigationService(adapter).find_implementations("Runner", "src/example.ts").to_dict()

    assert value["ok"] is True
    assert value["data"]["locations"] == adapter.normalized
    assert "kind" not in value["data"]["locations"][0]


def test_implementation_kind_filters_use_positive_evidence_for_unknown_kinds() -> None:
    symbols = [{"name": "Runner", "kind": 11, "range": _range(0, 0, 0, 19)}]
    unknown = {"relative_path": "src/unknown.ts", "location_kind": "workspace"}
    adapter = FakeAdapter(
        _document(
            "interface Runner {}\n",
            symbols,
            _capabilities(definition=True, declaration=False, implementation=True),
        ),
        normalized=[
            unknown,
            {"relative_path": "src/included.ts", "location_kind": "workspace", "kind": 6},
            {"relative_path": "src/excluded.ts", "location_kind": "workspace", "kind": 5},
        ],
    )
    service = DeclarationNavigationService(adapter)

    included = service.find_implementations(
        "Runner", "src/example.ts", include_kinds=[5, 6], exclude_kinds=[5]
    ).to_dict()
    excluded = service.find_implementations("Runner", "src/example.ts", exclude_kinds=[5]).to_dict()

    assert included["data"]["locations"] == [
        {"relative_path": "src/included.ts", "location_kind": "workspace", "kind": 6}
    ]
    assert excluded["data"]["locations"] == [
        {"relative_path": "src/included.ts", "location_kind": "workspace", "kind": 6},
        unknown,
    ]


def test_implementation_answer_bound_is_deterministic() -> None:
    symbols = [{"name": "Runner", "kind": 11, "range": _range(0, 0, 0, 16)}]
    adapter = FakeAdapter(
        _document(
            "interface Runner {}\n",
            symbols,
            _capabilities(definition=True, declaration=False, implementation=True),
        ),
        normalized=[
            {"relative_path": "src/z.ts", "name_path": "Z", "kind": 5},
            {"relative_path": "src/a.ts", "name_path": "A", "kind": 5},
        ],
    )
    service = DeclarationNavigationService(adapter)
    full = service.find_implementations("Runner", "src/example.ts", max_answer_chars=2_000).to_dict()
    base_size = len(
        '{"capabilities":{"derived":{"find_declaration":true,"find_implementations":true,'
        '"find_referencing_symbols":true,"get_symbols_overview":true,"global_find_symbol":true},'
        '"raw":{"declarationProvider":false,"definitionProvider":true,"documentSymbolProvider":true,'
        '"implementationProvider":true,"referencesProvider":true,"workspaceSymbolProvider":true}},'
        '"locations":[],"name_path":"Runner","relative_path":"src/example.ts"}'
    )
    bounded = service.find_implementations("Runner", "src/example.ts", max_answer_chars=base_size + 70).to_dict()

    assert [item["relative_path"] for item in full["data"]["locations"]] == ["src/a.ts", "src/z.ts"]
    assert bounded["ok"] is True
    assert bounded["data"]["locations"] == [{"relative_path": "src/a.ts", "name_path": "A", "kind": 5}]
    assert bounded["truncation"] == {"truncated": True, "omitted_count": 1}


@pytest.mark.parametrize("kinds", [[0], [27], [True], 5])
def test_implementation_kind_filters_fail_fast(kinds: Any) -> None:
    symbols = [{"name": "Runner", "kind": 11, "range": _range(0, 0, 0, 16)}]
    adapter = FakeAdapter(
        _document(
            "interface Runner {}\n",
            symbols,
            _capabilities(definition=True, declaration=False, implementation=True),
        )
    )

    value = (
        DeclarationNavigationService(adapter)
        .find_implementations("Runner", "src/example.ts", include_kinds=kinds)
        .to_dict()
    )

    assert value["error"]["code"] == "INVALID_INPUT"
    assert adapter.requests == []
