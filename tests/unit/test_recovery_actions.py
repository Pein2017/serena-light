from __future__ import annotations

from dataclasses import dataclass

from serena_light.tools.envelopes import AdapterMetadata, GenerationMetadata, WorkspaceMetadata
from serena_light.tools.global_symbols import (
    ConfiguredProgramScope,
    DocumentSymbolBatch,
    GlobalAdapterState,
    GlobalSymbolService,
    WorkspaceSymbolBatch,
)


@dataclass
class _GlobalMissProvider:
    state: GlobalAdapterState

    def global_symbol_state(self) -> GlobalAdapterState:
        return self.state

    def workspace_symbols(self, query: str, *, max_results: int) -> WorkspaceSymbolBatch:
        del query, max_results
        return WorkspaceSymbolBatch([], self.state.generations)

    def document_symbols(self, relative_path: str, uri: str) -> DocumentSymbolBatch:
        raise AssertionError(f"global miss must not load {relative_path} ({uri})")


def test_global_symbol_miss_does_not_advertise_file_overview_recovery() -> None:
    generations = GenerationMetadata(trust=1, program=2, document=3, index=4, scope="configured_program")
    state = GlobalAdapterState(
        workspace=WorkspaceMetadata("/repo", "git", "/repo"),
        adapter=AdapterMetadata("pyright", "python"),
        generations=generations,
        configured_program=ConfiguredProgramScope(("src/main.py",), "configured", "pyrightconfig.json"),
        workspace_symbols_supported=True,
        global_ready=True,
        phase="ready",
    )

    result = GlobalSymbolService([_GlobalMissProvider(state)]).find_symbol("missing").to_dict()

    assert result["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert result["error"]["details"]["scope"] == "configured_program"
    assert "next_action" not in result["error"]["details"]
