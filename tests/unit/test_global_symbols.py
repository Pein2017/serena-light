from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from serena_light.lsp.normalize import NormalizedSymbol, reparent
from serena_light.tools.envelopes import AdapterMetadata, GenerationMetadata, WorkspaceMetadata
from serena_light.tools.global_symbols import (
    ConfiguredProgramScope,
    DocumentSymbolBatch,
    GlobalAdapterState,
    GlobalSymbolService,
    WorkspaceSymbolBatch,
)


def _range(start_line: int, start_character: int, end_line: int, end_character: int) -> dict[str, Any]:
    return {
        "start": {"line": start_line, "character": start_character},
        "end": {"line": end_line, "character": end_character},
    }


def _candidate(name: str, relative_path: str, *, kind: int = 12, line: int = 1) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "location": {
            "uri": f"file:///repo/{relative_path}",
            "range": _range(line, 0, line, len(name)),
        },
    }


def _symbol(
    name: str,
    *,
    kind: int = 12,
    start_line: int = 1,
    end_line: int | None = None,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "range": _range(start_line, 0, start_line + 1 if end_line is None else end_line, 0),
        "selectionRange": _range(start_line, 0, start_line, len(name)),
    }
    if children is not None:
        value["children"] = children
    return value


def _generations(*, program: int = 3, index: int = 7) -> GenerationMetadata:
    return GenerationMetadata(trust=2, program=program, document=5, index=index, scope="configured_program")


def _state(
    adapter: str = "pyright",
    language: str = "python",
    *,
    paths: tuple[str, ...] = ("src/a.py",),
    supported: bool = True,
    ready: bool = True,
    generations: GenerationMetadata | None = None,
) -> GlobalAdapterState:
    return GlobalAdapterState(
        WorkspaceMetadata("/repo", "git", "/repo"),
        AdapterMetadata(adapter, language),
        generations or _generations(),
        ConfiguredProgramScope(paths, "configured", "pyrightconfig.json" if language == "python" else "tsconfig.json"),
        supported,
        ready,
        "ready" if ready else "global_warming",
        0.25 if not ready else None,
    )


@dataclass
class FakeProvider:
    state: GlobalAdapterState
    batch: WorkspaceSymbolBatch
    documents: dict[str, DocumentSymbolBatch]
    workspace_calls: list[tuple[str, int]] = field(default_factory=list)
    document_calls: list[tuple[str, str]] = field(default_factory=list)
    state_after_document: GlobalAdapterState | None = None

    def global_symbol_state(self) -> GlobalAdapterState:
        return self.state

    def workspace_symbols(self, query: str, *, max_results: int) -> WorkspaceSymbolBatch:
        self.workspace_calls.append((query, max_results))
        return self.batch

    def document_symbols(self, relative_path: str, uri: str) -> DocumentSymbolBatch:
        self.document_calls.append((relative_path, uri))
        value = self.documents[relative_path]
        if self.state_after_document is not None:
            self.state = self.state_after_document
        return value


def _provider(
    *,
    state: GlobalAdapterState | None = None,
    candidates: list[dict[str, Any]] | None = None,
    documents: dict[str, list[dict[str, Any]]] | None = None,
    batch_generations: GenerationMetadata | None = None,
    truncated: bool = False,
    omitted_count: int = 0,
) -> FakeProvider:
    actual_state = state or _state()
    document_values = documents or {"src/a.py": [_symbol("Target")]}
    batches = {
        path: DocumentSymbolBatch(
            path,
            f"file:///repo/{path}",
            raw,
            actual_state.generations,
        )
        for path, raw in document_values.items()
    }
    return FakeProvider(
        actual_state,
        WorkspaceSymbolBatch(
            candidates if candidates is not None else [_candidate("Target", "src/a.py")],
            batch_generations or actual_state.generations,
            truncated,
            omitted_count,
        ),
        batches,
    )


def test_exact_query_filters_fuzzy_and_external_candidates_before_candidate_file_requests() -> None:
    state = _state(paths=("src/a.py", "src/fuzzy.py", "src/not-returned.py"))
    external = _candidate("Target", "src/a.py")
    external["location"]["uri"] = "file:///outside/site-packages/lib.py"
    omitted = _candidate("Target", "src/not-returned.py")
    omitted["location"]["uri"] = "file:///repo/src/not-in-program.py"
    provider = _provider(
        state=state,
        candidates=[
            _candidate("TargetHelper", "src/fuzzy.py"),
            external,
            omitted,
            _candidate("Target", "src/a.py"),
            _candidate("Target", "src/a.py"),
        ],
        documents={
            "src/a.py": [
                _symbol("Outer", kind=5, start_line=0, end_line=4, children=[_symbol("Target")]),
            ],
        },
    )

    value = GlobalSymbolService([provider]).find_symbol("Outer/Target").to_dict()

    assert value["ok"] is True
    assert provider.workspace_calls == [("Target", 128)]
    assert provider.document_calls == [("src/a.py", "file:///repo/src/a.py")]
    assert [item["name_path"] for item in value["data"]["symbols"]] == ["Outer/Target"]
    assert value["data"]["scope"] == "configured_program"
    advertised = value["data"]["adapters"][0]
    assert advertised["configured_program"]["file_count"] == 3
    assert len(advertised["configured_program"]["sha256"]) == 64
    assert advertised["generations"] == {
        "trust": 2,
        "program": 3,
        "document": 5,
        "index": 7,
        "scope": "configured_program",
    }


def test_suffix_and_absolute_name_paths_are_rebuilt_from_candidate_document_trees() -> None:
    provider = _provider(
        candidates=[_candidate("Target", "src/a.py", line=2), _candidate("Target", "src/a.py", line=6)],
        documents={
            "src/a.py": [
                _symbol(
                    "Package",
                    kind=5,
                    start_line=0,
                    end_line=4,
                    children=[
                        _symbol(
                            "Outer",
                            kind=5,
                            start_line=1,
                            end_line=4,
                            children=[_symbol("Target", start_line=2)],
                        )
                    ],
                ),
                _symbol("Outer", kind=5, start_line=5, end_line=8, children=[_symbol("Target", start_line=6)]),
            ]
        },
    )
    service = GlobalSymbolService([provider])

    suffix = service.find_symbol("Outer/Target").to_dict()
    absolute = service.find_symbol("/Outer/Target").to_dict()

    assert [item["name_path"] for item in suffix["data"]["symbols"]] == [
        "Package/Outer/Target",
        "Outer/Target",
    ]
    assert [item["name_path"] for item in absolute["data"]["symbols"]] == ["Outer/Target"]
    assert provider.workspace_calls == [("Target", 128), ("Target", 128)]
    assert provider.document_calls == [
        ("src/a.py", "file:///repo/src/a.py"),
        ("src/a.py", "file:///repo/src/a.py"),
    ]


def test_substring_matching_is_opt_in_candidate_bounded_and_reports_omissions() -> None:
    state = _state(paths=("src/a.py", "src/b.py", "src/c.py"))
    provider = _provider(
        state=state,
        candidates=[
            _candidate("AlphaTarget", "src/a.py"),
            _candidate("BetaTarget", "src/b.py"),
            _candidate("GammaTarget", "src/c.py"),
        ],
        documents={
            "src/a.py": [_symbol("AlphaTarget")],
            "src/b.py": [_symbol("BetaTarget")],
            "src/c.py": [_symbol("GammaTarget")],
        },
        truncated=True,
        omitted_count=4,
    )

    value = GlobalSymbolService([provider]).find_symbol(
        "Target",
        substring_matching=True,
        max_candidates_per_adapter=2,
    ).to_dict()

    assert provider.workspace_calls == [("Target", 2)]
    assert [call[0] for call in provider.document_calls] == ["src/a.py", "src/b.py"]
    assert [item["name"] for item in value["data"]["symbols"]] == ["AlphaTarget", "BetaTarget"]
    assert value["truncation"] == {"truncated": True, "omitted_count": 5}


def test_exact_default_does_not_fetch_substring_only_candidates() -> None:
    state = _state(paths=("src/a.py", "src/b.py"))
    provider = _provider(
        state=state,
        candidates=[_candidate("TargetHelper", "src/b.py"), _candidate("Target", "src/a.py")],
        documents={"src/a.py": [_symbol("Target")], "src/b.py": [_symbol("TargetHelper")]},
    )

    value = GlobalSymbolService([provider]).find_symbol("Target").to_dict()

    assert value["ok"] is True
    assert provider.document_calls == [("src/a.py", "file:///repo/src/a.py")]


def test_capability_and_readiness_failures_are_typed_without_provider_queries() -> None:
    unsupported = _provider(state=_state(supported=False))
    not_ready = _provider(state=_state(ready=False))

    unsupported_value = GlobalSymbolService([unsupported]).find_symbol("Target").to_dict()
    not_ready_value = GlobalSymbolService([not_ready]).find_symbol("Target").to_dict()

    assert unsupported_value["error"]["code"] == "UNSUPPORTED"
    assert unsupported_value["error"]["details"]["scope"] == "configured_program"
    assert not_ready_value["error"]["code"] == "NOT_READY"
    assert not_ready_value["error"]["retry"] == {"retryable": True, "retry_after_seconds": 0.25}
    assert unsupported.workspace_calls == not_ready.workspace_calls == []
    assert unsupported.document_calls == not_ready.document_calls == []


def test_workspace_candidate_and_document_generation_drift_fail_closed_as_not_ready() -> None:
    state = _state()
    stale_batch = _provider(state=state, batch_generations=_generations(program=2))
    batch_value = GlobalSymbolService([stale_batch]).find_symbol("Target").to_dict()
    assert batch_value["error"]["code"] == "NOT_READY"
    assert stale_batch.document_calls == []

    stale_document = _provider(state=state)
    stale_document.documents["src/a.py"] = replace(
        stale_document.documents["src/a.py"],
        generations=_generations(index=6),
    )
    document_value = GlobalSymbolService([stale_document]).find_symbol("Target").to_dict()
    assert document_value["error"]["code"] == "NOT_READY"
    assert document_value["error"]["details"]["expected_generations"]["index"] == 7
    assert document_value["error"]["details"]["observed_generations"]["index"] == 6

    invalidated_during_verification = _provider(state=state)
    invalidated_during_verification.state_after_document = replace(
        state,
        generations=_generations(program=4, index=7),
        global_ready=False,
        phase="global_warming",
    )
    raced_value = GlobalSymbolService([invalidated_during_verification]).find_symbol("Target").to_dict()
    assert raced_value["error"]["code"] == "NOT_READY"
    assert raced_value["error"]["retry"]["target_generation"] == 3
    assert raced_value["error"]["retry"]["observed_generation"] == 4


def test_two_fixed_adapters_merge_deterministically_with_language_and_generation_ownership() -> None:
    python = _provider()
    typescript_state = _state("typescript", "typescript", paths=("src/a.ts",))
    typescript = _provider(
        state=typescript_state,
        candidates=[_candidate("Target", "src/a.ts")],
        documents={"src/a.ts": [_symbol("Target")]},
    )

    value = GlobalSymbolService([typescript, python]).find_symbol("Target").to_dict()

    assert [item["adapter"]["name"] for item in value["data"]["symbols"]] == ["pyright", "typescript"]
    assert [item["adapter"]["language"] for item in value["data"]["symbols"]] == ["python", "typescript"]
    assert [item["adapter"]["name"] for item in value["data"]["adapters"]] == ["pyright", "typescript"]
    assert "adapter" not in value
    assert "generations" not in value


def test_adapter_owned_normalization_and_containment_recovery_verify_full_path() -> None:
    provider = _provider(
        candidates=[_candidate("run", "src/a.py", line=2)],
        documents={"src/a.py": [_symbol("run()", start_line=2)]},
    )

    def recover(symbols: tuple[NormalizedSymbol, ...]) -> tuple[NormalizedSymbol, ...]:
        return tuple(reparent(symbol, ("Recovered",)) for symbol in symbols)

    provider.documents["src/a.py"] = replace(
        provider.documents["src/a.py"],
        normalize_name=lambda name: name.removesuffix("()"),
        recover_containment=recover,
    )

    value = GlobalSymbolService([provider]).find_symbol("/Recovered/run").to_dict()

    assert value["ok"] is True
    assert value["data"]["symbols"][0]["name_path"] == "Recovered/run"


def test_workspace_candidate_must_match_verified_document_kind_name_and_location() -> None:
    state = _state(paths=("src/a.py", "src/b.py"))
    wrong_uri = _candidate("Target", "src/a.py")
    wrong_uri["location"]["uri"] = "file:///repo/src/b.py"
    provider = _provider(
        state=state,
        candidates=[_candidate("Target", "src/a.py", kind=5), wrong_uri],
        documents={"src/a.py": [_symbol("Target", kind=12)], "src/b.py": [_symbol("Other", kind=12)]},
    )

    value = GlobalSymbolService([provider]).find_symbol("Target").to_dict()

    assert value["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert [call[0] for call in provider.document_calls] == ["src/a.py", "src/b.py"]


def test_output_char_bound_is_deterministic_and_reports_omitted_symbols() -> None:
    state = _state(paths=("src/a.py", "src/b.py"))
    provider = _provider(
        state=state,
        candidates=[_candidate("Target", "src/a.py"), _candidate("Target", "src/b.py")],
        documents={"src/a.py": [_symbol("Target")], "src/b.py": [_symbol("Target")]},
    )
    service = GlobalSymbolService([provider])
    full = service.find_symbol("Target").to_dict()
    one_symbol_size = len(
        __import__("json").dumps(
            {**full["data"], "symbols": full["data"]["symbols"][:1]},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )

    bounded = service.find_symbol("Target", max_answer_chars=one_symbol_size).to_dict()

    assert len(bounded["data"]["symbols"]) == 1
    assert bounded["truncation"] == {"truncated": True, "omitted_count": 1}


def test_invalid_bounds_provider_shape_and_missing_symbol_fail_explicitly() -> None:
    provider = _provider(candidates=[])
    service = GlobalSymbolService([provider])

    assert service.find_symbol("", max_candidates_per_adapter=1).to_dict()["error"]["code"] == "INVALID_INPUT"
    assert service.find_symbol("Target", max_candidates_per_adapter=0).to_dict()["error"]["code"] == "INVALID_INPUT"
    missing = service.find_symbol("Target").to_dict()
    assert missing["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert missing["error"]["details"]["scope"] == "configured_program"


def test_service_rejects_more_than_two_or_duplicate_fixed_adapters() -> None:
    first = _provider()
    duplicate = _provider()
    third = _provider(state=_state("third", "python"))

    try:
        GlobalSymbolService([first, duplicate])
    except ValueError as exc:
        assert "unique" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("duplicate adapters must be rejected")

    try:
        GlobalSymbolService([first, third, duplicate])
    except ValueError as exc:
        assert "maximum 2" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("a third adapter must be rejected")
