"""Production-shaped raw-protocol probe for the locked ty candidate."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from scripts.backend_eval.manifests import read_stable_source_text
from scripts.backend_eval.models import (
    CAPABILITY_TASK_UTILITY_DEFERRED,
    PHASE2_REQUIRED_CAPABILITY_NAMES,
    CandidateProtocolOutcome,
    CapabilityEvidence,
    LifecycleEvidence,
    ServiceConfigIdentity,
)
from scripts.backend_eval.process import Deadline
from scripts.backend_eval.protocol import (
    BackendProtocolSpec,
    redacted_evidence_text,
    run_protocol_probe,
)
from scripts.backend_eval.runtime import (
    CandidateRuntime,
    load_prepared_candidate_runtime,
)
from serena_light.lsp.adapter import (
    EngineMetadata,
    RawLspProviders,
    read_only_client_request_handlers,
)
from serena_light.lsp.client import CONTENT_MODIFIED, LspResponseError, SyncLspClient
from serena_light.lsp.normalize import (
    NormalizationError,
    normalize_document_symbols,
    normalize_location,
)
from serena_light.lsp.positions import PositionEncoding

__all__ = ["run_ty_capability_probe", "ty_protocol_spec"]

# Task 1.8's reviewed immutable runtime. Loading is read-only and re-verifies the canonical
# manifest and candidate paths; it never prepares, repairs, resolves, installs, or consults PATH.
_RUNTIME_LOCK_DIGEST = "6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062"
_RUNTIME_MANIFEST_SHA256 = "e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec"
_RUNTIME_ROOT = Path("/data/CoordExp/.codex/runtime/serena-light/backend-eval") / _RUNTIME_LOCK_DIGEST
_TY_VERSION = "0.0.70"
_REQUEST_CANCELLED = -32800

_CAPABILITY_NAMES = (
    "definition",
    "document_symbols",
    "implementation",
    "references",
    "workspace_symbols",
)
@dataclass(frozen=True, slots=True)
class _ObservedCapability:
    name: str
    accepted: bool
    normalized_valid: bool
    notes: str
    error_code: int | None = None
    ready_elapsed: float | None = None


@dataclass(frozen=True, slots=True)
class _TySessionResult:
    observations: tuple[_ObservedCapability, ...]
    document_close_error: str | None = None


def ty_protocol_spec(
    runtime: CandidateRuntime,
    service_config: ServiceConfigIdentity,
) -> BackendProtocolSpec:
    """Bind the locked ``ty server`` command to service-owned state only."""

    expected_config = runtime.config / "ty/ty.toml"
    if (
        service_config not in runtime.service_configs
        or service_config.backend != "ty"
        or Path(service_config.config_path) != expected_config
        or service_config.home_path != str(runtime.home)
        or service_config.cache_path != str(runtime.cache)
    ):
        raise ValueError("service-owned ty configuration is not bound to this candidate runtime")
    selected_interpreter = _selected_ms_interpreter(runtime)
    expected_scope_uri: str | None = None
    client_options = {
        "configurationFile": service_config.config_path,
        "configuration": {"environment": {"python": str(selected_interpreter)}},
    }

    def require_bound_runtime(candidate_runtime: CandidateRuntime) -> None:
        if candidate_runtime is not runtime:
            raise ValueError("ty protocol spec requires its exact caller-bound runtime")

    def build_command(candidate_runtime: CandidateRuntime) -> tuple[str, ...]:
        require_bound_runtime(candidate_runtime)
        return (str(runtime.ty), "server")

    def engine(candidate_runtime: CandidateRuntime) -> EngineMetadata:
        require_bound_runtime(candidate_runtime)
        return EngineMetadata(
            name="ty",
            version=_TY_VERSION,
            executable=runtime.ty,
            interpreter=selected_interpreter,
        )

    def initialize_params(workspace_root: Path) -> Mapping[str, object]:
        nonlocal expected_scope_uri
        root = workspace_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"ty workspace root is not a directory: {root}")
        root_uri = root.as_uri()
        expected_scope_uri = root_uri
        return {
            "processId": os.getpid(),
            "clientInfo": {"name": "serena-light", "version": "0.1.0"},
            "rootPath": str(root),
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": root.name}],
            "capabilities": {
                "general": {"positionEncodings": ["utf-16", "utf-8", "utf-32"]},
                "workspace": {
                    "workspaceFolders": True,
                    "configuration": True,
                    "symbol": {"dynamicRegistration": False},
                },
                "textDocument": {
                    "synchronization": {"dynamicRegistration": False, "didSave": True},
                    "definition": {"dynamicRegistration": False},
                    "references": {"dynamicRegistration": False},
                    "documentSymbol": {
                        "dynamicRegistration": False,
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                    "publishDiagnostics": {"relatedInformation": True},
                },
            },
            "initializationOptions": {},
            "trace": "off",
        }

    def workspace_configuration(params: object) -> list[dict[str, object]]:
        if expected_scope_uri is None:
            raise ValueError(
                "ty workspace/configuration arrived before initialize params bound the scope"
            )
        if not isinstance(params, Mapping) or set(params) != {"items"}:
            raise ValueError("ty workspace/configuration params must contain only items")
        items = cast("Mapping[str, object]", params).get("items")
        if (
            not isinstance(items, Sequence)
            or isinstance(items, str | bytes)
            or len(items) != 1
        ):
            raise ValueError("ty workspace/configuration must contain exactly one item")
        item = items[0]
        if not isinstance(item, Mapping) or set(item) != {"scopeUri", "section"}:
            raise ValueError(
                "ty workspace/configuration item must contain only scopeUri and section"
            )
        item_mapping = cast("Mapping[str, object]", item)
        if (
            item_mapping.get("scopeUri") != expected_scope_uri
            or item_mapping.get("section") != "ty"
        ):
            raise ValueError(
                "ty workspace/configuration item must bind the exact workspace scope and ty section"
            )
        return [
            {
                "configurationFile": client_options["configurationFile"],
                "configuration": {
                    "environment": {
                        "python": str(selected_interpreter),
                    }
                },
            }
        ]

    return BackendProtocolSpec(
        name="ty",
        build_command=build_command,
        initialize_params=initialize_params,
        request_handlers=read_only_client_request_handlers(workspace_configuration),
        engine=engine,
        position_encoding=PositionEncoding.UTF16,
        diagnostics_mode="pull",
    )


def run_ty_capability_probe(
    runtime: CandidateRuntime,
    workspace_root: Path,
    target: Path,
    symbol_position: tuple[int, int],
    *,
    deadline: Deadline,
) -> CandidateProtocolOutcome:
    """Exercise the locked ty baseline providers without a hidden retry."""

    root = workspace_root
    source = target if target.is_absolute() else root / target
    line, character = symbol_position
    if (
        isinstance(line, bool)
        or not isinstance(line, int)
        or line < 0
        or isinstance(character, bool)
        or not isinstance(character, int)
        or character < 0
    ):
        raise ValueError("ty probe symbol_position must contain two non-negative integers")

    service_config = _ty_service_config(runtime)
    source_text = read_stable_source_text(root, source, deadline=deadline)
    source_uri = source.as_uri()
    started_elapsed = deadline.elapsed()

    def session(
        client: SyncLspClient,
        providers: RawLspProviders,
    ) -> _TySessionResult:
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
                _observe_request(
                    client,
                    deadline,
                    "definition",
                    "textDocument/definition",
                    position_params,
                    _normalize_locations,
                ),
                _observe_request(
                    client,
                    deadline,
                    "references",
                    "textDocument/references",
                    {**position_params, "context": {"includeDeclaration": True}},
                    _normalize_locations,
                ),
                *(
                    (
                        _observe_request(
                            client,
                            deadline,
                            "implementation",
                            "textDocument/implementation",
                            position_params,
                            _normalize_locations,
                        ),
                    )
                    if providers.implementation
                    else ()
                ),
                _observe_request(
                    client,
                    deadline,
                    "document_symbols",
                    "textDocument/documentSymbol",
                    {"textDocument": {"uri": source_uri}},
                    lambda value: _normalize_document_symbol_result(value, source_uri),
                ),
                _observe_request(
                    client,
                    deadline,
                    "workspace_symbols",
                    "workspace/symbol",
                    {"query": source.stem},
                    _normalize_workspace_symbol_result,
                ),
            )
        except BaseException as primary:
            try:
                client.notify("textDocument/didClose", {"textDocument": {"uri": source_uri}})
            except BaseException as close_error:
                primary.add_note(
                    redacted_evidence_text(
                        "ty document close also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )
                )
            raise
        try:
            client.notify("textDocument/didClose", {"textDocument": {"uri": source_uri}})
        except BaseException as close_error:
            return _TySessionResult(
                observations=observations,
                document_close_error=redacted_evidence_text(
                    "ty document close failed: "
                    f"{type(close_error).__name__}: {close_error}"
                ),
            )
        return _TySessionResult(observations=observations)

    protocol_session = run_protocol_probe(
        ty_protocol_spec(runtime, service_config),
        runtime,
        root,
        deadline=deadline,
        session=session,
    )
    observations = {
        observation.name: observation for observation in protocol_session.result.observations
    }
    advertised = _advertised_capabilities(protocol_session.raw_providers)
    capabilities = tuple(
        _capability_evidence(name, advertised[name], observations)
        for name in _CAPABILITY_NAMES
    )

    capability_issues = [
        redacted_evidence_text(
            f"{capability.name}: "
            f"{capability.notes or 'advertised request was not normalized-valid'}"
        )
        for capability in capabilities
        if capability.name in PHASE2_REQUIRED_CAPABILITY_NAMES
        and capability.advertised
        and (capability.accepted is not True or capability.normalized_valid is not True)
    ]
    capability_issues.extend(
        f"{name}: locked ty did not advertise the required provider"
        for name in sorted(PHASE2_REQUIRED_CAPABILITY_NAMES)
        if not advertised[name]
    )
    required_readiness = tuple(
        observation.ready_elapsed
        for observation in observations.values()
        if observation.name in PHASE2_REQUIRED_CAPABILITY_NAMES
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
        lifecycle_issues.append(f"ty exited with status {protocol_session.exit_status}")
    issues = tuple(sorted({*capability_issues, *lifecycle_issues}))
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
        diagnostics_mode="pull",
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
    seam_issue = "ty uses pull diagnostics; the current product seam requires push diagnostics"
    disposition = "fail" if issues else "seam_incompatible_pull_only"
    return CandidateProtocolOutcome(
        candidate="ty",
        engine_version=protocol_session.engine.version,
        raw_providers=protocol_session.raw_providers,
        capabilities=capabilities,
        lifecycle=lifecycle,
        gate_disposition=disposition,
        issues=(issues if issues else (seam_issue,)),
    )


def _capability_evidence(
    name: str,
    advertised: bool,
    observations: Mapping[str, _ObservedCapability],
) -> CapabilityEvidence:
    if name == "implementation" and not advertised:
        return CapabilityEvidence(
            name=name,
            advertised=advertised,
            accepted=None,
            normalized_valid=None,
            task_utility=CAPABILITY_TASK_UTILITY_DEFERRED,
            notes="locked ty version does not advertise textDocument/implementation",
        )
    observation = observations[name]
    return CapabilityEvidence(
        name=name,
        advertised=advertised,
        accepted=observation.accepted,
        normalized_valid=observation.normalized_valid,
        task_utility=CAPABILITY_TASK_UTILITY_DEFERRED,
        notes=observation.notes,
    )


def _observe_request(
    client: SyncLspClient,
    deadline: Deadline,
    name: str,
    method: str,
    params: object,
    normalize: Any,
) -> _ObservedCapability:
    try:
        deadline.check(f"ty {method}")
        timeout = deadline.remaining()
        if timeout <= 0:
            deadline.check(f"ty {method} timeout")
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
        if not isinstance(symbol.get("location"), Mapping):
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


def _ty_service_config(runtime: CandidateRuntime) -> ServiceConfigIdentity:
    matches = tuple(
        identity for identity in runtime.service_configs if identity.backend == "ty"
    )
    if len(matches) != 1:
        raise ValueError("candidate runtime must bind exactly one service-owned ty configuration")
    return matches[0]


def _selected_ms_interpreter(runtime: CandidateRuntime) -> Path:
    matches = tuple(
        identity for identity in runtime.environments if identity.name == "ms"
    )
    if len(matches) != 1:
        raise ValueError("candidate runtime must bind exactly one frozen ms interpreter")
    return Path(matches[0].interpreter_path)


def _prepared_candidate_runtime() -> CandidateRuntime:
    return load_prepared_candidate_runtime(
        _RUNTIME_ROOT,
        expected_lock_digest=_RUNTIME_LOCK_DIGEST,
        expected_manifest_sha256=_RUNTIME_MANIFEST_SHA256,
    )
