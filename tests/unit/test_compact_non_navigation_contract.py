"""Regression guards for contracts intentionally outside navigation compaction.

These assertions are deliberately structural: the compact-navigation change may
replace navigation *success* data, but must not collapse operational authority
or typed failures into a generic empty success.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from serena_light.daemon.leases import LeaseLifecycle
from serena_light.daemon.service import WorkspaceDaemonService
from serena_light.lsp.adapter import AdapterError, AdapterErrorCode
from serena_light.lsp.executor import ExecutorBusyError
from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.lsp.state import DiagnosticsSnapshot, DiagnosticsState
from serena_light.tools.compact_adapter import compact_navigation_result
from serena_light.tools.diagnostics import (
    DiagnosticDocumentInput,
    DiagnosticEngineFacts,
    DiagnosticsReadiness,
    get_diagnostics_for_file,
)
from serena_light.tools.envelopes import (
    AdapterMetadata,
    ErrorCode,
    GenerationMetadata,
    RetryMetadata,
    WorkspaceMetadata,
    from_adapter_error,
    from_executor_busy,
    from_timeout,
)
from serena_light.tools.navigation import DocumentNavigation, DocumentSymbolInput, find_symbol, get_symbols_overview
from serena_light.workspace.registry import ResolvedWorkspace, WorkspaceRuntimeRegistry


@dataclass(eq=False)
class _Runtime:
    identity: str

    def status(self) -> Mapping[str, object]:
        return {"authority": "native", "generation": 7}


def _metadata() -> tuple[WorkspaceMetadata, AdapterMetadata, GenerationMetadata]:
    return (
        WorkspaceMetadata("/workspace", "git", "/workspace/subdir"),
        AdapterMetadata("pyright", "python"),
        GenerationMetadata(trust=3, program=5, document=7, index=11, scope="path"),
    )


def _document(*, symbols: list[Mapping[str, object]] | None = None) -> DocumentSymbolInput:
    workspace, adapter, generations = _metadata()
    return DocumentSymbolInput(
        "src/example.py",
        "file:///workspace/src/example.py",
        FileSnapshot.from_bytes(b"def example():\n    pass\n"),
        [] if symbols is None else symbols,
        PositionEncoding.UTF16,
        workspace,
        adapter,
        generations,
    )


def test_runtime_status_and_workspace_lifecycle_keep_lease_authority_and_outcome_fields() -> None:
    created: list[_Runtime] = []

    def factory(identity: str) -> _Runtime:
        runtime = _Runtime(identity)
        created.append(runtime)
        return runtime

    service = WorkspaceDaemonService[str, _Runtime](
        lifecycle=LeaseLifecycle(clock=lambda: 100.0),
        registry=WorkspaceRuntimeRegistry(factory),
        resolver=lambda path, _python_environment: ResolvedWorkspace(
            identity=str(path.parent), working_subdirectory=path
        ),
        runtime_stopper=lambda _runtime: None,
    )

    async def scenario() -> None:
        lease = await service.acquire_lease(mcp_session_id="transport-only")
        lease_id = cast(str, lease["lease_id"])
        activation = await service.activate_workspace(lease_id=lease_id, absolute_path="/workspace/subdir")
        status = await service.get_runtime_status(lease_id=lease_id)
        released = await service.release_workspace(lease_id=lease_id, immediate=True)

        assert activation["lease_id"] == lease_id
        assert cast(Mapping[str, object], activation["workspace"])["working_subdirectory"] == "/workspace/subdir"
        assert status["ok"] is True
        data = cast(Mapping[str, object], status["data"])
        assert cast(Mapping[str, object], data["lease"])["lease_id"] == lease_id
        assert cast(Mapping[str, object], data["binding"])["identity"] == "/workspace"
        assert cast(Mapping[str, object], data["runtime"]) == {"authority": "native", "generation": 7}
        assert released["lease_id"] == lease_id
        assert released["released"] is True
        assert released["bound"] is False
        assert {"immediate", "reason", "active_holders", "runtime_stopped", "runtime_stop_pending"} <= released.keys()

    asyncio.run(scenario())
    assert len(created) == 1


def test_diagnostics_keep_engine_authority_and_current_generation_on_success_and_retry_on_not_ready() -> None:
    document = _document()
    engine = DiagnosticEngineFacts("pyright", "python", "1.2.3", "/opt/ms/bin/python")
    publication = DiagnosticsSnapshot(
        DiagnosticsState.CLEAN,
        document.uri,
        Path("/workspace/src/example.py"),
        1,
        7,
        13,
        (),
    )
    ready = DiagnosticDocumentInput(document, 7, engine, publication)
    current = get_diagnostics_for_file(ready).to_dict()
    assert current["ok"] is True
    assert current["workspace"] == {"root": "/workspace", "kind": "git", "working_subdirectory": "/workspace/subdir"}
    assert current["adapter"] == {"name": "pyright", "language": "python"}
    assert current["generations"] == {"trust": 3, "program": 5, "document": 7, "index": 11, "scope": "path"}
    assert current["data"]["diagnostics_generation"] == 13
    assert current["data"]["engine"]["authority"] == "engine"

    cold = DiagnosticDocumentInput(
        document,
        7,
        engine,
        None,
        DiagnosticsReadiness.NOT_READY,
        "cold",
        RetryMetadata(True, retry_after_seconds=0.25, target_generation=7, observed_generation=6),
    )
    not_ready = get_diagnostics_for_file(cold).to_dict()
    assert not_ready["ok"] is False
    assert not_ready["error"]["code"] == "NOT_READY"
    assert not_ready["error"]["retry"] == {
        "retryable": True,
        "retry_after_seconds": 0.25,
        "target_generation": 7,
        "observed_generation": 6,
    }
    assert not_ready["generations"] == current["generations"]


def test_typed_operational_failures_keep_retry_and_never_resemble_navigation_success() -> None:
    workspace, adapter, generations = _metadata()
    failures = (
        from_adapter_error(
            AdapterError(AdapterErrorCode.NOT_READY, "hidden", retry_after_seconds=0.1), adapter=adapter
        ),
        from_adapter_error(AdapterError(AdapterErrorCode.SCOPE_INCOMPATIBLE, "hidden"), adapter=adapter),
        from_adapter_error(AdapterError(AdapterErrorCode.COOLDOWN, "hidden", retry_after_seconds=3.0), adapter=adapter),
        from_executor_busy(ExecutorBusyError("hidden")),
        from_timeout(TimeoutError("hidden"), timeout_seconds=2.0),
    )
    expected = ("NOT_READY", "SCOPE_INCOMPATIBLE", "COOLDOWN", "BUSY", "TIMED_OUT")
    for failure, code in zip(failures, expected, strict=True):
        value = failure.to_dict()
        assert value["ok"] is False
        assert value["error"]["code"] == code
        assert "data" not in value
        assert "files" not in value
        assert "omitted" not in value
        assert "retry" in value["error"]

    # Error envelopes retain ownership metadata when the failing operation has it.
    enriched = from_timeout(TimeoutError(), timeout_seconds=1.0)
    assert enriched.to_dict()["error"]["retry"] == {"retryable": True, "timeout_seconds": 1.0}
    navigation_error = find_symbol(DocumentNavigation.from_input(_document()), "missing").to_dict()
    assert navigation_error["ok"] is False
    assert navigation_error["error"]["code"] == ErrorCode.SYMBOL_NOT_FOUND.value
    assert navigation_error["workspace"] == workspace.to_dict()
    assert navigation_error["adapter"] == adapter.to_dict()
    assert navigation_error["generations"] == generations.to_dict()


def test_genuine_compact_navigation_empty_success_is_distinct_from_typed_failure() -> None:
    document = DocumentNavigation.from_input(_document())
    empty = compact_navigation_result("get_symbols_overview", get_symbols_overview(document).to_dict())
    missing = find_symbol(document, "missing").to_dict()

    assert empty.structuredContent == {
        "ok": True,
        "data": {"workspace": "/workspace", "files": [], "omitted": 0},
    }
    assert missing["ok"] is False
    assert missing["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert "data" not in missing
