from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from serena_light.lsp.adapter import (
    AdapterLanguageFacts,
    AdapterRuntime,
    EngineMetadata,
    LanguageAdapter,
    PublishedDiagnosticsWitness,
)
from serena_light.lsp.client import LspTransportClosed
from serena_light.lsp.executor import BoundedLspExecutor
from serena_light.lsp.state import LspState
from serena_light.workspace.scope import (
    LanguageFamily,
    NativeProgramAttribution,
    ProjectKind,
    ScopeGenerationTracker,
    ScopeProjection,
)


class IsolationClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = deque(responses)

    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> object:
        if method == "initialize":
            return {"capabilities": {"workspaceSymbolProvider": True}}
        response = self._responses.popleft() if self._responses else []
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, Deferred):
            return response.call()
        return response

    def notify(self, method: str, params: object = None) -> None:
        pass

    def shutdown(self, *, timeout: float = 2.0) -> None:
        pass


class Deferred:
    def __init__(self, call: Callable[[], object]) -> None:
        self.call = call


class IsolationProvider:
    def __init__(self, client: IsolationClient) -> None:
        self._client = client

    def start(
        self,
        *,
        notification_handler: Callable[[str, Any], None],
        terminal_handler: Callable[[BaseException], None],
    ) -> AdapterRuntime:
        return AdapterRuntime(client=self._client)

    def stop(self, runtime: AdapterRuntime) -> None:
        runtime.client.shutdown()


def _adapter(name: str, responses: list[object]) -> tuple[LanguageAdapter, BoundedLspExecutor]:
    projection = ScopeProjection.from_attribution(
        trust_inventory_paths=("src/example.py",),
        attribution=NativeProgramAttribution(
            language=LanguageFamily.PYTHON,
            project_kind=ProjectKind.WORKSPACE_DEFAULT,
            selected_config_path=None,
            configured_program_paths=("src/example.py",),
        ),
    )
    executor = BoundedLspExecutor(queue_capacity=4, name=name)
    adapter = LanguageAdapter(
        workspace_root=Path(f"/workspace/{name}"),
        facts=AdapterLanguageFacts(
            name=name,
            language_id="python",
            extensions=frozenset({".py"}),
            engine=EngineMetadata("fake", "1", Path("/runtime/fake")),
            initialize_params={},
        ),
        runtime_provider=IsolationProvider(IsolationClient(responses)),
        executor=executor,
        scope_tracker=ScopeGenerationTracker(projection),
        lsp_state=LspState(),
        document_witness=PublishedDiagnosticsWitness(),
        operation_lock=threading.RLock(),
    )
    return adapter, executor


def test_blocked_and_failed_adapter_does_not_block_another_root() -> None:
    started = threading.Event()
    release = threading.Event()

    def block() -> list[str]:
        started.set()
        assert release.wait(1)
        return ["first-root"]

    first, first_executor = _adapter("first", [Deferred(block), LspTransportClosed("lost")])
    second, second_executor = _adapter("second", [["second-root"]])
    try:
        blocked = first.submit_read(lambda client: client.request("workspace/symbol", {}))
        assert started.wait(1)

        healthy = second.submit_read(lambda client: client.request("workspace/symbol", {}))
        assert healthy.result(timeout=0.2) == ["second-root"]
        release.set()
        assert blocked.result(timeout=1) == ["first-root"]

        failed = first.submit_edit(lambda client: client.request("workspace/symbol", {}))
        try:
            failed.result(timeout=1)
        except LspTransportClosed:
            pass
        else:  # pragma: no cover - assertion rendered explicitly for a clearer failure
            raise AssertionError("first adapter transport loss was not surfaced")
        still_healthy = second.submit_read(lambda client: client.request("workspace/symbol", {}))
        assert still_healthy.result(timeout=0.2) == []
    finally:
        release.set()
        first.stop().result(timeout=1)
        second.stop().result(timeout=1)
        first_executor.close()
        second_executor.close()
