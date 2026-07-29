"""Focused local acceptance for the task-9.6 generation barrier contract."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from serena_light.tools.envelopes import AdapterMetadata, GenerationMetadata, WorkspaceMetadata
from serena_light.tools.global_symbols import (
    ConfiguredProgramScope,
    DocumentSymbolBatch,
    GlobalAdapterState,
    GlobalSymbolService,
    WorkspaceSymbolBatch,
)
from serena_light.workspace.scope import (
    FileChangeType,
    LanguageFamily,
    NativeProgramAttribution,
    ProjectKind,
    ScopeGenerationTracker,
    ScopeProjection,
    WatchedFileEvent,
)


@dataclass
class _BarrierProvider:
    state: GlobalAdapterState
    batch: WorkspaceSymbolBatch
    document: DocumentSymbolBatch
    workspace_calls: int = 0

    def global_symbol_state(self) -> GlobalAdapterState:
        return self.state

    def workspace_symbols(self, query: str, *, max_results: int) -> WorkspaceSymbolBatch:
        assert query == "Target"
        assert max_results > 0
        self.workspace_calls += 1
        return self.batch

    def document_symbols(self, relative_path: str, uri: str) -> DocumentSymbolBatch:
        assert relative_path == "src/main.py"
        assert uri == "file:///repo/src/main.py"
        return self.document


def _metadata(program: int, index: int) -> GenerationMetadata:
    return GenerationMetadata(trust=1, program=program, document=1, index=index, scope="configured_program")


def _state(generations: GenerationMetadata, *, ready: bool) -> GlobalAdapterState:
    return GlobalAdapterState(
        WorkspaceMetadata("/repo", "git", "/repo"),
        AdapterMetadata("pyright", "python"),
        generations,
        ConfiguredProgramScope(("src/main.py",), "configured", "pyrightconfig.json"),
        True,
        ready,
        "ready" if ready else "global_warming",
        None if ready else 0.1,
    )


@pytest.mark.parametrize("change_type", tuple(FileChangeType))
def test_create_change_delete_global_query_observes_new_generation_or_returns_not_ready(
    change_type: FileChangeType,
) -> None:
    projection = ScopeProjection.from_attribution(
        trust_inventory_paths=("src/main.py",),
        attribution=NativeProgramAttribution(
            language=LanguageFamily.PYTHON,
            project_kind=ProjectKind.CONFIGURED,
            selected_config_path="pyrightconfig.json",
            configured_program_paths=("src/main.py",),
        ),
    )
    tracker = ScopeGenerationTracker(projection)
    assert tracker.observe_configured_program(1)
    transition = tracker.apply_did_change_watched_files(
        [WatchedFileEvent("src/main.py", change_type, may_change_program=True)]
    )
    assert transition.configured_program_invalidated
    assert transition.after.configured_program == 2

    stale = _metadata(program=2, index=1)
    provider = _BarrierProvider(
        state=_state(stale, ready=False),
        batch=WorkspaceSymbolBatch((), stale),
        document=DocumentSymbolBatch("src/main.py", "file:///repo/src/main.py", (), stale),
    )
    before_observation = GlobalSymbolService((provider,)).find_symbol("Target").to_dict()
    assert before_observation["error"]["code"] == "NOT_READY"
    assert provider.workspace_calls == 0

    assert tracker.observe_configured_program(2)
    current = _metadata(program=2, index=2)
    provider.state = _state(current, ready=True)
    provider.batch = WorkspaceSymbolBatch(
        (
            {
                "name": "Target",
                "kind": 12,
                "location": {
                    "uri": "file:///repo/src/main.py",
                    "range": {
                        "start": {"line": 0, "character": 4},
                        "end": {"line": 0, "character": 10},
                    },
                },
            },
        ),
        current,
    )
    provider.document = DocumentSymbolBatch(
        "src/main.py",
        "file:///repo/src/main.py",
        (
            {
                "name": "Target",
                "kind": 12,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 1, "character": 0},
                },
                "selectionRange": {
                    "start": {"line": 0, "character": 4},
                    "end": {"line": 0, "character": 10},
                },
            },
        ),
        current,
    )

    after_observation = GlobalSymbolService((provider,)).find_symbol("Target").to_dict()
    assert after_observation["ok"] is True
    assert after_observation["generations"]["program"] == 2
    assert after_observation["generations"]["index"] == 2
    assert after_observation["data"]["symbols"][0]["name"] == "Target"
    assert provider.workspace_calls == 1
