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
    DocumentReadinessTarget,
    DocumentSymbolReadinessProbe,
    EngineMetadata,
    GlobalReadinessWitness,
    LanguageAdapter,
    PublishedDiagnosticsWitness,
    ReadinessWitnessError,
    read_only_client_request_handlers,
)
from serena_light.lsp.client import LspResponseError, LspTransportClosed
from serena_light.lsp.executor import BoundedLspExecutor, ExecutorBusyError
from serena_light.lsp.positions import PositionEncoding
from serena_light.lsp.state import LspState
from serena_light.tools.editing import NotificationResult, ReplacementNotification
from serena_light.workspace.runtime import _WorkspaceLanguageAdapter
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
        stop_failures: int = 0,
    ) -> None:
        self._factories = deque(client_factories)
        self._processes = deque(processes or [])
        self.clients: list[FakeClient] = []
        self.stop_count = 0
        self._stop_failures = stop_failures
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
        if self._stop_failures:
            self._stop_failures -= 1
            raise RuntimeError("fake provider stop failed")
        runtime.client.shutdown()

    def publish(self, *, uri: str, version: int | None, diagnostics: list[object] | None = None) -> None:
        assert self.notification_handler is not None
        self.notification_handler(
            "textDocument/publishDiagnostics",
            {"uri": uri, "version": version, "diagnostics": diagnostics or []},
        )


class SynchronousDiagnosticsClient(FakeClient):
    """A fake client whose document-lifecycle notifications synchronously
    deliver a matching ``publishDiagnostics`` push, exercising the exact race
    window between sending a changed-document notification and installing its
    target as the current diagnostics publication owner.

    ``include_version`` controls whether every synthesized publication copies
    the notified document version (an ordinary versioned engine) or omits it
    (the pinned TypeScript server's actual ``publishDiagnostics`` shape,
    which never names a version at all).  ``close_diagnostics``/
    ``open_diagnostics`` let a test model a stale pre-change publication
    racing ``didClose`` and a fresh publication arriving synchronously from
    ``didOpen`` once a caller stops assuming every changed document is
    updated in place with ``didChange``.
    """

    def __init__(
        self,
        initialize_result: Mapping[str, object],
        provider: FakeRuntimeProvider,
        *,
        behaviors: Mapping[str, list[object]] | None = None,
        diagnostics: list[object] | None = None,
        include_version: bool = True,
        open_diagnostics: list[object] | None = None,
        close_diagnostics: list[object] | None = None,
        fail_didchange_with: BaseException | None = None,
        fail_didopen_with: BaseException | None = None,
        fail_didopen_after: int = 0,
        fail_didclose_with: BaseException | None = None,
        fail_didclose_after: int = 0,
        before_fail_hook: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(initialize_result, behaviors)
        self._provider = provider
        self._diagnostics = diagnostics if diagnostics is not None else []
        self._include_version = include_version
        self._open_diagnostics = open_diagnostics
        self._close_diagnostics = close_diagnostics
        self._fail_didchange_with = fail_didchange_with
        self._fail_didopen_with = fail_didopen_with
        self._fail_didopen_after = fail_didopen_after
        self._fail_didclose_with = fail_didclose_with
        self._fail_didclose_after = fail_didclose_after
        self._before_fail_hook = before_fail_hook
        self._didopen_calls = 0
        self._didclose_calls = 0

    def notify(self, method: str, params: object = None) -> None:
        if method == "textDocument/didChange" and self._fail_didchange_with is not None:
            self._invoke_before_fail_hook()
            raise self._fail_didchange_with
        if method == "textDocument/didOpen" and self._fail_didopen_with is not None:
            should_fail = self._didopen_calls == self._fail_didopen_after
            self._didopen_calls += 1
            if should_fail:
                self._invoke_before_fail_hook()
                raise self._fail_didopen_with
        elif method == "textDocument/didOpen":
            self._didopen_calls += 1
        if method == "textDocument/didClose" and self._fail_didclose_with is not None:
            should_fail = self._didclose_calls == self._fail_didclose_after
            self._didclose_calls += 1
            if should_fail:
                self._invoke_before_fail_hook()
                raise self._fail_didclose_with
        elif method == "textDocument/didClose":
            self._didclose_calls += 1
        super().notify(method, params)
        if not isinstance(params, Mapping):
            return
        payload = cast(Mapping[str, object], params)
        text_document = payload.get("textDocument")
        if not isinstance(text_document, Mapping):
            return
        document = cast(Mapping[str, object], text_document)
        uri = document.get("uri")
        if method == "textDocument/didChange":
            self._publish(uri, document.get("version") if self._include_version else None, self._diagnostics)
        elif method == "textDocument/didOpen" and self._open_diagnostics is not None:
            self._publish(uri, document.get("version") if self._include_version else None, self._open_diagnostics)
        elif method == "textDocument/didClose" and self._close_diagnostics is not None:
            self._publish(uri, None, self._close_diagnostics)

    def _invoke_before_fail_hook(self) -> None:
        if self._before_fail_hook is not None:
            self._before_fail_hook()

    def _publish(self, uri: object, version: object, diagnostics: list[object]) -> None:
        assert self._provider.notification_handler is not None
        self._provider.notification_handler(
            "textDocument/publishDiagnostics",
            {"uri": uri, "version": version, "diagnostics": diagnostics},
        )


class AsynchronousCloseDiagnosticsClient(FakeClient):
    """Model the locked TypeScript close-publication ordering exactly.

    The first ``didClose`` starts a publication on another thread, but that
    thread cannot run until either the causal barrier request or the following
    ``didOpen`` begins.  Thus the empty close publication is guaranteed to
    arrive after ``notify(didClose)`` returned.  A client without the barrier
    installs the replacement owner before that publication and falsely caches
    CLEAN; a client with the barrier drops it before opening the new content.
    """

    def __init__(
        self,
        initialize_result: Mapping[str, object],
        provider: FakeRuntimeProvider,
        *,
        fresh_diagnostics: list[object],
        barrier_failures: list[BaseException] | None = None,
    ) -> None:
        super().__init__(initialize_result, {"textDocument/documentSymbol": [[], []]})
        self._provider = provider
        self._fresh_diagnostics = fresh_diagnostics
        self._barrier_failures = list(barrier_failures or [])
        self._didopen_calls = 0
        self._close_release = threading.Event()
        self._close_published = threading.Event()
        self._close_thread: threading.Thread | None = None

    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> object:
        if method != "workspace/willRenameFiles":
            return super().request(method, params, timeout=timeout)
        self.requests.append((method, params, timeout))
        if self._barrier_failures:
            raise self._barrier_failures.pop(0)
        self._close_release.set()
        assert self._close_published.wait(1), "asynchronous close publication did not reach the client"
        return []

    def notify(self, method: str, params: object = None) -> None:
        if method == "textDocument/didOpen" and self._close_thread is not None:
            # This is the old-ordering escape hatch: without a barrier, allow
            # the close publication only after the adapter has already begun
            # the replacement didOpen call, then publish the true result.
            self._close_release.set()
            assert self._close_published.wait(1), "asynchronous close publication did not reach the client"
        super().notify(method, params)
        if not isinstance(params, Mapping):
            return
        payload = cast(Mapping[str, object], params)
        text_document = payload.get("textDocument")
        if not isinstance(text_document, Mapping):
            return
        document = cast(Mapping[str, object], text_document)
        uri = document.get("uri")
        if method == "textDocument/didClose" and self._close_thread is None:
            self._close_thread = threading.Thread(
                target=lambda: self._publish_close(uri),
                name="adapter-test-close-diagnostics",
                daemon=True,
            )
            self._close_thread.start()
        elif method == "textDocument/didOpen":
            self._didopen_calls += 1
            if self._didopen_calls == 2:
                self._publish(uri, self._fresh_diagnostics)

    def _publish_close(self, uri: object) -> None:
        assert self._close_release.wait(1), "test never released the asynchronous close publication"
        self._publish(uri, [])
        self._close_published.set()

    def _publish(self, uri: object, diagnostics: list[object]) -> None:
        assert self._provider.notification_handler is not None
        self._provider.notification_handler(
            "textDocument/publishDiagnostics",
            {"uri": uri, "diagnostics": diagnostics},
        )


class DelayedCreatedCloseDiagnosticsClient(FakeClient):
    """Model a watcher-created close publication released by the next probe.

    The real TypeScript server may publish the empty close result after the
    raw watcher didClose returns.  Releasing it from the next same-connection
    request makes the ordering deterministic without relying on a timer.
    """

    def __init__(
        self,
        initialize_result: Mapping[str, object],
        provider: FakeRuntimeProvider,
        *,
        diagnostics_uri: str,
        findings: list[object],
        behaviors: Mapping[str, list[object]] | None = None,
    ) -> None:
        super().__init__(initialize_result, behaviors)
        self._provider = provider
        self._diagnostics_uri = diagnostics_uri
        self._findings = findings
        self._pending_created_close = False

    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> object:
        result = super().request(method, params, timeout=timeout)
        if method in {"workspace/willRenameFiles", "textDocument/documentSymbol"}:
            self._publish_pending_close()
        return result

    def notify(self, method: str, params: object = None) -> None:
        super().notify(method, params)
        if not isinstance(params, Mapping):
            return
        payload = cast(Mapping[str, object], params)
        text_document = payload.get("textDocument")
        if not isinstance(text_document, Mapping):
            return
        document = cast(Mapping[str, object], text_document)
        if document.get("uri") != self._diagnostics_uri:
            return
        if method == "textDocument/didOpen":
            self._publish(self._findings)
        elif method == "textDocument/didClose":
            self._pending_created_close = True

    def _publish_pending_close(self) -> None:
        if not self._pending_created_close:
            return
        self._pending_created_close = False
        self._publish([])

    def _publish(self, diagnostics: list[object]) -> None:
        assert self._provider.notification_handler is not None
        self._provider.notification_handler(
            "textDocument/publishDiagnostics",
            {"uri": self._diagnostics_uri, "diagnostics": diagnostics},
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


def _projection(
    paths: tuple[str, ...] = ("src/example.py",),
    *,
    language: LanguageFamily = LanguageFamily.PYTHON,
) -> ScopeProjection:
    return ScopeProjection.from_attribution(
        trust_inventory_paths=paths,
        attribution=NativeProgramAttribution(
            language=language,
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


def _unversioned_facts() -> AdapterLanguageFacts:
    return AdapterLanguageFacts(
        name="fake-unversioned",
        language_id="typescript",
        extensions=frozenset({".ts"}),
        engine=EngineMetadata("fake-unversioned", "1.2.3", Path("/runtime/fake-unversioned")),
        initialize_params={"rootUri": "file:///workspace"},
        diagnostic_publications_include_version=False,
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
        queue_capacity: int = 8,
        facts: AdapterLanguageFacts | None = None,
        projection_language: LanguageFamily = LanguageFamily.PYTHON,
    ) -> None:
        self.provider = provider
        self.executor = BoundedLspExecutor(queue_capacity=queue_capacity, name="adapter-test")
        self.lock = threading.RLock()
        self.scope = ScopeGenerationTracker(_projection(paths, language=projection_language), max_wait_seconds=0.2)
        self.state = LspState()
        self.adapter = LanguageAdapter(
            workspace_root=Path("/workspace"),
            facts=facts or _facts(),
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


class WorkspaceDiagnosticHarness:
    """Exercise the production snapshot/diagnostics seam with a fake language server.

    Defaults to the unversioned-diagnostics TypeScript engine; ``language="python"``
    builds an otherwise identical harness for a versioned engine so a test can
    compare both engines' full-text-change notification lifecycles fairly.
    """

    def __init__(
        self,
        root: Path,
        provider: FakeRuntimeProvider,
        *,
        configured: bool,
        language: str = "typescript",
        paths: tuple[str, ...] | None = None,
    ) -> None:
        self.provider = provider
        self.language = language
        self.executor = BoundedLspExecutor(queue_capacity=8, name="workspace-diagnostics-test")
        self.lock = threading.RLock()
        if language == "typescript":
            family, extension, diagnostics_include_version = LanguageFamily.TYPESCRIPT, ".ts", False
        elif language == "python":
            family, extension, diagnostics_include_version = LanguageFamily.PYTHON, ".py", True
        else:
            raise ValueError(f"unsupported harness language: {language!r}")
        self.relative_path = f"src/example{extension}"
        trusted_paths = paths or (self.relative_path,)
        projection = ScopeProjection.from_attribution(
            trust_inventory_paths=trusted_paths,
            attribution=NativeProgramAttribution(
                language=family,
                project_kind=ProjectKind.CONFIGURED if configured else ProjectKind.WORKSPACE_DEFAULT,
                selected_config_path="tsconfig.json" if configured else None,
                configured_program_paths=trusted_paths,
            ),
        )
        self.scope = ScopeGenerationTracker(projection, max_wait_seconds=0.2)
        self.state = LspState()
        self.adapter = _WorkspaceLanguageAdapter(
            workspace_root=root,
            facts=AdapterLanguageFacts(
                name=f"fake-{language}",
                language_id=language,
                extensions=frozenset({extension}),
                engine=EngineMetadata(f"fake-{language}", "1.2.3", Path(f"/runtime/fake-{language}")),
                initialize_params={"rootUri": root.as_uri()},
                diagnostic_publications_include_version=diagnostics_include_version,
            ),
            runtime_provider=provider,
            executor=self.executor,
            scope_tracker=self.scope,
            lsp_state=self.state,
            document_witness=PublishedDiagnosticsWitness(),
            operation_lock=self.lock,
            readiness_timeout=0.2,
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


def test_stop_uses_owned_cleanup_capacity_when_ordinary_queue_is_saturated() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())])
    harness = AdapterHarness(provider, queue_capacity=1)
    entered = threading.Event()
    release = threading.Event()
    try:
        harness.adapter.start().result(timeout=1)

        def block_worker() -> None:
            entered.set()
            assert release.wait(5)

        blocked = harness.executor.submit(block_worker)
        assert entered.wait(5)
        queued = harness.adapter.submit_read(lambda _client: "ordinary")
        with pytest.raises(ExecutorBusyError):
            harness.adapter.submit_read(lambda _client: "overflow")

        stopped = harness.adapter.stop()
        assert not stopped.done()
        assert harness.executor.snapshot().queue_size == 1
        release.set()

        blocked.result(timeout=5)
        with pytest.raises(ExecutorBusyError, match="sealed for stop"):
            queued.result(timeout=5)
        assert stopped.result(timeout=5).phase is AdapterPhase.COLD
        assert provider.stop_count == 1
        assert provider.clients[0].shutdown_count == 1
    finally:
        release.set()
        harness.executor.close()


def test_stop_seals_ordinary_admission_before_its_cleanup_worker_runs() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())])
    harness = AdapterHarness(provider)
    entered = threading.Event()
    release = threading.Event()
    try:
        harness.adapter.start().result(timeout=1)

        def block_worker() -> None:
            entered.set()
            assert release.wait(5)

        blocked = harness.executor.submit(block_worker)
        assert entered.wait(5)
        queued_before_stop = harness.adapter.submit_read(lambda _client: "must not run")

        stopped = harness.adapter.stop()
        assert not stopped.done()
        with pytest.raises(ExecutorBusyError, match="sealed for stop"):
            harness.adapter.submit_read(lambda _client: "after stop")
        with pytest.raises(ExecutorBusyError, match="sealed for stop"):
            harness.adapter.submit_edit(lambda _client: "after stop")
        assert harness.adapter.stop() is stopped

        release.set()
        blocked.result(timeout=5)
        with pytest.raises(ExecutorBusyError, match="sealed for stop"):
            queued_before_stop.result(timeout=5)
        assert stopped.result(timeout=5).phase is AdapterPhase.COLD
        assert len(provider.clients) == 1
        assert provider.stop_count == 1
    finally:
        release.set()
        harness.executor.close()


def test_stop_cleanup_admission_failure_keeps_ordinary_work_sealed_and_is_retryable() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())])
    harness = AdapterHarness(provider)
    entered = threading.Event()
    release = threading.Event()
    try:
        harness.adapter.start().result(timeout=1)

        def block_worker() -> None:
            entered.set()
            assert release.wait(5)

        blocked = harness.executor.submit(block_worker)
        assert entered.wait(5)
        reserved = [
            harness.executor.submit_cleanup(lambda: None),
            harness.executor.submit_cleanup(lambda: None),
        ]
        with pytest.raises(ExecutorBusyError, match="cleanup reserve is full"):
            harness.adapter.stop()
        with pytest.raises(ExecutorBusyError, match="sealed for stop"):
            harness.adapter.submit_read(lambda _client: "after failed stop")
        with pytest.raises(ExecutorBusyError, match="sealed for stop"):
            harness.adapter.submit_edit(lambda _client: "after failed stop")

        release.set()
        blocked.result(timeout=5)
        for future in reserved:
            future.result(timeout=5)
        assert harness.adapter.stop().result(timeout=5).phase is AdapterPhase.COLD
        assert provider.stop_count == 1
    finally:
        release.set()
        harness.executor.close()


def test_stop_retries_a_completed_failed_cleanup_future() -> None:
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())], stop_failures=1)
    harness = AdapterHarness(provider)
    try:
        harness.adapter.start().result(timeout=1)
        first_stop = harness.adapter.stop()
        with pytest.raises(RuntimeError, match="fake provider stop failed"):
            first_stop.result(timeout=1)

        retried_stop = harness.adapter.stop()
        assert retried_stop is not first_stop
        assert retried_stop.result(timeout=1).phase is AdapterPhase.COLD
        assert provider.stop_count == 2
        assert provider.clients[0].shutdown_count == 1
    finally:
        harness.executor.close()


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


@pytest.mark.parametrize("configured", [False, True], ids=["path-scoped", "configured-program"])
def test_typescript_diagnostics_reuses_snapshot_and_keeps_async_publication_owner(
    tmp_path: Path, configured: bool
) -> None:
    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result(), {"textDocument/documentSymbol": [[]]})])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=configured)
    uri = source.as_uri()
    try:
        # A prior document-symbol read establishes readiness but not a durable
        # diagnostics owner.  The first diagnostics read must reuse that exact
        # snapshot/generation instead of sending a same-text didChange.
        _snapshot, original = harness.adapter.snapshot_open_and_probe_document(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        first_snapshot, first = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert first == original
        assert harness.state.document(uri) is not None
        assert harness.state.document(uri).generation == original.document_generation  # type: ignore[union-attr]
        assert [method for method, _params in provider.clients[0].notifications].count("textDocument/didChange") == 0

        # The publication is deliberately delivered after document-symbol
        # readiness; it must still be accepted for the retained target.
        provider.publish(uri=uri, version=first.version)
        assert harness.adapter.diagnostics_snapshot(first).state.name == "CLEAN"

        repeated_snapshot, repeated = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert repeated_snapshot == first_snapshot
        assert repeated == first
        assert [method for method, _params in provider.clients[0].notifications].count("textDocument/didChange") == 0
        assert [method for method, _params, _timeout in provider.clients[0].requests].count(
            "textDocument/documentSymbol"
        ) == 3

        # A real byte change creates a new generation and cannot surface the
        # old clean publication as current.  Freshness delivers the change
        # first; the following diagnostics load must reuse that exact buffer
        # rather than sending a lifecycle notification a second time.  The
        # unversioned TypeScript engine's publishDiagnostics omits version,
        # so the repaired reconcile path never reuses ``didChange`` for a
        # full-text change: it drains old tracking, ``didClose``s, then
        # ``didOpen``s the exact new content.
        source.write_text("export const answer = 2;\n")
        event = WatchedFileEvent("src/example.ts", FileChangeType.CHANGED)
        harness.scope.apply_did_change_watched_files([event])
        harness.adapter.reconcile_watched_files(
            events=[event],
            created=(),
            versions={"src/example.ts": 2},
        ).result(timeout=1)
        _changed_snapshot, changed = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=3,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert changed.document_generation == first.document_generation + 1
        assert changed.version == 2
        assert harness.adapter.diagnostics_snapshot(changed).state.name == "STALE"
        lifecycle = [
            method
            for method, _params in provider.clients[0].notifications
            if method in {"textDocument/didOpen", "textDocument/didChange", "textDocument/didClose"}
        ]
        assert lifecycle[-2:] == ["textDocument/didClose", "textDocument/didOpen"]
        reopened = [
            params for method, params in provider.clients[0].notifications if method == "textDocument/didOpen"
        ][-1]
        assert reopened == {
            "textDocument": {
                "uri": uri,
                "languageId": "typescript",
                "version": 2,
                "text": "export const answer = 2;\n",
            }
        }
        provider.publish(uri=uri, version=first.version)
        assert harness.adapter.diagnostics_snapshot(changed).state.name == "STALE"
        provider.publish(uri=uri, version=changed.version, diagnostics=[{"message": "new"}])
        assert harness.adapter.diagnostics_snapshot(changed).state.name == "FINDINGS"
    finally:
        harness.close()


def test_typescript_diagnostics_reopens_document_after_transport_restart(tmp_path: Path) -> None:
    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    provider = FakeRuntimeProvider(
        [
            lambda: FakeClient(_initialize_result(), {"textDocument/documentSymbol": [[]]}),
            lambda: FakeClient(_initialize_result(), {"textDocument/documentSymbol": [[]]}),
        ]
    )
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        _snapshot, original = harness.adapter.snapshot_open_and_probe_document(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        assert provider.terminal_handler is not None
        provider.terminal_handler(LspTransportClosed("server exited"))

        _snapshot, reopened = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        assert reopened != original
        assert len(provider.clients) == 2
        lifecycle = [
            method
            for method, _params in provider.clients[1].notifications
            if method in {"textDocument/didOpen", "textDocument/didChange"}
        ]
        assert lifecycle == ["textDocument/didOpen"]
        provider.publish(uri=uri, version=reopened.version)
        assert harness.adapter.diagnostics_snapshot(reopened).state.name == "CLEAN"
    finally:
        harness.close()


def test_diagnostics_cancel_retains_owner_for_late_publication_on_same_target(tmp_path: Path) -> None:
    """A tiny-timeout waiter that cancels must still accept a publication
    that legitimately arrives afterward for the same current target, and a
    retry against the identical target/generation must return it without
    requiring a fresh document-generation bump."""

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result(), {"textDocument/documentSymbol": [[]]})])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        _snapshot, target = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert harness.adapter.diagnostics_snapshot(target).state.name == "MISSING"

        # The synchronous runtime waiter gave up before any push arrived.
        harness.adapter.cancel_diagnostics_target(target)

        # The publication legitimately arrives only after cancellation.
        provider.publish(uri=uri, version=target.version)
        assert harness.adapter.diagnostics_snapshot(target).state.name == "CLEAN"

        # A retry reuses the identical open snapshot/generation and is
        # satisfied by the cached late publication.
        _snapshot2, retried = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert retried.document_generation == target.document_generation
        assert harness.adapter.diagnostics_snapshot(retried).state.name == "CLEAN"
    finally:
        harness.close()


def test_diagnostics_cancel_late_publication_cannot_satisfy_a_superseding_target(tmp_path: Path) -> None:
    """A late publication correlated with a cancelled, now-superseded target
    must never satisfy or overwrite a newer target's diagnostics."""

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result(), {"textDocument/documentSymbol": [[]]})])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        _snapshot, old_target = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        harness.adapter.cancel_diagnostics_target(old_target)

        # A real content change supersedes the cancelled target before its
        # publication arrives.
        source.write_text("export const answer = 2;\n")
        event = WatchedFileEvent("src/example.ts", FileChangeType.CHANGED)
        harness.scope.apply_did_change_watched_files([event])
        harness.adapter.reconcile_watched_files(events=[event], created=(), versions={"src/example.ts": 2}).result(
            timeout=1
        )
        _snapshot2, new_target = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=3,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert new_target.document_generation != old_target.document_generation

        # The stale, cancelled target's late publication must not satisfy or
        # overwrite the new target's diagnostics.
        provider.publish(uri=uri, version=old_target.version)
        assert harness.adapter.diagnostics_snapshot(new_target).state.name == "MISSING"

        # Only a publication correlated with the new target/version resolves it.
        provider.publish(uri=uri, version=new_target.version)
        assert harness.adapter.diagnostics_snapshot(new_target).state.name == "CLEAN"
    finally:
        harness.close()


def test_typescript_unversioned_stale_close_publication_is_dropped_and_synchronous_open_publication_becomes_current(
    tmp_path: Path,
) -> None:
    """Confirmed locked TypeScript behavior: the pinned server's
    ``publishDiagnostics`` omits ``version``, so a stale pre-change debounced
    publication can otherwise survive past a generation change unnoticed.
    The repaired lifecycle drains the old diagnostics owner before
    ``didClose`` so that publication is dropped, then installs a fresh owner
    before ``didOpen`` so only the synchronous new-content publication is
    stamped current."""

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    stale: list[object] = [{"message": "stale-debounced-for-old-content"}]
    fresh: list[object] = [{"message": "fresh-for-new-content"}]

    def make_client() -> SynchronousDiagnosticsClient:
        return SynchronousDiagnosticsClient(
            _initialize_result(),
            provider,
            behaviors={"textDocument/documentSymbol": [[], []]},
            include_version=False,
            close_diagnostics=stale,
            open_diagnostics=fresh,
        )

    provider = FakeRuntimeProvider([make_client])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        _snapshot, original = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        # The very first didOpen also fires the synchronous unversioned
        # publication hook; it is legitimately current for the first target.
        assert harness.adapter.diagnostics_snapshot(original).state.name == "FINDINGS"
        assert harness.adapter.diagnostics_snapshot(original).diagnostics == tuple(fresh)

        source.write_text("export const answer = 2;\n")
        _snapshot2, changed = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert changed.document_generation == original.document_generation + 1

        published = harness.adapter.diagnostics_snapshot(changed)
        assert published.state.name == "FINDINGS"
        assert published.diagnostics == tuple(fresh)
        assert published.diagnostics != tuple(stale)
        assert published.generation == changed.document_generation

        lifecycle = [
            method
            for method, _params in provider.clients[0].notifications
            if method in {"textDocument/didOpen", "textDocument/didChange", "textDocument/didClose"}
        ]
        assert lifecycle == [
            "textDocument/didOpen",
            "textDocument/didClose",
            "textDocument/didOpen",
        ]

        # Once the fresh publication is consumed inline, no owner or waiter
        # remains pending for a later, already-superseded publication.
        assert harness.adapter._pending_diagnostics.get(uri) is None
        assert harness.adapter._pending_documents.get(uri) is None
    finally:
        harness.close()


def test_typescript_async_close_publication_is_drained_before_replacement_owner_is_installed(
    tmp_path: Path,
) -> None:
    """The close publication arrives only after ``didClose`` notification
    delivery returned, reproducing the locked server race that made 6/6
    erroring documents falsely CLEAN without a same-client response barrier.

    The close publication must be dropped while there is no owner.  The true
    post-open publication must then be the first publication eligible for the
    replacement generation.
    """

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    fresh: list[object] = [{"message": "type error from reopened content"}]

    def make_client() -> AsynchronousCloseDiagnosticsClient:
        return AsynchronousCloseDiagnosticsClient(
            _initialize_result(),
            provider,
            fresh_diagnostics=fresh,
        )

    provider = FakeRuntimeProvider([make_client])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        _snapshot, original = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        source.write_text('export const answer: string = 1;\n')
        _snapshot2, changed = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        assert changed.document_generation == original.document_generation + 1
        publication = harness.adapter.diagnostics_snapshot(changed)
        assert publication.state.name == "FINDINGS"
        assert publication.diagnostics == tuple(fresh)
        assert publication.generation == changed.document_generation
        assert harness.adapter._pending_diagnostics.get(uri) is None
        assert harness.adapter._pending_documents.get(uri) is None

        client = cast(SynchronousDiagnosticsClient, provider.clients[0])
        assert (
            "workspace/willRenameFiles",
            {"files": []},
            0.2,
        ) in client.requests
        assert [method for method, _params in _document_lifecycle_notifications(client)] == [
            "textDocument/didOpen",
            "textDocument/didClose",
            "textDocument/didOpen",
        ]
    finally:
        harness.close()


def test_diagnostics_changed_document_synchronous_didopen_publication_is_not_dropped(tmp_path: Path) -> None:
    """A changed-document repaired ``didOpen`` whose matching
    ``publishDiagnostics`` arrives synchronously inside notification delivery
    must be observed for the new version/generation and must not leave a
    stale publication owner installed afterward."""

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    diagnostics: list[object] = [{"message": "changed"}]

    def make_client() -> SynchronousDiagnosticsClient:
        return SynchronousDiagnosticsClient(
            _initialize_result(),
            provider,
            behaviors={"textDocument/documentSymbol": [[], []]},
            include_version=False,
            open_diagnostics=diagnostics,
        )

    provider = FakeRuntimeProvider([make_client])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        _snapshot, original = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert harness.adapter.diagnostics_snapshot(original).diagnostics == tuple(diagnostics)

        source.write_text("export const answer = 2;\n")
        _snapshot2, changed = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert changed.version == 2
        assert changed.document_generation == original.document_generation + 1

        published = harness.adapter.diagnostics_snapshot(changed)
        assert published.state.name == "FINDINGS"
        # The published version reflects the locally tracked document version
        # (2), not the wire publication's omitted version: the adapter still
        # knows which version it opened even though the unversioned engine
        # never echoes it back on ``publishDiagnostics``.
        assert published.version == 2
        assert published.generation == changed.document_generation
        assert published.diagnostics == tuple(diagnostics)

        # The synchronous publication already matched this exact target; no
        # publication owner should remain pending for it afterward.
        assert harness.adapter._pending_diagnostics.get(uri) is None
        assert harness.adapter._pending_documents.get(uri) is None
    finally:
        harness.close()


def test_typescript_full_text_change_lifecycle_is_didclose_then_didopen(tmp_path: Path) -> None:
    """The pinned TypeScript server's ``publishDiagnostics`` omits version, so
    the repaired unversioned-diagnostics engine lifecycle for a full-text
    change is ``didClose`` followed by ``didOpen`` with the exact new
    content; a same-document ``didChange`` can never again be mistaken for a
    freshly analyzed generation."""

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    provider = FakeRuntimeProvider(
        [lambda: FakeClient(_initialize_result(), {"textDocument/documentSymbol": [[], []]})]
    )
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        source.write_text("export const answer = 2;\n")
        harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        lifecycle = _document_lifecycle_notifications(provider.clients[0])
        assert lifecycle == [
            (
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": "typescript",
                        "version": 1,
                        "text": "export const answer = 1;\n",
                    }
                },
            ),
            ("textDocument/didClose", {"textDocument": {"uri": uri}}),
            (
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": "typescript",
                        "version": 2,
                        "text": "export const answer = 2;\n",
                    }
                },
            ),
        ]
    finally:
        harness.close()


def test_python_full_text_change_lifecycle_still_uses_didchange(tmp_path: Path) -> None:
    """Only unversioned-diagnostics engines gain the didClose/didOpen repair;
    a versioned engine such as Pyright keeps sending an ordinary ``didChange``
    for an already-open document, through the identical workspace seam used
    for TypeScript."""

    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("answer = 1\n")
    provider = FakeRuntimeProvider(
        [lambda: FakeClient(_initialize_result(), {"textDocument/documentSymbol": [[], []]})]
    )
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False, language="python")
    uri = source.as_uri()
    try:
        harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.py",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        source.write_text("answer = 2\n")
        harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.py",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        lifecycle = [method for method, _params in _document_lifecycle_notifications(provider.clients[0])]
        assert lifecycle == ["textDocument/didOpen", "textDocument/didChange"]
    finally:
        harness.close()


def test_watched_file_reconcile_typescript_change_drains_async_close_publication_before_new_open(
    tmp_path: Path,
) -> None:
    """The file-watcher-driven full-text reconcile path applies the same
    unversioned-diagnostics repair as the manual diagnostics load: an empty
    close publication delivered asynchronously after ``didClose`` returns is
    drained by the barrier before only the fresh ``didOpen`` publication can
    be stamped current."""

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    fresh: list[object] = [{"message": "fresh-after-open"}]

    def make_client() -> AsynchronousCloseDiagnosticsClient:
        return AsynchronousCloseDiagnosticsClient(
            _initialize_result(),
            provider,
            fresh_diagnostics=fresh,
        )

    provider = FakeRuntimeProvider([make_client])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        _snapshot, original = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        source.write_text("export const answer = 2;\n")
        event = WatchedFileEvent("src/example.ts", FileChangeType.CHANGED)
        harness.scope.apply_did_change_watched_files([event])
        harness.adapter.reconcile_watched_files(
            events=[event], created=(), versions={"src/example.ts": 2}
        ).result(timeout=1)

        # Read the resulting state directly instead of issuing a second
        # diagnostics load: a later diagnostics load legitimately re-arms
        # pending tracking to stay eligible for a late publication, which
        # would mask the "no resurrection right after reconcile" property
        # this test exists to check.
        document = harness.state.document(uri)
        assert document is not None
        assert isinstance(document.version, int)
        changed = DocumentReadinessTarget(
            uri,
            "src/example.ts",
            source,
            document.version,
            document.generation,
            harness.scope.generations.path_scoped.get("src/example.ts", 0),
        )
        assert changed.document_generation == original.document_generation + 1

        published = harness.adapter.diagnostics_snapshot(changed)
        assert published.state.name == "FINDINGS"
        assert published.diagnostics == tuple(fresh)
        assert published.generation == changed.document_generation

        lifecycle = [
            method
            for method, _params in provider.clients[0].notifications
            if method in {"textDocument/didOpen", "textDocument/didChange", "textDocument/didClose"}
        ]
        assert lifecycle == ["textDocument/didOpen", "textDocument/didClose", "textDocument/didOpen"]

        assert harness.adapter._pending_diagnostics.get(uri) is None
        assert harness.adapter._pending_documents.get(uri) is None
    finally:
        harness.close()


def test_guarded_edit_notification_seam_drains_async_close_publication_before_reopen(
    tmp_path: Path,
) -> None:
    """The guarded-edit bridge calls the same exact-client open/change seam;
    its installed replacement must receive the causal close barrier too."""

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text('export const answer: string = 1;\n')
    fresh: list[object] = [{"message": "edited content type error"}]

    def make_client() -> AsynchronousCloseDiagnosticsClient:
        return AsynchronousCloseDiagnosticsClient(
            _initialize_result(),
            provider,
            fresh_diagnostics=fresh,
        )

    provider = FakeRuntimeProvider([make_client])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        def edit_notification(client: Any) -> tuple[DocumentReadinessTarget, NotificationResult]:
            opened = harness.adapter.open_edit_document_with_client(
                client,
                absolute_path=source,
                relative_path="src/example.ts",
                uri=uri,
                version=1,
                text="export const answer = 1;\n",
            )
            result = harness.adapter.notify_edit_with_client(
                client,
                opened,
                ReplacementNotification(
                    path=source,
                    relative_path="src/example.ts",
                    uri=uri,
                    text='export const answer: string = 1;\n',
                    old_hash="old",
                    new_hash="new",
                    symbol_name_path="answer",
                ),
            )
            return opened, result

        original, notification = harness.adapter.submit_edit(edit_notification).result(timeout=1)
        assert notification.state == "notified"
        document = harness.state.document(uri)
        assert document is not None
        changed = DocumentReadinessTarget(
            uri=uri,
            relative_path="src/example.ts",
            absolute_path=source,
            version=2,
            document_generation=document.generation,
            path_generation=harness.scope.generations.path_scoped.get("src/example.ts", 0),
        )
        assert changed.document_generation == original.document_generation + 1
        publication = harness.adapter.diagnostics_snapshot(changed)
        assert publication.state.name == "FINDINGS"
        assert publication.diagnostics == tuple(fresh)
        assert harness.adapter._pending_documents.get(uri) is None
        assert [method for method, _params in _document_lifecycle_notifications(provider.clients[0])] == [
            "textDocument/didOpen",
            "textDocument/didClose",
            "textDocument/didOpen",
        ]
    finally:
        harness.close()


def test_diagnostics_changed_document_didopen_notify_failure_leaves_no_phantom_pending_owner(
    tmp_path: Path,
) -> None:
    """If sending the repaired changed-document ``didOpen`` notification
    fails, the new, never-delivered target must not remain, or become, the
    current diagnostics publication owner: compare-and-remove cleanup must
    pop exactly the failed target, leaving nothing pending."""

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    failure = RuntimeError("transport rejected the reopen")

    def make_client() -> SynchronousDiagnosticsClient:
        return SynchronousDiagnosticsClient(
            _initialize_result(),
            provider,
            behaviors={"textDocument/documentSymbol": [[]]},
            fail_didopen_with=failure,
            fail_didopen_after=1,
        )

    provider = FakeRuntimeProvider([make_client])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        _snapshot, original = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        source.write_text("export const answer = 2;\n")
        pending = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        )
        with pytest.raises(RuntimeError, match="transport rejected the reopen"):
            pending.result(timeout=1)

        # The failed didOpen never delivered a fresh publication, and the old
        # generation's tracking was already causally drained before didClose;
        # exactly nothing may remain pending for this uri.
        assert harness.adapter._pending_diagnostics.get(uri) is None
        assert harness.adapter._pending_documents.get(uri) is None
    finally:
        harness.close()


def test_unversioned_didclose_delivery_failure_is_redelivered_then_drained_on_retry(
    tmp_path: Path,
) -> None:
    """Unknown didClose delivery remains explicitly retryable: retry may send
    it twice, but it must not open until the redelivered close has drained."""

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    failure = RuntimeError("transport rejected the close")

    def make_client() -> SynchronousDiagnosticsClient:
        return SynchronousDiagnosticsClient(
            _initialize_result(),
            provider,
            behaviors={"textDocument/documentSymbol": [[]]},
            fail_didclose_with=failure,
        )

    provider = FakeRuntimeProvider([make_client])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        _snapshot, original = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        source.write_text("export const answer = 2;\n")
        pending = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        )
        with pytest.raises(RuntimeError, match="transport rejected the close"):
            pending.result(timeout=1)

        # The drain step forgot this uri's tracking before sending didClose;
        # a transport failure on that very notify must not resurrect the old
        # target's ownership or leave any phantom new owner installed.
        assert harness.adapter._pending_diagnostics.get(uri) is None
        assert harness.adapter._pending_documents.get(uri) is None
        assert uri not in harness.adapter._open_documents
        marker = harness.adapter._undrained_unversioned_closes.get(uri)
        assert marker is not None
        assert not marker.close_delivered

        _snapshot2, retried = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert retried.version == 2
        client = cast(SynchronousDiagnosticsClient, provider.clients[0])
        assert [method for method, _params in _document_lifecycle_notifications(client)] == [
            "textDocument/didOpen",
            "textDocument/didClose",
            "textDocument/didOpen",
        ]
        assert client._didclose_calls == 2
        assert [method for method, _params, _timeout in client.requests].count("workspace/willRenameFiles") == 1
        assert uri not in harness.adapter._undrained_unversioned_closes
    finally:
        harness.close()


@pytest.mark.parametrize(
    "barrier_failure",
    [
        TimeoutError("close barrier timed out"),
        LspResponseError(-32603, "close barrier failed"),
    ],
    ids=("timeout", "response-error"),
)
def test_unversioned_close_barrier_failure_retries_same_connection_drain_before_reopen(
    tmp_path: Path,
    barrier_failure: BaseException,
) -> None:
    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    fresh: list[object] = [{"message": "fresh-after-retried-barrier"}]

    def make_client() -> AsynchronousCloseDiagnosticsClient:
        return AsynchronousCloseDiagnosticsClient(
            _initialize_result(),
            provider,
            fresh_diagnostics=fresh,
            barrier_failures=[barrier_failure],
        )

    provider = FakeRuntimeProvider([make_client])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        _snapshot, original = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        source.write_text("export const answer = 2;\n")

        failed = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        )
        with pytest.raises(type(barrier_failure), match="close barrier"):
            failed.result(timeout=1)

        current = harness.state.document(uri)
        assert current is not None
        assert current.version == original.version
        assert current.generation == original.document_generation
        assert uri not in harness.adapter._open_documents
        assert harness.adapter._pending_diagnostics.get(uri) is None
        assert harness.adapter._pending_documents.get(uri) is None
        marker = harness.adapter._undrained_unversioned_closes.get(uri)
        assert marker is not None and marker.close_delivered
        client = provider.clients[0]
        assert [method for method, _params in _document_lifecycle_notifications(client)] == [
            "textDocument/didOpen",
            "textDocument/didClose",
        ]

        # The first barrier failed after didClose delivery.  The close's empty
        # publication remains delayed, so retry must issue another barrier on
        # this same connection before it installs the replacement owner.
        _snapshot2, retried = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert retried.version == 2
        assert retried.document_generation == original.document_generation + 1
        assert [method for method, _params in _document_lifecycle_notifications(client)] == [
            "textDocument/didOpen",
            "textDocument/didClose",
            "textDocument/didOpen",
        ]
        assert harness.adapter.diagnostics_snapshot(retried).state.name == "FINDINGS"
        assert harness.adapter.diagnostics_snapshot(retried).diagnostics == tuple(fresh)
        assert [method for method, _params, _timeout in client.requests].count("workspace/willRenameFiles") >= 2
        assert uri not in harness.adapter._undrained_unversioned_closes
    finally:
        harness.close()


def test_unversioned_close_barrier_transport_loss_leaves_no_phantom_owner_or_reopen(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")

    def lose_transport() -> object:
        assert provider.terminal_handler is not None
        error = LspTransportClosed("close barrier transport lost")
        provider.terminal_handler(error)
        raise error

    provider = FakeRuntimeProvider(
        [
            lambda: FakeClient(
                _initialize_result(),
                {
                    "textDocument/documentSymbol": [[]],
                    "workspace/willRenameFiles": [Deferred(lose_transport)],
                },
            ),
            lambda: FakeClient(_initialize_result(), {"textDocument/documentSymbol": [[]]}),
        ]
    )
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        source.write_text("export const answer = 2;\n")

        failed = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        )
        with pytest.raises(LspTransportClosed, match="close barrier transport lost"):
            failed.result(timeout=1)

        assert harness.state.document(uri) is None
        assert uri not in harness.adapter._open_documents
        assert harness.adapter._pending_diagnostics.get(uri) is None
        assert harness.adapter._pending_documents.get(uri) is None
        assert uri not in harness.adapter._undrained_unversioned_closes
        assert [method for method, _params in _document_lifecycle_notifications(provider.clients[0])] == [
            "textDocument/didOpen",
            "textDocument/didClose",
        ]

        _snapshot, retried = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert retried.version == 2
        assert len(provider.clients) == 2
        assert [method for method, _params in _document_lifecycle_notifications(provider.clients[1])] == [
            "textDocument/didOpen"
        ]
    finally:
        harness.close()


def test_diagnostics_changed_document_notify_failure_compare_and_remove_preserves_a_newer_owner(
    tmp_path: Path,
) -> None:
    """Failure cleanup is compare-and-remove: if a legitimately newer target
    has already replaced the failed target as the pending diagnostics owner
    before cleanup runs, that newer owner must survive untouched instead of
    being clobbered by the stale failure's cleanup."""

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    failure = RuntimeError("transport rejected the reopen")
    newer_targets: list[DocumentReadinessTarget] = []

    def install_newer_owner() -> None:
        current = harness.adapter._pending_documents[uri]
        newer = DocumentReadinessTarget(
            current.uri,
            current.relative_path,
            current.absolute_path,
            current.version + 1,
            current.document_generation + 1,
            current.path_generation,
        )
        newer_targets.append(newer)
        harness.adapter._pending_documents[uri] = newer
        harness.adapter._pending_diagnostics[uri] = newer

    def make_client() -> SynchronousDiagnosticsClient:
        return SynchronousDiagnosticsClient(
            _initialize_result(),
            provider,
            behaviors={"textDocument/documentSymbol": [[]]},
            fail_didopen_with=failure,
            fail_didopen_after=1,
            before_fail_hook=install_newer_owner,
        )

    provider = FakeRuntimeProvider([make_client])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        _snapshot, original = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        source.write_text("export const answer = 2;\n")
        pending = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        )
        with pytest.raises(RuntimeError, match="transport rejected the reopen"):
            pending.result(timeout=1)

        assert newer_targets
        newer = newer_targets[0]
        assert harness.adapter._pending_documents.get(uri) == newer
        assert harness.adapter._pending_diagnostics.get(uri) == newer
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
        harness.adapter.open_document(relative_path="src/example.py", uri=uri, version=1, text="a = 1\n").result(
            timeout=1
        )
        harness.adapter.open_document(relative_path="src/example.py", uri=uri, version=2, text="a = 2\n").result(
            timeout=1
        )

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


def test_unversioned_lru_eviction_drains_close_before_reopen() -> None:
    paths = tuple(f"src/file_{index}.ts" for index in range(129))
    victim_uri = f"file:///workspace/{paths[0]}"

    def barrier() -> object:
        assert victim_uri not in harness.adapter._open_documents
        assert victim_uri in harness.adapter._undrained_unversioned_closes
        return []

    provider = FakeRuntimeProvider(
        [lambda: FakeClient(_initialize_result(), {"workspace/willRenameFiles": [Deferred(barrier)]})]
    )
    harness = AdapterHarness(
        provider,
        paths=paths,
        facts=_unversioned_facts(),
        projection_language=LanguageFamily.TYPESCRIPT,
    )
    try:
        for path in paths:
            harness.adapter.open_document(
                relative_path=path, uri=f"file:///workspace/{path}", version=1, text="x\n"
            ).result(timeout=1)

        marker = harness.adapter._undrained_unversioned_closes.get(victim_uri)
        assert marker is not None and marker.close_delivered
        harness.adapter.open_document(relative_path=paths[0], uri=victim_uri, version=2, text="y\n").result(timeout=1)

        client = provider.clients[0]
        assert [method for method, _params, _timeout in client.requests].count("workspace/willRenameFiles") == 1
        assert victim_uri not in harness.adapter._undrained_unversioned_closes
    finally:
        harness.close()


def test_unversioned_lru_close_markers_remain_bounded_across_many_distinct_opens() -> None:
    paths = tuple(f"src/file_{index}.ts" for index in range(512))
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result())])
    harness = AdapterHarness(
        provider,
        paths=paths,
        facts=_unversioned_facts(),
        projection_language=LanguageFamily.TYPESCRIPT,
    )
    try:
        for path in paths:
            harness.adapter.open_document(
                relative_path=path,
                uri=f"file:///workspace/{path}",
                version=1,
                text="x\n",
            ).result(timeout=1)

        assert len(harness.adapter._open_documents) == harness.adapter.MAX_OPEN_DOCUMENTS
        assert len(harness.adapter._undrained_unversioned_closes) == 1
        barriers = [
            method
            for method, _params, _timeout in provider.clients[0].requests
            if method == "workspace/willRenameFiles"
        ]
        assert len(barriers) == len(paths) - harness.adapter.MAX_OPEN_DOCUMENTS - 1
    finally:
        harness.close()


def test_unversioned_watched_delete_drains_close_before_reopen(tmp_path: Path) -> None:
    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    uri = source.as_uri()

    def barrier() -> object:
        assert uri not in harness.adapter._open_documents
        assert uri in harness.adapter._undrained_unversioned_closes
        return []

    provider = FakeRuntimeProvider(
        [
            lambda: FakeClient(
                _initialize_result(),
                {"textDocument/documentSymbol": [[], []], "workspace/willRenameFiles": [Deferred(barrier)]},
            )
        ]
    )
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    try:
        harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        harness.adapter.reconcile_watched_files(
            events=[WatchedFileEvent("src/example.ts", FileChangeType.DELETED)], created=(), versions={}
        ).result(timeout=1)

        marker = harness.adapter._undrained_unversioned_closes.get(uri)
        assert marker is not None and marker.close_delivered
        source.write_text("export const answer = 2;\n")
        _snapshot, reopened = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert reopened.version == 2
        assert [method for method, _params, _timeout in provider.clients[0].requests].count(
            "workspace/willRenameFiles"
        ) == 1
        assert uri not in harness.adapter._undrained_unversioned_closes
    finally:
        harness.close()


def test_unversioned_watched_create_never_temp_closes_an_owned_document(tmp_path: Path) -> None:
    """A watcher create fact must not overwrite the adapter's owned buffer.

    A same-path create can follow a failed watcher reconciliation.  The
    adapter already owns this URI, so a raw didOpen/didClose pair would both
    desynchronize the server buffer and leave a close marker paired with a
    still-open local owner.
    """

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    provider = FakeRuntimeProvider([lambda: FakeClient(_initialize_result(), {"textDocument/documentSymbol": [[]]})])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        harness.adapter.snapshot_open_and_probe_document(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        harness.adapter.reconcile_watched_files(
            events=[], created=("src/example.ts",), versions={}
        ).result(timeout=1)

        assert uri in harness.adapter._open_documents
        assert uri not in harness.adapter._undrained_unversioned_closes
        assert [method for method, _params in _document_lifecycle_notifications(provider.clients[0])] == [
            "textDocument/didOpen"
        ]
    finally:
        harness.close()


def test_watcher_delete_timeout_then_create_cannot_turn_cached_open_diagnostics_clean(tmp_path: Path) -> None:
    """Exercise the ordinary two-file watcher chain behind the false CLEAN.

    B's delete leaves an undrained marker.  The next A-delete reconciliation
    must abort at that head barrier, leaving A open.  On retry, watcher-created
    A must not issue a temporary raw lifecycle pair; the cached diagnostics
    retry consequently retains A's real findings rather than accepting a
    delayed close-empty publication.
    """

    source_a = tmp_path / "src" / "a.ts"
    source_b = tmp_path / "src" / "b.ts"
    source_a.parent.mkdir()
    source_a.write_text("export const answer: string = 1;\n")
    source_b.write_text("export const spare = 1;\n")
    uri_a = source_a.as_uri()
    uri_b = source_b.as_uri()
    findings: list[object] = [{"message": "TS2322"}]

    def make_client() -> DelayedCreatedCloseDiagnosticsClient:
        return DelayedCreatedCloseDiagnosticsClient(
            _initialize_result(),
            provider,
            diagnostics_uri=uri_a,
            findings=findings,
            behaviors={"workspace/willRenameFiles": [TimeoutError("head barrier timed out"), []]},
        )

    provider = FakeRuntimeProvider([make_client])
    harness = WorkspaceDiagnosticHarness(
        tmp_path,
        provider,
        configured=False,
        paths=("src/a.ts", "src/b.ts"),
    )
    try:
        _snapshot, original = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source_a,
            relative_path="src/a.ts",
            uri=uri_a,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        harness.adapter.snapshot_open_and_probe_document(
            absolute_path=source_b,
            relative_path="src/b.ts",
            uri=uri_b,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        client = cast(DelayedCreatedCloseDiagnosticsClient, provider.clients[0])
        assert harness.adapter.diagnostics_snapshot(original).state.name == "FINDINGS"

        # The ordinary watcher delete for B records and delivers its close,
        # while the following A-delete stops at that marker's head barrier.
        harness.adapter.reconcile_watched_files(
            events=[WatchedFileEvent("src/b.ts", FileChangeType.DELETED)], created=(), versions={}
        ).result(timeout=1)
        assert uri_b in harness.adapter._undrained_unversioned_closes
        with pytest.raises(TimeoutError, match="head barrier timed out"):
            harness.adapter.reconcile_watched_files(
                events=[WatchedFileEvent("src/a.ts", FileChangeType.DELETED)], created=(), versions={}
            ).result(timeout=1)
        assert uri_a in harness.adapter._open_documents
        assert uri_b not in harness.adapter._open_documents
        assert uri_b in harness.adapter._undrained_unversioned_closes

        # Retrying the watcher batch drains B first.  A is still owned, so
        # its create fact must not manufacture an unversioned close marker.
        harness.adapter.reconcile_watched_files(events=[], created=("src/a.ts",), versions={}).result(timeout=1)
        assert uri_a in harness.adapter._open_documents
        assert uri_a not in harness.adapter._undrained_unversioned_closes
        assert not set(harness.adapter._open_documents).intersection(harness.adapter._undrained_unversioned_closes)
        assert uri_b not in harness.adapter._undrained_unversioned_closes
        assert [method for method, _params, _timeout in client.requests].count("workspace/willRenameFiles") == 2

        _snapshot, retried = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source_a,
            relative_path="src/a.ts",
            uri=uri_a,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        assert retried == original
        assert harness.adapter.diagnostics_snapshot(retried).state.name == "FINDINGS"
        assert harness.adapter.diagnostics_snapshot(retried).diagnostics == tuple(findings)
        lifecycle_a = [
            method
            for method, params in _document_lifecycle_notifications(client)
            if cast(Mapping[str, Mapping[str, object]], params)["textDocument"]["uri"] == uri_a
        ]
        assert lifecycle_a == ["textDocument/didOpen"]
    finally:
        harness.close()


def test_cached_diagnostics_reuse_drains_a_stranded_unversioned_close_before_retaining_owner(
    tmp_path: Path,
) -> None:
    """Cached reuse cannot convert a legacy close marker into CLEAN.

    This is the recovery side of the watcher ownership invariant.  A failed
    reconciliation can leave a marker from an older process version while the
    local open map still claims the URI.  Reuse must disown and drain that
    marker before it retains a diagnostics publication owner; otherwise the
    close-empty publication is incorrectly stamped onto the unchanged target.
    """

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    fresh: list[object] = [{"message": "TS2322"}]

    def make_client() -> AsynchronousCloseDiagnosticsClient:
        return AsynchronousCloseDiagnosticsClient(
            _initialize_result(), provider, fresh_diagnostics=fresh
        )

    provider = FakeRuntimeProvider([make_client])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        harness.adapter.snapshot_open_and_probe_document(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        client = cast(AsynchronousCloseDiagnosticsClient, provider.clients[0])

        # Model a marker produced before the watcher ownership guard existed.
        # The adapter must repair this same-process inconsistency rather than
        # let the close publication satisfy the cached target.
        harness.adapter._send_unversioned_didclose(client, uri)
        assert uri in harness.adapter._open_documents
        assert uri in harness.adapter._undrained_unversioned_closes

        _snapshot, target = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        assert [method for method, _params, _timeout in client.requests].count("workspace/willRenameFiles") == 1
        assert uri in harness.adapter._open_documents
        assert uri not in harness.adapter._undrained_unversioned_closes
        assert harness.adapter.diagnostics_snapshot(target).state.name == "FINDINGS"
        assert harness.adapter.diagnostics_snapshot(target).diagnostics == tuple(fresh)
        assert [method for method, _params in _document_lifecycle_notifications(client)] == [
            "textDocument/didOpen",
            "textDocument/didClose",
            "textDocument/didOpen",
        ]
    finally:
        harness.close()


@pytest.mark.parametrize(
    "barrier_failure",
    [
        TimeoutError("cached close barrier timed out"),
        LspResponseError(-32603, "cached close barrier failed"),
    ],
    ids=("timeout", "response-error"),
)
def test_cached_diagnostics_reuse_keeps_stranded_close_marker_when_drain_fails(
    tmp_path: Path,
    barrier_failure: BaseException,
) -> None:
    """A failed cached-reuse drain is retryable and cannot return CLEAN."""

    source = tmp_path / "src" / "example.ts"
    source.parent.mkdir()
    source.write_text("export const answer = 1;\n")
    fresh: list[object] = [{"message": "TS2322"}]

    def make_client() -> AsynchronousCloseDiagnosticsClient:
        return AsynchronousCloseDiagnosticsClient(
            _initialize_result(), provider, fresh_diagnostics=fresh, barrier_failures=[barrier_failure]
        )

    provider = FakeRuntimeProvider([make_client])
    harness = WorkspaceDiagnosticHarness(tmp_path, provider, configured=False)
    uri = source.as_uri()
    try:
        harness.adapter.snapshot_open_and_probe_document(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=1,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)
        client = cast(AsynchronousCloseDiagnosticsClient, provider.clients[0])
        harness.adapter._send_unversioned_didclose(client, uri)

        with pytest.raises(type(barrier_failure), match="cached close barrier"):
            harness.adapter.snapshot_open_and_probe_diagnostics(
                absolute_path=source,
                relative_path="src/example.ts",
                uri=uri,
                version=2,
                probe=DocumentSymbolReadinessProbe(),
            ).result(timeout=1)

        marker = harness.adapter._undrained_unversioned_closes.get(uri)
        assert marker is not None and marker.close_delivered
        assert uri not in harness.adapter._open_documents
        assert harness.adapter._pending_diagnostics.get(uri) is None
        assert harness.adapter._pending_documents.get(uri) is None

        _snapshot, target = harness.adapter.snapshot_open_and_probe_diagnostics(
            absolute_path=source,
            relative_path="src/example.ts",
            uri=uri,
            version=2,
            probe=DocumentSymbolReadinessProbe(),
        ).result(timeout=1)

        assert [method for method, _params, _timeout in client.requests].count("workspace/willRenameFiles") == 2
        assert uri not in harness.adapter._undrained_unversioned_closes
        assert harness.adapter.diagnostics_snapshot(target).state.name == "FINDINGS"
        assert harness.adapter.diagnostics_snapshot(target).diagnostics == tuple(fresh)
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
        harness.adapter.open_document(relative_path="src/example.py", uri=uri, version=1, text="a\n").result(timeout=1)
        assert provider.clients[0].notifications[-1][0] == "textDocument/didOpen"

        with pytest.raises(LspTransportClosed):
            harness.adapter.submit_edit(lambda client: client.request("workspace/applyEdit", {})).result(timeout=1)

        harness.adapter.open_document(relative_path="src/example.py", uri=uri, version=2, text="b\n").result(timeout=1)

        assert len(provider.clients) == 2
        assert provider.clients[1].notifications[-1][0] == "textDocument/didOpen"
    finally:
        harness.close()


def test_current_transport_loss_immediately_drops_process_owned_document_state() -> None:
    provider = FakeRuntimeProvider(
        [
            lambda: FakeClient(_initialize_result(), {"textDocument/documentSymbol": [[]]}),
            lambda: FakeClient(_initialize_result(), {"textDocument/documentSymbol": [[]]}),
        ]
    )
    harness = AdapterHarness(provider)
    uri = "file:///workspace/src/example.py"
    try:
        target = harness.adapter.open_document(
            relative_path="src/example.py", uri=uri, version=1, text="answer = 1\n"
        ).result(timeout=1)
        provider.publish(uri=uri, version=1)
        assert harness.state.diagnostics_snapshot(uri, generation=target.document_generation).state.name == "CLEAN"

        assert provider.terminal_handler is not None
        provider.terminal_handler(LspTransportClosed("server exited"))

        assert harness.state.document(uri) is None
        assert harness.state.diagnostics_snapshot(uri).state.name == "MISSING"
        reopened = harness.adapter.open_document(
            relative_path="src/example.py", uri=uri, version=2, text="answer = 1\n"
        ).result(timeout=1)
        assert reopened.document_generation == 1
        assert len(provider.clients) == 2
        assert provider.clients[1].notifications[-1][0] == "textDocument/didOpen"
    finally:
        harness.close()
