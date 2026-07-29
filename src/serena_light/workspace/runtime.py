"""Composition root for one physical workspace's fixed language adapters.

The daemon registry owns reuse and leases.  This module owns the expensive state
behind one normalized ``(kind, root)`` key: trust, native projections, one LSP
executor and operation lock, and the independently lazy Python and TypeScript
adapters.  Session working-directory metadata deliberately remains outside this
shared object.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Any, Protocol, cast
from urllib.parse import unquote, urlparse

from serena_light.lsp.adapter import (
    AdapterClient,
    AdapterPhase,
    AdapterSnapshot,
    DocumentReadinessProbe,
    DocumentReadinessTarget,
    GlobalReadinessWitness,
    LanguageAdapter,
    PublishedDiagnosticsWitness,
)
from serena_light.lsp.executor import BoundedLspExecutor
from serena_light.lsp.normalize import Location, NormalizedSymbol, Position, Range
from serena_light.lsp.positions import FileSnapshot, LspPosition, PositionError, PositionMapper
from serena_light.lsp.pyright import PyrightFacts
from serena_light.lsp.state import DiagnosticsSnapshot, DiagnosticsState, LspState
from serena_light.lsp.typescript import (
    TYPESCRIPT_VERSION,
    TypeScriptAdapterConfig,
    attribute_native_program,
    select_default_entry,
)
from serena_light.tools.declarations import CapabilityMatrix, DeclarationNavigationService, SemanticDocumentInput
from serena_light.tools.diagnostics import (
    DiagnosticDocumentInput,
    DiagnosticEngineFacts,
    DiagnosticsReadiness,
    DiagnosticsService,
    ExternalRootMetadata,
)
from serena_light.tools.editing import (
    AuthorizedEdit,
    NotificationResult,
    OperationLock,
    ReplacementNotification,
)
from serena_light.tools.editing import (
    replace_symbol_body as _replace_symbol_body,
)
from serena_light.tools.envelopes import (
    AdapterMetadata,
    ErrorCode,
    ErrorEnvelope,
    GenerationMetadata,
    RetryMetadata,
    ToolEnvelope,
    WorkspaceMetadata,
    error,
    from_workspace_error,
)
from serena_light.tools.global_symbols import (
    ConfiguredProgramScope,
    DocumentSymbolBatch,
    GlobalAdapterState,
    GlobalSymbolService,
    WorkspaceSymbolBatch,
)
from serena_light.tools.navigation import (
    DocumentNavigation,
    DocumentNavigationService,
    DocumentSymbolInput,
    source_body,
    source_range,
)
from serena_light.tools.references import (
    ReferenceDocumentInput,
    ReferenceNavigationService,
    ReferenceRequest,
    ReferenceTarget,
)
from serena_light.workspace.identity import (
    LocationKind,
    SemanticLocation,
    WorkspaceError,
    WorkspaceIdentity,
    WorkspaceKind,
)
from serena_light.workspace.inventory import (
    JAVASCRIPT_TYPESCRIPT_EXTENSIONS,
    PYTHON_EXTENSIONS,
    TrustInventory,
    git_trust_inventory,
    transformers_trust_inventory,
)
from serena_light.workspace.scope import LanguageFamily, ScopeGenerationTracker, ScopeProjection

type PhysicalWorkspaceKey = tuple[WorkspaceKind, Path]
type ProgramAttributor = Callable[[Path, tuple[str, ...]], ScopeProjection]


class RuntimeErrorCode(StrEnum):
    SCOPE_INCOMPATIBLE = "SCOPE_INCOMPATIBLE"
    UNSUPPORTED = "UNSUPPORTED"
    STOPPED = "STOPPED"


class WorkspaceRuntimeError(RuntimeError):
    """Transport-neutral runtime failure for a later typed envelope owner."""

    def __init__(self, code: str, message: str, *, paths: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.paths = paths


class WorkspacePathPolicy(Protocol):
    def authorize_path_operand(
        self,
        identity: WorkspaceIdentity,
        path: str | Path,
        inventory: Sequence[Path],
    ) -> Path: ...


class RuntimeAdapter(Protocol):
    def routes(self, path: str | Path) -> bool: ...

    def snapshot(self) -> AdapterSnapshot: ...

    def snapshot_open_and_probe_document(
        self,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        probe: DocumentReadinessProbe,
    ) -> Future[tuple[FileSnapshot, DocumentReadinessTarget]]: ...

    def submit_read(self, operation: Callable[[AdapterClient], Any]) -> Future[Any]: ...

    def submit_edit(self, operation: Callable[[AdapterClient], Any]) -> Future[Any]: ...

    def warm_global(
        self, witness: GlobalReadinessWitness, *, timeout: float | None = None
    ) -> Future[tuple[Mapping[str, object], ...]]: ...

    def diagnostics_snapshot(self, target: DocumentReadinessTarget) -> DiagnosticsSnapshot: ...

    def open_edit_document_with_client(
        self,
        client: AdapterClient,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        text: str,
    ) -> DocumentReadinessTarget: ...

    def notify_edit_with_client(
        self,
        client: AdapterClient,
        target: DocumentReadinessTarget,
        notification: ReplacementNotification,
    ) -> NotificationResult: ...

    def stop(self) -> Future[AdapterSnapshot]: ...


@dataclass(frozen=True, slots=True)
class AdapterBuildContext:
    family: LanguageFamily
    workspace_root: Path
    trusted_paths: tuple[str, ...]
    projection: ScopeProjection
    scope_tracker: ScopeGenerationTracker
    executor: BoundedLspExecutor
    operation_lock: threading.RLock


type AdapterFactory = Callable[[AdapterBuildContext], RuntimeAdapter]
type InventoryFactory = Callable[[WorkspaceIdentity], TrustInventory]
type ExecutorFactory = Callable[[Path], BoundedLspExecutor]


class _DocumentSymbolCapture:
    """Capture one bounded document-symbol response through the adapter probe seam."""

    def __init__(self) -> None:
        self.raw_symbols: tuple[Mapping[str, Any], ...] | None = None

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
        if result is None:
            self.raw_symbols = None
            return True
        if not isinstance(result, Sequence) or isinstance(result, str | bytes):
            return False
        if not all(isinstance(item, Mapping) for item in result):
            return False
        self.raw_symbols = tuple(cast(Mapping[str, Any], item) for item in result)
        return True


class _WorkspaceLanguageAdapter(LanguageAdapter):
    """Add the workspace-owned atomic snapshot/open/probe seam."""

    def snapshot_open_and_probe_document(
        self,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        probe: DocumentReadinessProbe,
    ) -> Future[tuple[FileSnapshot, DocumentReadinessTarget]]:
        def worker() -> tuple[FileSnapshot, DocumentReadinessTarget]:
            with self._operation_lock:
                snapshot = FileSnapshot.from_bytes(absolute_path.read_bytes())
                target = self._open_document_worker(
                    relative_path=relative_path,
                    uri=uri,
                    version=version,
                    text=snapshot.text,
                )
                observed = self._probe_document_worker(target, probe)
                return snapshot, observed

        return self._executor.submit(worker)

    def snapshot_open_and_probe_diagnostics(
        self,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        probe: DocumentReadinessProbe,
    ) -> Future[tuple[FileSnapshot, DocumentReadinessTarget]]:
        """Open or change one document while retaining push-diagnostic correlation."""

        def worker() -> tuple[FileSnapshot, DocumentReadinessTarget]:
            with self._operation_lock:
                snapshot = FileSnapshot.from_bytes(absolute_path.read_bytes())
                current = self._lsp_state.document(uri)
                if current is None:
                    target = self._open_document_worker(
                        relative_path=relative_path,
                        uri=uri,
                        version=version,
                        text=snapshot.text,
                    )
                else:
                    client = self._ensure_started_worker()
                    document = self._lsp_state.update_document(uri=uri, path=absolute_path, version=version)
                    if document is None:
                        raise ValueError(f"document version {version} is not newer for {uri}")
                    self._lsp_state.advance_source_generation()
                    path_generation = self._scope.generations.path_scoped.get(relative_path, 0)
                    target = DocumentReadinessTarget(
                        uri,
                        relative_path,
                        absolute_path,
                        version,
                        document.generation,
                        path_generation,
                    )
                    with self._state_lock:
                        self._pending_documents[uri] = target
                    client.notify(
                        "textDocument/didChange",
                        {
                            "textDocument": {"uri": uri, "version": version},
                            "contentChanges": [{"text": snapshot.text}],
                        },
                    )
                observed = self._probe_document_worker(target, probe)
                return snapshot, observed

        return self._executor.submit(worker)

    def _mark_document_ready(self, target: DocumentReadinessTarget) -> bool:
        """Keep the current target routable for diagnostics after readiness."""

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
            if self._phase in {AdapterPhase.STARTING, AdapterPhase.DEGRADED}:
                self._transition(AdapterPhase.DOCUMENT_READY, f"document witness for {target.relative_path}")
        return True

    def diagnostics_snapshot(self, target: DocumentReadinessTarget) -> DiagnosticsSnapshot:
        """Expose only the current push publication for runtime diagnostics."""

        return self._lsp_state.diagnostics_snapshot(target.uri, generation=target.document_generation)

    def open_edit_document_with_client(
        self,
        client: AdapterClient,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        text: str,
    ) -> DocumentReadinessTarget:
        """Open the exact pre-install snapshot from an already-owned edit worker."""

        del absolute_path
        return self._open_document_worker(relative_path=relative_path, uri=uri, version=version, text=text)

    def notify_edit_with_client(
        self,
        client: AdapterClient,
        target: DocumentReadinessTarget,
        notification: ReplacementNotification,
    ) -> NotificationResult:
        """Publish one installed replacement and advance the owned document state."""

        if notification.uri != target.uri or notification.path != target.absolute_path:
            raise ValueError("replacement notification does not match the opened edit document")
        version = target.version + 1
        client.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": target.uri, "version": version},
                "contentChanges": [{"text": notification.text}],
            },
        )
        document = self._lsp_state.update_document(uri=target.uri, path=target.absolute_path, version=version)
        if document is None:
            raise ValueError("replacement document version is not newer")
        self._lsp_state.advance_source_generation()
        changed = DocumentReadinessTarget(
            target.uri,
            target.relative_path,
            target.absolute_path,
            version,
            document.generation,
            target.path_generation,
        )
        with self._state_lock:
            self._pending_documents[target.uri] = changed
        return NotificationResult("notified", document.generation, _path_generations(self.snapshot()))


class WorkspaceRuntime:
    """One shared runtime for exactly one normalized physical workspace key."""

    def __init__(
        self,
        identity: WorkspaceIdentity | PhysicalWorkspaceKey,
        *,
        path_policy: WorkspacePathPolicy,
        inventory: TrustInventory | None = None,
        inventory_factory: InventoryFactory | None = None,
        attributors: Mapping[LanguageFamily, ProgramAttributor] | None = None,
        adapter_factories: Mapping[LanguageFamily, AdapterFactory] | None = None,
        executor_factory: ExecutorFactory | None = None,
        future_timeout: float = 35.0,
    ) -> None:
        if future_timeout <= 0:
            raise ValueError("future_timeout must be positive")
        self.identity = _physical_identity(identity)
        self.key = self.identity.registry_key
        self._path_policy = path_policy
        self._future_timeout = future_timeout
        self.inventory = inventory or (inventory_factory or _default_inventory)(self.identity)
        if self.inventory.root.resolve(strict=True) != self.identity.root:
            raise ValueError("trust inventory root does not match the physical workspace key")

        family_paths = {
            LanguageFamily.PYTHON: _paths_for(self.inventory.paths, PYTHON_EXTENSIONS),
            LanguageFamily.TYPESCRIPT: _paths_for(self.inventory.paths, JAVASCRIPT_TYPESCRIPT_EXTENSIONS),
        }
        self._projections: dict[LanguageFamily, ScopeProjection] = {}
        for family, paths in family_paths.items():
            if not paths:
                continue
            attributor = (attributors or {}).get(family) or _default_attributor(family)
            try:
                projection = attributor(self.identity.root, paths)
            except Exception as error:
                raise WorkspaceRuntimeError(
                    RuntimeErrorCode.SCOPE_INCOMPATIBLE,
                    f"{family.value} native-program attribution failed: {error}",
                ) from error
            if projection.language is not family:
                raise ValueError(f"{family.value} attributor returned a projection for {projection.language.value}")
            if not projection.compatible:
                raise WorkspaceRuntimeError(
                    RuntimeErrorCode.SCOPE_INCOMPATIBLE,
                    f"{family.value} configured program contains paths outside trust",
                    paths=tuple(item.path for item in projection.configured_program_outside_trust),
                )
            self._projections[family] = projection

        self._executor = (executor_factory or _default_executor)(self.identity.root)
        self._operation_lock = threading.RLock()
        self._adapters: dict[LanguageFamily, RuntimeAdapter] = {}
        self._versions: dict[str, int] = {}
        self._state_lock = threading.RLock()
        self._stopped = False
        try:
            for family, projection in self._projections.items():
                context = AdapterBuildContext(
                    family=family,
                    workspace_root=self.identity.root,
                    trusted_paths=family_paths[family],
                    projection=projection,
                    scope_tracker=ScopeGenerationTracker(projection),
                    executor=self._executor,
                    operation_lock=self._operation_lock,
                )
                factory = (adapter_factories or {}).get(family) or _default_adapter_factory(family)
                self._adapters[family] = factory(context)
        except BaseException:
            stop_futures: list[Future[AdapterSnapshot]] = []
            for adapter in self._adapters.values():
                with suppress(BaseException):
                    stop_futures.append(adapter.stop())
            for future in stop_futures:
                with suppress(BaseException):
                    future.result(timeout=self._future_timeout)
            self._executor.close(cancel_queued=True)
            raise

    @property
    def executor(self) -> BoundedLspExecutor:
        return self._executor

    @property
    def projections(self) -> Mapping[LanguageFamily, ScopeProjection]:
        return dict(self._projections)

    def route(self, relative_path: str) -> RuntimeAdapter:
        """Authorize one path and select exactly one fixed adapter without starting it."""

        return self._route(relative_path)[1]

    def _route(self, relative_path: str) -> tuple[LanguageFamily, RuntimeAdapter]:
        """Return the fixed family together with its authorized adapter."""

        normalized = _relative_path(relative_path, allow_parent=True)
        self._require_running()
        if ".." in PurePosixPath(normalized).parts and not (self.identity.root / normalized).exists():
            raise ValueError(f"path does not exist: {relative_path!r}")
        inventory_paths = tuple(self.identity.root / path for path in self.inventory.paths)
        authorized = self._path_policy.authorize_path_operand(
            self.identity,
            self.identity.root / normalized,
            inventory_paths,
        )
        if ".." in PurePosixPath(normalized).parts:
            raise ValueError(f"path is not normalized within the active workspace: {relative_path!r}")
        routed = tuple((family, adapter) for family, adapter in self._adapters.items() if adapter.routes(authorized))
        if len(routed) != 1:
            raise WorkspaceRuntimeError(
                RuntimeErrorCode.UNSUPPORTED,
                f"authorized path routes to {len(routed)} adapters",
                paths=(normalized,),
            )
        return routed[0]

    def load_document_symbols(self, relative_path: str) -> DocumentSymbolInput:
        """Authorize, snapshot, open and probe one document through executor futures."""

        document, _target, _family, _adapter = self._load_document(relative_path)
        return document

    def get_symbols_overview(
        self,
        relative_path: str,
        *,
        max_depth: int = 1,
        max_answer_chars: int = 12_000,
    ) -> ToolEnvelope:
        """Render the existing one-document overview core through this runtime."""

        return self._semantic_envelope(
            relative_path,
            lambda: DocumentNavigationService(self).get_symbols_overview(
                relative_path, max_depth=max_depth, max_answer_chars=max_answer_chars
            )
        )

    def find_symbol(
        self,
        name_path: str | Sequence[str],
        *,
        relative_path: str | None = None,
        substring_matching: bool = False,
        include_body: bool = False,
        include_info: bool = False,
        max_answer_chars: int = 12_000,
        max_candidates_per_adapter: int = 128,
    ) -> ToolEnvelope:
        """Find a symbol in one selected file, or via bounded configured-program search."""

        def operation() -> ToolEnvelope:
            if relative_path is not None:
                self._route(relative_path)
                return DocumentNavigationService(self).find_symbol(
                    relative_path,
                    name_path,
                    substring_matching=substring_matching,
                    include_body=include_body,
                    include_info=include_info,
                    max_answer_chars=max_answer_chars,
                )
            warmed = self._warm_global_candidates(name_path)
            return GlobalSymbolService(
                tuple(_GlobalProvider(self, family, warmed.get(family)) for family in self._adapters)
            ).find_symbol(
                name_path,
                substring_matching=substring_matching,
                max_candidates_per_adapter=max_candidates_per_adapter,
                max_answer_chars=max_answer_chars,
            )

        return self._tool_envelope(operation)

    def _warm_global_candidates(
        self, name_path: str | Sequence[str]
    ) -> Mapping[LanguageFamily, _WarmGlobalSeed]:
        """Warm fixed adapters from one controlled witness within one shared budget.

        A newly started language server may answer ``workspace/symbol`` before
        its configured-program index contains the requested symbol.  Polling is
        intentionally outside the workspace executor and operation lock: every
        short request is serialized normally.  If the requested name is absent
        from one language family, one deterministic configured-program document
        supplies that adapter's exact readiness sentinel instead.
        """

        query = _final_name_segment(name_path)
        if query is None:
            return {}
        budget = min(30.0, self._future_timeout)
        warm_budget = budget if budget <= 0.5 else budget - 0.5
        deadline = time.monotonic() + warm_budget
        warmed: dict[LanguageFamily, _WarmGlobalSeed] = {}
        fallback_documents: dict[LanguageFamily, DocumentSymbolInput] = {}
        pending = sorted(
            (
                (family, adapter)
                for family, adapter in self._adapters.items()
                if adapter.snapshot().phase.value != "ready"
            ),
            key=lambda item: self._projections[item[0]].configured_program.count,
        )
        while pending and (remaining := deadline - time.monotonic()) > 0:
            next_pending: list[tuple[LanguageFamily, RuntimeAdapter]] = []
            for position, (family, adapter) in enumerate(pending):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                families_left = len(pending) - position
                turn_deadline = time.monotonic() + remaining / families_left
                try:
                    document = fallback_documents.get(family)
                    if document is None:
                        paths = self._projections[family].configured_program.paths
                        relative_path = next(
                            (path for path in paths if not PurePosixPath(path).name.startswith("__init__.")),
                            paths[0] if paths else "",
                        )
                        if not relative_path:
                            next_pending.append((family, adapter))
                            continue
                        request_timeout = turn_deadline - time.monotonic()
                        document, _target, loaded_family, _loaded_adapter = self._load_document(
                            relative_path, timeout=request_timeout
                        )
                        if loaded_family is not family:
                            next_pending.append((family, adapter))
                            continue
                        fallback_documents[family] = document
                    witness_query = _first_document_symbol_name(document.raw_symbols) or ""
                    if not witness_query:
                        next_pending.append((family, adapter))
                        continue
                    remaining = turn_deadline - time.monotonic()
                    if remaining <= 0:
                        next_pending.append((family, adapter))
                        continue
                    request_timeout = remaining
                    candidates = adapter.warm_global(
                        GlobalReadinessWitness(witness_query, document.uri, witness_query), timeout=request_timeout
                    ).result(timeout=request_timeout)
                    snapshot = adapter.snapshot()
                    if snapshot.phase.value == "ready":
                        warmed[family] = _WarmGlobalSeed(
                            witness_query,
                            tuple(candidates),
                            document,
                            _global_generations(snapshot),
                        )
                except Exception:
                    next_pending.append((family, adapter))
            pending = next_pending
            if pending:
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return warmed

    def find_declaration(
        self,
        relative_path: str,
        regex: str,
        containing_symbol_name_path: str | None = None,
        include_body: bool = False,
        include_info: bool = False,
    ) -> ToolEnvelope:
        return self._semantic_envelope(
            relative_path,
            lambda: DeclarationNavigationService(self).find_declaration(
                relative_path,
                regex,
                containing_symbol_name_path=containing_symbol_name_path,
                include_body=include_body,
                include_info=include_info,
            )
        )

    def find_implementations(
        self,
        name_path: str,
        relative_path: str,
        include_info: bool = False,
        include_kinds: Sequence[int] | None = None,
        exclude_kinds: Sequence[int] | None = None,
        max_answer_chars: int = 12_000,
    ) -> ToolEnvelope:
        return self._semantic_envelope(
            relative_path,
            lambda: DeclarationNavigationService(self).find_implementations(
                name_path,
                relative_path,
                include_info=include_info,
                include_kinds=include_kinds,
                exclude_kinds=exclude_kinds,
                max_answer_chars=max_answer_chars,
            )
        )

    def find_referencing_symbols(
        self,
        relative_path: str,
        name_path: str,
        *,
        max_snippet_chars: int = 240,
        max_answer_chars: int = 12_000,
    ) -> ToolEnvelope:
        """Resolve one local symbol, then delegate presentation to the reference core."""

        def operation() -> ToolEnvelope:
            loaded = self.load_semantic_document(relative_path)
            if isinstance(loaded, ErrorEnvelope):
                return loaded
            document = DocumentNavigation.from_input(loaded.document)
            selected = _selected_symbol(document, name_path)
            if isinstance(selected, ErrorEnvelope):
                return selected
            request = ReferenceRequest(
                document.relative_path,
                selected.selection_range.start,
                document.workspace,
                document.adapter,
                document.generations,
            )
            return ReferenceNavigationService(self, self, self).find_referencing_symbols(
                request, max_snippet_chars=max_snippet_chars, max_answer_chars=max_answer_chars
            )

        return self._semantic_envelope(relative_path, operation)

    def get_diagnostics_for_file(
        self,
        relative_path: str,
        *,
        timeout_seconds: float = 1.0,
        maximum_severity: int = 2,
        max_answer_chars: int = 12_000,
    ) -> ToolEnvelope:
        return self._semantic_envelope(
            relative_path,
            lambda: DiagnosticsService(self).get_diagnostics_for_file(
                relative_path,
                timeout_seconds=timeout_seconds,
                maximum_severity=maximum_severity,
                max_answer_chars=max_answer_chars,
            )
        )

    def get_diagnostics_for_symbol(
        self,
        relative_path: str,
        name_path: str,
        *,
        timeout_seconds: float = 1.0,
        maximum_severity: int = 2,
        max_answer_chars: int = 12_000,
    ) -> ToolEnvelope:
        return self._semantic_envelope(
            relative_path,
            lambda: DiagnosticsService(self).get_diagnostics_for_symbol(
                relative_path,
                name_path,
                timeout_seconds=timeout_seconds,
                maximum_severity=maximum_severity,
                max_answer_chars=max_answer_chars,
            )
        )

    def replace_symbol_body(
        self,
        name_path: str | Sequence[str],
        relative_path: str,
        body: str,
        expected_hash: str,
    ) -> ToolEnvelope:
        """Run one hash-guarded edit transaction on the selected adapter worker."""

        def operation() -> ToolEnvelope:
            adapter, authorized = self._edit_adapter(relative_path)

            def transaction(client: AdapterClient) -> ToolEnvelope:
                bridge = _EditBridge(self, adapter, client, authorized)
                return _replace_symbol_body(
                    name_path,
                    relative_path,
                    body,
                    expected_hash,
                    authorizer=bridge,
                    symbol_provider=bridge,
                    notifier=bridge,
                    operation_lock=cast(OperationLock, _NoopOperationLock()),
                )

            return adapter.submit_edit(transaction).result(timeout=self._future_timeout)

        return self._tool_envelope(operation)

    def _edit_adapter(self, relative_path: str) -> tuple[RuntimeAdapter, AuthorizedEdit]:
        normalized = _relative_path(relative_path, allow_parent=True)
        self._require_running()
        if ".." in PurePosixPath(normalized).parts and not (self.identity.root / normalized).exists():
            raise ValueError(f"path does not exist: {relative_path!r}")
        authorize = getattr(self._path_policy, "authorize_edit", None)
        if not callable(authorize):
            raise ValueError("workspace policy does not support edits")
        authorized = authorize(
            self.identity,
            self.identity.root / normalized,
            tuple(self.identity.root / item for item in self.inventory.paths),
        )
        if ".." in PurePosixPath(normalized).parts:
            raise ValueError(f"path is not normalized within the active workspace: {relative_path!r}")
        routed = tuple(adapter for adapter in self._adapters.values() if adapter.routes(authorized))
        if len(routed) != 1:
            raise WorkspaceRuntimeError(
                RuntimeErrorCode.UNSUPPORTED, "edit path has no unique adapter", paths=(normalized,)
            )
        return routed[0], AuthorizedEdit(authorized, normalized, _workspace_metadata(self.identity))

    def _tool_envelope(self, operation: Callable[[], ToolEnvelope]) -> ToolEnvelope:
        try:
            return operation()
        except WorkspaceError as caught:
            return from_workspace_error(caught)
        except WorkspaceRuntimeError as caught:
            code = (
                ErrorCode.SCOPE_INCOMPATIBLE
                if caught.code is RuntimeErrorCode.SCOPE_INCOMPATIBLE
                else ErrorCode.UNSUPPORTED
            )
            return error(code, details={"paths": caught.paths} if caught.paths else {})
        except (OSError, TypeError, ValueError):
            return error(ErrorCode.INVALID_INPUT)

    def _semantic_envelope(
        self, relative_path: str, operation: Callable[[], ToolEnvelope]
    ) -> ToolEnvelope:
        def authorized() -> ToolEnvelope:
            self._route(relative_path)
            return operation()

        return self._tool_envelope(authorized)

    # The following narrow methods are injected-provider seams consumed by the
    # transport-neutral semantic cores above.  They deliberately do not expose
    # inventory enumeration, pull diagnostics, or edit operations.

    def load_semantic_document(self, relative_path: str) -> SemanticDocumentInput:
        document, _target, _family, adapter = self._load_document(relative_path)
        return SemanticDocumentInput(document, CapabilityMatrix.from_raw(adapter.snapshot().raw_providers))

    def request_locations(
        self, method: str, *, document_uri: str, position: LspPosition
    ) -> object:
        adapter = self._adapter_for_workspace_uri(document_uri)
        if method not in {
            "textDocument/definition",
            "textDocument/implementation",
            "textDocument/references",
        }:
            raise ValueError(f"unsupported semantic request: {method}")
        params: dict[str, object] = {
            "textDocument": {"uri": document_uri},
            "position": {"line": position.line, "character": position.character},
        }
        if method == "textDocument/references":
            params["context"] = {"includeDeclaration": True}
        return adapter.submit_read(
            lambda client: client.request(method, params, timeout=self._future_timeout)
        ).result(timeout=self._future_timeout)

    def normalize_and_classify_locations(
        self,
        raw_locations: object,
        *,
        include_body: bool,
        include_info: bool,
    ) -> Sequence[Mapping[str, object]] | ErrorEnvelope:
        """Classify and render locations against each target's immutable snapshot."""
        locations = _raw_location_mappings(raw_locations)
        if locations is None:
            return error(ErrorCode.INVALID_INPUT, details={"field": "semantic_locations"})
        rendered: list[Mapping[str, object]] = []
        for raw in locations:
            try:
                location = _location_from_raw(raw)
                semantic = self._classify_semantic_location(location.uri)
                target_adapter = _unique_routing_adapter(tuple(self._adapters.values()), semantic.path)
                mapper = PositionMapper(
                    FileSnapshot.from_bytes(semantic.path.read_bytes()), target_adapter.snapshot().position_encoding
                )
                rendered_range = source_range(mapper, location.range)
                rendered_body = source_body(mapper, location.range) if include_body else None
            except (OSError, PositionError, TypeError, ValueError):
                return error(ErrorCode.UNTRUSTED_ROOT, details={"field": "semantic_location"})
            data: dict[str, object] = {
                "absolute_path": str(semantic.path),
                "location_kind": semantic.kind.value,
                "range": rendered_range,
            }
            if semantic.kind is LocationKind.WORKSPACE:
                data["relative_path"] = str(semantic.path.relative_to(self.identity.root))
            else:
                data["read_only_external"] = True
            raw_kind = raw.get("kind")
            if isinstance(raw_kind, int) and not isinstance(raw_kind, bool):
                data["kind"] = raw_kind
            if include_body:
                data["body"] = rendered_body
            if include_info:
                data["info"] = {"selection_range": rendered_range}
            rendered.append(data)
        return rendered

    def find_references(self, request: ReferenceRequest) -> Sequence[Location] | ErrorEnvelope:
        try:
            raw = self.request_locations(
                "textDocument/references",
                document_uri=(self.identity.root / request.relative_path).resolve(strict=True).as_uri(),
                position=LspPosition(request.position.line, request.position.character),
            )
            locations = _raw_location_mappings(raw)
            if locations is None:
                raise ValueError("references are not locations")
            normalized = tuple(_location_from_raw(item) for item in locations)
            return normalized
        except (OSError, TypeError, ValueError, WorkspaceRuntimeError):
            return error(ErrorCode.INVALID_INPUT, details={"field": "references"})

    def classify_reference_location(self, location: Location) -> ReferenceTarget | ErrorEnvelope:
        try:
            semantic = self._classify_semantic_location(location.uri)
        except (OSError, TypeError, ValueError):
            return error(ErrorCode.UNTRUSTED_ROOT, details={"field": "reference_location"})
        return ReferenceTarget(
            location,
            str(semantic.path),
            str(semantic.path.relative_to(self.identity.root))
            if semantic.kind is LocationKind.WORKSPACE
            else str(semantic.path),
            semantic.kind is LocationKind.READ_ONLY_EXTERNAL,
        )

    def load_reference_document(self, target: ReferenceTarget) -> ReferenceDocumentInput | ErrorEnvelope:
        try:
            if target.read_only_external:
                path = Path(target.key).resolve(strict=True)
                semantic = self._classify_semantic_location(path.as_uri())
                routed = tuple(adapter for adapter in self._adapters.values() if adapter.routes(path))
                if semantic.kind is not LocationKind.READ_ONLY_EXTERNAL or len(routed) != 1:
                    raise ValueError("external reference is not safely routable")
                return ReferenceDocumentInput(
                    path.as_uri(),
                    FileSnapshot.from_bytes(path.read_bytes()),
                    None,
                    routed[0].snapshot().position_encoding,
                )
            relative_path = str(Path(target.key).relative_to(self.identity.root))
            document, _target, _family, _adapter = self._load_document(relative_path)
            return ReferenceDocumentInput(
                document.uri,
                document.snapshot,
                document.raw_symbols,
                document.position_encoding,
            )
        except (OSError, TypeError, ValueError, WorkspaceRuntimeError):
            return error(ErrorCode.INVALID_PATH, details={"path": target.display_path})

    def load_diagnostics(self, relative_path: str, *, timeout_seconds: float) -> DiagnosticDocumentInput:
        document, target, family, adapter = self._load_document(relative_path, for_diagnostics=True)
        initial = adapter.snapshot()
        if initial.phase.value in {"cold", "starting", "cooldown", "degraded"}:
            return DiagnosticDocumentInput(
                document=document,
                requested_generation=target.document_generation,
                engine=_diagnostic_engine(initial, family, self.identity, self._external_diagnostic_root()),
                readiness=DiagnosticsReadiness.NOT_READY,
                phase=initial.phase.value,
                retry=RetryMetadata(True, retry_after_seconds=0.1),
            )
        deadline = time.monotonic() + timeout_seconds
        publication = adapter.diagnostics_snapshot(target)
        while publication.state in {DiagnosticsState.MISSING, DiagnosticsState.STALE} and time.monotonic() < deadline:
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            publication = adapter.diagnostics_snapshot(target)
        waited = max(0.0, timeout_seconds - max(0.0, deadline - time.monotonic()))
        readiness = (
            DiagnosticsReadiness.READY
            if publication.state is not DiagnosticsState.MISSING
            else DiagnosticsReadiness.TIMED_OUT
        )
        snapshot = adapter.snapshot()
        return DiagnosticDocumentInput(
            document=document,
            requested_generation=target.document_generation,
            engine=_diagnostic_engine(snapshot, family, self.identity, self._external_diagnostic_root()),
            publication=publication,
            readiness=readiness,
            phase=snapshot.phase.value,
            retry=RetryMetadata(True, retry_after_seconds=0.1) if readiness is DiagnosticsReadiness.TIMED_OUT else None,
            waited_seconds=waited,
        )

    def _load_document(
        self,
        relative_path: str,
        *,
        timeout: float | None = None,
        for_diagnostics: bool = False,
    ) -> tuple[DocumentSymbolInput, DocumentReadinessTarget, LanguageFamily, RuntimeAdapter]:
        normalized = _relative_path(relative_path, allow_parent=True)
        family, adapter = self._route(normalized)
        absolute = (self.identity.root / normalized).resolve(strict=True)
        with self._state_lock:
            version = self._versions.get(normalized, 0) + 1
            self._versions[normalized] = version
        uri = absolute.as_uri()
        capture = _DocumentSymbolCapture()
        loader = (
            getattr(adapter, "snapshot_open_and_probe_diagnostics", adapter.snapshot_open_and_probe_document)
            if for_diagnostics
            else adapter.snapshot_open_and_probe_document
        )
        snapshot, target = loader(
            absolute_path=absolute,
            relative_path=normalized,
            uri=uri,
            version=version,
            probe=capture,
        ).result(timeout=self._future_timeout if timeout is None else timeout)
        adapter_status = adapter.snapshot()
        generations = adapter_status.generations
        document = DocumentSymbolInput(
            relative_path=normalized,
            uri=uri,
            snapshot=snapshot,
            raw_symbols=capture.raw_symbols,
            position_encoding=adapter_status.position_encoding,
            workspace=WorkspaceMetadata(
                root=str(self.identity.root),
                kind=self.identity.kind.value,
                working_subdirectory=str(self.identity.root),
            ),
            adapter=AdapterMetadata(
                name="pyright" if family is LanguageFamily.PYTHON else "typescript",
                language=family.value,
            ),
            generations=GenerationMetadata(
                trust=generations.trust,
                program=generations.program,
                document=generations.document,
                index=generations.index,
                scope="path",
            ),
        )
        return document, target, family, adapter

    def _external_diagnostic_root(self) -> Path | None:
        value = getattr(self._path_policy, "allowed_non_git_root", None)
        return value if isinstance(value, Path) else None

    def _adapter_for_workspace_uri(self, uri: str) -> RuntimeAdapter:
        path = _file_uri_path(uri)
        return self._route(str(path.relative_to(self.identity.root)))[1]

    def _classify_semantic_location(self, uri: str) -> SemanticLocation:
        path = _file_uri_path(uri).resolve(strict=True)
        classify = getattr(self._path_policy, "classify_semantic_location", None)
        if callable(classify):
            return cast(SemanticLocation, classify(self.identity, path))
        if path.is_relative_to(self.identity.root):
            authorized = self._path_policy.authorize_path_operand(
                self.identity, path, tuple(self.identity.root / item for item in self.inventory.paths)
            )
            return SemanticLocation(authorized, LocationKind.WORKSPACE)
        raise ValueError("path policy lacks semantic external-root classification")

    def status(self) -> Mapping[str, object]:
        """Return secret-free task-6.2 facts without entering the executor queue."""

        adapters: dict[str, object] = {}
        for family, adapter in self._adapters.items():
            projection = self._projections[family]
            adapters[family.value] = _adapter_status(adapter.snapshot(), projection)
        executor = self._executor.snapshot()
        return {
            "identity": {"root": str(self.identity.root), "kind": self.identity.kind.value},
            "trust_inventory": {
                "kind": self.inventory.kind,
                "count": self.inventory.count,
                "sha256": self.inventory.digest,
                "rejected": tuple({"path": item.path, "reason": item.reason} for item in self.inventory.rejected),
            },
            "adapters": adapters,
            "skipped_language_families": tuple(
                family.value for family in LanguageFamily if family not in self._adapters
            ),
            "executor": {
                "queue_size": executor.queue_size,
                "queue_capacity": executor.queue_capacity,
                "active": executor.active,
                "stopping": executor.stopping,
            },
            "stopped": self._stopped,
        }

    def stop(self) -> None:
        """Stop every adapter before closing the sole workspace executor."""

        with self._state_lock:
            if self._stopped:
                return
            self._stopped = True
        failures: list[BaseException] = []
        futures = tuple(adapter.stop() for adapter in self._adapters.values())
        for future in futures:
            try:
                future.result(timeout=self._future_timeout)
            except BaseException as error:
                failures.append(error)
        try:
            self._executor.close(cancel_queued=True, timeout=min(self._future_timeout, 5.0))
        except BaseException as error:
            failures.append(error)
        if failures:
            raise RuntimeError(f"workspace runtime stop failed: {failures[0]}") from failures[0]

    def _require_running(self) -> None:
        with self._state_lock:
            if self._stopped:
                raise WorkspaceRuntimeError(RuntimeErrorCode.STOPPED, "workspace runtime is stopped")


@dataclass(frozen=True, slots=True)
class _WarmGlobalSeed:
    """Call-local current-generation evidence reused by the global core."""

    query: str
    candidates: tuple[Mapping[str, object], ...]
    document: DocumentSymbolInput
    generations: GenerationMetadata


@dataclass(frozen=True, slots=True)
class _GlobalProvider:
    """One fixed-adapter bridge for the bounded global-symbol core."""

    runtime: WorkspaceRuntime
    family: LanguageFamily
    seed: _WarmGlobalSeed | None = None

    @property
    def _adapter(self) -> RuntimeAdapter:
        return self.runtime._adapters[self.family]

    def global_symbol_state(self) -> GlobalAdapterState:
        snapshot = self._adapter.snapshot()
        projection = self.runtime._projections[self.family]
        return GlobalAdapterState(
            _workspace_metadata(self.runtime.identity),
            _adapter_metadata(self.family),
            _global_generations(snapshot),
            ConfiguredProgramScope(
                projection.configured_program.paths,
                projection.project_kind.value,
                projection.selected_config_path,
            ),
            snapshot.derived_tools.global_find_symbol,
            snapshot.phase.value == "ready",
            snapshot.phase.value,
            0.1 if snapshot.phase.value != "ready" else None,
        )

    def workspace_symbols(self, query: str, *, max_results: int) -> WorkspaceSymbolBatch:
        if self.seed is not None and query == self.seed.query:
            candidates = self.seed.candidates
            return WorkspaceSymbolBatch(
                candidates[:max_results],
                self.seed.generations,
                len(candidates) > max_results,
                max(0, len(candidates) - max_results),
            )
        raw = self._adapter.submit_read(
            lambda client: client.request("workspace/symbol", {"query": query}, timeout=self.runtime._future_timeout)
        ).result(timeout=self.runtime._future_timeout)
        if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
            raw = ()
        candidates = tuple(item for item in raw if isinstance(item, Mapping))
        return WorkspaceSymbolBatch(
            candidates[:max_results],
            _global_generations(self._adapter.snapshot()),
            len(candidates) > max_results,
            max(0, len(candidates) - max_results),
        )

    def document_symbols(self, relative_path: str, uri: str) -> DocumentSymbolBatch:
        if (
            self.seed is not None
            and self.seed.document.relative_path == relative_path
            and self.seed.document.uri == uri
        ):
            return DocumentSymbolBatch(
                relative_path,
                uri,
                self.seed.document.raw_symbols,
                self.seed.generations,
            )
        # The core gives us only workspace/symbol candidates that already passed
        # its configured-program authorization; this re-check is fail-closed.
        document, _target, _family, _adapter = self.runtime._load_document(relative_path)
        if document.uri != uri:
            raise ValueError("workspace-symbol candidate URI changed before document verification")
        return DocumentSymbolBatch(
            relative_path,
            uri,
            document.raw_symbols,
            _global_generations(self._adapter.snapshot()),
        )


class _NoopOperationLock:
    """The adapter edit worker already holds the sole workspace operation lock."""

    def __enter__(self) -> _NoopOperationLock:
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        return False


class _EditBridge:
    """Per-transaction authorizer, document provider, and notifier for the edit core."""

    def __init__(
        self,
        runtime: WorkspaceRuntime,
        adapter: RuntimeAdapter,
        client: AdapterClient,
        authorized: AuthorizedEdit,
    ) -> None:
        self._runtime = runtime
        self._adapter = adapter
        self._client = client
        self._authorized = authorized
        self._target: DocumentReadinessTarget | None = None

    def authorize_edit(self, relative_path: str) -> AuthorizedEdit:
        if relative_path != self._authorized.relative_path:
            raise ValueError("edit target changed after authorization")
        return self._authorized

    def resolve_document_symbols(self, target: AuthorizedEdit, snapshot: FileSnapshot) -> DocumentSymbolInput:
        with self._runtime._state_lock:
            version = self._runtime._versions.get(target.relative_path, 0) + 1
            self._runtime._versions[target.relative_path] = version
        opened = self._adapter.open_edit_document_with_client(
            self._client,
            absolute_path=target.path,
            relative_path=target.relative_path,
            uri=target.path.as_uri(),
            version=version,
            text=snapshot.text,
        )
        raw = self._client.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": opened.uri}},
            timeout=self._runtime._future_timeout,
        )
        if raw is not None and (not isinstance(raw, Sequence) or isinstance(raw, str | bytes)):
            raise TypeError("document-symbol response is not a sequence")
        self._target = opened
        status = self._adapter.snapshot()
        return DocumentSymbolInput(
            target.relative_path,
            opened.uri,
            snapshot,
            cast(Sequence[Mapping[str, Any]] | None, raw),
            status.position_encoding,
            _workspace_metadata(self._runtime.identity),
            _adapter_metadata_for_snapshot(status),
            _path_generations(status),
        )

    def notify_replaced(self, notification: ReplacementNotification) -> NotificationResult:
        if self._target is None:
            raise ValueError("edit notifier ran before document-symbol resolution")
        return self._adapter.notify_edit_with_client(self._client, self._target, notification)


def _workspace_metadata(identity: WorkspaceIdentity) -> WorkspaceMetadata:
    return WorkspaceMetadata(str(identity.root), identity.kind.value, str(identity.root))


def _adapter_metadata(family: LanguageFamily) -> AdapterMetadata:
    return AdapterMetadata("pyright" if family is LanguageFamily.PYTHON else "typescript", family.value)


def _adapter_metadata_for_snapshot(snapshot: AdapterSnapshot) -> AdapterMetadata:
    return _adapter_metadata(LanguageFamily(snapshot.name))


def _path_generations(snapshot: AdapterSnapshot, *, scope: str = "path") -> GenerationMetadata:
    generations = snapshot.generations
    return GenerationMetadata(
        trust=generations.trust,
        program=generations.program,
        document=generations.document,
        index=generations.index,
        scope=scope,
    )


def _global_generations(snapshot: AdapterSnapshot) -> GenerationMetadata:
    return _path_generations(snapshot, scope="configured_program")


def _diagnostic_engine(
    snapshot: AdapterSnapshot,
    family: LanguageFamily,
    identity: WorkspaceIdentity,
    external_root: Path | None,
) -> DiagnosticEngineFacts:
    engine = snapshot.engine
    if family is LanguageFamily.PYTHON:
        assert engine.interpreter is not None
        root = identity.root if identity.kind is WorkspaceKind.ALLOWLISTED_NON_GIT else external_root
        external = ExternalRootMetadata("read_only_external", str(root)) if root is not None else None
        return DiagnosticEngineFacts(
            engine.name,
            family.value,
            engine.version,
            interpreter=str(engine.interpreter),
            external_root=external,
        )
    return DiagnosticEngineFacts(
        engine.name,
        family.value,
        engine.version,
        semantic_engine_name="typescript",
        semantic_engine_version=TYPESCRIPT_VERSION,
        native_typecheck_command=_native_typecheck_command(identity.root),
    )


def _native_typecheck_command(root: Path) -> str | None:
    """Report the conventional command only when package.json declares it."""

    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(package, Mapping):
        return None
    scripts = package.get("scripts")
    if not isinstance(scripts, Mapping):
        return None
    typecheck = scripts.get("typecheck")
    return "npm run typecheck" if isinstance(typecheck, str) and typecheck.strip() else None


def _raw_location_mappings(value: object) -> tuple[Mapping[str, object], ...] | None:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return (cast(Mapping[str, object], value),)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return None
    if not all(isinstance(item, Mapping) for item in value):
        return None
    return tuple(cast(Mapping[str, object], item) for item in value)


def _unique_routing_adapter(adapters: Sequence[RuntimeAdapter], path: Path) -> RuntimeAdapter:
    routed = tuple(adapter for adapter in adapters if adapter.routes(path))
    if len(routed) != 1:
        raise ValueError("semantic target does not route to one adapter")
    return routed[0]


def _location_from_raw(raw: Mapping[str, object]) -> Location:
    uri = raw.get("uri") or raw.get("targetUri")
    raw_range = raw.get("range") or raw.get("targetSelectionRange") or raw.get("targetRange")
    if not isinstance(uri, str) or not isinstance(raw_range, Mapping):
        raise ValueError("LSP location is incomplete")
    return Location(uri, _range_from_raw(cast(Mapping[str, object], raw_range)), str(_file_uri_path(uri)))


def _range_from_raw(raw: Mapping[str, object]) -> Range:
    start = raw.get("start")
    end = raw.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        raise ValueError("LSP range is incomplete")
    return Range(
        _position_from_raw(cast(Mapping[str, object], start)),
        _position_from_raw(cast(Mapping[str, object], end)),
    )


def _position_from_raw(raw: Mapping[str, object]) -> Position:
    line, character = raw.get("line"), raw.get("character")
    if (
        isinstance(line, bool)
        or isinstance(character, bool)
        or not isinstance(line, int)
        or not isinstance(character, int)
    ):
        raise ValueError("LSP position is invalid")
    return Position(line, character)


def _file_uri_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("semantic URI must be a local file URI")
    return Path(unquote(parsed.path))


def _final_name_segment(name_path: str | Sequence[str]) -> str | None:
    if isinstance(name_path, str):
        parts = tuple(name_path.lstrip("/").rstrip("/").split("/"))
    elif isinstance(name_path, Sequence) and not isinstance(name_path, str | bytes):
        parts = tuple(name_path)
    else:
        return None
    return parts[-1] if parts and all(isinstance(part, str) and part for part in parts) else None


def _first_document_symbol_name(symbols: Sequence[Any] | None) -> str | None:
    """Select one exact top-level name from a controlled document witness."""

    for symbol in symbols or ():
        if isinstance(symbol, Mapping) and isinstance(name := symbol.get("name"), str) and name:
            return name
    return None


def _selected_symbol(document: DocumentNavigation, name_path: str) -> NormalizedSymbol | ErrorEnvelope:
    absolute = name_path.startswith("/")
    parts = tuple(name_path.lstrip("/").rstrip("/").split("/"))
    if not parts or any(not part for part in parts):
        return error(ErrorCode.INVALID_INPUT, details={"field": "name_path"})
    matches = [
        symbol
        for root in document.symbols
        for symbol in root.iter_depth_first()
        if len(symbol.name_path) >= len(parts)
        and (not absolute or len(symbol.name_path) == len(parts))
        and symbol.name_path[-len(parts) :] == parts
    ]
    if not matches:
        return error(
            ErrorCode.SYMBOL_NOT_FOUND,
            details={"relative_path": document.relative_path, "name_path": name_path},
            workspace=document.workspace,
            adapter=document.adapter,
            generations=document.generations,
        )
    if len(matches) != 1:
        return error(
            ErrorCode.AMBIGUOUS_SYMBOL,
            details={"relative_path": document.relative_path, "name_path": name_path},
            workspace=document.workspace,
            adapter=document.adapter,
            generations=document.generations,
        )
    return matches[0]


def _physical_identity(identity: WorkspaceIdentity | PhysicalWorkspaceKey) -> WorkspaceIdentity:
    if isinstance(identity, WorkspaceIdentity):
        kind, supplied_root = identity.registry_key
        root = supplied_root.resolve(strict=True)
    else:
        kind, supplied_root = identity
        root = supplied_root.resolve(strict=True)
    return WorkspaceIdentity(root=root, kind=kind, working_subdirectory=root)


def _default_inventory(identity: WorkspaceIdentity) -> TrustInventory:
    if identity.kind is WorkspaceKind.GIT:
        return git_trust_inventory(identity.root)
    if identity.kind is WorkspaceKind.ALLOWLISTED_NON_GIT:
        return transformers_trust_inventory(identity.root)
    raise ValueError(f"unsupported workspace kind: {identity.kind!r}")


def _paths_for(paths: tuple[str, ...], extensions: frozenset[str]) -> tuple[str, ...]:
    return tuple(path for path in paths if PurePosixPath(path).suffix.lower() in extensions)


def _default_attributor(family: LanguageFamily) -> ProgramAttributor:
    if family is LanguageFamily.PYTHON:
        facts = PyrightFacts.locked()
        return lambda root, paths: facts.attribute_program(root, paths)
    config = TypeScriptAdapterConfig.locked()
    return lambda root, paths: attribute_native_program(
        config,
        root,
        trust_inventory_paths=paths,
        entry_path=select_default_entry(root, paths),
    ).require_compatible()


def _default_adapter_factory(family: LanguageFamily) -> AdapterFactory:
    def build(context: AdapterBuildContext) -> _WorkspaceLanguageAdapter:
        language = PyrightFacts.locked() if family is LanguageFamily.PYTHON else TypeScriptAdapterConfig.locked()
        return _WorkspaceLanguageAdapter(
            workspace_root=context.workspace_root,
            facts=language.adapter_language_facts(context.workspace_root),
            runtime_provider=language.runtime_provider(context.workspace_root),
            executor=context.executor,
            scope_tracker=context.scope_tracker,
            lsp_state=LspState(),
            document_witness=PublishedDiagnosticsWitness(),
            operation_lock=context.operation_lock,
        )

    return build


def _default_executor(root: Path) -> BoundedLspExecutor:
    return BoundedLspExecutor(queue_capacity=32, name=f"workspace:{root.name}")


def _relative_path(path: str, *, allow_parent: bool = False) -> str:
    candidate = PurePosixPath(path)
    normalized = str(candidate)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or (".." in candidate.parts and not allow_parent)
        or normalized != path
    ):
        raise ValueError(f"path is not a normalized relative path: {path!r}")
    return normalized


def _adapter_status(snapshot: AdapterSnapshot, projection: ScopeProjection) -> Mapping[str, object]:
    crash = snapshot.crash
    generations = snapshot.generations
    return {
        "phase": snapshot.phase.value,
        "running": snapshot.running,
        "raw_providers": {
            "definition": snapshot.raw_providers.definition,
            "declaration": snapshot.raw_providers.declaration,
            "implementation": snapshot.raw_providers.implementation,
            "references": snapshot.raw_providers.references,
            "document_symbols": snapshot.raw_providers.document_symbols,
            "workspace_symbols": snapshot.raw_providers.workspace_symbols,
        },
        "derived_tools": {
            "find_declaration": snapshot.derived_tools.find_declaration,
            "find_implementations": snapshot.derived_tools.find_implementations,
            "find_referencing_symbols": snapshot.derived_tools.find_referencing_symbols,
            "get_symbols_overview": snapshot.derived_tools.get_symbols_overview,
            "global_find_symbol": snapshot.derived_tools.global_find_symbol,
        },
        "engine": {
            "name": snapshot.engine.name,
            "version": snapshot.engine.version,
            "executable": str(snapshot.engine.executable),
            "interpreter": str(snapshot.engine.interpreter) if snapshot.engine.interpreter is not None else None,
        },
        "position_encoding": snapshot.position_encoding.value,
        "selected_native_config": projection.selected_config_path,
        "project_kind": projection.project_kind.value,
        "trust_inventory": {
            "count": projection.trust_inventory.count,
            "sha256": projection.trust_inventory.sha256,
        },
        "configured_program": {
            "count": projection.configured_program.count,
            "sha256": projection.configured_program.sha256,
        },
        "trusted_not_in_configured_program": tuple(
            {"path": item.path, "reason": item.reason.value}
            for item in projection.trusted_not_in_configured_program
        ),
        "configured_program_outside_trust": tuple(
            {"path": item.path, "reason": item.reason.value}
            for item in projection.configured_program_outside_trust
        ),
        "scope_compatible": projection.compatible,
        "overlay_generated": projection.overlay_generated,
        "generations": {
            "trust": generations.trust,
            "program": generations.program,
            "document": generations.document,
            "index": generations.index,
        },
        "crash": {
            "total": crash.total,
            "window_count": crash.window_count,
            "last_timestamp": crash.last_timestamp,
            "last_error": crash.last_error,
        },
        "cooldown": {
            "until": crash.cooldown_until,
            "remaining_seconds": crash.cooldown_remaining_seconds,
        },
        "transitions": tuple(
            {"phase": item.phase.value, "timestamp": item.timestamp, "reason": item.reason}
            for item in snapshot.transitions
        ),
    }
