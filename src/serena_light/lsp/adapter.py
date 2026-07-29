"""Language-neutral lazy LSP adapter ownership and readiness core.

Language-specific modules inject commands, initialization parameters, document
readiness witnesses, and dynamically selected global sentinel symbols.  This
module deliberately contains no Pyright or TypeScript policy.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import IO, Any, Protocol, TypeVar, cast

from serena_light.lsp.client import LspTransportClosed, SyncLspClient
from serena_light.lsp.executor import BoundedLspExecutor
from serena_light.lsp.positions import PositionEncoding
from serena_light.lsp.state import LspState
from serena_light.processes import (
    Command,
    LanguageServerSubprocessLauncher,
    terminate_process_tree_with_kill_fallback,
)
from serena_light.workspace.scope import (
    ReadinessCode,
    ReadinessResult,
    ScopeGenerationTracker,
)

T = TypeVar("T")
JSON = dict[str, Any]


class AdapterPhase(StrEnum):
    COLD = "cold"
    STARTING = "starting"
    DOCUMENT_READY = "document_ready"
    GLOBAL_WARMING = "global_warming"
    READY = "ready"
    DEGRADED = "degraded"
    COOLDOWN = "cooldown"
    STOPPING = "stopping"


class AdapterErrorCode(StrEnum):
    NOT_READY = "NOT_READY"
    SCOPE_INCOMPATIBLE = "SCOPE_INCOMPATIBLE"
    COOLDOWN = "COOLDOWN"


class AdapterError(RuntimeError):
    """Typed operational failure owned by one adapter."""

    def __init__(self, code: AdapterErrorCode, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class LspProcessLost(LspTransportClosed):
    """Raised when the owned subprocess exits while the adapter is active."""


class ReadinessWitnessError(AdapterError):
    """A controlled document or exact global sentinel was not observed."""


@dataclass(frozen=True, slots=True)
class EngineMetadata:
    name: str
    version: str
    executable: Path
    interpreter: Path | None = None


@dataclass(frozen=True, slots=True)
class RawLspProviders:
    definition: bool = False
    declaration: bool = False
    implementation: bool = False
    references: bool = False
    document_symbols: bool = False
    workspace_symbols: bool = False

    @classmethod
    def from_initialize_result(cls, result: Mapping[str, object]) -> RawLspProviders:
        capabilities = result.get("capabilities")
        values = cast(Mapping[str, object], capabilities) if isinstance(capabilities, Mapping) else {}
        return cls(
            definition=_provider_enabled(values.get("definitionProvider")),
            declaration=_provider_enabled(values.get("declarationProvider")),
            implementation=_provider_enabled(values.get("implementationProvider")),
            references=_provider_enabled(values.get("referencesProvider")),
            document_symbols=_provider_enabled(values.get("documentSymbolProvider")),
            workspace_symbols=_provider_enabled(values.get("workspaceSymbolProvider")),
        )


@dataclass(frozen=True, slots=True)
class DerivedToolAvailability:
    find_declaration: bool
    find_implementations: bool
    find_referencing_symbols: bool
    get_symbols_overview: bool
    global_find_symbol: bool

    @classmethod
    def from_raw(cls, raw: RawLspProviders) -> DerivedToolAvailability:
        return cls(
            find_declaration=raw.definition,
            find_implementations=raw.implementation,
            find_referencing_symbols=raw.references,
            get_symbols_overview=raw.document_symbols,
            global_find_symbol=raw.workspace_symbols,
        )


@dataclass(frozen=True, slots=True)
class AdapterLanguageFacts:
    """Fixed facts supplied by one language adapter, never discovered as plugins."""

    name: str
    language_id: str
    extensions: frozenset[str]
    engine: EngineMetadata
    initialize_params: Mapping[str, object]
    default_position_encoding: PositionEncoding = PositionEncoding.UTF16
    language_ids: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.language_id:
            raise ValueError("adapter name and language_id must be non-empty")
        normalized = frozenset(_normalize_extension(extension) for extension in self.extensions)
        if not normalized:
            raise ValueError("an adapter must route at least one extension")
        language_ids = (
            dict.fromkeys(normalized, self.language_id)
            if self.language_ids is None
            else {_normalize_extension(extension): language_id for extension, language_id in self.language_ids.items()}
        )
        if set(language_ids) != set(normalized) or any(not language_id for language_id in language_ids.values()):
            raise ValueError("language_ids must map every routed extension exactly once")
        object.__setattr__(self, "extensions", normalized)
        object.__setattr__(self, "initialize_params", MappingProxyType(dict(self.initialize_params)))
        object.__setattr__(self, "language_ids", MappingProxyType(language_ids))

    def routes(self, path: str | Path) -> bool:
        return PurePosixPath(str(path)).suffix.lower() in self.extensions

    def language_id_for(self, path: str | Path) -> str:
        suffix = PurePosixPath(str(path)).suffix.lower()
        try:
            assert self.language_ids is not None
            return self.language_ids[suffix]
        except KeyError as error:
            raise ValueError(f"unsupported extension for {self.name}: {suffix!r}") from error


@dataclass(frozen=True, slots=True)
class AdapterGenerations:
    trust: int
    program: int
    document: int
    index: int


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    phase: AdapterPhase
    timestamp: float
    reason: str
    generations: AdapterGenerations


@dataclass(frozen=True, slots=True)
class CrashSnapshot:
    total: int
    window_count: int
    last_timestamp: float | None
    last_error: str | None
    cooldown_until: float | None
    cooldown_remaining_seconds: float


@dataclass(frozen=True, slots=True)
class AdapterSnapshot:
    name: str
    phase: AdapterPhase
    raw_providers: RawLspProviders
    derived_tools: DerivedToolAvailability
    engine: EngineMetadata
    position_encoding: PositionEncoding
    generations: AdapterGenerations
    crash: CrashSnapshot
    transitions: tuple[PhaseTransition, ...]
    running: bool


@dataclass(frozen=True, slots=True)
class CrashPolicy:
    threshold: int = 3
    window_seconds: float = 60.0
    cooldown_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("crash threshold must be positive")
        if self.window_seconds <= 0 or self.cooldown_seconds < 0:
            raise ValueError("crash window must be positive and cooldown non-negative")


@dataclass(frozen=True, slots=True)
class DocumentReadinessTarget:
    uri: str
    relative_path: str
    absolute_path: Path
    version: int
    document_generation: int
    path_generation: int


class DocumentReadinessWitness(Protocol):
    """Typed server-specific proof that one controlled document became ready."""

    def observe(
        self,
        method: str,
        params: object,
        target: DocumentReadinessTarget,
        state: LspState,
    ) -> bool: ...


class DocumentReadinessProbe(Protocol):
    """Typed request/response proof for servers without prompt publications."""

    def observe(
        self,
        client: AdapterClient,
        target: DocumentReadinessTarget,
        *,
        timeout: float,
    ) -> bool: ...


class PublishedDiagnosticsWitness:
    """Explicitly use a correlated diagnostics publication as document proof."""

    def observe(
        self,
        method: str,
        params: object,
        target: DocumentReadinessTarget,
        state: LspState,
    ) -> bool:
        if method != "textDocument/publishDiagnostics" or not isinstance(params, Mapping):
            return False
        publication = cast(Mapping[str, object], params)
        if publication.get("uri") != target.uri:
            return False
        version = publication.get("version")
        if version is not None and (not isinstance(version, int) or version != target.version):
            return False
        diagnostics = publication.get("diagnostics")
        if not isinstance(diagnostics, Sequence) or isinstance(diagnostics, str | bytes):
            return False
        return state.publish_diagnostics(
            uri=target.uri,
            path=target.absolute_path,
            version=version,
            generation=target.document_generation,
            diagnostics=diagnostics,
        )


class DocumentSymbolReadinessProbe:
    """Use a bounded current-document symbol response as readiness proof."""

    def observe(
        self,
        client: AdapterClient,
        target: DocumentReadinessTarget,
        *,
        timeout: float,
    ) -> bool:
        result = client.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": target.uri}},
            timeout=timeout,
        )
        return isinstance(result, Sequence) and not isinstance(result, str | bytes)


@dataclass(frozen=True, slots=True)
class GlobalReadinessWitness:
    """An exact, dynamically selected configured-program symbol witness."""

    exact_symbol: str
    uri: str
    query: str | None = None

    def __post_init__(self) -> None:
        if not self.exact_symbol:
            raise ValueError("global readiness requires a non-empty exact symbol")
        if not self.uri.startswith("file://"):
            raise ValueError("global readiness requires the exact configured-file URI")
        if self.query is not None and not self.query:
            raise ValueError("global readiness query must be non-empty when supplied")


class AdapterClient(Protocol):
    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> Any: ...

    def notify(self, method: str, params: object = None) -> None: ...

    def shutdown(self, *, timeout: float = 2.0) -> None: ...


@dataclass(slots=True)
class AdapterRuntime:
    client: AdapterClient
    process: subprocess.Popen[bytes] | None = None
    stderr_capture: BoundedStderrCapture | None = None
    stopping: threading.Event | None = None
    process_observer: threading.Thread | None = None


class AdapterRuntimeProvider(Protocol):
    def start(
        self,
        *,
        notification_handler: Callable[[str, Any], None],
        terminal_handler: Callable[[BaseException], None],
    ) -> AdapterRuntime: ...

    def stop(self, runtime: AdapterRuntime) -> None: ...


def read_only_client_request_handlers(
    configuration: Callable[[Any], Any],
) -> Mapping[str, Callable[[Any], Any]]:
    """Return the minimal server-request surface shared by the fixed adapters."""

    return MappingProxyType(
        {
            "client/registerCapability": lambda _params: None,
            "window/workDoneProgress/create": lambda _params: None,
            "workspace/configuration": configuration,
            "workspace/executeClientCommand": lambda _params: [],
            "workspace/applyEdit": lambda _params: {
                "applied": False,
                "failureReason": "serena-light language adapters are read-only",
            },
        }
    )


class BoundedStderrCapture:
    """Continuously drain stderr while retaining only the most recent bytes."""

    def __init__(self, stream: IO[bytes], *, max_bytes: int = 64 * 1024) -> None:
        if max_bytes < 1:
            raise ValueError("stderr capture bound must be positive")
        self._stream = stream
        self._max_bytes = max_bytes
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="serena-light-lsp-stderr", daemon=True)
        self._thread.start()

    def snapshot(self) -> bytes:
        with self._lock:
            return b"".join(self._chunks)

    def join(self, timeout: float = 0.2) -> None:
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while True:
            with suppress(OSError, ValueError):
                chunk = self._stream.read(4096)
                if chunk:
                    self._append(chunk)
                    continue
            return

    def _append(self, chunk: bytes) -> None:
        with self._lock:
            self._chunks.append(chunk)
            self._size += len(chunk)
            while self._size > self._max_bytes and self._chunks:
                excess = self._size - self._max_bytes
                first = self._chunks[0]
                if len(first) <= excess:
                    self._chunks.popleft()
                    self._size -= len(first)
                else:
                    self._chunks[0] = first[excess:]
                    self._size -= excess


class SubprocessAdapterRuntimeProvider:
    """Generic owned subprocess/client provider used by both fixed adapters."""

    def __init__(
        self,
        *,
        command: Command,
        cwd: Path,
        launcher: LanguageServerSubprocessLauncher,
        env: Mapping[str, str | None] | None = None,
        request_timeout: float = 30.0,
        request_handlers: Mapping[str, Callable[[Any], Any]] | None = None,
        stderr_max_bytes: int = 64 * 1024,
        terminate_timeout: float = 2.0,
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._launcher = launcher
        self._env = env
        self._request_timeout = request_timeout
        self._request_handlers = request_handlers
        self._stderr_max_bytes = stderr_max_bytes
        self._terminate_timeout = terminate_timeout

    @property
    def environment(self) -> Mapping[str, str | None]:
        return MappingProxyType(dict(self._env or {}))

    def start(
        self,
        *,
        notification_handler: Callable[[str, Any], None],
        terminal_handler: Callable[[BaseException], None],
    ) -> AdapterRuntime:
        process = self._launcher.launch(self._command, cwd=self._cwd, env=self._env)
        if process.stdin is None or process.stdout is None or process.stderr is None:
            terminate_process_tree_with_kill_fallback(process, self._terminate_timeout, "language server")
            raise RuntimeError("language-server process did not expose all standard streams")
        stopping = threading.Event()
        capture = BoundedStderrCapture(process.stderr, max_bytes=self._stderr_max_bytes)

        def report_terminal(error: BaseException) -> None:
            if not stopping.is_set():
                terminal_handler(error)

        client = SyncLspClient(
            process.stdout,
            process.stdin,
            request_timeout=self._request_timeout,
            notification_handler=notification_handler,
            request_handlers=self._request_handlers,
            terminal_handler=report_terminal,
        )
        client.start()
        runtime = AdapterRuntime(client=client, process=process, stderr_capture=capture, stopping=stopping)

        def observe_process() -> None:
            return_code = process.wait()
            if not stopping.is_set():
                tail = capture.snapshot().decode("utf-8", "replace")[-1024:]
                suffix = f"; stderr tail: {tail}" if tail else ""
                terminal_handler(LspProcessLost(f"language server exited with status {return_code}{suffix}"))

        observer = threading.Thread(target=observe_process, name="serena-light-lsp-process", daemon=True)
        runtime.process_observer = observer
        observer.start()
        return runtime

    def stop(self, runtime: AdapterRuntime) -> None:
        if runtime.stopping is not None:
            runtime.stopping.set()
        with suppress(Exception):
            runtime.client.shutdown(timeout=self._terminate_timeout)
        process = runtime.process
        if process is not None and process.poll() is None:
            terminate_process_tree_with_kill_fallback(process, self._terminate_timeout, "language server")
        if runtime.stderr_capture is not None:
            runtime.stderr_capture.join()
        observer = runtime.process_observer
        if observer is not None and observer is not threading.current_thread():
            observer.join(timeout=0.2)


class LanguageAdapter:
    """One independently lazy, failure-isolated language-server adapter.

    Public operations enqueue exactly one worker call on the injected executor.
    Readiness waits are intentionally separate synchronous methods and never
    acquire the workspace operation lock or submit recursively from that worker.
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        facts: AdapterLanguageFacts,
        runtime_provider: AdapterRuntimeProvider,
        executor: BoundedLspExecutor,
        scope_tracker: ScopeGenerationTracker,
        lsp_state: LspState,
        document_witness: DocumentReadinessWitness,
        operation_lock: threading.RLock | threading.Lock,
        crash_policy: CrashPolicy | None = None,
        readiness_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        timestamp: Callable[[], float] = time.time,
        notification_handler: Callable[[str, Any], None] | None = None,
    ) -> None:
        if readiness_timeout < 0 or readiness_timeout > 30.0:
            raise ValueError("adapter readiness timeout must be between 0 and 30 seconds")
        self.workspace_root = workspace_root.resolve()
        self.facts = facts
        self._runtime_provider = runtime_provider
        self._executor = executor
        self._scope = scope_tracker
        self._lsp_state = lsp_state
        self._document_witness = document_witness
        self._operation_lock = operation_lock
        self._crash_policy = crash_policy or CrashPolicy()
        self._readiness_timeout = readiness_timeout
        self._clock = clock
        self._timestamp = timestamp
        self._notification_handler = notification_handler
        self._state_lock = threading.RLock()
        self._phase = AdapterPhase.COLD
        self._runtime: AdapterRuntime | None = None
        self._runtime_token = 0
        self._crashed_runtime_tokens: set[int] = set()
        self._pending_documents: dict[str, DocumentReadinessTarget] = {}
        self._raw_providers = RawLspProviders()
        self._derived_tools = DerivedToolAvailability.from_raw(self._raw_providers)
        self._position_encoding = facts.default_position_encoding
        self._crash_times: deque[float] = deque()
        self._crash_total = 0
        self._last_crash_timestamp: float | None = None
        self._last_crash_error: str | None = None
        self._cooldown_until_monotonic: float | None = None
        self._cooldown_until_timestamp: float | None = None
        self._transitions: list[PhaseTransition] = []
        self._transition(AdapterPhase.COLD, "registered")

    def routes(self, path: str | Path) -> bool:
        return self.facts.routes(path)

    def snapshot(self) -> AdapterSnapshot:
        with self._state_lock:
            self._refresh_cooldown_locked()
            scope_generations = self._scope.generations
            if (
                self._phase is AdapterPhase.READY
                and scope_generations.observed_configured_program < scope_generations.configured_program
            ):
                self._transition(AdapterPhase.GLOBAL_WARMING, "configured-program generation is stale")
            runtime = self._runtime
            process_running = runtime is not None and (runtime.process is None or runtime.process.poll() is None)
            return AdapterSnapshot(
                name=self.facts.name,
                phase=self._phase,
                raw_providers=self._raw_providers,
                derived_tools=self._derived_tools,
                engine=self.facts.engine,
                position_encoding=self._position_encoding,
                generations=self._generations(),
                crash=self._crash_snapshot_locked(),
                transitions=tuple(self._transitions),
                running=process_running,
            )

    def start(self) -> Future[AdapterSnapshot]:
        return self._executor.submit(self._start_and_snapshot_worker)

    def stop(self) -> Future[AdapterSnapshot]:
        return self._executor.submit(self._stop_and_snapshot_worker)

    def submit_read(self, operation: Callable[[AdapterClient], T]) -> Future[T]:
        return self._executor.submit(lambda: self._execute_worker(operation, read_only=True))

    def submit_edit(self, operation: Callable[[AdapterClient], T]) -> Future[T]:
        return self._executor.submit(lambda: self._execute_worker(operation, read_only=False))

    def open_document(
        self,
        *,
        relative_path: str,
        uri: str,
        version: int,
        text: str,
    ) -> Future[DocumentReadinessTarget]:
        return self._executor.submit(
            lambda: self._open_document_worker(
                relative_path=relative_path,
                uri=uri,
                version=version,
                text=text,
            )
        )

    def warm_global(
        self,
        witness: GlobalReadinessWitness,
        *,
        timeout: float | None = None,
    ) -> Future[tuple[Mapping[str, object], ...]]:
        bounded_timeout = self._bounded_timeout(timeout)
        return self._executor.submit(lambda: self._warm_global_worker(witness, bounded_timeout))

    def probe_document(
        self,
        target: DocumentReadinessTarget,
        probe: DocumentReadinessProbe,
    ) -> Future[DocumentReadinessTarget]:
        return self._executor.submit(lambda: self._probe_document_worker(target, probe))

    def wait_for_document(
        self,
        target: DocumentReadinessTarget,
        *,
        timeout: float | None = None,
    ) -> ReadinessResult:
        current = self._lsp_state.document(target.uri)
        if current is None or current.generation != target.document_generation:
            return self._scope.wait_for_path(target.relative_path, timeout=0)
        return self._scope.wait_for_path(target.relative_path, timeout=self._bounded_timeout(timeout))

    def wait_for_global(self, *, timeout: float | None = None) -> ReadinessResult:
        result = self._scope.wait_for_configured_program(timeout=self._bounded_timeout(timeout))
        with self._state_lock:
            if result.code is ReadinessCode.SCOPE_INCOMPATIBLE:
                self._transition(AdapterPhase.DEGRADED, "configured program is outside trust")
            elif not result.ready and self._phase is AdapterPhase.READY:
                self._transition(AdapterPhase.GLOBAL_WARMING, "configured-program generation is stale")
        return result

    def _start_and_snapshot_worker(self) -> AdapterSnapshot:
        self._ensure_started_worker()
        return self.snapshot()

    def _stop_and_snapshot_worker(self) -> AdapterSnapshot:
        self._stop_worker()
        return self.snapshot()

    def _ensure_started_worker(self) -> AdapterClient:
        with self._state_lock:
            self._refresh_cooldown_locked()
            if self._phase is AdapterPhase.COOLDOWN:
                crash = self._crash_snapshot_locked()
                raise AdapterError(
                    AdapterErrorCode.COOLDOWN,
                    f"adapter {self.facts.name} is cooling down",
                    retry_after_seconds=crash.cooldown_remaining_seconds,
                )
            runtime = self._runtime
            if runtime is not None and self._runtime_token not in self._crashed_runtime_tokens:
                self._raise_if_process_lost(runtime)
                return runtime.client
            self._transition(AdapterPhase.STARTING, "lazy start")
            self._runtime_token += 1
            token = self._runtime_token
            self._runtime = None

        if runtime is not None:
            self._runtime_provider.stop(runtime)
        try:
            started = self._runtime_provider.start(
                notification_handler=self._on_notification,
                terminal_handler=lambda error: self._on_terminal(token, error),
            )
            with self._state_lock:
                self._runtime = started
            result = started.client.request("initialize", dict(self.facts.initialize_params))
            if not isinstance(result, Mapping):
                raise TypeError("initialize result must be an object")
            raw = RawLspProviders.from_initialize_result(result)
            selected_encoding = _selected_position_encoding(result, self.facts.default_position_encoding)
            started.client.notify("initialized", {})
            started.client.notify("workspace/didChangeConfiguration", {"settings": {}})
        except BaseException as error:
            with self._state_lock:
                failed = self._runtime
                self._runtime = None
            if failed is not None:
                self._runtime_provider.stop(failed)
            self._record_crash(token, error)
            raise
        with self._state_lock:
            self._raw_providers = raw
            self._derived_tools = DerivedToolAvailability.from_raw(raw)
            self._position_encoding = selected_encoding
            return started.client

    def _stop_worker(self) -> None:
        with self._state_lock:
            runtime = self._runtime
            if runtime is None and self._phase is AdapterPhase.COLD:
                return
            self._transition(AdapterPhase.STOPPING, "explicit stop")
            self._runtime = None
            self._pending_documents.clear()
        if runtime is not None:
            self._runtime_provider.stop(runtime)
        with self._state_lock:
            self._transition(AdapterPhase.COLD, "stopped")

    def _execute_worker(self, operation: Callable[[AdapterClient], T], *, read_only: bool) -> T:
        attempts = 2 if read_only else 1
        for attempt in range(attempts):
            client = self._ensure_started_worker()
            try:
                with self._operation_lock:
                    runtime = self._runtime
                    if runtime is None:
                        raise LspTransportClosed("adapter runtime disappeared before dispatch")
                    self._raise_if_process_lost(runtime)
                    return operation(client)
            except (LspTransportClosed, LspProcessLost) as error:
                self._record_crash(self._runtime_token, error)
                if attempt + 1 >= attempts:
                    raise
                self._restart_worker()
        raise AssertionError("unreachable adapter retry state")

    def _restart_worker(self) -> None:
        with self._state_lock:
            self._refresh_cooldown_locked()
            if self._phase is AdapterPhase.COOLDOWN:
                crash = self._crash_snapshot_locked()
                raise AdapterError(
                    AdapterErrorCode.COOLDOWN,
                    f"adapter {self.facts.name} entered cooldown",
                    retry_after_seconds=crash.cooldown_remaining_seconds,
                )
            runtime = self._runtime
            self._runtime = None
            self._transition(AdapterPhase.STARTING, "restart after transport loss")
        if runtime is not None:
            self._runtime_provider.stop(runtime)
        self._ensure_started_worker()

    def _open_document_worker(
        self,
        *,
        relative_path: str,
        uri: str,
        version: int,
        text: str,
    ) -> DocumentReadinessTarget:
        normalized = _normalize_relative_path(relative_path)
        if normalized not in self._scope.projection.trust_inventory.paths:
            raise ValueError(f"path is outside the current trust inventory: {normalized}")
        client = self._ensure_started_worker()
        with self._operation_lock:
            document = self._lsp_state.update_document(
                uri=uri,
                path=self.workspace_root / normalized,
                version=version,
            )
            if document is None:
                raise ValueError(f"document version {version} is not newer for {uri}")
            self._lsp_state.advance_source_generation()
            path_generation = self._scope.generations.path_scoped.get(normalized, 0)
            target = DocumentReadinessTarget(
                uri=uri,
                relative_path=normalized,
                absolute_path=document.path,
                version=version,
                document_generation=document.generation,
                path_generation=path_generation,
            )
            with self._state_lock:
                self._pending_documents[uri] = target
            try:
                client.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": self.facts.language_id_for(normalized),
                            "version": version,
                            "text": text,
                        }
                    },
                )
            except BaseException:
                with self._state_lock:
                    self._pending_documents.pop(uri, None)
                raise
            return target

    def _warm_global_worker(
        self,
        witness: GlobalReadinessWitness,
        timeout: float,
    ) -> tuple[Mapping[str, object], ...]:
        client = self._ensure_started_worker()
        with self._state_lock:
            if self._phase not in {AdapterPhase.DOCUMENT_READY, AdapterPhase.GLOBAL_WARMING, AdapterPhase.READY}:
                raise ReadinessWitnessError(
                    AdapterErrorCode.NOT_READY,
                    "global warm-up requires a current controlled document witness",
                    retry_after_seconds=0.1,
                )
            self._transition(AdapterPhase.GLOBAL_WARMING, f"exact sentinel {witness.exact_symbol!r}")
        target_generation = self._scope.generations.configured_program
        with self._operation_lock:
            result = client.request(
                "workspace/symbol",
                {"query": witness.query or witness.exact_symbol},
                timeout=timeout,
            )
        if not isinstance(result, Sequence) or isinstance(result, str | bytes):
            raise ReadinessWitnessError(AdapterErrorCode.NOT_READY, "workspace-symbol sentinel returned no result list")
        exact_name = tuple(
            item
            for item in result
            if isinstance(item, Mapping)
            and item.get("name") == witness.exact_symbol
        )
        if not any(_workspace_symbol_uri(item) == witness.uri for item in exact_name):
            with self._state_lock:
                self._transition(AdapterPhase.DEGRADED, f"exact sentinel {witness.exact_symbol!r} was not observed")
            raise ReadinessWitnessError(
                AdapterErrorCode.NOT_READY,
                f"workspace-symbol sentinel did not return exact symbol/path {witness.exact_symbol!r}",
                retry_after_seconds=0.1,
            )
        if self._scope.generations.configured_program != target_generation:
            raise ReadinessWitnessError(
                AdapterErrorCode.NOT_READY,
                "configured-program generation changed during global warm-up",
                retry_after_seconds=0.1,
            )
        self._lsp_state.observe_index_generation(target_generation)
        observed_current = self._scope.observe_configured_program(target_generation)
        with self._state_lock:
            self._transition(
                AdapterPhase.READY if observed_current else AdapterPhase.GLOBAL_WARMING,
                "current configured-program sentinel observed",
            )
        return exact_name

    def _probe_document_worker(
        self,
        target: DocumentReadinessTarget,
        probe: DocumentReadinessProbe,
    ) -> DocumentReadinessTarget:
        client = self._ensure_started_worker()
        with self._operation_lock:
            observed = probe.observe(client, target, timeout=self._readiness_timeout)
        if not observed or not self._mark_document_ready(target):
            raise ReadinessWitnessError(
                AdapterErrorCode.NOT_READY,
                f"document readiness probe did not observe current generation for {target.relative_path}",
                retry_after_seconds=0.1,
            )
        return target

    def _on_notification(self, method: str, params: Any) -> None:
        if self._notification_handler is not None:
            self._notification_handler(method, params)
        with self._state_lock:
            target = self._pending_documents.get(params.get("uri") if isinstance(params, Mapping) else "")
        if target is None:
            return
        if not self._document_witness.observe(method, params, target, self._lsp_state):
            return
        self._mark_document_ready(target)

    def _mark_document_ready(self, target: DocumentReadinessTarget) -> bool:
        current = self._lsp_state.document(target.uri)
        generations = self._scope.generations
        if (
            current is None
            or current.generation != target.document_generation
            or generations.path_scoped.get(target.relative_path, 0) != target.path_generation
        ):
            return False
        if not self._scope.observe_path(target.relative_path, target.path_generation):
            return False
        with self._state_lock:
            self._pending_documents.pop(target.uri, None)
            if self._phase in {AdapterPhase.STARTING, AdapterPhase.DEGRADED}:
                self._transition(AdapterPhase.DOCUMENT_READY, f"document witness for {target.relative_path}")
        return True

    def _on_terminal(self, token: int, error: BaseException) -> None:
        with self._state_lock:
            if self._phase in {AdapterPhase.COLD, AdapterPhase.STOPPING}:
                return
        self._record_crash(token, error)

    def _record_crash(self, token: int, error: BaseException) -> None:
        now = self._clock()
        timestamp = self._timestamp()
        with self._state_lock:
            if token in self._crashed_runtime_tokens:
                return
            self._crashed_runtime_tokens.add(token)
            self._crash_total += 1
            self._last_crash_timestamp = timestamp
            self._last_crash_error = f"{type(error).__name__}: {error}"
            cutoff = now - self._crash_policy.window_seconds
            while self._crash_times and self._crash_times[0] < cutoff:
                self._crash_times.popleft()
            self._crash_times.append(now)
            if len(self._crash_times) >= self._crash_policy.threshold:
                self._cooldown_until_monotonic = now + self._crash_policy.cooldown_seconds
                self._cooldown_until_timestamp = timestamp + self._crash_policy.cooldown_seconds
                self._transition(AdapterPhase.COOLDOWN, self._last_crash_error)
            else:
                self._transition(AdapterPhase.DEGRADED, self._last_crash_error)

    def _refresh_cooldown_locked(self) -> None:
        deadline = self._cooldown_until_monotonic
        if self._phase is not AdapterPhase.COOLDOWN or deadline is None or self._clock() < deadline:
            return
        self._cooldown_until_monotonic = None
        self._cooldown_until_timestamp = None
        self._crash_times.clear()
        self._transition(AdapterPhase.DEGRADED, "cooldown expired")

    def _raise_if_process_lost(self, runtime: AdapterRuntime) -> None:
        process = runtime.process
        if process is not None and process.poll() is not None:
            raise LspProcessLost(f"language server exited with status {process.returncode}")

    def _bounded_timeout(self, timeout: float | None) -> float:
        if timeout is None:
            return self._readiness_timeout
        if timeout < 0:
            raise ValueError("readiness timeout must be non-negative")
        return min(timeout, self._readiness_timeout)

    def _transition(self, phase: AdapterPhase, reason: str) -> None:
        if self._transitions and self._phase is phase:
            return
        self._phase = phase
        self._transitions.append(
            PhaseTransition(
                phase=phase,
                timestamp=self._timestamp(),
                reason=reason,
                generations=self._generations(),
            )
        )

    def _generations(self) -> AdapterGenerations:
        scope = self._scope.generations
        lsp = self._lsp_state.generations
        return AdapterGenerations(
            trust=scope.trust_inventory,
            program=scope.configured_program,
            document=lsp.source,
            index=lsp.index,
        )

    def _crash_snapshot_locked(self) -> CrashSnapshot:
        now = self._clock()
        cutoff = now - self._crash_policy.window_seconds
        window_count = sum(crash >= cutoff for crash in self._crash_times)
        remaining = (
            max(0.0, self._cooldown_until_monotonic - now) if self._cooldown_until_monotonic is not None else 0.0
        )
        return CrashSnapshot(
            total=self._crash_total,
            window_count=window_count,
            last_timestamp=self._last_crash_timestamp,
            last_error=self._last_crash_error,
            cooldown_until=self._cooldown_until_timestamp,
            cooldown_remaining_seconds=remaining,
        )


def _provider_enabled(value: object) -> bool:
    return value is True or isinstance(value, Mapping)


def _workspace_symbol_uri(item: Mapping[str, object]) -> str | None:
    location = item.get("location")
    if not isinstance(location, Mapping):
        return None
    uri = cast(Mapping[str, object], location).get("uri")
    return uri if isinstance(uri, str) else None


def _normalize_extension(extension: str) -> str:
    normalized = extension.lower()
    if not normalized.startswith(".") or len(normalized) < 2 or "/" in normalized or "\\" in normalized:
        raise ValueError(f"invalid routed extension: {extension!r}")
    return normalized


def _normalize_relative_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if not path or path.startswith("/") or "\\" in path or "\x00" in path or ".." in candidate.parts:
        raise ValueError(f"invalid relative path: {path!r}")
    normalized = str(candidate)
    if normalized in {"", "."}:
        raise ValueError(f"invalid relative path: {path!r}")
    return normalized


def _selected_position_encoding(
    initialize_result: Mapping[str, object],
    default: PositionEncoding,
) -> PositionEncoding:
    capabilities = initialize_result.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return default
    selected = cast(Mapping[str, object], capabilities).get("positionEncoding")
    if selected is None:
        return default
    try:
        return PositionEncoding(str(selected))
    except ValueError as error:
        raise ValueError(f"server selected unsupported position encoding: {selected!r}") from error
