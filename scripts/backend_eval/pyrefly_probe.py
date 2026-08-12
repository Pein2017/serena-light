"""Locked-Pyrefly raw-protocol probe with service-owned configuration isolation."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, cast

from scripts.backend_eval.identity import capture_evaluator_identity
from scripts.backend_eval.manifests import (
    capture_root_manifest,
    default_corpus_requests,
    read_stable_source_text,
)
from scripts.backend_eval.models import (
    CAPABILITY_TASK_UTILITY_DEFERRED,
    CandidateProtocolOutcome,
    CapabilityEvidence,
    LifecycleEvidence,
    RootManifest,
    ServiceConfigIdentity,
)
from scripts.backend_eval.process import Deadline
from scripts.backend_eval.protocol import (
    BackendProtocolSpec,
    redacted_evidence_text,
    run_protocol_probe,
)
from scripts.backend_eval.runtime import CandidateRuntime
from scripts.backend_eval.source_binding import EVALUATION_OWNER_ROOT, HelperExpectation
from serena_light.lsp.adapter import EngineMetadata, RawLspProviders, read_only_client_request_handlers
from serena_light.lsp.client import CONTENT_MODIFIED, LspResponseError, SyncLspClient
from serena_light.lsp.normalize import NormalizationError, normalize_document_symbols, normalize_location
from serena_light.lsp.positions import PositionEncoding

__all__ = [
    "PyreflyWorkspaceMutation",
    "pyrefly_protocol_spec",
    "run_pyrefly_capability_probe",
]

# Frozen by the admitted candidate lock and independently confirmed from the locked executable
# with ``pyrefly --version``. Runtime paths and digests are never copied here: callers supply the
# strictly loaded CandidateRuntime that owns the executable and service configuration.
_PYREFLY_VERSION = "1.2.0"
_REQUEST_CANCELLED = -32800

_CAPABILITY_NAMES = (
    "definition",
    "document_symbols",
    "implementation",
    "references",
    "workspace_symbols",
)
_REQUIRED_ADVERTISEMENTS = frozenset(_CAPABILITY_NAMES)


class PyreflyWorkspaceMutation(RuntimeError):
    """Pyrefly changed an evaluated workspace between the bounded manifests."""

    def __init__(self, before_manifest: object, after_manifest: object) -> None:
        super().__init__("Pyrefly changed the evaluated workspace during the protocol probe")
        self.before_manifest = before_manifest
        self.after_manifest = after_manifest


@dataclass(frozen=True, slots=True)
class _ObservedCapability:
    name: str
    accepted: bool
    normalized_valid: bool
    notes: str
    error_code: int | None = None
    ready_elapsed: float | None = None


@dataclass(frozen=True, slots=True)
class _PyreflySessionResult:
    observations: tuple[_ObservedCapability, ...]
    document_close_error: str | None = None


def pyrefly_protocol_spec(
    runtime: CandidateRuntime,
    service_config: ServiceConfigIdentity,
    *,
    notification_handler: Callable[[str, Any], None] | None = None,
) -> BackendProtocolSpec:
    """Build Pyrefly's spec from one locked runtime and its exact external config."""

    expected_config = next(
        (identity for identity in runtime.service_configs if identity.backend == "pyrefly"),
        None,
    )
    if service_config != expected_config:
        raise ValueError("Pyrefly requires the service-owned Pyrefly configuration bound to its runtime")
    interpreter = _selected_ms_interpreter(runtime)
    initialization_options = _initialization_options(service_config, interpreter)

    def require_bound_runtime(candidate_runtime: CandidateRuntime) -> None:
        if candidate_runtime is not runtime:
            raise ValueError("Pyrefly protocol spec requires its exact caller-bound runtime")

    def build_command(candidate_runtime: CandidateRuntime) -> tuple[str, ...]:
        require_bound_runtime(candidate_runtime)
        return (
            str(runtime.pyrefly),
            "lsp",
            "--indexing-mode",
            "lazy-blocking",
            "--threads",
            "1",
            "--workspace-indexing-limit",
            "2000",
        )

    def engine(candidate_runtime: CandidateRuntime) -> EngineMetadata:
        require_bound_runtime(candidate_runtime)
        return _engine(runtime, interpreter)

    return BackendProtocolSpec(
        name="pyrefly",
        build_command=build_command,
        initialize_params=lambda workspace_root: _initialize_params(
            workspace_root,
            initialization_options,
        ),
        validate_initialize_params=lambda params: _validate_initialize_params(
            params,
            interpreter=interpreter,
            service_config=service_config,
        ),
        request_handlers=read_only_client_request_handlers(
            lambda params: _workspace_configuration(params, initialization_options)
        ),
        engine=engine,
        position_encoding=PositionEncoding.UTF16,
        diagnostics_mode="push",
        notification_handler=notification_handler,
    )


def run_pyrefly_capability_probe(
    runtime: CandidateRuntime,
    workspace_root: Path,
    target: Path,
    symbol_position: tuple[int, int],
    *,
    deadline: Deadline,
) -> CandidateProtocolOutcome:
    """Exercise Pyrefly's five providers and fail typed on any workspace change."""

    line, character = symbol_position
    if (
        isinstance(line, bool)
        or not isinstance(line, int)
        or line < 0
        or isinstance(character, bool)
        or not isinstance(character, int)
        or character < 0
    ):
        raise ValueError("Pyrefly probe symbol_position must contain two non-negative integers")
    if deadline.reserve <= 0:
        raise ValueError("Pyrefly probe requires a positive Deadline reserve for manifest proof")

    before_manifest = _capture_workspace_manifest(workspace_root, deadline)
    try:
        outcome = _run_capability_probe(
            runtime,
            workspace_root,
            target,
            line,
            character,
            deadline,
        )
    except BaseException as primary:
        try:
            after_manifest = _capture_workspace_manifest(workspace_root, deadline)
        except BaseException as after_error:
            primary.add_note(
                redacted_evidence_text(
                    "Pyrefly after-manifest capture also failed: "
                    f"{type(after_error).__name__}: {after_error}"
                )
            )
            raise primary from None
        if after_manifest != before_manifest:
            raise PyreflyWorkspaceMutation(before_manifest, after_manifest) from primary
        raise

    try:
        after_manifest = _capture_workspace_manifest(workspace_root, deadline)
    except BaseException as after_error:
        cast("Any", after_error).pyrefly_capability_outcome = outcome
        after_error.add_note(
            redacted_evidence_text(
                "Pyrefly capability outcome completed before after-manifest proof failed: "
                f"gate_disposition={outcome.gate_disposition}"
            )
        )
        raise
    if after_manifest != before_manifest:
        raise PyreflyWorkspaceMutation(before_manifest, after_manifest)
    return outcome


def _run_capability_probe(
    runtime: CandidateRuntime,
    workspace_root: Path,
    target: Path,
    line: int,
    character: int,
    deadline: Deadline,
) -> CandidateProtocolOutcome:
    service_config = next(
        (identity for identity in runtime.service_configs if identity.backend == "pyrefly"),
        None,
    )
    if service_config is None:
        raise ValueError("CandidateRuntime has no service-owned Pyrefly configuration")
    source = target if target.is_absolute() else workspace_root / target
    source_text = read_stable_source_text(workspace_root, source, deadline=deadline)
    source_uri = source.as_uri()
    started_elapsed = deadline.elapsed()
    push_diagnostics_observed = Event()

    def observe_notification(method: str, params: Any) -> None:
        if method != "textDocument/publishDiagnostics" or not isinstance(params, Mapping):
            return
        typed_params = cast("Mapping[str, object]", params)
        diagnostics = typed_params.get("diagnostics")
        if (
            typed_params.get("uri") == source_uri
            and isinstance(diagnostics, Sequence)
            and not isinstance(diagnostics, str | bytes)
        ):
            push_diagnostics_observed.set()

    spec = pyrefly_protocol_spec(
        runtime,
        service_config,
        notification_handler=observe_notification,
    )

    def session(
        client: SyncLspClient,
        providers: RawLspProviders,
    ) -> _PyreflySessionResult:
        client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": source_uri,
                    "languageId": "python",
                    "version": 1,
                    "text": source_text,
                }
            },
        )
        try:
            position_params = {
                "textDocument": {"uri": source_uri},
                "position": {"line": line, "character": character},
            }
            observations = (
                (
                    _observe_request(
                        client,
                        deadline,
                        "definition",
                        "textDocument/definition",
                        position_params,
                        _normalize_locations,
                    )
                    if providers.definition
                    else _unadvertised_provider("definition")
                ),
                (
                    _observe_request(
                        client,
                        deadline,
                        "references",
                        "textDocument/references",
                        {**position_params, "context": {"includeDeclaration": True}},
                        _normalize_locations,
                    )
                    if providers.references
                    else _unadvertised_provider("references")
                ),
                (
                    _observe_request(
                        client,
                        deadline,
                        "implementation",
                        "textDocument/implementation",
                        position_params,
                        _normalize_locations,
                    )
                    if providers.implementation
                    else _unadvertised_provider("implementation")
                ),
                (
                    _observe_request(
                        client,
                        deadline,
                        "document_symbols",
                        "textDocument/documentSymbol",
                        {"textDocument": {"uri": source_uri}},
                        lambda value: _normalize_document_symbol_result(value, source_uri),
                    )
                    if providers.document_symbols
                    else _unadvertised_provider("document_symbols")
                ),
                (
                    _observe_request(
                        client,
                        deadline,
                        "workspace_symbols",
                        "workspace/symbol",
                        {"query": source.stem},
                        _normalize_workspace_symbol_result,
                    )
                    if providers.workspace_symbols
                    else _unadvertised_provider("workspace_symbols")
                ),
            )
        except BaseException as primary:
            try:
                client.notify("textDocument/didClose", {"textDocument": {"uri": source_uri}})
            except BaseException as close_error:
                primary.add_note(
                    redacted_evidence_text(
                        "Pyrefly document close also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )
                )
            raise
        try:
            client.notify("textDocument/didClose", {"textDocument": {"uri": source_uri}})
        except BaseException as close_error:
            return _PyreflySessionResult(
                observations=observations,
                document_close_error=redacted_evidence_text(
                    "Pyrefly document close failed: "
                    f"{type(close_error).__name__}: {close_error}"
                ),
            )
        return _PyreflySessionResult(observations=observations)

    protocol_session = run_protocol_probe(
        spec,
        runtime,
        workspace_root,
        deadline=deadline,
        session=session,
    )
    observations = {
        observation.name: observation for observation in protocol_session.result.observations
    }
    advertised = _advertised_capabilities(protocol_session.raw_providers)
    capabilities = tuple(
        CapabilityEvidence(
            name=name,
            advertised=advertised[name],
            accepted=observations[name].accepted,
            normalized_valid=observations[name].normalized_valid,
            task_utility=CAPABILITY_TASK_UTILITY_DEFERRED,
            notes=observations[name].notes,
        )
        for name in _CAPABILITY_NAMES
    )
    capability_issues = [
        redacted_evidence_text(
            f"{capability.name}: "
            f"{capability.notes or 'advertised request was not normalized-valid'}"
        )
        for capability in capabilities
        if capability.advertised
        and (capability.accepted is not True or capability.normalized_valid is not True)
    ]
    capability_issues.extend(
        f"{name}: locked Pyrefly did not advertise the required provider"
        for name in sorted(_REQUIRED_ADVERTISEMENTS)
        if not advertised[name]
    )
    required_readiness = tuple(
        observation.ready_elapsed
        for observation in observations.values()
        if observation.name in _REQUIRED_ADVERTISEMENTS
        and observation.ready_elapsed is not None
    )
    if not required_readiness:
        capability_issues.append(
            "cold readiness was not achieved: no required capability returned normalized-valid evidence"
        )
    lifecycle_issues = [*protocol_session.terminal_errors, *protocol_session.cleanup_errors]
    if protocol_session.result.document_close_error is not None:
        lifecycle_issues.append(protocol_session.result.document_close_error)
    if protocol_session.exit_status not in (None, 0):
        lifecycle_issues.append(f"Pyrefly exited with status {protocol_session.exit_status}")
    diagnostics_mode = "pull" if protocol_session.diagnostic_provider else "push"
    seam_issue = (
        "Pyrefly initialize advertised pull diagnostics; the current product seam requires push diagnostics"
        if diagnostics_mode == "pull"
        else None
    )
    push_diagnostics_issue = (
        "Pyrefly did not publish diagnostics for the controlled document URI; "
        "push diagnostics remain unproven"
        if diagnostics_mode == "push" and not push_diagnostics_observed.is_set()
        else None
    )
    non_seam_issues = [
        *capability_issues,
        *lifecycle_issues,
        *(() if push_diagnostics_issue is None else (push_diagnostics_issue,)),
    ]
    issues = tuple(sorted({*non_seam_issues, *(() if seam_issue is None else (seam_issue,))}))
    shutdown_clean = (
        protocol_session.exit_status == 0
        and not protocol_session.terminal_errors
        and not protocol_session.cleanup_errors
        and protocol_session.result.document_close_error is None
    )
    error_codes = tuple(
        observation.error_code
        for observation in observations.values()
        if observation.error_code is not None
    )
    cold_readiness_elapsed = min(required_readiness, default=deadline.elapsed())
    lifecycle = LifecycleEvidence(
        cold_readiness_seconds=max(0.0, cold_readiness_elapsed - started_elapsed),
        diagnostics_mode=diagnostics_mode,
        content_modified_count=error_codes.count(CONTENT_MODIFIED),
        request_cancelled_count=error_codes.count(_REQUEST_CANCELLED),
        retry_seam_disabled=True,
        bounded_timeout_observed=False,
        crash_handled=False,
        shutdown_clean=shutdown_clean,
        cleanup_clean=(
            not protocol_session.cleanup_errors
            and protocol_session.result.document_close_error is None
        ),
        proxy_rejected=False,
        minimal_environment_verified=False,
        redaction_verified=False,
    )
    if non_seam_issues:
        disposition = "fail"
    elif seam_issue is not None:
        disposition = "seam_incompatible_pull_only"
    else:
        disposition = "pass"
    return CandidateProtocolOutcome(
        candidate="pyrefly",
        engine_version=protocol_session.engine.version,
        raw_providers=protocol_session.raw_providers,
        capabilities=capabilities,
        lifecycle=lifecycle,
        gate_disposition=disposition,
        issues=issues,
    )


def _selected_ms_interpreter(runtime: CandidateRuntime) -> Path:
    selected = next(
        (identity for identity in runtime.environments if identity.name == "ms"),
        None,
    )
    if selected is None:
        raise ValueError("CandidateRuntime has no frozen ms interpreter")
    return Path(selected.interpreter_path)


def _engine(runtime: CandidateRuntime, interpreter: Path) -> EngineMetadata:
    return EngineMetadata(
        name="pyrefly",
        version=_PYREFLY_VERSION,
        executable=runtime.pyrefly,
        interpreter=interpreter,
    )


def _initialization_options(
    service_config: ServiceConfigIdentity,
    interpreter: Path,
) -> dict[str, object]:
    return {
        "pythonPath": str(interpreter),
        "pyrefly": {
            "configPath": service_config.config_path,
            "diagnosticMode": "workspace",
        },
    }


def _initialize_params(
    workspace_root: Path,
    initialization_options: Mapping[str, object],
) -> dict[str, object]:
    root_uri = workspace_root.as_uri()
    return {
        "processId": os.getpid(),
        "clientInfo": {"name": "serena-light", "version": "0.1.0"},
        "rootPath": str(workspace_root),
        "rootUri": root_uri,
        "workspaceFolders": [{"uri": root_uri, "name": workspace_root.name}],
        "capabilities": {
            "general": {"positionEncodings": ["utf-16", "utf-8", "utf-32"]},
            "workspace": {
                "workspaceFolders": True,
                "configuration": True,
                "didChangeConfiguration": {"dynamicRegistration": True},
                "symbol": {"dynamicRegistration": True},
            },
            "window": {"workDoneProgress": True},
            "textDocument": {
                "synchronization": {"dynamicRegistration": True, "didSave": True},
                "definition": {"dynamicRegistration": True},
                "references": {"dynamicRegistration": True},
                "implementation": {"dynamicRegistration": True},
                "documentSymbol": {
                    "dynamicRegistration": True,
                    "hierarchicalDocumentSymbolSupport": True,
                },
                "publishDiagnostics": {"relatedInformation": True},
            },
        },
        "initializationOptions": dict(initialization_options),
        "trace": "off",
    }


def _validate_initialize_params(
    params: Mapping[str, object],
    *,
    interpreter: Path,
    service_config: ServiceConfigIdentity,
) -> None:
    initialization_options = params.get("initializationOptions")
    if not isinstance(initialization_options, Mapping):
        raise ValueError("Pyrefly initialize params require initializationOptions to be an object")
    typed_options = cast("Mapping[str, object]", initialization_options)
    if typed_options.get("pythonPath") != str(interpreter):
        raise ValueError("Pyrefly initialize params require pythonPath to equal the selected interpreter")
    pyrefly = typed_options.get("pyrefly")
    if not isinstance(pyrefly, Mapping):
        raise ValueError("Pyrefly initialize params require pyrefly options to be an object")
    typed_pyrefly = cast("Mapping[str, object]", pyrefly)
    if typed_pyrefly.get("configPath") != service_config.config_path:
        raise ValueError("Pyrefly initialize params require configPath to equal the service-owned config")
    if typed_pyrefly.get("diagnosticMode") != "workspace":
        raise ValueError("Pyrefly initialize params require diagnosticMode=workspace")


def _workspace_configuration(
    params: object,
    initialization_options: Mapping[str, object],
) -> list[dict[str, object]]:
    if not isinstance(params, Mapping):
        raise ValueError("Pyrefly workspace/configuration params must be an object")
    items = cast("Mapping[str, object]", params).get("items")
    if not isinstance(items, Sequence) or isinstance(items, str | bytes):
        raise ValueError("Pyrefly workspace/configuration items must be a sequence")
    answers: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("Pyrefly workspace/configuration item must be an object")
        options = dict(initialization_options)
        pyrefly = options.get("pyrefly")
        if isinstance(pyrefly, Mapping):
            options["pyrefly"] = dict(pyrefly)
        answers.append(options)
    return answers


def _capture_workspace_manifest(workspace_root: Path, deadline: Deadline) -> RootManifest:
    request = next(
        (request for request in default_corpus_requests() if request.root == workspace_root),
        None,
    )
    if request is None:
        raise ValueError(f"Pyrefly probe workspace is not an admitted corpus root: {workspace_root}")
    expectation = HelperExpectation.from_identity(
        capture_evaluator_identity(),
        owner_root=EVALUATION_OWNER_ROOT,
    )
    return capture_root_manifest(request, expectation=expectation, deadline=deadline)


def _observe_request(
    client: SyncLspClient,
    deadline: Deadline,
    name: str,
    method: str,
    params: object,
    normalize: Any,
) -> _ObservedCapability:
    try:
        deadline.check(f"pyrefly {method}")
        timeout = deadline.remaining()
        if timeout <= 0:
            deadline.check(f"pyrefly {method} timeout")
            raise AssertionError("deadline.check must raise when no request time remains")
        raw_result = client.request(method, params, timeout=timeout)
    except LspResponseError as error:
        return _ObservedCapability(
            name=name,
            accepted=False,
            normalized_valid=False,
            notes=redacted_evidence_text(
                f"LspResponseError code={error.code} message={error.message}"
            ),
            error_code=error.code,
        )
    try:
        normalized = normalize(raw_result)
    except (NormalizationError, TypeError, ValueError) as error:
        return _ObservedCapability(
            name=name,
            accepted=True,
            normalized_valid=False,
            notes=redacted_evidence_text(f"normalization failed: {error}"),
        )
    if not normalized:
        return _ObservedCapability(
            name=name,
            accepted=True,
            normalized_valid=False,
            notes="normalization returned no evidence",
        )
    return _ObservedCapability(
        name=name,
        accepted=True,
        normalized_valid=True,
        notes="",
        ready_elapsed=deadline.elapsed(),
    )


def _normalize_locations(raw_result: object) -> tuple[object, ...]:
    if raw_result is None:
        return ()
    if isinstance(raw_result, Mapping):
        entries: Sequence[object] = (raw_result,)
    elif isinstance(raw_result, Sequence) and not isinstance(raw_result, str | bytes):
        entries = raw_result
    else:
        raise NormalizationError("location result must be a location, sequence, or null")
    locations: list[object] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise NormalizationError("location result contains a non-object entry")
        locations.append(normalize_location(cast("Mapping[str, Any]", entry)))
    return tuple(locations)


def _normalize_document_symbol_result(
    raw_result: object,
    document_uri: str,
) -> tuple[object, ...]:
    if raw_result is None:
        entries: Sequence[object] = ()
    elif isinstance(raw_result, Sequence) and not isinstance(raw_result, str | bytes):
        entries = raw_result
    else:
        raise NormalizationError("document-symbol result must be a sequence or null")
    symbols: list[Mapping[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise NormalizationError("document-symbol result contains a non-object entry")
        symbols.append(cast("Mapping[str, Any]", entry))
    return cast(
        "tuple[object, ...]",
        normalize_document_symbols(symbols, document_uri=document_uri),
    )


def _unadvertised_provider(name: str) -> _ObservedCapability:
    return _ObservedCapability(
        name=name,
        accepted=False,
        normalized_valid=False,
        notes="",
    )


def _normalize_workspace_symbol_result(raw_result: object) -> tuple[object, ...]:
    if raw_result is None:
        return ()
    if not isinstance(raw_result, Sequence) or isinstance(raw_result, str | bytes):
        raise NormalizationError("workspace-symbol result must be a sequence or null")
    symbols: list[Mapping[str, Any]] = []
    for entry in raw_result:
        if not isinstance(entry, Mapping):
            raise NormalizationError("workspace-symbol result contains a non-object entry")
        symbol = cast("Mapping[str, Any]", entry)
        location = symbol.get("location")
        if not isinstance(location, Mapping):
            raise NormalizationError(
                "workspace-symbol result contains an entry without a location"
            )
        symbols.append(symbol)
    return cast(
        "tuple[object, ...]",
        normalize_document_symbols(symbols, document_uri=""),
    )


def _advertised_capabilities(providers: RawLspProviders) -> dict[str, bool]:
    return {
        "definition": providers.definition,
        "document_symbols": providers.document_symbols,
        "implementation": providers.implementation,
        "references": providers.references,
        "workspace_symbols": providers.workspace_symbols,
    }
