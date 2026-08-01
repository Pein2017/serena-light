"""Composition root for one physical workspace's fixed language adapters.

The daemon registry owns reuse and leases.  This module owns the expensive state
behind one normalized ``(kind, root)`` key: trust, native projections, one LSP
executor and operation lock, and the independently lazy Python and TypeScript
adapters.  Session working-directory metadata deliberately remains outside this
shared object.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
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
    AdapterLanguageFacts,
    AdapterPhase,
    AdapterRuntimeProvider,
    AdapterSnapshot,
    CrashPolicy,
    DerivedToolAvailability,
    DocumentReadinessProbe,
    DocumentReadinessTarget,
    DocumentReadinessWitness,
    GlobalReadinessWitness,
    LanguageAdapter,
    PublishedDiagnosticsWitness,
    RawLspProviders,
)
from serena_light.lsp.client import LspProtocolError, LspResponseError, LspTransportClosed
from serena_light.lsp.executor import BoundedLspExecutor, EditCommit, EditCommitState, ExecutorBusyError
from serena_light.lsp.normalize import Location, NormalizedSymbol, Position, Range, containing_symbol
from serena_light.lsp.positions import FileSnapshot, LspPosition, PositionEncoding, PositionError
from serena_light.lsp.pyright import PyrightFacts
from serena_light.lsp.state import DiagnosticsSnapshot, DiagnosticsState, LspState
from serena_light.lsp.typescript import (
    NATIVE_CONFIG_NAMES,
    TYPESCRIPT_VERSION,
    TypeScriptAdapterConfig,
    attribute_native_program,
    select_default_entry,
)
from serena_light.lsp.typescript_assignment_recovery import (
    assignment_recovery_positions,
    recover_typescript_top_level_variable_symbols,
)
from serena_light.tools.declarations import (
    CapabilityMatrix,
    ClassifiedLocationInput,
    DeclarationNavigationService,
    SemanticDocumentInput,
)
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
    safe_current_hash,
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
    SuccessEnvelope,
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
)
from serena_light.tools.references import (
    RawReferenceDocumentInput,
    ReferenceCoverage,
    ReferenceDocumentInput,
    ReferenceNavigationService,
    ReferenceQueryResult,
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
    TargetedPathState,
    TrustInventory,
    git_trust_inventory,
    observe_file_digest,
    transformers_trust_inventory,
)
from serena_light.workspace.scope import (
    FileChangeType,
    LanguageFamily,
    ScopeGenerationTracker,
    ScopeProjection,
    WatchedFileEvent,
    bounded_difference_status,
)

type PhysicalWorkspaceKey = tuple[WorkspaceKind, Path]
type ProgramAttributor = Callable[[Path, tuple[str, ...]], ScopeProjection]
type ContentIdentity = tuple[int | None, int | None, int | None, int | None, str | None]
type AdapterResponseIdentity = tuple[
    int,
    AdapterPhase,
    RawLspProviders,
    DerivedToolAvailability,
    int,
    int,
    int,
    int,
    PositionEncoding,
]
type AdapterReplayIdentity = tuple[
    int,
    AdapterPhase,
    RawLspProviders,
    DerivedToolAvailability,
    int,
    int,
    int,
    PositionEncoding,
]

_FAMILY_EXTENSIONS: Mapping[LanguageFamily, frozenset[str]] = {
    LanguageFamily.PYTHON: PYTHON_EXTENSIONS,
    LanguageFamily.TYPESCRIPT: JAVASCRIPT_TYPESCRIPT_EXTENSIONS,
}

# Pyright and tsserver own native config selection; these root-relative names
# only decide when a family must be asked to attribute its program again.  A
# false positive costs one reattribution, a miss would keep a stale program.
_NATIVE_CONFIG_WATCH: Mapping[LanguageFamily, tuple[str, ...]] = {
    LanguageFamily.PYTHON: ("pyrightconfig.json", "pyproject.toml"),
    LanguageFamily.TYPESCRIPT: tuple(sorted(NATIVE_CONFIG_NAMES)),
}

_LANGUAGE_IDS: Mapping[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascriptreact",
}

# One freshness pass may not open an unbounded number of newly created files.
MAX_CONTROLLED_OPENS = 32
MAX_RESPONSE_OWNED_TARGETS = 64
REFERENCE_COVERAGE_SAMPLE_LIMIT = 16
# One raced read transaction is replayed exactly once before it fails retryably;
# continuous external churn must never become an unbounded outer loop.
MAX_FRESH_READ_ATTEMPTS = 2
MAX_FRESH_READ_EVIDENCE_PATHS = 16
# Failure reasons whose evidence is the content of response-owned bytes rather
# than the adapter's own condition.  A read that returns one of these has made a
# statement about the source and must be validated exactly like a success.
SOURCE_DERIVED_ERROR_REASONS = frozenset(
    {
        "incomplete_assignment_range",
        "candidate_snapshot_range_mismatch",
        "verified_target_snapshot_unavailable",
        "verified target snapshot unavailable",
        "semantic target locations changed",
        "semantic target set exceeds snapshot bound",
        "external target bytes unavailable",
        "workspace_reference_snapshot_unavailable",
        "workspace_reference_snapshot_range_mismatch",
        "workspace_reference_document_invalid",
        "workspace_reference_uri_mismatch",
    }
)
SOURCE_SNAPSHOT_UNAVAILABLE_REASON = "source_snapshot_unavailable"


class RuntimeErrorCode(StrEnum):
    BUSY = "BUSY"
    SCOPE_INCOMPATIBLE = "SCOPE_INCOMPATIBLE"
    NOT_READY = "NOT_READY"
    TIMED_OUT = "TIMED_OUT"
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

    def reconcile_watched_files(
        self,
        *,
        events: Sequence[WatchedFileEvent],
        created: Sequence[str],
        versions: Mapping[str, int],
    ) -> Future[None]: ...

    def warm_global(
        self, witness: GlobalReadinessWitness, *, timeout: float | None = None
    ) -> Future[tuple[Mapping[str, object], ...]]: ...

    def diagnostics_snapshot(self, target: DocumentReadinessTarget) -> DiagnosticsSnapshot: ...

    def open_snapshot_document_with_client(
        self,
        client: AdapterClient,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        text: str,
    ) -> DocumentReadinessTarget: ...

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
    debug_reporter: Callable[[str, str], object] | None = None


@dataclass(frozen=True, slots=True)
class FamilyAttribution:
    projection: ScopeProjection | None = None
    error: WorkspaceRuntimeError | None = None


type AdapterFactory = Callable[[AdapterBuildContext], RuntimeAdapter]
type InventoryFactory = Callable[[WorkspaceIdentity], TrustInventory]
type ExecutorFactory = Callable[[Path], BoundedLspExecutor]


class _DocumentSymbolCapture:
    """Capture one bounded document-symbol response through the adapter probe seam."""

    def __init__(self, *, capture_selection_ranges: bool = False) -> None:
        self.raw_symbols: tuple[Mapping[str, Any], ...] | None = None
        self.selection_ranges: tuple[Mapping[str, Any], ...] | None = None
        self._capture_selection_ranges = capture_selection_ranges

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
        positions = assignment_recovery_positions(self.raw_symbols) if self._capture_selection_ranges else ()
        if positions:
            try:
                raw_ranges = client.request(
                    "textDocument/selectionRange",
                    {
                        "textDocument": {"uri": target.uri},
                        "positions": list(positions),
                    },
                    timeout=timeout,
                )
            except LspResponseError:
                raw_ranges = None
            if (
                isinstance(raw_ranges, Sequence)
                and not isinstance(raw_ranges, str | bytes)
                and all(isinstance(item, Mapping) for item in raw_ranges)
            ):
                self.selection_ranges = tuple(cast(Mapping[str, Any], item) for item in raw_ranges)
        return True


@dataclass(frozen=True, slots=True)
class _ClassifiedSemanticLocation:
    """One canonicalized location from a single semantic response."""

    raw: Mapping[str, object]
    location: Location
    semantic: SemanticLocation
    adapter: RuntimeAdapter


@dataclass(frozen=True, slots=True)
class _BoundSemanticTarget:
    """Exact client-open snapshot retained for the authoritative response."""

    path: Path
    adapter: RuntimeAdapter
    snapshot: FileSnapshot
    position_encoding: PositionEncoding
    reference_document: ReferenceDocumentInput | None = None
    symbol_document: DocumentSymbolInput | None = None


@dataclass(frozen=True, slots=True)
class _ResponseAdapterState:
    adapter: RuntimeAdapter
    identity: AdapterResponseIdentity


@dataclass(frozen=True, slots=True)
class _ResponseOwnedSemanticLocations:
    """Second semantic response plus the exact snapshots it observed.

    ``preparation_targets`` are trusted workspace documents opened only to make
    a cold TypeScript query deterministic.  They never enter the public target
    set, but their bytes influence the response and therefore remain freshness
    witnesses.  ``external_digests`` are the guarded byte identities of the read-only
    external targets, observed inside the adapter transaction immediately
    before the authoritative second request.  They are retained so a write that
    lands while that request is in flight cannot be concealed by a later
    agreeing observation on the calling thread.
    """

    locations: tuple[_ClassifiedSemanticLocation, ...]
    targets: tuple[_BoundSemanticTarget, ...]
    preparation_targets: tuple[_BoundSemanticTarget, ...]
    adapter_states: tuple[_ResponseAdapterState, ...]
    external_digests: tuple[tuple[Path, str], ...]


class _WorkspaceLanguageAdapter(LanguageAdapter):
    """Add the workspace-owned atomic snapshot/open/probe seam."""

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
        debug_reporter: Callable[[str, str], object] | None = None,
    ) -> None:
        super().__init__(
            workspace_root=workspace_root,
            facts=facts,
            runtime_provider=runtime_provider,
            executor=executor,
            scope_tracker=scope_tracker,
            lsp_state=lsp_state,
            document_witness=document_witness,
            operation_lock=operation_lock,
            crash_policy=crash_policy,
            readiness_timeout=readiness_timeout,
            clock=clock,
            timestamp=timestamp,
            notification_handler=notification_handler,
            debug_reporter=debug_reporter,
        )
        self._diagnostic_snapshots: dict[str, tuple[int, str]] = {}

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
                client = self._ensure_started_worker()
                snapshot = FileSnapshot.from_bytes(absolute_path.read_bytes())
                target = self._open_document_with_client(
                    client,
                    relative_path=relative_path,
                    uri=uri,
                    version=version,
                    text=snapshot.text,
                )
                observed = self._probe_document_with_client(client, target, probe)
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
        """Bind diagnostics to one unchanged snapshot without synthetic changes."""

        def worker() -> tuple[FileSnapshot, DocumentReadinessTarget]:
            with self._operation_lock:
                client = self._ensure_started_worker()
                owner_token = self._runtime_token
                snapshot = FileSnapshot.from_bytes(absolute_path.read_bytes())
                # A same-process close marker must be causally drained before
                # this path retains a diagnostics publication owner.  If the
                # barrier fails, its marker remains for retry and this worker
                # propagates the failure instead of returning a false CLEAN.
                self._drain_recorded_unversioned_closes(client)
                current = self._lsp_state.document(uri)
                cached = self._diagnostic_snapshots.get(uri)
                if (
                    current is not None
                    and current.path == absolute_path
                    and isinstance(current.version, int)
                    and uri in self._open_documents
                    and cached == (owner_token, snapshot.text)
                ):
                    # A prior document-symbol load already opened these exact
                    # bytes.  Do not manufacture a version/generation merely
                    # to start diagnostics; retain the existing target so a
                    # delayed publication remains correlated.
                    self._open_documents.move_to_end(uri)
                    target = DocumentReadinessTarget(
                        uri,
                        relative_path,
                        absolute_path,
                        current.version,
                        current.generation,
                        self._scope.generations.path_scoped.get(relative_path, 0),
                    )
                    self._retain_diagnostics_target(target)
                    # Diagnostics-for-symbol still needs the current document
                    # symbols even though the bytes and generation are reused.
                    # Re-probe without sending a synthetic didChange; the
                    # workspace override keeps the diagnostics target eligible
                    # for a later asynchronous publication.
                    observed = self._probe_document_with_client(client, target, probe)
                    return snapshot, observed
                target = self._open_document_with_client(
                    client,
                    relative_path=relative_path,
                    uri=uri,
                    version=version,
                    text=snapshot.text,
                    retain_diagnostics_target=True,
                )
                observed = self._probe_document_with_client(client, target, probe)
                return snapshot, observed

        return self._executor.submit(worker)

    def _on_document_forgotten(self, uri: str) -> None:
        self._diagnostic_snapshots.pop(uri, None)

    def _on_all_documents_forgotten(self) -> None:
        self._diagnostic_snapshots.clear()

    def _on_document_text(self, uri: str, text: str) -> None:
        self._diagnostic_snapshots[uri] = (self._runtime_token, text)

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

    def open_snapshot_document_with_client(
        self,
        client: AdapterClient,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        text: str,
    ) -> DocumentReadinessTarget:
        """Open an exact snapshot from an already-owned adapter worker."""

        expected = (self.workspace_root / relative_path).resolve()
        if absolute_path.resolve() != expected:
            raise ValueError("snapshot document path does not match its workspace-relative path")
        return self._open_document_with_client(
            client,
            relative_path=relative_path,
            uri=uri,
            version=version,
            text=text,
        )

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

        return self.open_snapshot_document_with_client(
            client,
            absolute_path=absolute_path,
            relative_path=relative_path,
            uri=uri,
            version=version,
            text=text,
        )

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
        changed = self._open_document_with_client(
            client,
            relative_path=target.relative_path,
            uri=target.uri,
            version=version,
            text=notification.text,
        )
        return NotificationResult("notified", changed.document_generation, _path_generations(self.snapshot()))


@dataclass(frozen=True, slots=True)
class FreshnessScan:
    """What exactly one completed freshness pass observed and did."""

    created: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    symlinked: tuple[str, ...] = ()
    config_changed: tuple[str, ...] = ()
    reattributed: tuple[LanguageFamily, ...] = ()
    notified: tuple[LanguageFamily, ...] = ()
    opened: tuple[str, ...] = ()
    unopened: tuple[str, ...] = ()

    @property
    def dirty(self) -> bool:
        return bool(self.created or self.changed or self.deleted or self.config_changed)

    def as_status(self) -> Mapping[str, object]:
        return {
            "created": self.created,
            "changed": self.changed,
            "deleted": self.deleted,
            "symlinked": self.symlinked,
            "config_changed": self.config_changed,
            "reattributed": tuple(family.value for family in self.reattributed),
            "notified": tuple(family.value for family in self.notified),
            "opened": self.opened,
            "unopened": self.unopened,
        }


@dataclass(frozen=True, slots=True)
class _PendingAdapterRestart:
    """A replacement that cannot be published until one exact old stop resolves."""

    family: LanguageFamily
    projection: ScopeProjection
    tracker: ScopeGenerationTracker | None
    trusted_paths: tuple[str, ...]
    stop_adapter: RuntimeAdapter | None
    stop_future: Future[AdapterSnapshot] | None


@dataclass(frozen=True, slots=True)
class _PendingAdapterRetirement:
    """An unavailable family retains exact cleanup ownership until stop settles."""

    family: LanguageFamily
    stop_adapter: RuntimeAdapter | None
    stop_future: Future[AdapterSnapshot] | None


def _stop_future_needs_retry(future: Future[AdapterSnapshot]) -> bool:
    """Return whether one terminal cleanup attempt failed without consuming ownership."""

    return future.done() and (future.cancelled() or future.exception() is not None)


@dataclass(slots=True)
class _PendingWatchedReconcile:
    """One watcher batch that must settle before unchanged facts can authorize work."""

    family: LanguageFamily
    adapter: RuntimeAdapter
    events: tuple[WatchedFileEvent, ...]
    created: tuple[str, ...]
    future: Future[None] | Future[Any] | None


@dataclass(frozen=True, slots=True)
class _FreshnessAdmission:
    """One completed guarded scan and the evidence it left, read under its ticket.

    ``version`` advances with every committed freshness change, so a change that
    a different call's scan consumed between this call's preflight and its
    postflight is still visible here.  ``digests`` are the guarded full-byte
    identities the coordinator holds for the caller-named paths.
    """

    scan: FreshnessScan
    version: int
    digests: Mapping[str, str | None]


@dataclass(frozen=True, slots=True)
class _ReadAttemptWitnesses:
    """Byte identities one in-flight read attempt's response owns.

    ``source`` holds workspace-relative paths, which the guarded scan observes
    and commits.  ``external`` holds absolute paths on a trusted read-only
    external root, which no workspace scan covers and which are therefore
    revalidated by observing exactly those files again.  These witnesses let
    range/body/target failures prove source ownership.  Workspace-wide missing
    or ambiguity answers and measured answer-size failures are source-derived by
    construction even when they do not retain one exact document witness.
    """

    source: dict[str, set[str]]
    external: dict[Path, set[str]]

    @property
    def owns_source(self) -> bool:
        return bool(self.source or self.external)


class FreshnessCoordinator:
    """Run one synchronous lexical freshness pass before a workspace operation.

    There is deliberately no time-based success cache and no cross-call
    coalescing: every call is assigned a monotonic arrival ticket and is
    admitted, in FIFO order, to run its own guarded scan that begins only after
    that ticket was issued.  A call that arrives while another scan is already
    running waits for that scan to settle and then runs a distinct scan of its
    own; it can never accept a scan that was already in progress at its
    arrival.  At most one scan runs at a time.  Every scan runs on the calling
    thread, never on the shared LSP executor, because notifications and edits
    are submitted to that single worker and a scan waiting on it would
    deadlock.

    A scan is either whole-root or targeted at exactly the paths a caller named.
    Both kinds observe and commit the same state, so both take the same
    admission queue; a targeted scan claims freshness for its named paths only
    and never answers a membership question.
    """

    def __init__(self, runtime: WorkspaceRuntime) -> None:
        self._runtime = runtime
        self._lock = threading.Lock()
        self._admission_condition = threading.Condition()
        self._next_ticket = 0
        self._serving_ticket = 0
        self._states: dict[str, ContentIdentity] = {}
        self._config_states: dict[str, ContentIdentity] = {}
        self._pending_reconciles: dict[LanguageFamily, _PendingWatchedReconcile] = {}
        self._last = FreshnessScan()
        # Advances with every committed content, config, or membership change.
        # A read compares this across its own two scans, so it observes a change
        # even when another call's scan consumed that change first.
        self._version = 0
        self._capture_baseline(runtime.inventory)

    @property
    def last_scan(self) -> FreshnessScan:
        with self._lock:
            return self._last

    def ensure_fresh(self) -> FreshnessScan:
        """Rebuild and reconcile the Git lexical inventory before one operation."""

        return self.ensure_fresh_admitted().scan

    def ensure_fresh_admitted(self, *, witnessed: Sequence[str] = ()) -> _FreshnessAdmission:
        """Run one guarded scan and report the evidence it committed.

        A ticket is assigned at arrival and admitted in FIFO order; the guarded
        scan this call runs always begins after its own ticket was issued, so a
        call can never accept a scan that another caller already started.  The
        version and the caller-named digests are read before the ticket is
        released, so no other call's scan can commit between this scan and the
        evidence attributed to it.
        """

        if self._runtime.identity.kind is not WorkspaceKind.GIT:
            # The allowlisted read-only root is never fully walked for an
            # activation or an edit preflight; a read states its own scope
            # through ensure_paths_fresh_admitted or ensure_root_fresh_admitted.
            return self._admission(FreshnessScan(), witnessed)
        return self.ensure_root_fresh_admitted(witnessed=witnessed)

    def ensure_root_fresh_admitted(self, *, witnessed: Sequence[str] = ()) -> _FreshnessAdmission:
        """Run one whole-root guarded scan for a read that names no single file.

        A global query, and a directory scope, both depend on root membership:
        a file created, deleted, or replaced by a symlink since the last scan is
        invisible to any per-path observation, so targeted freshness can never
        authorize them.  On the one allowlisted non-Git root this is the same
        bounded no-symlink inventory the workspace was built from, reconciled by
        the same owner as Git rather than by a second mechanism.
        """

        ticket = self._enter_admission_queue()
        try:
            scan = self._scan_root()
            # Commit the result before handing admission to the next ticket, so
            # a later ticket's own commit can never be overwritten by this one
            # resuming after it.
            with self._lock:
                self._last = scan
            return self._admission(scan, witnessed)
        finally:
            self._leave_admission_queue(ticket)

    def _admission(self, scan: FreshnessScan, witnessed: Sequence[str]) -> _FreshnessAdmission:
        with self._lock:
            return _FreshnessAdmission(
                scan,
                self._version,
                {path: _committed_digest(self._states.get(path)) for path in witnessed},
            )

    def _enter_admission_queue(self) -> int:
        with self._admission_condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            # Notify so a waiter blocked on ticket-issuance (rather than on
            # being served) can observe the new ticket count deterministically.
            self._admission_condition.notify_all()
            while self._serving_ticket != ticket:
                self._admission_condition.wait()
            return ticket

    def _leave_admission_queue(self, ticket: int) -> None:
        with self._admission_condition:
            self._serving_ticket = ticket + 1
            self._admission_condition.notify_all()

    def ensure_path_fresh(self, relative_path: str) -> FreshnessScan:
        """Stat exactly one operand on the read-only non-Git root."""

        return self.ensure_paths_fresh_admitted((relative_path,)).scan

    def ensure_paths_fresh_admitted(
        self, paths: Sequence[str], *, witnessed: Sequence[str] = ()
    ) -> _FreshnessAdmission:
        """Validate exactly the caller-named operands on the read-only non-Git root.

        Only the named paths are observed, so a read that repeatedly selects one
        file never walks the package.  The named set is therefore the whole
        freshness claim: membership questions belong to
        ``ensure_root_fresh_admitted``.
        """

        if self._runtime.identity.kind is WorkspaceKind.GIT:
            return self._admission(FreshnessScan(), witnessed)
        # Targeted and whole-root scans share one admission queue: they observe,
        # commit, and deliver against the same state, so a targeted call must not
        # interleave with a root scan on this workspace's inventory, committed
        # identities, or pending reconciles.
        ticket = self._enter_admission_queue()
        try:
            scan = self._refresh_paths(paths)
            with self._lock:
                self._last = scan
            return self._admission(scan, witnessed)
        finally:
            self._leave_admission_queue(ticket)

    def _refresh_paths(self, paths: Sequence[str]) -> FreshnessScan:
        """Observe the named paths once and deliver exactly the changes found."""

        self._settle_pending_reconciles()
        inventory = self._runtime.inventory
        selected = sorted({path for path in paths if inventory.contains(path)})
        if not selected:
            return FreshnessScan()
        states = inventory.targeted_states(selected)
        unsafe = tuple(sorted(state.path for state in states if not state.trusted))
        if unsafe:
            raise WorkspaceRuntimeError(
                RuntimeErrorCode.NOT_READY,
                "workspace paths could not be observed safely for freshness",
                paths=unsafe,
            )
        with self._lock:
            changed = tuple(state.path for state in states if self._states.get(state.path) != state.content_identity)
            for state in states:
                self._states[state.path] = state.content_identity
            if changed:
                self._version += 1
        if not changed:
            return FreshnessScan()
        notified, _opened, _unopened = self._apply_events(
            tuple(WatchedFileEvent(path, FileChangeType.CHANGED) for path in changed)
        )
        return FreshnessScan(changed=changed, notified=notified)

    def _capture_baseline(self, inventory: TrustInventory) -> None:
        """Record the stat facts a later scan compares against."""

        self._states = {state.path: state.content_identity for state in inventory.targeted_states(inventory.paths)}
        self._config_states = self._observe_configs(inventory)

    def _observe_configs(self, inventory: TrustInventory) -> dict[str, ContentIdentity]:
        return {state.path: state.content_identity for state in self._config_states_for(inventory)}

    def _config_states_for(self, inventory: TrustInventory) -> tuple[TargetedPathState, ...]:
        candidates = _native_config_candidates(inventory, self._runtime.projections)
        return inventory.targeted_states(sorted(candidates))

    def _scan_root(self) -> FreshnessScan:
        """Rebuild the whole owning inventory and reconcile everything it moved.

        The owning source is the workspace's own inventory factory: Git for a
        repository, the bounded no-symlink package index for the allowlisted
        non-Git root.  Membership, content, symlink substitution, and native
        config are therefore reconciled identically for both.
        """

        runtime = self._runtime
        # A failed config restart remains decision-bearing even after its
        # filesystem facts have been committed.  Resolve that exact pending stop
        # before an unchanged scan can authorize another semantic operation.
        runtime.retry_pending_restarts()
        self._settle_pending_reconciles()
        previous = runtime.inventory
        rebuilt = runtime.rebuild_inventory()
        before, after = set(previous.paths), set(rebuilt.paths)
        created = tuple(sorted(after - before))
        deleted = tuple(sorted(before - after))
        observed_states = rebuilt.targeted_states(rebuilt.paths)
        unsafe_sources = tuple(sorted(state.path for state in observed_states if not state.trusted))
        config_states = self._config_states_for(rebuilt)
        unsafe_configs = tuple(
            sorted(state.path for state in config_states if state.reason is not None and state.reason != "missing")
        )
        unsafe = tuple(sorted({*unsafe_sources, *unsafe_configs}))
        if unsafe:
            raise WorkspaceRuntimeError(
                RuntimeErrorCode.NOT_READY,
                "workspace paths changed or became unsafe during freshness observation",
                paths=unsafe,
            )
        states = {state.path: state for state in observed_states}
        changed = tuple(
            path for path in sorted(after & before) if self._states.get(path) != states[path].content_identity
        )
        symlinked = tuple(sorted(item.path for item in rebuilt.rejected if item.reason.startswith("symlink")))
        configs = {state.path: state.content_identity for state in config_states}
        config_changed = tuple(
            sorted(name for name, value in configs.items() if self._config_states.get(name) != value)
        )
        membership_changed = bool(created or deleted) or set(previous.rejected) != set(rebuilt.rejected)
        if not (membership_changed or changed or config_changed):
            return FreshnessScan()

        # Attribute affected families before installation; typed per-family
        # failures are installed as unavailable state without blocking healthy
        # families or reverting the rebuilt lexical inventory.
        affected = _affected_families((*created, *deleted, *symlinked), config_changed)
        projections = runtime.build_projections(rebuilt, affected) if membership_changed or config_changed else {}
        with self._lock:
            self._states = {path: state.content_identity for path, state in states.items()}
            self._config_states = configs
            # Advance with the commit itself rather than with a successful
            # return: the installation below may fail, but this observation is
            # already visible to every later call and must not be missed.
            self._version += 1
        # Native config discovery happens before an LSP starts.  A running
        # adapter cannot be allowed to report readiness for a newly attributed
        # program until it has restarted against that native configuration.
        restart_families = _affected_families((), config_changed)
        install_failure: BaseException | None = None
        try:
            runtime.install_freshness(
                rebuilt,
                projections,
                restart_families=restart_families,
                config_paths=config_changed,
            )
        except WorkspaceRuntimeError as caught:
            if caught.code is RuntimeErrorCode.STOPPED:
                raise
            install_failure = caught
        except Exception as caught:
            # Installation has already published typed recovery ownership for
            # any failed family.  Healthy-family source facts from this same
            # scan must still advance and enqueue exactly once before that
            # family-local failure is surfaced.
            install_failure = caught
        reattributed = tuple(sorted(projections))

        events = (
            *(WatchedFileEvent(path, FileChangeType.CREATED) for path in created),
            *(WatchedFileEvent(path, FileChangeType.CHANGED) for path in changed),
            *(WatchedFileEvent(path, FileChangeType.DELETED) for path in deleted),
        )
        notified, opened, unopened = self._apply_events(
            events,
            created=created,
            force_notify=restart_families,
            wait_for_delivery=install_failure is None,
        )
        scan = FreshnessScan(
            created=created,
            changed=changed,
            deleted=deleted,
            symlinked=symlinked,
            config_changed=config_changed,
            reattributed=reattributed,
            notified=notified,
            opened=opened,
            unopened=unopened,
        )
        if install_failure is not None:
            raise install_failure
        return scan

    def _apply_events(
        self,
        events: tuple[WatchedFileEvent, ...],
        *,
        created: tuple[str, ...] = (),
        force_notify: Collection[LanguageFamily] = (),
        wait_for_delivery: bool = True,
    ) -> tuple[tuple[LanguageFamily, ...], tuple[str, ...], tuple[str, ...]]:
        """Advance generations first, then notify running adapters best-effort.

        Each family sees only its own events: membership and config invalidation
        are already owned by the reattributed projection, so one family's churn
        must not invalidate another family's configured program.  The generation
        bookkeeping is synchronous and unconditional, so a notification that
        cannot be queued degrades to ``NOT_READY`` through the readiness barriers
        rather than to a stale success.
        """

        runtime = self._runtime
        forced = frozenset(force_notify)
        notified: list[LanguageFamily] = []
        opened: list[str] = []
        unopened: list[str] = []
        deliveries: list[_PendingWatchedReconcile] = []
        affected_batches: list[tuple[LanguageFamily, tuple[WatchedFileEvent, ...], tuple[str, ...]]] = []

        # Phase one is only generation bookkeeping.  No adapter access or
        # submission may fail before every affected family is invalidated.
        for family, tracker in runtime.trackers.items():
            family_events = tuple(event for event in events if _family_of(event.path) is family)
            if not family_events:
                continue
            tracker.apply_did_change_watched_files(family_events)
            family_created = tuple(path for path in created if _family_of(path) is family)
            affected_batches.append((family, family_events, family_created))

        # Publish ownership for every runnable delivery before attempting to
        # admit any one family to the executor.
        for family, family_events, family_created in affected_batches:
            adapter = runtime.adapters.get(family)
            if adapter is None or (not adapter.snapshot().running and family not in forced):
                unopened.extend(family_created)
                continue
            opens = family_created[:MAX_CONTROLLED_OPENS]
            pending = _PendingWatchedReconcile(family, adapter, family_events, opens, None)
            deliveries.append(pending)

        with self._lock:
            for pending in deliveries:
                if pending.family in self._pending_reconciles:
                    raise WorkspaceRuntimeError(
                        RuntimeErrorCode.NOT_READY,
                        f"{pending.family.value} freshness reconciliation already has pending ownership",
                    )
                self._pending_reconciles[pending.family] = pending

        # Phase two admits every owned batch before waiting for any one family.
        # Failures are retained by family and surfaced only after all later
        # families have either settled or remain explicitly pending.
        failures: dict[LanguageFamily, WorkspaceRuntimeError] = {}
        admitted: set[LanguageFamily] = set()
        for pending in deliveries:
            family = pending.family
            family_created = tuple(path for path in created if _family_of(path) is family)
            try:
                future = runtime.notify_watched_files(pending.adapter, pending.events, pending.created)
            except ExecutorBusyError as error:
                failures[family] = WorkspaceRuntimeError(
                    RuntimeErrorCode.BUSY,
                    f"{family.value} freshness reconciliation could not enter the bounded executor",
                )
                failures[family].__cause__ = error
            except Exception as error:
                failures[family] = WorkspaceRuntimeError(
                    RuntimeErrorCode.NOT_READY,
                    f"{family.value} freshness reconciliation could not be submitted ({type(error).__name__})",
                )
                failures[family].__cause__ = error
            else:
                with self._lock:
                    current = self._pending_reconciles.get(family)
                    if current is pending:
                        pending.future = future
                admitted.add(family)
                if not wait_for_delivery:
                    notified.append(family)
                    opened.extend(pending.created)
                    unopened.extend(family_created[MAX_CONTROLLED_OPENS:])
            if family not in admitted:
                unopened.extend(family_created)

        if wait_for_delivery:
            for pending in deliveries:
                family = pending.family
                if family in failures:
                    continue
                family_created = tuple(path for path in created if _family_of(path) is family)
                try:
                    self._settle_pending_reconciles((family,))
                except WorkspaceRuntimeError as error:
                    failures[family] = error
                    unopened.extend(family_created)
                else:
                    notified.append(family)
                    opened.extend(pending.created)
                    unopened.extend(family_created[MAX_CONTROLLED_OPENS:])

        for pending in deliveries:
            failure = failures.get(pending.family)
            if failure is not None and wait_for_delivery:
                raise failure
        return tuple(notified), tuple(sorted(opened)), tuple(sorted(unopened))

    def _settle_pending_reconciles(self, families: Collection[LanguageFamily] | None = None) -> None:
        """Wait or retry every exact watcher task before dispatch can trust unchanged facts."""

        with self._lock:
            selected = tuple(families) if families is not None else tuple(self._pending_reconciles)
        failures: list[WorkspaceRuntimeError] = []
        for family in selected:
            try:
                self._settle_pending_reconcile(family)
            except WorkspaceRuntimeError as error:
                failures.append(error)
        if failures:
            raise failures[0]

    def _settle_pending_reconcile(self, family: LanguageFamily) -> None:
        """Settle one owned batch, retiring a failed future so a later scan can retry."""

        with self._lock:
            pending = self._pending_reconciles.get(family)
            future = pending.future if pending is not None else None
        if pending is None:
            return
        if future is not None and future.done():
            try:
                future.result()
            except Exception as error:
                with self._lock:
                    current = self._pending_reconciles.get(family)
                    if current is pending and pending.future is future:
                        pending.future = None
                raise WorkspaceRuntimeError(
                    RuntimeErrorCode.NOT_READY,
                    f"{family.value} freshness reconciliation failed ({type(error).__name__})",
                ) from error
            else:
                with self._lock:
                    if self._pending_reconciles.get(family) is pending:
                        self._pending_reconciles.pop(family, None)
                return
        if future is None:
            try:
                submitted = self._runtime.notify_watched_files(pending.adapter, pending.events, pending.created)
            except ExecutorBusyError as error:
                raise WorkspaceRuntimeError(
                    RuntimeErrorCode.BUSY,
                    f"{family.value} freshness reconciliation is waiting for executor capacity",
                ) from error
            except Exception as error:
                raise WorkspaceRuntimeError(
                    RuntimeErrorCode.NOT_READY,
                    (f"{family.value} freshness reconciliation retry could not be submitted ({type(error).__name__})"),
                ) from error
            with self._lock:
                current = self._pending_reconciles.get(family)
                if current is not pending:
                    return
                pending.future = submitted
            future = submitted
        try:
            future.result(timeout=self._runtime._future_timeout)
        except TimeoutError as error:
            raise WorkspaceRuntimeError(
                RuntimeErrorCode.BUSY,
                f"{family.value} freshness reconciliation is still pending",
            ) from error
        except Exception as error:
            with self._lock:
                current = self._pending_reconciles.get(family)
                if current is pending and pending.future is future:
                    pending.future = None
            raise WorkspaceRuntimeError(
                RuntimeErrorCode.NOT_READY,
                f"{family.value} freshness reconciliation failed ({type(error).__name__})",
            ) from error
        with self._lock:
            if self._pending_reconciles.get(family) is pending:
                self._pending_reconciles.pop(family, None)


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
        debug_reporter: Callable[[str, str], object] | None = None,
    ) -> None:
        if future_timeout <= 0:
            raise ValueError("future_timeout must be positive")
        self.identity = _physical_identity(identity)
        self.key = self.identity.registry_key
        self._path_policy = path_policy
        self._future_timeout = future_timeout
        self._debug_reporter = debug_reporter
        # An explicitly injected inventory is its own rescan source unless the
        # caller also owns a factory; production supplies neither and therefore
        # rebuilds from Git on every scan.
        self._inventory_factory = inventory_factory or (
            _default_inventory if inventory is None else _fixed_inventory(inventory)
        )
        self.inventory = inventory or self._inventory_factory(self.identity)
        if self.inventory.root.resolve(strict=True) != self.identity.root:
            raise ValueError("trust inventory root does not match the physical workspace key")

        self._supplied_attributors = dict(attributors or {})
        self._attributors: dict[LanguageFamily, ProgramAttributor] = {}
        self._family_errors: dict[LanguageFamily, WorkspaceRuntimeError] = {}
        family_paths = _family_paths(self.inventory)
        self._projections: dict[LanguageFamily, ScopeProjection] = {}
        for family, paths in family_paths.items():
            if not paths:
                continue
            try:
                projection = self._attribute(family, paths)
            except WorkspaceRuntimeError as error:
                self._family_errors[family] = error
                continue
            self._projections[family] = projection
            if not projection.compatible:
                self._family_errors[family] = _projection_error(family, projection)

        self._executor = (executor_factory or _default_executor)(self.identity.root)
        self._operation_lock = threading.RLock()
        self._adapter_factories = dict(adapter_factories or {})
        self._adapters: dict[LanguageFamily, RuntimeAdapter] = {}
        self._trackers: dict[LanguageFamily, ScopeGenerationTracker] = {}
        self._pending_restarts: dict[LanguageFamily, _PendingAdapterRestart] = {}
        self._pending_retirements: dict[LanguageFamily, _PendingAdapterRetirement] = {}
        self._shutdown_futures: dict[int, Future[AdapterSnapshot]] = {}
        self._versions: dict[str, int] = {}
        # Byte identities of the snapshots one in-flight read attempt owns.  It
        # is written only on the calling thread, never inside an adapter worker,
        # so an executor thread can never observe another call's attempt.
        self._read_witnesses = threading.local()
        self._state_lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._stopping = False
        self._stopped = False
        try:
            for family, projection in self._projections.items():
                if family in self._family_errors:
                    continue
                adapter, tracker = self._build_adapter(family, projection, family_paths[family])
                self._trackers[family] = tracker
                self._adapters[family] = adapter
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
        self._freshness = FreshnessCoordinator(self)

    @property
    def executor(self) -> BoundedLspExecutor:
        return self._executor

    @property
    def projections(self) -> Mapping[LanguageFamily, ScopeProjection]:
        return dict(self._projections)

    @property
    def adapters(self) -> Mapping[LanguageFamily, RuntimeAdapter]:
        return dict(self._adapters)

    @property
    def trackers(self) -> Mapping[LanguageFamily, ScopeGenerationTracker]:
        return dict(self._trackers)

    @property
    def freshness(self) -> FreshnessCoordinator:
        return self._freshness

    def ensure_fresh(self) -> FreshnessScan:
        """Reconcile the workspace with disk; also the same-root activation hook."""

        self._require_running()
        return self._freshness.ensure_fresh()

    def rebuild_inventory(self) -> TrustInventory:
        """Rebuild the lexical inventory from this workspace's owning source."""

        rebuilt = self._inventory_factory(self.identity)
        if rebuilt.root.resolve(strict=True) != self.identity.root:
            raise ValueError("rebuilt trust inventory root does not match the physical workspace key")
        return rebuilt

    def build_projections(
        self, inventory: TrustInventory, families: Collection[LanguageFamily]
    ) -> dict[LanguageFamily, FamilyAttribution]:
        """Reattribute only the named families, without installing the result."""

        family_paths = _family_paths(inventory)
        rebuilt: dict[LanguageFamily, FamilyAttribution] = {}
        for family in families:
            paths = family_paths.get(family, ())
            if not paths:
                rebuilt[family] = FamilyAttribution(
                    error=WorkspaceRuntimeError(
                        RuntimeErrorCode.SCOPE_INCOMPATIBLE,
                        f"{family.value} has no trusted source paths",
                    )
                )
                continue
            try:
                projection = self._attribute(family, paths)
            except WorkspaceRuntimeError as error:
                rebuilt[family] = FamilyAttribution(error=error)
                continue
            rebuilt[family] = FamilyAttribution(
                projection=projection,
                error=None if projection.compatible else _projection_error(family, projection),
            )
        return rebuilt

    def install_freshness(
        self,
        inventory: TrustInventory,
        attributions: Mapping[LanguageFamily, FamilyAttribution],
        *,
        restart_families: Collection[LanguageFamily] = (),
        config_paths: Collection[str] = (),
    ) -> None:
        """Swap inventory and projections, restarting adapters for native-config changes."""

        # Keep this runtime seam correct even when a caller does not arrive via
        # FreshnessCoordinator: newer facts cannot replace unsettled cleanup
        # ownership from an earlier restart or retirement.
        self.retry_pending_restarts()
        paths_by_family = _family_paths(inventory)
        retirements: list[_PendingAdapterRetirement] = []
        restart_adapters: list[_PendingAdapterRestart] = []
        restart_without_adapter: list[_PendingAdapterRestart] = []
        restart = frozenset(restart_families)
        config_events = {
            family: tuple(
                WatchedFileEvent(path, FileChangeType.CHANGED, may_change_program=True)
                for path in config_paths
                if PurePosixPath(path).name in _NATIVE_CONFIG_WATCH[family]
            )
            for family in restart
        }
        with self._state_lock:
            if self._stopping or self._stopped:
                raise WorkspaceRuntimeError(RuntimeErrorCode.STOPPED, "workspace runtime is stopped")
            self.inventory = inventory
            for family, attribution in attributions.items():
                projection = attribution.projection
                if projection is not None:
                    self._projections[family] = projection
                if attribution.error is not None:
                    self._family_errors[family] = attribution.error
                    adapter = self._adapters.pop(family, None)
                    self._trackers.pop(family, None)
                    if adapter is not None:
                        retirement = _PendingAdapterRetirement(family, adapter, None)
                        self._pending_retirements[family] = retirement
                        retirements.append(retirement)
                    continue
                assert projection is not None
                self._family_errors.pop(family, None)
                if family in restart:
                    tracker = self._trackers.get(family)
                    if tracker is not None:
                        before_program = tracker.generations.configured_program
                        tracker.update_projection(projection)
                        if tracker.generations.configured_program == before_program:
                            tracker.apply_did_change_watched_files(config_events[family])
                    adapter = self._adapters.pop(family, None)
                    pending = _PendingAdapterRestart(
                        family,
                        projection,
                        tracker,
                        paths_by_family[family],
                        adapter,
                        None,
                    )
                    # Removal and cleanup ownership are one publication.  A
                    # concurrent runtime stop can therefore observe the old
                    # adapter either as active or as this exact pending stop,
                    # never in an ownerless gap.
                    self._pending_restarts[family] = pending
                    self._family_errors[family] = WorkspaceRuntimeError(
                        RuntimeErrorCode.NOT_READY,
                        f"{family.value} adapter restart is in progress",
                    )
                    if adapter is None:
                        restart_without_adapter.append(pending)
                    else:
                        restart_adapters.append(pending)
                    continue
                tracker = self._trackers.get(family)
                if tracker is not None:
                    tracker.update_projection(projection)
                    continue
                try:
                    adapter, tracker = self._build_adapter(family, projection, paths_by_family[family])
                except Exception as error:
                    self._family_errors[family] = WorkspaceRuntimeError(
                        RuntimeErrorCode.SCOPE_INCOMPATIBLE,
                        f"{family.value} adapter construction failed ({type(error).__name__})",
                    )
                    continue
                self._trackers[family] = tracker
                self._adapters[family] = adapter
        failures: list[BaseException] = []
        for retirement in retirements:
            failure = self._settle_pending_retirement(retirement)
            if failure is not None:
                failures.append(failure)
        for pending in restart_without_adapter:
            failure = self._complete_pending_restart(pending)
            if failure is not None:
                failures.append(failure)
        for pending in restart_adapters:
            pending, failure = self._start_pending_stop(pending)
            if failure is not None:
                failures.append(failure)
                continue
            assert pending is not None
            stop_future = pending.stop_future
            assert stop_future is not None
            try:
                stop_future.result(timeout=self._future_timeout)
            except TimeoutError as error:
                with self._state_lock:
                    self._family_errors[pending.family] = WorkspaceRuntimeError(
                        RuntimeErrorCode.TIMED_OUT,
                        f"{pending.family.value} adapter stop timed out; freshness will retry",
                    )
                failures.append(error)
                continue
            except Exception as error:
                failure = WorkspaceRuntimeError(
                    RuntimeErrorCode.UNSUPPORTED,
                    f"{pending.family.value} adapter stop failed ({type(error).__name__})",
                )
                with self._state_lock:
                    self._family_errors[pending.family] = failure
                failures.append(failure)
                continue
            failure = self._complete_pending_restart(pending)
            if failure is not None:
                failures.append(failure)
        if failures:
            raise failures[0]

    def retry_pending_restarts(self) -> None:
        """Resolve uncertain cleanup before unchanged facts can pass."""

        with self._state_lock:
            pending_retirements = tuple(self._pending_retirements.values())
            pending_restarts = tuple(self._pending_restarts.values())
        failures: list[BaseException] = []
        for retirement in pending_retirements:
            failure = self._settle_pending_retirement(retirement)
            if failure is not None:
                failures.append(failure)
        for pending in pending_restarts:
            pending, failure = self._start_pending_stop(pending)
            if failure is not None:
                failures.append(failure)
                continue
            if pending is None:
                continue
            stop_future = pending.stop_future
            if stop_future is not None:
                try:
                    stop_future.result(timeout=self._future_timeout)
                except TimeoutError as error:
                    with self._state_lock:
                        self._family_errors[pending.family] = WorkspaceRuntimeError(
                            RuntimeErrorCode.TIMED_OUT,
                            f"{pending.family.value} adapter stop timed out; freshness will retry",
                        )
                    failures.append(error)
                    continue
                except Exception as error:
                    failure = WorkspaceRuntimeError(
                        RuntimeErrorCode.UNSUPPORTED,
                        f"{pending.family.value} adapter stop failed ({type(error).__name__})",
                    )
                    with self._state_lock:
                        self._family_errors[pending.family] = failure
                    failures.append(failure)
                    continue
            failure = self._complete_pending_restart(pending)
            if failure is not None:
                failures.append(failure)
        if failures:
            raise failures[0]

    def _start_pending_retirement(
        self,
        pending: _PendingAdapterRetirement,
    ) -> tuple[_PendingAdapterRetirement | None, WorkspaceRuntimeError | None]:
        """Submit or retry one unavailable adapter stop under lifecycle lock."""

        with self._state_lock:
            current = self._pending_retirements.get(pending.family)
            if current is None:
                return None, None
            if current is not pending:
                return current, None
            stop_future = pending.stop_future
            if stop_future is not None and not _stop_future_needs_retry(stop_future):
                return current, None
            stop_adapter = pending.stop_adapter
            if stop_adapter is None:
                return pending, None
            try:
                stop_future = stop_adapter.stop()
            except Exception as error:
                return pending, WorkspaceRuntimeError(
                    RuntimeErrorCode.UNSUPPORTED,
                    f"{pending.family.value} adapter stop failed ({type(error).__name__})",
                )
            replacement = _PendingAdapterRetirement(pending.family, stop_adapter, stop_future)
            self._pending_retirements[pending.family] = replacement
            return replacement, None

    def _settle_pending_retirement(
        self,
        pending: _PendingAdapterRetirement,
    ) -> BaseException | None:
        """Retain an unavailable adapter until its exact stop is proven complete."""

        current, failure = self._start_pending_retirement(pending)
        if failure is not None or current is None:
            return failure
        stop_future = current.stop_future
        if stop_future is None:
            return WorkspaceRuntimeError(
                RuntimeErrorCode.UNSUPPORTED,
                f"{current.family.value} adapter cleanup has no terminal future",
            )
        try:
            stop_future.result(timeout=self._future_timeout)
        except BaseException as error:
            return error
        with self._state_lock:
            if self._pending_retirements.get(current.family) is current:
                self._pending_retirements.pop(current.family, None)
        return None

    def _start_pending_stop(
        self,
        pending: _PendingAdapterRestart,
    ) -> tuple[_PendingAdapterRestart | None, WorkspaceRuntimeError | None]:
        """Submit or retry one pending old-adapter stop under lifecycle lock."""

        with self._state_lock:
            current = self._pending_restarts.get(pending.family)
            if current is None:
                return None, None
            if current is not pending:
                return current, None
            stop_future = pending.stop_future
            if stop_future is not None and not _stop_future_needs_retry(stop_future):
                return current, None
            stop_adapter = pending.stop_adapter
            if stop_adapter is None:
                return pending, None
            try:
                stop_future = stop_adapter.stop()
            except Exception as error:
                failure = WorkspaceRuntimeError(
                    RuntimeErrorCode.UNSUPPORTED,
                    f"{pending.family.value} adapter stop failed ({type(error).__name__})",
                )
                self._family_errors[pending.family] = failure
                return pending, failure
            replacement = _PendingAdapterRestart(
                pending.family,
                pending.projection,
                pending.tracker,
                pending.trusted_paths,
                stop_adapter,
                stop_future,
            )
            self._pending_restarts[pending.family] = replacement
            return replacement, None

    def _complete_pending_restart(
        self,
        pending: _PendingAdapterRestart,
    ) -> WorkspaceRuntimeError | None:
        """Publish one cold replacement only after its old stop is proven done."""

        with self._state_lock:
            if self._pending_restarts.get(pending.family) is not pending:
                return None
            if self._stopping or self._stopped:
                return None
            try:
                adapter, tracker = self._build_adapter(
                    pending.family,
                    pending.projection,
                    pending.trusted_paths,
                    scope_tracker=pending.tracker,
                )
            except Exception as error:
                failure = WorkspaceRuntimeError(
                    RuntimeErrorCode.SCOPE_INCOMPATIBLE,
                    f"{pending.family.value} adapter construction failed ({type(error).__name__})",
                )
                self._family_errors[pending.family] = failure
                return failure
            self._trackers[pending.family] = tracker
            self._adapters[pending.family] = adapter
            self._family_errors.pop(pending.family, None)
            self._pending_restarts.pop(pending.family, None)
            return None

    def notify_watched_files(
        self,
        adapter: RuntimeAdapter,
        events: Sequence[WatchedFileEvent],
        created: Sequence[str],
    ) -> Future[None] | Future[Any]:
        """Queue one watcher batch plus bounded open/close for created files.

        The coordinator retains and settles the returned future before it lets
        unchanged filesystem facts authorize semantic or edit work.
        """

        changed_versions: dict[str, int] = {}
        with self._state_lock:
            for event in events:
                if event.change_type is not FileChangeType.CHANGED:
                    continue
                version = self._versions.get(event.path, 0) + 1
                self._versions[event.path] = version
                changed_versions[event.path] = version

        # LanguageAdapter owns the open-buffer map, so only it can refresh an
        # existing URI without reopening the configured program.  Keep the
        # small fallback for injected legacy test adapters; all production
        # adapters implement the explicit reconciliation seam.
        reconcile = getattr(adapter, "reconcile_watched_files", None)
        if callable(reconcile):
            return reconcile(events=events, created=created, versions=changed_versions)

        workspace_uri = self.identity.root.as_uri()
        changes = [dict(event.as_lsp_change(workspace_uri)) for event in events]
        opens: list[tuple[str, str, str]] = []
        for relative in created:
            language_id = _LANGUAGE_IDS.get(PurePosixPath(relative).suffix.lower())
            if language_id is None:
                continue
            try:
                text = (self.identity.root / relative).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            opens.append(((self.identity.root / relative).as_uri(), language_id, text))

        def send(client: AdapterClient) -> None:
            client.notify("workspace/didChangeWatchedFiles", {"changes": changes})
            # A created-file notification alone does not make every backend bind
            # the file, so force one parse with a controlled open/close pair.
            for uri, language_id, text in opens:
                client.notify(
                    "textDocument/didOpen",
                    {"textDocument": {"uri": uri, "languageId": language_id, "version": 1, "text": text}},
                )
                client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

        return adapter.submit_read(send)

    def _attribute(self, family: LanguageFamily, paths: tuple[str, ...]) -> ScopeProjection:
        attributor = self._attributors.get(family)
        if attributor is None:
            attributor = self._supplied_attributors.get(family) or _default_attributor(family)
            self._attributors[family] = attributor
        try:
            projection = attributor(self.identity.root, paths)
        except Exception as error:
            raise WorkspaceRuntimeError(
                RuntimeErrorCode.SCOPE_INCOMPATIBLE,
                f"{family.value} native-program attribution failed ({type(error).__name__})",
            ) from error
        if projection.language is not family:
            raise WorkspaceRuntimeError(
                RuntimeErrorCode.SCOPE_INCOMPATIBLE,
                f"{family.value} attributor returned the wrong language family",
            )
        return projection

    def _build_adapter(
        self,
        family: LanguageFamily,
        projection: ScopeProjection,
        trusted_paths: tuple[str, ...],
        *,
        scope_tracker: ScopeGenerationTracker | None = None,
    ) -> tuple[RuntimeAdapter, ScopeGenerationTracker]:
        tracker = scope_tracker or ScopeGenerationTracker(projection)
        context = AdapterBuildContext(
            family=family,
            workspace_root=self.identity.root,
            trusted_paths=trusted_paths,
            projection=projection,
            scope_tracker=tracker,
            executor=self._executor,
            operation_lock=self._operation_lock,
            debug_reporter=self._debug_reporter,
        )
        factory = self._adapter_factories.get(family) or _default_adapter_factory(family)
        return factory(context), tracker

    def route(self, relative_path: str) -> RuntimeAdapter:
        """Authorize one path and select exactly one fixed adapter without starting it."""

        return self._route(relative_path)[1]

    def _route(self, relative_path: str) -> tuple[LanguageFamily, RuntimeAdapter]:
        """Return the fixed family together with its authorized adapter.

        Routing owns authorization and adapter selection only.  Freshness for a
        read belongs to the enclosing fresh-read transaction, which already
        observed this operand under its own scope, so routing inside an
        operation must not open a second, unvalidated preflight.
        """

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
        family = _family_of(normalized)
        if family is not None and family in self._family_errors:
            raise self._family_errors[family]
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
            ),
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
        _error_max_answer_chars: int | None = None,
    ) -> ToolEnvelope:
        """Find a symbol in one selected file, or via bounded configured-program search."""

        def operation() -> ToolEnvelope:
            if relative_path is not None:
                if max_candidates_per_adapter != 128:
                    return error(
                        ErrorCode.UNSUPPORTED,
                        details={
                            "operation": "find_symbol",
                            "reason": "max_candidates_per_adapter_applies_only_to_global_scope",
                        },
                        workspace=_workspace_metadata(self.identity),
                    )
                normalized_scope = relative_path.rstrip("/") or "."
                if normalized_scope not in self.inventory.paths:
                    try:
                        selected = self.inventory.paths_under(normalized_scope)
                    except ValueError:
                        self._route(normalized_scope)
                        raise AssertionError("workspace policy accepted an invalid directory scope") from None
                    if selected:
                        available = tuple(path for path in selected if _family_of(path) in self._adapters)
                        if not available:
                            raise WorkspaceRuntimeError(
                                RuntimeErrorCode.SCOPE_INCOMPATIBLE,
                                "directory scope contains only unavailable language families",
                                paths=tuple(
                                    sorted(
                                        {
                                            path
                                            for family in {_family_of(path) for path in selected}
                                            if family in self._family_errors
                                            for path in self._family_errors[family].paths
                                        }
                                    )
                                ),
                            )
                        return DocumentNavigationService(self).find_symbol_in_documents(
                            available,
                            name_path,
                            relative_scope=normalized_scope,
                            substring_matching=substring_matching,
                            include_body=include_body,
                            include_info=include_info,
                            max_answer_chars=max_answer_chars,
                        )
                self._route(normalized_scope)
                return DocumentNavigationService(self).find_symbol(
                    normalized_scope,
                    name_path,
                    substring_matching=substring_matching,
                    include_body=include_body,
                    include_info=include_info,
                    max_answer_chars=max_answer_chars,
                    _error_max_answer_chars=_error_max_answer_chars,
                )
            if not self._adapters and self._family_errors:
                raise WorkspaceRuntimeError(
                    RuntimeErrorCode.SCOPE_INCOMPATIBLE,
                    "all attributed language families are unavailable",
                    paths=tuple(sorted({path for failure in self._family_errors.values() for path in failure.paths})),
                )
            warmed = self._warm_global_candidates(name_path)
            return GlobalSymbolService(
                tuple(_GlobalProvider(self, family, warmed.get(family)) for family in self._adapters)
            ).find_symbol(
                name_path,
                substring_matching=substring_matching,
                include_body=include_body,
                include_info=include_info,
                max_candidates_per_adapter=max_candidates_per_adapter,
                max_answer_chars=max_answer_chars,
            )

        return self._fresh_read_envelope(operation, scope=self._read_scope(relative_path))

    def _warm_global_candidates(self, name_path: str | Sequence[str]) -> Mapping[LanguageFamily, _WarmGlobalSeed]:
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
                except (LspResponseError, LspProtocolError, LspTransportClosed):
                    raise
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
        response_owner = _ResponseOwnedDeclarationProvider(self)
        return self._semantic_envelope(
            relative_path,
            lambda: DeclarationNavigationService(response_owner).find_declaration(
                relative_path,
                regex,
                containing_symbol_name_path=containing_symbol_name_path,
                include_body=include_body,
                include_info=include_info,
            ),
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
        response_owner = _ResponseOwnedDeclarationProvider(self)
        return self._semantic_envelope(
            relative_path,
            lambda: DeclarationNavigationService(response_owner).find_implementations(
                name_path,
                relative_path,
                include_info=include_info,
                include_kinds=include_kinds,
                exclude_kinds=exclude_kinds,
                max_answer_chars=max_answer_chars,
            ),
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
            response_owner = _ResponseOwnedReferenceProvider(self, loaded.document)
            return ReferenceNavigationService(response_owner, response_owner, response_owner).find_referencing_symbols(
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
            ),
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
            ),
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
            commit = EditCommit()

            def transaction(client: AdapterClient) -> ToolEnvelope:
                commit.mark_running()
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
                    commit=commit,
                )

            future = adapter.submit_edit(transaction)
            try:
                return future.result(timeout=self._future_timeout)
            except TimeoutError:
                # Cancellation succeeds only while the work is still queued, and
                # a queued entry can never write later.
                if future.cancel() and commit.state is EditCommitState.QUEUED:
                    return error(
                        ErrorCode.TIMED_OUT,
                        retry=RetryMetadata(retryable=True),
                        details={"relative_path": authorized.relative_path, "commit_state": commit.state.value},
                        workspace=authorized.workspace,
                    )
                return _uncertain_edit(authorized, commit, "timeout")
            except Exception:
                # Only an installed replacement is uncertain; anything earlier
                # provably never reached os.replace and keeps its own envelope.
                if not commit.installed:
                    raise
                return _uncertain_edit(authorized, commit, "transport")

        def preflighted() -> ToolEnvelope:
            # Exactly one preflight, and no postflight or replay: an edit that
            # started or may have committed is never invoked a second time.
            self.ensure_fresh()
            return operation()

        return self._tool_envelope(preflighted)

    def _edit_adapter(self, relative_path: str) -> tuple[RuntimeAdapter, AuthorizedEdit]:
        normalized = _relative_path(relative_path, allow_parent=True)
        self._require_running()
        self._freshness.ensure_path_fresh(normalized)
        if ".." in PurePosixPath(normalized).parts and not (self.identity.root / normalized).exists():
            raise ValueError(f"path does not exist: {relative_path!r}")
        authorized = self.authorize_edit(normalized)
        if ".." in PurePosixPath(normalized).parts:
            raise ValueError(f"path is not normalized within the active workspace: {relative_path!r}")
        routed = tuple(adapter for adapter in self._adapters.values() if adapter.routes(authorized))
        if len(routed) != 1:
            raise WorkspaceRuntimeError(
                RuntimeErrorCode.UNSUPPORTED, "edit path has no unique adapter", paths=(normalized,)
            )
        return routed[0], AuthorizedEdit(authorized, normalized, _workspace_metadata(self.identity), self.identity.root)

    def authorize_edit(self, relative_path: str) -> Path:
        """Check lexical membership and every guarded path component once."""

        authorize = getattr(self._path_policy, "authorize_edit", None)
        if not callable(authorize):
            raise ValueError("workspace policy does not support edits")
        inventory = self.inventory
        return cast(
            Path,
            authorize(
                self.identity,
                self.identity.root / relative_path,
                tuple(self.identity.root / item for item in inventory.paths),
            ),
        )

    def _tool_envelope(self, operation: Callable[[], ToolEnvelope]) -> ToolEnvelope:
        """Map domain failures onto the public envelope vocabulary.

        This owns error translation only.  Freshness belongs to
        ``_run_fresh_read`` for reads and to one explicit preflight for edits,
        so a typed invalid, trust, readiness, cooldown, or timeout failure keeps
        its own authority and is never replayed to manufacture another error.
        """

        try:
            return operation()
        except WorkspaceError as caught:
            return from_workspace_error(caught)
        except WorkspaceRuntimeError as caught:
            try:
                code = ErrorCode(caught.code)
            except ValueError:
                code = ErrorCode.UNSUPPORTED
            retry = (
                RetryMetadata(retryable=True)
                if code in {ErrorCode.BUSY, ErrorCode.NOT_READY, ErrorCode.TIMED_OUT}
                else None
            )
            return error(
                code,
                details={"paths": caught.paths} if caught.paths else {},
                retry=retry,
            )
        # TimeoutError is an OSError; it must keep its own code rather than be
        # rewritten as invalid input by the clause below.
        except TimeoutError:
            return error(ErrorCode.TIMED_OUT, retry=RetryMetadata(retryable=True))
        except (OSError, TypeError, ValueError):
            return error(ErrorCode.INVALID_INPUT)

    def _semantic_envelope(self, relative_path: str, operation: Callable[[], ToolEnvelope]) -> ToolEnvelope:
        def authorized() -> ToolEnvelope:
            self._route(relative_path)
            return operation()

        return self._fresh_read_envelope(authorized, scope=self._read_scope(relative_path))

    def _read_scope(self, relative_path: str | None) -> tuple[str, ...] | None:
        """The exact paths a read may validate targeted, or ``None`` for whole-root.

        Only an operand that already is one indexed file can be validated by
        observing that file.  A global query, a directory scope, and a path the
        current inventory does not contain all depend on membership evidence a
        per-path observation cannot produce—most plainly a source file created
        since the last scan—so they take the whole-root guarded scan.  A Git
        workspace always scans its whole root, so this only chooses scope on the
        allowlisted non-Git root.
        """

        if relative_path is None:
            return None
        scope = relative_path.rstrip("/") or "."
        return (scope,) if self.inventory.contains(scope) else None

    def _fresh_read_envelope(
        self, operation: Callable[[], ToolEnvelope], *, scope: tuple[str, ...] | None = None
    ) -> ToolEnvelope:
        return self._tool_envelope(lambda: self._run_fresh_read(operation, scope=scope))

    def _admit_fresh_read(self, scope: tuple[str, ...] | None, *, witnessed: Sequence[str] = ()) -> _FreshnessAdmission:
        """Run one guarded scan over exactly the scope this read claims."""

        if scope is None or self.identity.kind is WorkspaceKind.GIT:
            return self._freshness.ensure_root_fresh_admitted(witnessed=witnessed)
        return self._freshness.ensure_paths_fresh_admitted(scope, witnessed=witnessed)

    def _run_fresh_read(self, operation: Callable[[], ToolEnvelope], *, scope: tuple[str, ...] | None) -> ToolEnvelope:
        """Own one read transaction: preflight, operation, guarded postflight.

        Every source-derived result is validated, whether it succeeded or
        failed: a symbol that was not found, a candidate set that was ambiguous,
        and a range or body that could not be resolved are all derived from the
        same response-owned bytes as a success, so a concurrent source write
        must not be able to leak out through the error.  A result whose
        workspace moved underneath it is discarded whole and replayed exactly
        once; a second race returns retryable ``NOT_READY`` rather than stale,
        mixed, or empty success and rather than either attempt's source-derived
        payload or candidates.  A failure whose contract deliberately does not
        make a source-content claim—an invalid locator, an untrusted operand, an
        unsupported capability, or an adapter condition such as cold, cooling,
        or timeout—keeps the authority of its single preflight, as
        ``_result_is_source_derived`` decides.  The response witnesses close
        the case where the operation observed bytes B that were restored to
        bytes A before the postflight, which no aggregate workspace identity can
        see.

        ``scope`` names the paths a targeted transaction validates, or is
        ``None`` for a whole-root transaction.  A targeted postflight also
        validates every path the response actually owns bytes from, so a
        response-owned file outside the requested scope is still observed before
        that response can be returned.
        """

        for attempt in range(1, MAX_FRESH_READ_ATTEMPTS + 1):
            self._require_running()
            admitted = self._admit_fresh_read(scope)
            witnesses = self._begin_read_attempt()
            source_failure_paths: tuple[str, ...] = ()
            try:
                try:
                    result = operation()
                except TimeoutError:
                    raise
                except OSError as caught:
                    source_path = _source_exception_path(caught, self.identity.root)
                    if source_path is None:
                        raise
                    source_failure_paths = (source_path,)
                    result = error(
                        ErrorCode.NOT_READY,
                        retry=RetryMetadata(retryable=True, retry_after_seconds=0.1),
                        details={
                            "reason": SOURCE_SNAPSHOT_UNAVAILABLE_REASON,
                            "paths": source_failure_paths,
                        },
                        workspace=_workspace_metadata(self.identity),
                    )
            finally:
                self._end_read_attempt()
            if not _result_is_source_derived(result, witnesses):
                return result
            final_scope = (
                None
                if scope is None
                else tuple(sorted({*scope, *witnesses.source, *source_failure_paths}))
            )
            final = self._admit_fresh_read(final_scope, witnessed=tuple(witnesses.source))
            changed = _fresh_read_change_evidence(admitted, final, witnesses.source, witnesses.external)
            if changed is None:
                return result
            if attempt == MAX_FRESH_READ_ATTEMPTS:
                return error(
                    ErrorCode.NOT_READY,
                    retry=RetryMetadata(retryable=True, retry_after_seconds=0.1),
                    details={
                        "reason": "workspace_changed_during_read",
                        "attempts": attempt,
                        "paths": changed[:MAX_FRESH_READ_EVIDENCE_PATHS],
                        # The freshness versions stay decisive when the changed
                        # paths are empty because another call's scan consumed
                        # the change before this attempt's postflight ran.
                        "preflight_freshness_version": admitted.version,
                        "postflight_freshness_version": final.version,
                    },
                    workspace=_workspace_metadata(self.identity),
                )
        raise AssertionError("bounded fresh-read replay did not terminate")

    def _begin_read_attempt(self) -> _ReadAttemptWitnesses:
        """Start one attempt-local witness set on this calling thread."""

        witnesses = _ReadAttemptWitnesses({}, {})
        self._read_witnesses.active = witnesses
        return witnesses

    def _end_read_attempt(self) -> None:
        self._read_witnesses.active = None

    def _record_read_witness(self, relative_path: str, raw_bytes: bytes) -> None:
        """Retain the byte identity of every response-owned source snapshot.

        One attempt can own more than one snapshot of the same path.  Each
        distinct digest is retained rather than replaced, because a later
        snapshot that agrees with the final guarded identity must not be able to
        conceal an earlier, differing one that a returning portion of the
        response was derived from.  Distinct digests per path are bounded by the
        snapshots the operation itself may own, which the response-owned target
        bound and the per-adapter candidate bounds already limit.  Outside a
        fresh-read attempt this records nothing.
        """

        witnesses = self._active_read_witnesses()
        if witnesses is None:
            return
        witnesses.source.setdefault(relative_path, set()).add(hashlib.sha256(raw_bytes).hexdigest())

    def _record_external_read_witness(self, path: Path, digest: str) -> None:
        """Retain one guarded byte identity of a trusted read-only external target.

        The external path is absolute because it lies outside the workspace
        root; it is revalidated by observing exactly this file again, never by a
        workspace scan and never by opening it in the language server.
        """

        witnesses = self._active_read_witnesses()
        if witnesses is None:
            return
        witnesses.external.setdefault(path, set()).add(digest)

    def _active_read_witnesses(self) -> _ReadAttemptWitnesses | None:
        return cast(_ReadAttemptWitnesses | None, getattr(self._read_witnesses, "active", None))

    # The following narrow methods are injected-provider seams consumed by the
    # transport-neutral semantic cores above.  They deliberately do not expose
    # inventory enumeration, pull diagnostics, or edit operations.

    def load_semantic_document(self, relative_path: str) -> SemanticDocumentInput:
        document, _target, _family, adapter = self._load_document(relative_path)
        return SemanticDocumentInput(document, CapabilityMatrix.from_raw(adapter.snapshot().raw_providers))

    def request_locations(
        self,
        method: str,
        *,
        document_uri: str,
        position: LspPosition,
        capture_target_symbols: bool = False,
    ) -> object:
        return self._request_locations(
            method,
            document_uri=document_uri,
            position=position,
            source_document=None,
            source_identity=None,
            capture_target_symbols=capture_target_symbols,
        )

    def _request_locations(
        self,
        method: str,
        *,
        document_uri: str,
        position: LspPosition,
        source_document: DocumentSymbolInput | None,
        source_identity: AdapterResponseIdentity | None,
        capture_target_symbols: bool = False,
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
        return self._stabilize_semantic_locations(
            adapter,
            method,
            params,
            capture_reference_documents=False,
            capture_target_symbols=capture_target_symbols,
            source_document=source_document,
            source_identity=source_identity,
        )

    def normalize_and_classify_locations(
        self,
        raw_locations: object,
        *,
        include_body: bool,
        include_info: bool,
    ) -> Sequence[ClassifiedLocationInput] | ErrorEnvelope:
        """Render only the snapshots owned by the authoritative second response."""

        del include_body
        if isinstance(raw_locations, ErrorEnvelope):
            return raw_locations
        if not isinstance(raw_locations, _ResponseOwnedSemanticLocations):
            return error(ErrorCode.INVALID_INPUT, details={"field": "semantic_locations"})
        if not self._response_adapter_states_are_current(raw_locations.adapter_states):
            return _semantic_locations_not_ready("semantic target generation changed")
        targets = {target.path: target for target in raw_locations.targets}
        classified: list[ClassifiedLocationInput] = []
        for item in raw_locations.locations:
            location = item.location
            semantic = item.semantic
            metadata: dict[str, object] = {
                "absolute_path": str(semantic.path),
                "location_kind": semantic.kind.value,
            }
            if semantic.kind is LocationKind.WORKSPACE:
                metadata["relative_path"] = str(semantic.path.relative_to(self.identity.root))
            else:
                metadata["read_only_external"] = True
            if semantic.kind is LocationKind.READ_ONLY_EXTERNAL:
                classified.append(
                    ClassifiedLocationInput.raw_lsp(
                        metadata,
                        location.range,
                        item.adapter.snapshot().position_encoding,
                    )
                )
                continue
            target = targets.get(semantic.path)
            if target is None or target.adapter is not item.adapter:
                return _semantic_locations_not_ready(
                    "verified target snapshot unavailable",
                    paths=(str(semantic.path),),
                )
            semantic_info: dict[str, object] | None = None
            symbol = _response_owned_target_symbol(target, location)
            if symbol is not None:
                metadata["kind"] = symbol.kind
                if include_info:
                    metadata["name_path"] = "/".join(symbol.name_path)
                    if symbol.detail:
                        semantic_info = {"detail": symbol.detail}
            classified.append(
                ClassifiedLocationInput.verified(
                    metadata,
                    location.range,
                    target.snapshot,
                    target.position_encoding,
                    semantic_info=semantic_info,
                )
            )
        if not self._response_adapter_states_are_current(raw_locations.adapter_states):
            return _semantic_locations_not_ready("semantic target generation changed")
        return classified

    def _stabilize_semantic_locations(
        self,
        source_adapter: RuntimeAdapter,
        method: str,
        params: Mapping[str, object],
        *,
        capture_reference_documents: bool,
        capture_target_symbols: bool = False,
        source_document: DocumentSymbolInput | None = None,
        source_identity: AdapterResponseIdentity | None = None,
        location_filter: Callable[[_ClassifiedSemanticLocation], bool] | None = None,
    ) -> _ResponseOwnedSemanticLocations | ErrorEnvelope:
        """Issue exactly two semantic requests in one adapter-owned transaction."""

        deadline = time.monotonic() + self._future_timeout
        expected_identity = source_identity or _adapter_response_identity(source_adapter.snapshot())
        expected_replay_identity = _adapter_replay_identity_from_response(expected_identity)

        if source_document is not None and (
            not _semantic_document_is_current(source_adapter, source_document)
            or _adapter_response_identity(source_adapter.snapshot()) != expected_identity
        ):
            return _semantic_locations_not_ready("semantic source adapter identity changed")

        def transaction(client: AdapterClient) -> _ResponseOwnedSemanticLocations | ErrorEnvelope:
            if _adapter_response_identity(source_adapter.snapshot()) != expected_identity:
                return _semantic_locations_not_ready("semantic source adapter identity changed")
            if source_document is not None and not _semantic_document_is_current(source_adapter, source_document):
                return _semantic_locations_not_ready("semantic source adapter identity changed")
            prepared_targets = self._prepare_typescript_semantic_owner_with_client(
                source_adapter,
                client,
                method,
                params,
                source_document=source_document,
                deadline=deadline,
            )
            if isinstance(prepared_targets, ErrorEnvelope):
                return prepared_targets
            prepared_identity = _adapter_response_identity(source_adapter.snapshot())
            if _adapter_replay_identity_from_response(prepared_identity) != expected_replay_identity:
                return _semantic_locations_not_ready("semantic source adapter identity changed")
            first_raw = client.request(method, params, timeout=_remaining_semantic_timeout(deadline))
            if _adapter_response_identity(source_adapter.snapshot()) != prepared_identity:
                return _semantic_locations_not_ready("semantic source adapter identity changed")
            first = self._classify_semantic_response(first_raw)
            if isinstance(first, ErrorEnvelope):
                return first
            if location_filter is not None:
                first = tuple(item for item in first if location_filter(item))
            if any(item.adapter is not source_adapter for item in first):
                return _semantic_locations_not_ready("semantic target crossed adapter family")
            bound = self._bind_semantic_targets_with_client(
                source_adapter,
                client,
                first,
                capture_reference_documents=capture_reference_documents,
                capture_target_symbols=capture_target_symbols,
                source_document=source_document,
                deadline=deadline,
            )
            if isinstance(bound, ErrorEnvelope):
                return bound
            # Opening response-owned targets intentionally advances the adapter's
            # document generation.  Every other process, capability, phase,
            # configured-program, index, and encoding fact must still match the
            # source owner captured before dispatch.
            if _adapter_replay_identity(source_adapter.snapshot()) != expected_replay_identity:
                return _semantic_locations_not_ready("semantic source adapter identity changed")
            states = (_response_adapter_state(source_adapter),)
            if not self._response_adapter_states_are_current(states):
                return _semantic_locations_not_ready("semantic target generation changed")
            # External targets are never opened in the server, so the only
            # evidence about them is their bytes.  This observation brackets the
            # authoritative request from below: a write that lands while it is
            # in flight leaves this identity disagreeing with the one taken
            # after it, and disagreement is a race the read must replay.
            dispatch_digests = _guarded_external_digests(_external_target_paths(first))
            if isinstance(dispatch_digests, ErrorEnvelope):
                return dispatch_digests
            second_raw = client.request(method, params, timeout=_remaining_semantic_timeout(deadline))
            if not self._response_adapter_states_are_current(states):
                return _semantic_locations_not_ready("semantic target generation changed")
            second = self._classify_semantic_response(second_raw)
            if isinstance(second, ErrorEnvelope):
                return second
            if location_filter is not None:
                second = tuple(item for item in second if location_filter(item))
            if any(item.adapter is not source_adapter for item in second):
                return _semantic_locations_not_ready("semantic target crossed adapter family")
            if _canonical_semantic_locations(first) != _canonical_semantic_locations(second):
                return _semantic_locations_not_ready("semantic target locations changed")
            if not self._response_adapter_states_are_current(states):
                return _semantic_locations_not_ready("semantic target generation changed")
            return _ResponseOwnedSemanticLocations(second, bound, prepared_targets, states, dispatch_digests)

        owned = source_adapter.submit_read(transaction).result(timeout=self._future_timeout)
        if isinstance(owned, _ResponseOwnedSemanticLocations):
            # Bound targets are workspace paths only; read-only external targets
            # are never opened in the server and are witnessed by their bytes
            # alone.  Both are recorded on the calling thread, after the adapter
            # transaction has returned its authoritative second response.
            for bound_target in (*owned.preparation_targets, *owned.targets):
                self._record_read_witness(
                    str(bound_target.path.relative_to(self.identity.root)),
                    bound_target.snapshot.raw_bytes,
                )
            unwitnessed = self._witness_external_targets(owned)
            if unwitnessed is not None:
                return unwitnessed
        return owned

    def _prepare_typescript_semantic_owner_with_client(
        self,
        source_adapter: RuntimeAdapter,
        client: AdapterClient,
        method: str,
        params: Mapping[str, object],
        *,
        source_document: DocumentSymbolInput | None,
        deadline: float,
    ) -> tuple[_BoundSemanticTarget, ...] | ErrorEnvelope:
        """Open a TypeScript semantic owner before the authoritative query.

        The locked TypeScript language server can resolve a cold imported name
        only to its local import alias until the target document is open.  Test
        order previously hid that behavior: an unrelated overview happened to
        open the owner first.  ``typeDefinition`` can identify the owner without
        parsing imports, so use it only as a bounded preparation hint for cold
        definition/reference queries.  The public result still comes from two
        matching authoritative definition/reference responses.

        Unavailable, malformed, untrusted, or external hints are ignored because
        this internal hint is not a public capability contract.  Once it names a
        trusted workspace target, however, opening that target is required; an
        unstable target therefore fails typed instead of allowing a known-local
        alias/partial-reference result to escape.
        """

        if (
            source_document is None
            or method not in {"textDocument/definition", "textDocument/references"}
            or source_adapter.snapshot().name != LanguageFamily.TYPESCRIPT.value
        ):
            return ()
        try:
            raw = client.request(
                "textDocument/typeDefinition",
                params,
                timeout=_remaining_semantic_timeout(deadline),
            )
        except LspResponseError:
            # This is an optional warm-up hint, not the authoritative public
            # operation.  Any ordinary response error leaves the definition or
            # references request to establish its own typed outcome.
            return ()
        classified = self._classify_semantic_response(raw)
        if isinstance(classified, ErrorEnvelope):
            return ()
        workspace_targets = tuple(
            item
            for item in classified
            if item.semantic.kind is LocationKind.WORKSPACE and item.adapter is source_adapter
        )
        if not workspace_targets:
            return ()
        return self._bind_semantic_targets_with_client(
            source_adapter,
            client,
            workspace_targets,
            capture_reference_documents=False,
            capture_target_symbols=False,
            source_document=source_document,
            deadline=deadline,
        )

    def _witness_external_targets(self, owned: _ResponseOwnedSemanticLocations) -> ErrorEnvelope | None:
        """Own the exact bytes of every trusted external target this response names.

        A read-only external target is rendered as the server's raw LSP range,
        so the range is only meaningful while the file it was computed over is
        unchanged.  The identity observed before the authoritative request and
        the identity observed here, after it, are both retained: a later
        agreeing observation must not conceal an earlier differing one, exactly
        as for a workspace snapshot.  Both observations use the guarded stable
        digest a trust inventory uses, over exactly these bounded, deduplicated
        paths—no external tree is walked and no document is opened.  A path that
        is missing, linked, or unstable yields no identity at all, which leaves
        the raw range unwitnessed and fails typed rather than returning it.
        """

        observed = _guarded_external_digests(_external_target_paths(owned.locations))
        if isinstance(observed, ErrorEnvelope):
            return observed
        for path, digest in (*owned.external_digests, *observed):
            self._record_external_read_witness(path, digest)
        return None

    def _classify_semantic_response(
        self,
        raw_locations: object,
    ) -> tuple[_ClassifiedSemanticLocation, ...] | ErrorEnvelope:
        locations = _raw_location_mappings(raw_locations)
        if locations is None:
            return error(ErrorCode.INVALID_INPUT, details={"field": "semantic_locations"})
        classified: list[_ClassifiedSemanticLocation] = []
        try:
            with self._state_lock:
                adapters = tuple(self._adapters.values())
            for raw in locations:
                location = _location_from_raw(raw)
                semantic = self._classify_semantic_location(location.uri)
                target_adapter = _unique_routing_adapter(adapters, semantic.path)
                classified.append(_ClassifiedSemanticLocation(raw, location, semantic, target_adapter))
        except (OSError, PositionError, TypeError, ValueError):
            return error(ErrorCode.UNTRUSTED_ROOT, details={"field": "semantic_location"})
        return tuple(classified)

    def _bind_semantic_targets_with_client(
        self,
        adapter: RuntimeAdapter,
        client: AdapterClient,
        locations: Sequence[_ClassifiedSemanticLocation],
        *,
        capture_reference_documents: bool,
        capture_target_symbols: bool,
        source_document: DocumentSymbolInput | None,
        deadline: float,
    ) -> tuple[_BoundSemanticTarget, ...] | ErrorEnvelope:
        unique: dict[Path, _ClassifiedSemanticLocation] = {}
        for location in locations:
            prior = unique.setdefault(location.semantic.path, location)
            if prior.adapter is not location.adapter or prior.semantic.kind is not location.semantic.kind:
                return error(ErrorCode.UNTRUSTED_ROOT, details={"field": "semantic_location"})
        # Bound the complete canonical target set before separating workspace
        # from external paths or reading/opening any target snapshot.  External
        # targets consume the same response-owned evidence budget even though
        # they are never materialized for workspace rendering.
        if len(unique) > MAX_RESPONSE_OWNED_TARGETS:
            ordered_paths = tuple(sorted(str(path) for path in unique))
            return error(
                ErrorCode.UNSUPPORTED,
                details={
                    "reason": "semantic target set exceeds snapshot bound",
                    "paths": ordered_paths[:MAX_RESPONSE_OWNED_TARGETS],
                    "total": len(ordered_paths),
                    "omitted": len(ordered_paths) - MAX_RESPONSE_OWNED_TARGETS,
                },
            )
        bound: list[_BoundSemanticTarget] = []
        source_path = _file_uri_path(source_document.uri) if source_document is not None else None
        try:
            for path, item in sorted(unique.items(), key=lambda pair: str(pair[0])):
                if item.semantic.kind is LocationKind.READ_ONLY_EXTERNAL:
                    continue
                _remaining_semantic_timeout(deadline)
                relative_path = str(path.relative_to(self.identity.root))
                if source_document is not None and path == source_path:
                    snapshot = source_document.snapshot
                    position_encoding = source_document.position_encoding
                    raw_symbols = source_document.raw_symbols
                    target = None
                else:
                    snapshot = FileSnapshot.from_bytes(path.read_bytes())
                    with self._state_lock:
                        version = self._versions.get(relative_path, 0) + 1
                        self._versions[relative_path] = version
                    target = adapter.open_snapshot_document_with_client(
                        client,
                        absolute_path=path,
                        relative_path=relative_path,
                        uri=path.as_uri(),
                        version=version,
                        text=snapshot.text,
                    )
                    position_encoding = adapter.snapshot().position_encoding
                    raw_symbols = None
                reference_document = None
                symbol_document = None
                if capture_reference_documents or capture_target_symbols:
                    family = _family_of(relative_path)
                    if family is None:
                        raise ValueError("semantic target has no language family")
                    if target is not None:
                        capture = _DocumentSymbolCapture(capture_selection_ranges=family is LanguageFamily.TYPESCRIPT)
                        if not capture.observe(client, target, timeout=_remaining_semantic_timeout(deadline)):
                            return error(ErrorCode.INVALID_INPUT, details={"field": "semantic_target_symbols"})
                        raw_symbols = capture.raw_symbols
                        if family is LanguageFamily.PYTHON:
                            recovered = PyrightFacts.locked().recover_assignment_document_symbols(
                                raw_symbols,
                                snapshot=snapshot,
                                position_encoding=position_encoding,
                            )
                            raw_symbols = recovered.raw_symbols
                        else:
                            recovered = recover_typescript_top_level_variable_symbols(
                                raw_symbols,
                                selection_ranges=capture.selection_ranges,
                                snapshot=snapshot,
                                position_encoding=position_encoding,
                            )
                            raw_symbols = recovered.raw_symbols
                    if capture_reference_documents:
                        reference_document = ReferenceDocumentInput(
                            path.as_uri(),
                            snapshot,
                            raw_symbols,
                            position_encoding,
                        )
                    if capture_target_symbols:
                        symbol_document = DocumentSymbolInput(
                            relative_path,
                            path.as_uri(),
                            snapshot,
                            raw_symbols,
                            position_encoding,
                        )
                bound.append(
                    _BoundSemanticTarget(
                        path,
                        item.adapter,
                        snapshot,
                        position_encoding,
                        reference_document,
                        symbol_document,
                    )
                )
        except TimeoutError:
            return error(
                ErrorCode.TIMED_OUT,
                retry=RetryMetadata(True),
                details={"field": "semantic_location"},
            )
        except WorkspaceRuntimeError as caught:
            return _runtime_error_envelope(caught, field="semantic_location")
        except (OSError, PositionError, TypeError, ValueError):
            return _semantic_locations_not_ready("verified target snapshot unavailable")
        return tuple(bound)

    def _response_adapter_states_are_current(self, states: Sequence[_ResponseAdapterState]) -> bool:
        with self._state_lock:
            active = tuple(self._adapters.values())
        return all(
            any(adapter is state.adapter for adapter in active)
            and _adapter_response_identity(state.adapter.snapshot()) == state.identity
            for state in states
        )

    def find_references(self, request: ReferenceRequest) -> ReferenceQueryResult | ErrorEnvelope:
        """Compatibility seam for callers that do not render reference content."""

        owned = self._find_response_owned_references(request)
        return owned if isinstance(owned, ErrorEnvelope) else owned[0]

    def _find_response_owned_references(
        self,
        request: ReferenceRequest,
        source_document: DocumentSymbolInput | None = None,
        source_identity: AdapterResponseIdentity | None = None,
    ) -> tuple[ReferenceQueryResult, _ResponseOwnedSemanticLocations] | ErrorEnvelope:
        """Dispatch references and retain the exact target documents for rendering."""

        try:
            family, adapter = self._route(request.relative_path)
            snapshot = adapter.snapshot()
            if not snapshot.derived_tools.find_referencing_symbols:
                return error(
                    ErrorCode.UNSUPPORTED,
                    details={"operation": "find_referencing_symbols"},
                    workspace=request.workspace,
                    adapter=request.adapter,
                    generations=request.generations,
                )
            if snapshot.phase is AdapterPhase.COOLDOWN:
                return error(
                    ErrorCode.COOLDOWN,
                    retry=RetryMetadata(True, retry_after_seconds=snapshot.crash.cooldown_remaining_seconds),
                    workspace=request.workspace,
                    adapter=request.adapter,
                    generations=request.generations,
                )
            if snapshot.phase in {
                AdapterPhase.COLD,
                AdapterPhase.STARTING,
                AdapterPhase.GLOBAL_WARMING,
                AdapterPhase.DEGRADED,
                AdapterPhase.STOPPING,
            }:
                return error(
                    ErrorCode.NOT_READY,
                    retry=RetryMetadata(True, retry_after_seconds=0.1),
                    workspace=request.workspace,
                    adapter=request.adapter,
                    generations=request.generations,
                )
            if request.adapter != _adapter_metadata(family) or (
                request.generations is None
                or request.generations.trust != snapshot.generations.trust
                or request.generations.program != snapshot.generations.program
            ):
                return error(
                    ErrorCode.NOT_READY,
                    retry=RetryMetadata(True, retry_after_seconds=0.1),
                    details={"reason": "reference dispatch generation changed"},
                    workspace=request.workspace,
                    adapter=request.adapter,
                    generations=request.generations,
                )
            with self._state_lock:
                projection = self._projections.get(family)
                active = self._adapters.get(family)
            if projection is None or active is not adapter:
                return error(
                    ErrorCode.NOT_READY,
                    retry=RetryMetadata(True, retry_after_seconds=0.1),
                    details={"reason": "reference adapter changed before dispatch"},
                    workspace=request.workspace,
                    adapter=request.adapter,
                    generations=request.generations,
                )
            if not projection.compatible:
                return error(
                    ErrorCode.SCOPE_INCOMPATIBLE,
                    details={"paths": tuple(item.path for item in projection.configured_program_outside_trust)},
                    workspace=request.workspace,
                    adapter=request.adapter,
                    generations=request.generations,
                )
            coverage = _reference_coverage(family, projection)
            response = self._stabilize_semantic_locations(
                adapter,
                "textDocument/references",
                {
                    "textDocument": {
                        "uri": (self.identity.root / request.relative_path).resolve(strict=True).as_uri()
                    },
                    "position": {"line": request.position.line, "character": request.position.character},
                    "context": {"includeDeclaration": True},
                },
                capture_reference_documents=True,
                source_document=source_document,
                source_identity=source_identity,
                location_filter=lambda item: _reference_location_is_in_configured_program(
                    item,
                    self.identity.root,
                    projection.configured_program.paths,
                ),
            )
            if isinstance(response, ErrorEnvelope):
                return _with_reference_context(response, request)
            with self._state_lock:
                current_projection = self._projections.get(family)
                current_adapter = self._adapters.get(family)
            if (
                current_adapter is not adapter
                or current_projection is not projection
                or not self._response_adapter_states_are_current(response.adapter_states)
            ):
                return error(
                    ErrorCode.NOT_READY,
                    retry=RetryMetadata(True, retry_after_seconds=0.1),
                    details={"reason": "reference configured-program generation changed"},
                    workspace=request.workspace,
                    adapter=request.adapter,
                    generations=request.generations,
                )
            return ReferenceQueryResult(tuple(item.location for item in response.locations), coverage), response
        except TimeoutError:
            return error(
                ErrorCode.TIMED_OUT,
                retry=RetryMetadata(True),
                workspace=request.workspace,
                adapter=request.adapter,
                generations=request.generations,
            )
        except WorkspaceRuntimeError as caught:
            try:
                code = ErrorCode(caught.code)
            except ValueError:
                code = ErrorCode.UNSUPPORTED
            return error(
                code,
                retry=(
                    RetryMetadata(True) if code in {ErrorCode.NOT_READY, ErrorCode.BUSY, ErrorCode.TIMED_OUT} else None
                ),
                details={"paths": caught.paths} if caught.paths else {},
                workspace=request.workspace,
                adapter=request.adapter,
                generations=request.generations,
            )
        except (OSError, TypeError, ValueError):
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
        return _semantic_locations_not_ready(
            "response-owned reference snapshot required",
            paths=(target.display_path,),
        )

    def load_diagnostics(self, relative_path: str, *, timeout_seconds: float) -> DiagnosticDocumentInput:
        try:
            document, target, family, adapter = self._load_document(relative_path, for_diagnostics=True)
        except LspResponseError as error:
            # In particular, an unversioned close-drain barrier may reject its
            # no-op request.  The recorded marker remains for a same-process
            # retry; expose that state as a retryable semantic readiness error
            # instead of allowing callers to mistake it for a clean result.
            raise WorkspaceRuntimeError(
                RuntimeErrorCode.NOT_READY,
                "diagnostics admission response was not ready for retry",
            ) from error
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
        cancel = getattr(adapter, "cancel_diagnostics_target", None)
        if callable(cancel):
            # Stop this synchronous waiter without discarding the current
            # document's publication owner.  A late matching push remains
            # eligible for caching and can satisfy a retry.
            cancel(target)
            # If publication won between the loop's last sample and
            # cancellation, its handler removed the owner first.  Re-sample
            # after cancellation so that accepted result is not reported as a
            # retryable false timeout.
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
            # Reserve a unique version before dispatch so a concurrent normal
            # document load cannot reuse the candidate.  A diagnostics reuse
            # rolls this reservation back below when no document changed.
            self._versions[normalized] = version
        uri = absolute.as_uri()
        capture = _DocumentSymbolCapture(capture_selection_ranges=family is LanguageFamily.TYPESCRIPT)
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
        if for_diagnostics:
            with self._state_lock:
                if self._versions.get(normalized) == version and target.version != version:
                    self._versions[normalized] = target.version
        adapter_status = adapter.snapshot()
        generations = adapter_status.generations
        raw_symbols = capture.raw_symbols
        body_completeness = None
        if family is LanguageFamily.PYTHON:
            recovered = PyrightFacts.locked().recover_assignment_document_symbols(
                raw_symbols,
                snapshot=snapshot,
                position_encoding=adapter_status.position_encoding,
            )
            raw_symbols = recovered.raw_symbols
            body_completeness = recovered.body_incomplete_reason
        elif family is LanguageFamily.TYPESCRIPT:
            recovered = recover_typescript_top_level_variable_symbols(
                raw_symbols,
                selection_ranges=capture.selection_ranges,
                snapshot=snapshot,
                position_encoding=adapter_status.position_encoding,
            )
            raw_symbols = recovered.raw_symbols
            body_completeness = recovered.body_incomplete_reason
        document = DocumentSymbolInput(
            relative_path=normalized,
            uri=uri,
            snapshot=snapshot,
            raw_symbols=raw_symbols,
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
            body_completeness=body_completeness,
        )
        # Recorded here, on the calling thread, once the executor future has
        # already produced the exact bytes this response owns.
        self._record_read_witness(normalized, snapshot.raw_bytes)
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
            "freshness": self._freshness.last_scan.as_status(),
            "adapters": adapters,
            "unavailable_language_families": {
                family.value: _unavailable_family_status(
                    self._family_errors[family],
                    self._projections.get(family),
                )
                for family in sorted(self._family_errors)
            },
            "skipped_language_families": tuple(
                family.value
                for family in LanguageFamily
                if family not in self._projections and family not in self._family_errors
            ),
            "executor": {
                "queue_size": executor.queue_size,
                "queue_capacity": executor.queue_capacity,
                "active": executor.active,
                "stopping": executor.stopping,
            },
            "stopping": self._stopping,
            "stopped": self._stopped,
        }

    def stop(self) -> None:
        """Settle every owned cleanup before publishing the stopped state."""

        with self._stop_lock:
            with self._state_lock:
                if self._stopped:
                    return
                self._stopping = True
                adapters = tuple(self._adapters.values())
                pending_retirements = tuple(self._pending_retirements.values())
                pending_restarts = tuple(self._pending_restarts.values())

            failures: list[BaseException] = []
            responsibilities: dict[int, Future[AdapterSnapshot]] = {}
            for adapter in adapters:
                adapter_key = id(adapter)
                with self._state_lock:
                    future = self._shutdown_futures.get(adapter_key)
                if future is None or _stop_future_needs_retry(future):
                    try:
                        future = adapter.stop()
                    except BaseException as error:
                        failures.append(error)
                        continue
                    with self._state_lock:
                        self._shutdown_futures[adapter_key] = future
                responsibilities[id(future)] = future
            for retirement in pending_retirements:
                retirement, failure = self._start_pending_retirement(retirement)
                if failure is not None:
                    failures.append(failure)
                    continue
                if retirement is not None and retirement.stop_future is not None:
                    responsibilities[id(retirement.stop_future)] = retirement.stop_future
            for pending in pending_restarts:
                pending, failure = self._start_pending_stop(pending)
                if failure is not None:
                    failures.append(failure)
                    continue
                if pending is not None and pending.stop_future is not None:
                    responsibilities[id(pending.stop_future)] = pending.stop_future

            # Admission failures leave the exact adapters/pending restarts in
            # their ownership maps.  A later stop retries instead of silently
            # succeeding because a stopped bit was published too early.
            if failures:
                raise RuntimeError(f"workspace runtime stop failed: {failures[0]}") from failures[0]

            for future in tuple(responsibilities.values()):
                try:
                    future.result(timeout=self._future_timeout)
                except BaseException as error:
                    failures.append(error)
            if failures:
                raise RuntimeError(f"workspace runtime stop failed: {failures[0]}") from failures[0]

            try:
                self._executor.close(cancel_queued=True, timeout=min(self._future_timeout, 5.0))
            except BaseException as error:
                raise RuntimeError(f"workspace runtime stop failed: {error}") from error
            with self._state_lock:
                self._pending_retirements.clear()
                self._pending_restarts.clear()
                self._shutdown_futures.clear()
                self._adapters.clear()
                self._stopped = True

    def _require_running(self) -> None:
        with self._state_lock:
            if self._stopping or self._stopped:
                raise WorkspaceRuntimeError(RuntimeErrorCode.STOPPED, "workspace runtime is stopped")


class _ResponseOwnedDeclarationProvider:
    """Call-local bridge that retains the exact semantic source snapshot."""

    def __init__(self, runtime: WorkspaceRuntime) -> None:
        self._runtime = runtime
        self._source: DocumentSymbolInput | None = None
        self._source_identity: AdapterResponseIdentity | None = None

    def load_semantic_document(self, relative_path: str) -> SemanticDocumentInput | ErrorEnvelope:
        loaded = self._runtime.load_semantic_document(relative_path)
        if isinstance(loaded, ErrorEnvelope):
            return loaded
        self._source = loaded.document
        adapter = self._runtime._adapter_for_workspace_uri(loaded.document.uri)
        self._source_identity = _adapter_response_identity(adapter.snapshot())
        return loaded

    def request_locations(
        self,
        method: str,
        *,
        document_uri: str,
        position: LspPosition,
        capture_target_symbols: bool = False,
    ) -> object:
        source = self._source
        source_identity = self._source_identity
        if source is None or source_identity is None or source.uri != document_uri:
            return error(ErrorCode.INVALID_INPUT, details={"field": "semantic_source_owner"})
        return self._runtime._request_locations(
            method,
            document_uri=document_uri,
            position=position,
            source_document=source,
            source_identity=source_identity,
            capture_target_symbols=capture_target_symbols,
        )

    def normalize_and_classify_locations(
        self,
        raw_locations: object,
        *,
        include_body: bool,
        include_info: bool,
    ) -> Sequence[ClassifiedLocationInput] | ErrorEnvelope:
        return self._runtime.normalize_and_classify_locations(
            raw_locations,
            include_body=include_body,
            include_info=include_info,
        )


class _ResponseOwnedReferenceProvider:
    """Call-local bridge that never reloads a target after references return."""

    def __init__(self, runtime: WorkspaceRuntime, source_document: DocumentSymbolInput) -> None:
        self._runtime = runtime
        self._source_document = source_document
        source_adapter = runtime._adapter_for_workspace_uri(source_document.uri)
        self._source_identity = _adapter_response_identity(source_adapter.snapshot())
        self._request: ReferenceRequest | None = None
        self._response: _ResponseOwnedSemanticLocations | None = None

    def find_references(self, request: ReferenceRequest) -> ReferenceQueryResult | ErrorEnvelope:
        owned = self._runtime._find_response_owned_references(
            request,
            self._source_document,
            self._source_identity,
        )
        if isinstance(owned, ErrorEnvelope):
            return owned
        query, response = owned
        self._request = request
        self._response = response
        return query

    def classify_reference_location(self, location: Location) -> ReferenceTarget | ErrorEnvelope:
        return self._runtime.classify_reference_location(location)

    def load_reference_document(
        self, target: ReferenceTarget
    ) -> ReferenceDocumentInput | RawReferenceDocumentInput | ErrorEnvelope:
        request = self._request
        response = self._response
        if request is None or response is None:
            return error(ErrorCode.INVALID_INPUT, details={"field": "reference_response_owner"})
        if not self._runtime._response_adapter_states_are_current(response.adapter_states):
            return _reference_not_ready(request, "semantic target generation changed")
        if target.read_only_external:
            if len(response.adapter_states) != 1:
                return _reference_not_ready(request, "semantic target generation changed")
            return RawReferenceDocumentInput(
                target.location.uri,
                response.adapter_states[0].identity[-1],
            )
        bound = next((item for item in response.targets if str(item.path) == target.key), None)
        if bound is None or bound.reference_document is None:
            return _reference_not_ready(
                request,
                "verified target snapshot unavailable",
                paths=(target.display_path,),
            )
        document = bound.reference_document
        return ReferenceDocumentInput(
            target.location.uri,
            document.snapshot,
            document.raw_symbols,
            document.position_encoding,
            document.recover_containment,
        )


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
                self.seed.document.snapshot,
                self.seed.document.position_encoding,
                body_completeness=self.seed.document.body_completeness,
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
            document.snapshot,
            document.position_encoding,
            body_completeness=document.body_completeness,
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
        # The first authorization ran before this worker owned the workspace
        # operation lock, so the guarded component walk is repeated here: a path
        # swapped for a symlink in between must still fail closed.
        if self._runtime.authorize_edit(relative_path) != self._authorized.path:
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
        if raw is not None and (
            not isinstance(raw, Sequence)
            or isinstance(raw, str | bytes)
            or not all(isinstance(symbol, Mapping) for symbol in raw)
        ):
            raise TypeError("document-symbol response is not a sequence")
        self._target = opened
        status = self._adapter.snapshot()
        raw_symbols = (
            tuple(cast(Mapping[str, Any], symbol) for symbol in raw)
            if isinstance(raw, Sequence)
            else None
        )
        body_completeness = None
        metadata = _adapter_metadata_for_snapshot(status)
        if metadata.language == LanguageFamily.PYTHON.value:
            recovered = PyrightFacts.locked().recover_assignment_document_symbols(
                raw_symbols,
                snapshot=snapshot,
                position_encoding=status.position_encoding,
            )
            # Editing cannot safely use an identifier-only assignment range.
            # Keep the ordinary read-only lookup contract elsewhere, but do not
            # expose an unresolved assignment as a replacement candidate.
            raw_symbols = tuple(
                symbol
                for symbol in recovered.raw_symbols
                if recovered.body_incomplete_reason(symbol) is None
            )
            body_completeness = recovered.body_incomplete_reason
        elif metadata.language == LanguageFamily.TYPESCRIPT.value:
            positions = assignment_recovery_positions(raw_symbols)
            selection_ranges: tuple[Mapping[str, Any], ...] | None = None
            if positions:
                try:
                    raw_ranges = self._client.request(
                        "textDocument/selectionRange",
                        {
                            "textDocument": {"uri": opened.uri},
                            "positions": list(positions),
                        },
                        timeout=self._runtime._future_timeout,
                    )
                except LspResponseError:
                    raw_ranges = None
                if (
                    isinstance(raw_ranges, Sequence)
                    and not isinstance(raw_ranges, str | bytes)
                    and all(isinstance(item, Mapping) for item in raw_ranges)
                ):
                    selection_ranges = tuple(cast(Mapping[str, Any], item) for item in raw_ranges)
            recovered = recover_typescript_top_level_variable_symbols(
                raw_symbols,
                selection_ranges=selection_ranges,
                snapshot=snapshot,
                position_encoding=status.position_encoding,
            )
            raw_symbols = tuple(
                symbol
                for symbol in recovered.raw_symbols
                if recovered.body_incomplete_reason(symbol) is None
            )
            body_completeness = recovered.body_incomplete_reason
        return DocumentSymbolInput(
            target.relative_path,
            opened.uri,
            snapshot,
            raw_symbols,
            status.position_encoding,
            _workspace_metadata(self._runtime.identity),
            metadata,
            _path_generations(status),
            body_completeness=body_completeness,
        )

    def notify_replaced(self, notification: ReplacementNotification) -> NotificationResult:
        if self._target is None:
            raise ValueError("edit notifier ran before document-symbol resolution")
        return self._adapter.notify_edit_with_client(self._client, self._target, notification)


def _uncertain_edit(target: AuthorizedEdit, commit: EditCommit, stage: str) -> ErrorEnvelope:
    """Report a possibly-installed edit that must never be replayed."""

    return error(
        ErrorCode.UNCERTAIN,
        retry=RetryMetadata(retryable=False),
        details={
            "relative_path": target.relative_path,
            "commit_state": commit.state.value,
            "current_hash": safe_current_hash(target),
            "uncertain_stage": stage,
            "requires_current_reread": True,
        },
        workspace=target.workspace,
    )


def _family_paths(inventory: TrustInventory) -> dict[LanguageFamily, tuple[str, ...]]:
    return {family: _paths_for(inventory.paths, extensions) for family, extensions in _FAMILY_EXTENSIONS.items()}


def _family_of(relative_path: str) -> LanguageFamily | None:
    suffix = PurePosixPath(relative_path).suffix.lower()
    for family, extensions in _FAMILY_EXTENSIONS.items():
        if suffix in extensions:
            return family
    return None


def _native_config_candidates(
    inventory: TrustInventory, projections: Mapping[LanguageFamily, ScopeProjection]
) -> frozenset[str]:
    """Stat every native config that could govern a trusted source path.

    The trust inventory deliberately excludes non-source files.  Derive the
    bounded watch set from each trusted source's directory ancestry instead of
    assuming that native configuration only lives at the workspace root.  An
    absent candidate is retained in the state map, so creating a nearer config
    is visible on the next scan without a background watcher.
    """

    candidates: set[str] = set()
    for family, paths in _family_paths(inventory).items():
        names = _NATIVE_CONFIG_WATCH[family]
        for source in paths:
            directory = PurePosixPath(source).parent
            while directory != PurePosixPath("."):
                candidates.update((directory / name).as_posix() for name in names)
                directory = directory.parent
            candidates.update(names)
        projection = projections.get(family)
        if projection is not None and projection.selected_config_path:
            candidates.add(projection.selected_config_path)
    return frozenset(candidates)


def _projection_error(family: LanguageFamily, projection: ScopeProjection) -> WorkspaceRuntimeError:
    return WorkspaceRuntimeError(
        RuntimeErrorCode.SCOPE_INCOMPATIBLE,
        f"{family.value} configured program contains paths outside trust",
        paths=tuple(item.path for item in projection.configured_program_outside_trust),
    )


def _committed_digest(identity: ContentIdentity | None) -> str | None:
    """The guarded full-byte digest a completed scan committed for one path."""

    return None if identity is None else identity[-1]


def _fresh_read_change_evidence(
    admitted: _FreshnessAdmission,
    final: _FreshnessAdmission,
    witnesses: Mapping[str, set[str]],
    external_witnesses: Mapping[Path, set[str]],
) -> tuple[str, ...] | None:
    """Bounded, deduplicated paths proving the workspace moved under one attempt.

    ``None`` means the read is linearized at the final guarded observation.  A
    version change reports a commit by this attempt's own postflight or by
    another call that consumed the same change first, and is decisive even when
    it names no path here; a witness mismatch reports bytes the response owns
    that the final guarded identity no longer agrees with.  A path whose
    response-owned snapshots disagree among themselves is already mismatched:
    at most one of them can equal the final identity.  Evidence is keyed by
    path, so a path with several disagreeing snapshots is still named once.
    """

    mismatched = tuple(
        sorted(
            path
            for path, digests in witnesses.items()
            if any(digest != final.digests.get(path) for digest in digests)
        )
    )
    external_mismatched = _external_witness_mismatches(external_witnesses)
    if final.version == admitted.version and not mismatched and not external_mismatched:
        return None
    scanned = (*final.scan.created, *final.scan.changed, *final.scan.deleted, *final.scan.config_changed)
    return tuple(sorted({*scanned, *mismatched, *external_mismatched}))


def _result_is_source_derived(result: ToolEnvelope, witnesses: _ReadAttemptWitnesses) -> bool:
    """Whether this result states something about response-owned source bytes.

    A success always does.  A missing or ambiguous symbol always does too, even
    when no exact byte witness was retained—a global query that matched nothing
    is still an answer about what the workspace contains.  A measured
    ``minimum_required`` is also source-derived by construction.  Other failures
    require witnessed bytes and a reason that reports what those bytes contain:
    an unresolvable assignment range, a candidate whose snapshot no longer maps,
    a target snapshot or external byte witness that could not be established, a
    location set that moved, or a response-owned target set that overran its
    bound and names the paths it sampled.  A failure that reports the adapter's own
    condition—cold, cooling, unsupported, busy, timed out, identity or
    generation moved—keeps the authority of its single preflight, because no
    concurrent source write can make it wrong.
    """

    if isinstance(result, SuccessEnvelope):
        return True
    if result.code in {ErrorCode.SYMBOL_NOT_FOUND, ErrorCode.AMBIGUOUS_SYMBOL}:
        return True
    # A budget failure that states a minimum is measuring selected content, so
    # it is source-derived by construction even when no exact document snapshot
    # was retained.  An ordinary invalid bound carries no such measurement.
    if "minimum_required" in result.details:
        return True
    if result.details.get("reason") == SOURCE_SNAPSHOT_UNAVAILABLE_REASON:
        return True
    if not witnesses.owns_source:
        return False
    reason = result.details.get("reason")
    # Details are JSON-shaped, so a reason is not guaranteed to be a hashable
    # string; an unrecognized shape is simply not one of these reasons.
    return isinstance(reason, str) and reason in SOURCE_DERIVED_ERROR_REASONS


def _source_exception_path(caught: OSError, root: Path) -> str | None:
    """Return one lexical in-workspace operand named by a source-read failure.

    The exception boundary must not relabel transport or executable failures as
    source races.  File-system exceptions produced while acquiring an operand
    carry ``filename`` (or ``filename2``); only a path lexically below the
    already-resolved workspace root enters the fresh-read postflight.  No
    ``resolve`` call is allowed here because the path may be missing or racing.
    """

    for raw_path in (caught.filename, caught.filename2):
        if not isinstance(raw_path, str | bytes):
            continue
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.absolute()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        normalized = relative.as_posix()
        if normalized and normalized != "." and ".." not in PurePosixPath(normalized).parts:
            return normalized
    return None


def _external_target_paths(locations: Sequence[_ClassifiedSemanticLocation]) -> tuple[Path, ...]:
    """The deduplicated trusted external paths one response names."""

    return tuple(
        sorted({item.semantic.path for item in locations if item.semantic.kind is LocationKind.READ_ONLY_EXTERNAL})
    )


def _guarded_external_digests(paths: Sequence[Path]) -> tuple[tuple[Path, str], ...] | ErrorEnvelope:
    """One guarded stable byte identity per named external path, or a typed failure."""

    observed: list[tuple[Path, str]] = []
    for path in paths:
        digest = observe_file_digest(path)
        if digest is None:
            return _semantic_locations_not_ready("external target bytes unavailable", paths=(str(path),))
        observed.append((path, digest))
    return tuple(observed)


def _external_witness_mismatches(witnesses: Mapping[Path, set[str]]) -> tuple[str, ...]:
    """Trusted external paths whose response-owned bytes no longer hold.

    A read-only external root is outside every workspace freshness scan and its
    targets are never opened in the language server, so the only final evidence
    that a response-owned external byte identity still holds is one guarded
    stable observation of exactly those bounded paths.  A path that is deleted,
    replaced by a link, or unstable yields no identity at all and is therefore
    reported as changed, which replays once and then fails retryably.
    """

    mismatched: list[str] = []
    for path, digests in witnesses.items():
        current = observe_file_digest(path)
        if current is None or any(digest != current for digest in digests):
            mismatched.append(str(path))
    return tuple(sorted(mismatched))


def _affected_families(membership_paths: Sequence[str], config_paths: Sequence[str]) -> frozenset[LanguageFamily]:
    """Reattribute only families whose membership or native config may have moved."""

    affected = {family for path in membership_paths if (family := _family_of(path)) is not None}
    for path in config_paths:
        name = PurePosixPath(path).name
        affected.update(family for family, names in _NATIVE_CONFIG_WATCH.items() if name in names)
    return frozenset(affected)


def _fixed_inventory(inventory: TrustInventory) -> InventoryFactory:
    def factory(_identity: WorkspaceIdentity) -> TrustInventory:
        return inventory

    return factory


def _workspace_metadata(identity: WorkspaceIdentity) -> WorkspaceMetadata:
    return WorkspaceMetadata(str(identity.root), identity.kind.value, str(identity.root))


def _adapter_metadata(family: LanguageFamily) -> AdapterMetadata:
    return AdapterMetadata("pyright" if family is LanguageFamily.PYTHON else "typescript", family.value)


def _reference_coverage(family: LanguageFamily, projection: ScopeProjection) -> ReferenceCoverage:
    """Render maintained projection evidence without discovering any paths."""

    uncovered = tuple(sorted(item.path for item in projection.trusted_not_in_configured_program))
    digest = hashlib.sha256("\0".join(uncovered).encode("utf-8", "surrogateescape")).hexdigest()
    sample = uncovered[:REFERENCE_COVERAGE_SAMPLE_LIMIT]
    return ReferenceCoverage(
        adapter=_adapter_metadata(family).name,
        language=family.value,
        scope_kind=projection.project_kind.value,
        configured_program_files=projection.configured_program.count,
        configured_program_digest=projection.configured_program.sha256,
        trusted_language_files=projection.trust_inventory.count,
        trusted_language_digest=projection.trust_inventory.sha256,
        uncovered_files=len(uncovered),
        uncovered_digest=digest,
        uncovered_sample=sample,
        uncovered_total=len(uncovered),
        uncovered_omitted=len(uncovered) - len(sample),
    )


def _reference_location_is_in_configured_program(
    location: _ClassifiedSemanticLocation,
    workspace_root: Path,
    configured_program_paths: Collection[str],
) -> bool:
    """Keep external targets and only configured-program workspace targets."""

    if location.semantic.read_only_external:
        return True
    try:
        relative_path = location.semantic.path.relative_to(workspace_root).as_posix()
    except ValueError:
        return False
    return relative_path in configured_program_paths


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


def _adapter_response_identity(
    snapshot: AdapterSnapshot,
) -> AdapterResponseIdentity:
    generations = snapshot.generations
    return (
        snapshot.runtime_token,
        snapshot.phase,
        snapshot.raw_providers,
        snapshot.derived_tools,
        generations.trust,
        generations.program,
        generations.document,
        generations.index,
        snapshot.position_encoding,
    )


def _adapter_replay_identity(snapshot: AdapterSnapshot) -> AdapterReplayIdentity:
    return _adapter_replay_identity_from_response(_adapter_response_identity(snapshot))


def _adapter_replay_identity_from_response(identity: AdapterResponseIdentity) -> AdapterReplayIdentity:
    runtime_token, phase, raw, derived, trust, program, _document, index, encoding = identity
    return runtime_token, phase, raw, derived, trust, program, index, encoding


def _response_adapter_state(adapter: RuntimeAdapter) -> _ResponseAdapterState:
    return _ResponseAdapterState(adapter, _adapter_response_identity(adapter.snapshot()))


def _response_owned_target_symbol(
    target: _BoundSemanticTarget,
    location: Location,
) -> NormalizedSymbol | None:
    """Resolve target metadata only from the response-owned symbol snapshot."""

    if target.symbol_document is None:
        return None
    try:
        document = DocumentNavigation.from_input(target.symbol_document)
    except (PositionError, TypeError, ValueError):
        return None
    symbols = tuple(symbol for root in document.symbols for symbol in root.iter_depth_first())
    exact = tuple(symbol for symbol in symbols if symbol.selection_range == location.range)
    if exact:
        return min(exact, key=lambda symbol: symbol.name_path)
    target_location = Location(document.uri, location.range, str(target.path))
    return containing_symbol(document.symbols, target_location)


def _semantic_document_is_current(adapter: RuntimeAdapter, document: DocumentSymbolInput) -> bool:
    snapshot = adapter.snapshot()
    generations = document.generations
    return (
        generations is not None
        and document.adapter == _adapter_metadata_for_snapshot(snapshot)
        and document.position_encoding is snapshot.position_encoding
        and generations.trust == snapshot.generations.trust
        and generations.program == snapshot.generations.program
        and generations.document == snapshot.generations.document
        and generations.index == snapshot.generations.index
    )


def _remaining_semantic_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _canonical_semantic_locations(locations: Sequence[_ClassifiedSemanticLocation]) -> tuple[str, ...]:
    canonical: list[str] = []
    for item in locations:
        raw_kind = item.raw.get("kind")
        canonical.append(
            json.dumps(
                {
                    "path": str(item.semantic.path),
                    "location_kind": item.semantic.kind.value,
                    "range": {
                        "start": {
                            "line": item.location.range.start.line,
                            "character": item.location.range.start.character,
                        },
                        "end": {
                            "line": item.location.range.end.line,
                            "character": item.location.range.end.character,
                        },
                    },
                    "kind": raw_kind if isinstance(raw_kind, int) and not isinstance(raw_kind, bool) else None,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return tuple(sorted(canonical))


def _semantic_locations_not_ready(reason: str, *, paths: tuple[str, ...] = ()) -> ErrorEnvelope:
    details: dict[str, object] = {"reason": reason}
    if paths:
        details["paths"] = paths
    return error(
        ErrorCode.NOT_READY,
        retry=RetryMetadata(True, retry_after_seconds=0.1),
        details=details,
    )


def _runtime_error_envelope(caught: WorkspaceRuntimeError, *, field: str) -> ErrorEnvelope:
    try:
        code = ErrorCode(caught.code)
    except ValueError:
        code = ErrorCode.UNSUPPORTED
    return error(
        code,
        retry=(RetryMetadata(True) if code in {ErrorCode.NOT_READY, ErrorCode.BUSY, ErrorCode.TIMED_OUT} else None),
        details={"paths": caught.paths} if caught.paths else {"field": field},
    )


def _with_reference_context(value: ErrorEnvelope, request: ReferenceRequest) -> ErrorEnvelope:
    return error(
        value.code,
        retry=value.retry,
        details=value.details,
        workspace=request.workspace,
        adapter=request.adapter,
        generations=request.generations,
    )


def _reference_not_ready(
    request: ReferenceRequest,
    reason: str,
    *,
    paths: tuple[str, ...] = (),
) -> ErrorEnvelope:
    return _with_reference_context(_semantic_locations_not_ready(reason, paths=paths), request)


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
            debug_reporter=context.debug_reporter,
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
        "trusted_not_in_configured_program": bounded_difference_status(projection.trusted_not_in_configured_program),
        "configured_program_outside_trust": bounded_difference_status(projection.configured_program_outside_trust),
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


def _unavailable_family_status(
    failure: WorkspaceRuntimeError,
    projection: ScopeProjection | None,
) -> Mapping[str, object]:
    status: dict[str, object] = {
        "error": {"code": failure.code, "paths": failure.paths},
        "scope_compatible": projection is not None and projection.compatible,
    }
    if projection is not None:
        status.update(
            {
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
                "trusted_not_in_configured_program": bounded_difference_status(
                    projection.trusted_not_in_configured_program
                ),
                "configured_program_outside_trust": bounded_difference_status(
                    projection.configured_program_outside_trust
                ),
            }
        )
    return status
