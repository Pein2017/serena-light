from __future__ import annotations

import io
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from serena_light.lsp.adapter import (
    AdapterError,
    AdapterErrorCode,
    AdapterLanguageFacts,
    AdapterPhase,
    AdapterRuntime,
    BoundedStderrCapture,
    CrashPolicy,
    DerivedToolAvailability,
    DocumentSymbolReadinessProbe,
    EngineMetadata,
    GlobalReadinessWitness,
    LanguageAdapter,
    PublishedDiagnosticsWitness,
    ReadinessWitnessError,
    read_only_client_request_handlers,
)
from serena_light.lsp.client import LspResponseError, LspTransportClosed
from serena_light.lsp.executor import BoundedLspExecutor
from serena_light.lsp.positions import PositionEncoding
from serena_light.lsp.state import LspState
from serena_light.workspace.scope import (
    FileChangeType,
    LanguageFamily,
    NativeProgramAttribution,
    ProjectKind,
    ReadinessCode,
    ReadinessResult,
    ScopeGenerationTracker,
    ScopeProjection,
    WatchedFileEvent,
)


@dataclass(frozen=True)
class Deferred:
    call: Callable[[], object]


class FakeClient:
    def __init__(
        self,
        initialize_result: Mapping[str, object],
        behaviors: Mapping[str, list[object]] | None = None,
    ) -> None:
        self.initialize_result = initialize_result
        self.behaviors = {method: deque(values) for method, values in (behaviors or {}).items()}
        self.requests: list[tuple[str, object, float | None]] = []
        self.notifications: list[tuple[str, object]] = []
        self.shutdown_count = 0

    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> object:
        self.requests.append((method, params, timeout))
        if method == "initialize":
            return self.initialize_result
        behavior = self.behaviors.get(method)
        result = behavior.popleft() if behavior else []
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, Deferred):
            return result.call()
        return result

    def notify(self, method: str, params: object = None) -> None:
        self.notifications.append((method, params))

    def shutdown(self, *, timeout: float = 2.0) -> None:
        self.shutdown_count += 1


class FakeRuntimeProvider:
    def __init__(
        self,
        client_factories: list[Callable[[], FakeClient]],
        *,
        processes: list[subprocess.Popen[bytes] | None] | None = None,
    ) -> None:
        self._factories = deque(client_factories)
        self._processes = deque(processes or [])
        self.clients: list[FakeClient] = []
        self.stop_count = 0
        self.notification_handler: Callable[[str, Any], None] | None = None
        self.terminal_handler: Callable[[BaseException], None] | None = None

    def start(
        self,
        *,
        notification_handler: Callable[[str, Any], None],
        terminal_handler: Callable[[BaseException], None],
    ) -> AdapterRuntime:
        if self._factories:
            created = self._factories.popleft()()
        elif self.clients:
            created = self.clients[-1]
        else:
            raise RuntimeError("fake provider has no client factory")
        self.clients.append(created)
        self.notification_handler = notification_handler
        self.terminal_handler = terminal_handler
        process = self._processes.popleft() if self._processes else None
        return AdapterRuntime(client=created, process=process)

    def stop(self, runtime: AdapterRuntime) -> None:
        self.stop_count += 1
        runtime.client.shutdown()

    def publish(self, *, uri: str, version: int | None, diagnostics: list[object] | None = None) -> None:
        assert self.notification_handler is not None
        self.notification_handler(
            "textDocument/publishDiagnostics",
            {"uri": uri, "version": version, "diagnostics": diagnostics or []},
        )


def _initialize_result(*, implementation: bool = False) -> dict[str, object]:
    return {
        "capabilities": {
            "positionEncoding": "utf-8",
            "definitionProvider": {"workDoneProgress": False},
            "declarationProvider": True,
            "implementationProvider": implementation,
            "referencesProvider": True,
            "documentSymbolProvider": True,
            "workspaceSymbolProvider": True,
        }
    }


def _projection(paths: tuple[str, ...] = ("src/example.py",)) -> ScopeProjection:
    return ScopeProjection.from_attribution(
        trust_inventory_paths=paths,
        attribution=NativeProgramAttribution(
            language=LanguageFamily.PYTHON,
            project_kind=ProjectKind.WORKSPACE_DEFAULT,
            selected_config_path=None,
            configured_program_paths=paths,
        ),
    )


def _facts() -> AdapterLanguageFacts:
    return AdapterLanguageFacts(
        name="fake-python",
        language_id="python",
        extensions=frozenset({".PY", ".pyi"}),
        engine=EngineMetadata("fake", "1.2.3", Path("/runtime/fake")),
        initialize_params={"rootUri": "file:///workspace"},
    )


class AdapterHarness:
    def __init__(
        self,
        provider: FakeRuntimeProvider,
        *,
        paths: tuple[str, ...] = ("src/example.py",),
        crash_policy: CrashPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        timestamp: Callable[[], float] = time.time,
        debug_reporter: Callable[[str, str], object] | None = None,
    ) -> None:
        self.provider = provider
        self.executor = BoundedLspExecutor(queue_capacity=8, name="adapter-test")
        self.lock = threading.RLock()
        self.scope = ScopeGenerationTracker(_projection(paths), max_wait_seconds=0.2)
        self.state = LspState()
        self.adapter = LanguageAdapter(
            workspace_root=Path("/workspace"),
            facts=_facts(),
            runtime_provider=provider,
            executor=self.executor,
            scope_tracker=self.scope,
            lsp_state=self.state,
            document_witness=PublishedDiagnosticsWitness(),
            operation_lock=self.lock,
            crash_policy=crash_policy,
            readiness_timeout=0.2,
            clock=clock,
            timestamp=timestamp,
            debug_reporter=debug_reporter,
        )

    def close(self) -> None:
        self.adapter.stop().result(timeout=1)
        self.executor.close()


def _ready_document(harness: AdapterHarness, *, path: str = "src/example.py", version: int = 1) -> None:
    uri = f"file:///workspace/{path}"
    target = harness.adapter.open_document(
        relative_path=path,
        uri=uri,
        version=version,
        text="def Witness():\n    pass\n",
    ).result(timeout=1)
    harness.provider.publish(uri=uri, version=version)
    assert harness.adapter.wait_for_document(target, timeout=0.1).ready


def test_lazy_start_extension_routing_and_capability_derivation() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result(implementation=True))])
    harness = AdapterHarness(provider)
    try:
        cold = harness.adapter.snapshot()
        assert cold.phase is AdapterPhase.COLD
        assert not cold.running
        assert provider.clients == []
        assert harness.adapter.routes("module.PY")
        assert not harness.adapter.routes("module.ts")

        started = harness.adapter.start().result(timeout=1)
        assert started.phase is AdapterPhase.STARTING
        assert started.running
        assert started.position_encoding is PositionEncoding.UTF8
        assert started.raw_providers.declaration
        assert started.derived_tools == DerivedToolAvailability(
            find_declaration=True,
            find_implementations=True,
            find_referencing_symbols=True,
            get_symbols_overview=True,
            global_find_symbol=True,
        )
        assert provider.clients[0].requests[0][0] == "initialize"
    finally:
        harness.close()


def test_language_id_routing_can_vary_by_extension() -> None:
    facts = AdapterLanguageFacts(
        name="fake-typescript",
        language_id="typescript",
        extensions=frozenset({".ts", ".tsx", ".js"}),
        language_ids={".ts": "typescript", ".tsx": "typescriptreact", ".js": "javascript"},
        engine=EngineMetadata("fake", "1.2.3", Path("/runtime/fake")),
        initialize_params={},
    )

    assert facts.language_id_for("src/view.tsx") == "typescriptreact"
    assert facts.language_id_for("src/main.js") == "javascript"
    with pytest.raises(ValueError, match="language_ids"):
        AdapterLanguageFacts(
            name="incomplete",
            language_id="typescript",
            extensions=frozenset({".ts", ".tsx"}),
            language_ids={".ts": "typescript"},
            engine=EngineMetadata("fake", "1.2.3", Path("/runtime/fake")),
            initialize_params={},
        )


def test_read_only_server_request_handlers_ack_protocol_without_allowing_edits() -> None:
    handlers = read_only_client_request_handlers(lambda params: [{"seen": params}])

    assert handlers["window/workDoneProgress/create"]({}) is None
    assert handlers["client/registerCapability"]({}) is None
    assert handlers["workspace/configuration"]({"items": []}) == [{"seen": {"items": []}}]
    assert handlers["workspace/applyEdit"]({}) == {
        "applied": False,
        "failureReason": "serena-light language adapters are read-only",
    }


def test_document_readiness_requires_current_correlated_publication_and_path_generation() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())])
    harness = AdapterHarness(provider)
    try:
        uri = "file:///workspace/src/example.py"
        target = harness.adapter.open_document(
            relative_path="src/example.py",
            uri=uri,
            version=7,
            text="def Witness():\n    pass\n",
        ).result(timeout=1)
        provider.publish(uri=uri, version=6)
        stale = harness.adapter.wait_for_document(target, timeout=0)
        assert stale.code is ReadinessCode.NOT_READY
        assert harness.adapter.snapshot().phase is AdapterPhase.STARTING

        provider.publish(uri=uri, version=7)
        assert harness.adapter.wait_for_document(target, timeout=0).ready
        snapshot = harness.adapter.snapshot()
        assert snapshot.phase is AdapterPhase.DOCUMENT_READY
        assert snapshot.generations.document == 1
        assert [transition.phase for transition in snapshot.transitions] == [
            AdapterPhase.COLD,
            AdapterPhase.STARTING,
            AdapterPhase.DOCUMENT_READY,
        ]
        assert all(transition.timestamp > 0 for transition in snapshot.transitions)
    finally:
        harness.close()


def test_transition_history_is_bounded_to_the_latest_64_entries() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())])
    harness = AdapterHarness(provider)
    try:
        for index in range(80):
            phase = AdapterPhase.STARTING if index % 2 == 0 else AdapterPhase.COLD
            harness.adapter._transition(phase, f"test-{index}")

        transitions = harness.adapter.snapshot().transitions
        assert len(transitions) == 64
        assert transitions[0].reason == "test-16"
        assert transitions[-1].reason == "test-79"
    finally:
        harness.close()


def test_document_witness_cannot_observe_an_invalidated_path_generation() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())])
    harness = AdapterHarness(provider)
    try:
        uri = "file:///workspace/src/example.py"
        target = harness.adapter.open_document(
            relative_path="src/example.py",
            uri=uri,
            version=1,
            text="def Witness():\n    pass\n",
        ).result(timeout=1)
        harness.scope.apply_did_change_watched_files([WatchedFileEvent("src/example.py", FileChangeType.CHANGED)])
        provider.publish(uri=uri, version=1)

        assert not harness.adapter.wait_for_document(target, timeout=0).ready
        assert harness.adapter.snapshot().phase is AdapterPhase.STARTING
    finally:
        harness.close()


def test_document_symbol_probe_is_an_explicit_fallback_when_no_publication_arrives() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result(), {"textDocument/documentSymbol": [[]]})])
    harness = AdapterHarness(provider)
    try:
        uri = "file:///workspace/src/example.py"
        target = harness.adapter.open_document(
            relative_path="src/example.py",
            uri=uri,
            version=1,
            text="",
        ).result(timeout=1)

        harness.adapter.probe_document(target, DocumentSymbolReadinessProbe()).result(timeout=1)

        assert harness.adapter.wait_for_document(target, timeout=0).ready
        assert provider.clients[0].requests[-1] == (
            "textDocument/documentSymbol",
            {"textDocument": {"uri": uri}},
            0.2,
        )
        assert harness.adapter.snapshot().phase is AdapterPhase.DOCUMENT_READY
    finally:
        harness.close()


def test_global_warmup_uses_injected_exact_witness_and_current_program_generation() -> None:
    arbitrary_symbol = "A_Dynamically_Selected_Sentinel"
    sentinel_uri = "file:///workspace/src/example.py"
    provider = FakeRuntimeProvider(
        [
            lambda: FakeClient(
                _initialize_result(),
                {"workspace/symbol": [[{"name": arbitrary_symbol, "kind": 12, "location": {"uri": sentinel_uri}}]]},
            )
        ]
    )
    harness = AdapterHarness(provider)
    try:
        _ready_document(harness)
        exact = harness.adapter.warm_global(GlobalReadinessWitness(arbitrary_symbol, sentinel_uri)).result(timeout=1)
        assert exact == ({"name": arbitrary_symbol, "kind": 12, "location": {"uri": sentinel_uri}},)
        assert provider.clients[0].requests[-1][1] == {"query": arbitrary_symbol}
        ready = harness.adapter.wait_for_global(timeout=0)
        assert ready.ready
        snapshot = harness.adapter.snapshot()
        assert snapshot.phase is AdapterPhase.READY
        assert snapshot.generations.index == 1
        assert snapshot.generations.program == ready.observed_generation
    finally:
        harness.close()


def test_global_warmup_fails_when_exact_symbol_is_absent() -> None:
    provider = FakeRuntimeProvider(
        [
            lambda: FakeClient(
                _initialize_result(),
                {
                    "workspace/symbol": [
                        [
                            {"name": "SimilarOnly", "location": {"uri": "file:///workspace/src/example.py"}},
                            {"name": "MissingExact", "location": {"uri": "file:///workspace/src/other.py"}},
                        ]
                    ]
                },
            )
        ]
    )
    harness = AdapterHarness(provider)
    try:
        _ready_document(harness)
        with pytest.raises(ReadinessWitnessError, match="did not return exact symbol/path") as caught:
            harness.adapter.warm_global(
                GlobalReadinessWitness("MissingExact", "file:///workspace/src/example.py")
            ).result(timeout=1)
        assert caught.value.code is AdapterErrorCode.NOT_READY
        assert harness.adapter.snapshot().phase is AdapterPhase.DEGRADED
        assert not harness.adapter.wait_for_global(timeout=0).ready
    finally:
        harness.close()


def test_global_witness_cannot_observe_a_changed_program_generation() -> None:
    requested = threading.Event()
    release = threading.Event()

    def delayed_exact() -> list[dict[str, object]]:
        requested.set()
        assert release.wait(1)
        return [
            {
                "name": "CurrentBeforeDispatch",
                "location": {"uri": "file:///workspace/src/example.py"},
            }
        ]

    provider = FakeRuntimeProvider(
        [
            lambda: FakeClient(
                _initialize_result(),
                {"workspace/symbol": [Deferred(delayed_exact)]},
            )
        ]
    )
    harness = AdapterHarness(provider)
    try:
        _ready_document(harness)
        warming = harness.adapter.warm_global(
            GlobalReadinessWitness("CurrentBeforeDispatch", "file:///workspace/src/example.py")
        )
        assert requested.wait(1)
        harness.scope.apply_did_change_watched_files(
            [WatchedFileEvent("src/example.py", FileChangeType.CHANGED, may_change_program=True)]
        )
        release.set()
        with pytest.raises(ReadinessWitnessError, match="generation changed"):
            warming.result(timeout=1)
        assert not harness.adapter.wait_for_global(timeout=0).ready
        assert harness.adapter.snapshot().generations.index == 0
    finally:
        release.set()
        harness.close()


def test_global_readiness_wait_does_not_acquire_workspace_operation_lock() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())])
    harness = AdapterHarness(provider)
    try:
        harness.lock.acquire()
        observed: list[ReadinessResult] = []
        waiter = threading.Thread(target=lambda: observed.append(harness.adapter.wait_for_global(timeout=0.2)))
        waiter.start()
        time.sleep(0.02)
        assert harness.scope.observe_configured_program(1)
        waiter.join(timeout=0.2)
        assert not waiter.is_alive()
        assert observed[0].ready
    finally:
        harness.lock.release()
        harness.close()


def test_read_only_transport_loss_restarts_and_retries_exactly_once() -> None:
    provider = FakeRuntimeProvider(
        [
            lambda: FakeClient(_initialize_result(), {"textDocument/definition": [LspTransportClosed("lost")]}),
            lambda: FakeClient(_initialize_result(), {"textDocument/definition": [["recovered"]]}),
        ]
    )
    harness = AdapterHarness(provider)
    try:
        result = harness.adapter.submit_read(
            lambda client: client.request("textDocument/definition", {"position": {}})
        ).result(timeout=1)
        assert result == ["recovered"]
        assert len(provider.clients) == 2
        assert provider.stop_count == 1
        assert harness.adapter.snapshot().crash.total == 1
    finally:
        harness.close()


@pytest.mark.parametrize(
    "error",
    [
        LspResponseError(-32602, "invalid params"),
        ValueError("invalid input"),
        TimeoutError("ordinary timeout"),
        AdapterError(AdapterErrorCode.SCOPE_INCOMPATIBLE, "scope incompatible"),
    ],
)
def test_semantic_input_and_timeout_failures_are_not_retried(error: BaseException) -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result(), {"textDocument/definition": [error]})])
    harness = AdapterHarness(provider)
    try:
        with pytest.raises(type(error)):
            harness.adapter.submit_read(lambda client: client.request("textDocument/definition", {})).result(timeout=1)
        assert len(provider.clients) == 1
        assert harness.adapter.snapshot().crash.total == 0
    finally:
        harness.close()


class ExitedFakeProcess:
    returncode = 9

    def poll(self) -> int:
        return self.returncode


def test_process_loss_is_observed_and_read_only_work_restarts_once() -> None:
    exited = cast(subprocess.Popen[bytes], ExitedFakeProcess())
    provider = FakeRuntimeProvider(
        [
            lambda: FakeClient(_initialize_result()),
            lambda: FakeClient(_initialize_result(), {"workspace/symbol": [["restarted"]]}),
        ],
        processes=[exited, None],
    )
    harness = AdapterHarness(provider)
    try:
        result = harness.adapter.submit_read(lambda client: client.request("workspace/symbol", {})).result(timeout=1)
        assert result == ["restarted"]
        assert len(provider.clients) == 2
        assert harness.adapter.snapshot().crash.total == 1
    finally:
        harness.close()


def test_startup_transport_failure_enters_the_crash_circuit() -> None:
    def fail_start() -> FakeClient:
        raise LspTransportClosed("startup transport failed")

    provider = FakeRuntimeProvider([fail_start])
    harness = AdapterHarness(
        provider,
        crash_policy=CrashPolicy(threshold=1, window_seconds=10, cooldown_seconds=5),
    )
    try:
        with pytest.raises(LspTransportClosed, match="startup transport failed"):
            harness.adapter.start().result(timeout=1)
        snapshot = harness.adapter.snapshot()
        assert snapshot.crash.total == 1
        assert snapshot.phase is AdapterPhase.COOLDOWN
    finally:
        harness.close()


def test_stderr_capture_retains_only_the_bounded_tail() -> None:
    capture = BoundedStderrCapture(io.BytesIO(b"0123456789"), max_bytes=4)
    capture.join(timeout=1)

    assert capture.snapshot() == b"6789"


def test_edit_transport_loss_is_never_retried() -> None:
    provider = FakeRuntimeProvider(
        [lambda: FakeClient(_initialize_result(), {"workspace/applyEdit": [LspTransportClosed("lost edit")]})]
    )
    harness = AdapterHarness(provider)
    try:
        with pytest.raises(LspTransportClosed, match="lost edit"):
            harness.adapter.submit_edit(lambda client: client.request("workspace/applyEdit", {})).result(timeout=1)
        assert len(provider.clients) == 1
        assert harness.adapter.snapshot().crash.total == 1
    finally:
        harness.close()


def test_per_adapter_circuit_breaker_isolated_and_expires() -> None:
    now = [10.0]
    debug_events: list[tuple[str, str]] = []
    failing_provider = FakeRuntimeProvider(
        [
            lambda: FakeClient(
                _initialize_result(),
                {"workspace/symbol": [LspTransportClosed("secret source payload")]},
            ),
            lambda: FakeClient(_initialize_result(), {"workspace/symbol": [["after cooldown"]]}),
        ]
    )
    healthy_provider = FakeRuntimeProvider(
        [lambda: FakeClient(_initialize_result(), {"workspace/symbol": [["healthy"]]})]
    )
    failing = AdapterHarness(
        failing_provider,
        crash_policy=CrashPolicy(threshold=1, window_seconds=10, cooldown_seconds=5),
        clock=lambda: now[0],
        timestamp=lambda: 1000 + now[0],
        debug_reporter=lambda event, message: debug_events.append((event, message)),
    )
    healthy = AdapterHarness(healthy_provider)
    try:
        with pytest.raises(AdapterError) as caught:
            failing.adapter.submit_read(lambda client: client.request("workspace/symbol", {})).result(timeout=1)
        assert caught.value.code is AdapterErrorCode.COOLDOWN
        assert failing.adapter.snapshot().phase is AdapterPhase.COOLDOWN
        assert debug_events == [("adapter_cooldown", "adapter=fake-python crashes=1 phase=cooldown")]
        assert "secret source payload" not in repr(failing.adapter.snapshot())
        assert healthy.adapter.submit_read(lambda client: client.request("workspace/symbol", {})).result(timeout=1) == [
            "healthy"
        ]

        now[0] += 6
        assert failing.adapter.snapshot().phase is AdapterPhase.DEGRADED
        after_cooldown = failing.adapter.submit_read(lambda client: client.request("workspace/symbol", {}))
        assert after_cooldown.result(timeout=1) == ["after cooldown"]
    finally:
        failing.close()
        healthy.close()


def test_stop_cleans_only_owned_runtime_and_preserves_executor_ownership() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())])
    harness = AdapterHarness(provider)
    harness.adapter.start().result(timeout=1)

    stopped = harness.adapter.stop().result(timeout=1)

    assert stopped.phase is AdapterPhase.COLD
    assert not stopped.running
    assert provider.stop_count == 1
    assert provider.clients[0].shutdown_count == 1
    assert harness.executor.submit(lambda: "still externally owned").result(timeout=1) == "still externally owned"
    harness.executor.close()


def _document_lifecycle_notifications(client: FakeClient) -> list[tuple[str, object]]:
    return [entry for entry in client.notifications if entry[0].startswith("textDocument/did")]


def test_open_document_sends_one_didopen_then_didchange_for_later_versions() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())])
    harness = AdapterHarness(provider)
    try:
        uri = "file:///workspace/src/example.py"
        harness.adapter.open_document(
            relative_path="src/example.py", uri=uri, version=1, text="a = 1\n"
        ).result(timeout=1)
        harness.adapter.open_document(
            relative_path="src/example.py", uri=uri, version=2, text="a = 2\n"
        ).result(timeout=1)

        notifications = _document_lifecycle_notifications(provider.clients[0])
        assert notifications == [
            (
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": "python",
                        "version": 1,
                        "text": "a = 1\n",
                    }
                },
            ),
            (
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": 2},
                    "contentChanges": [{"text": "a = 2\n"}],
                },
            ),
        ]
    finally:
        harness.close()


def test_stop_sends_didclose_for_every_open_document_before_shutdown() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())])
    harness = AdapterHarness(provider, paths=("src/a.py", "src/b.py"))
    try:
        harness.adapter.open_document(
            relative_path="src/a.py", uri="file:///workspace/src/a.py", version=1, text="a\n"
        ).result(timeout=1)
        harness.adapter.open_document(
            relative_path="src/b.py", uri="file:///workspace/src/b.py", version=1, text="b\n"
        ).result(timeout=1)

        stopped = harness.adapter.stop().result(timeout=1)

        client = provider.clients[0]
        close_notifications = [entry for entry in client.notifications if entry[0] == "textDocument/didClose"]
        assert close_notifications == [
            ("textDocument/didClose", {"textDocument": {"uri": "file:///workspace/src/a.py"}}),
            ("textDocument/didClose", {"textDocument": {"uri": "file:///workspace/src/b.py"}}),
        ]
        assert stopped.phase is AdapterPhase.COLD
        assert client.shutdown_count == 1
    finally:
        harness.close()


def test_lru_eviction_closes_least_recently_used_document_beyond_the_128_cap() -> None:
    paths = tuple(f"src/file_{index}.py" for index in range(129))
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())])
    harness = AdapterHarness(provider, paths=paths)
    try:
        for path in paths[:128]:
            harness.adapter.open_document(
                relative_path=path, uri=f"file:///workspace/{path}", version=1, text="x\n"
            ).result(timeout=1)

        # Touch file_0 again so it becomes most-recently-used; file_1 becomes the LRU victim.
        harness.adapter.open_document(
            relative_path=paths[0], uri=f"file:///workspace/{paths[0]}", version=2, text="y\n"
        ).result(timeout=1)

        harness.adapter.open_document(
            relative_path=paths[128], uri=f"file:///workspace/{paths[128]}", version=1, text="z\n"
        ).result(timeout=1)

        close_notifications = [
            entry for entry in provider.clients[0].notifications if entry[0] == "textDocument/didClose"
        ]
        assert close_notifications == [
            ("textDocument/didClose", {"textDocument": {"uri": f"file:///workspace/{paths[1]}"}}),
        ]
    finally:
        harness.close()


def test_transport_restart_clears_open_documents_so_reopen_sends_didopen_again() -> None:
    provider = FakeRuntimeProvider(
        [
            lambda: FakeClient(_initialize_result(), {"workspace/applyEdit": [LspTransportClosed("lost")]}),
            lambda: FakeClient(_initialize_result()),
        ]
    )
    harness = AdapterHarness(provider)
    try:
        uri = "file:///workspace/src/example.py"
        harness.adapter.open_document(
            relative_path="src/example.py", uri=uri, version=1, text="a\n"
        ).result(timeout=1)
        assert provider.clients[0].notifications[-1][0] == "textDocument/didOpen"

        with pytest.raises(LspTransportClosed):
            harness.adapter.submit_edit(lambda client: client.request("workspace/applyEdit", {})).result(timeout=1)

        harness.adapter.open_document(
            relative_path="src/example.py", uri=uri, version=2, text="b\n"
        ).result(timeout=1)

        assert len(provider.clients) == 2
        assert provider.clients[1].notifications[-1][0] == "textDocument/didOpen"
    finally:
        harness.close()
