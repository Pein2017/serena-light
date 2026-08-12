"""Production-fact Pyright protocol and capability probe for Phase 2."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from scripts.backend_eval.manifests import read_stable_source_text
from scripts.backend_eval.models import (
    CAPABILITY_TASK_UTILITY_DEFERRED,
    CandidateProtocolOutcome,
    CapabilityEvidence,
    LifecycleEvidence,
)
from scripts.backend_eval.process import Deadline
from scripts.backend_eval.protocol import (
    BackendProtocolSpec,
    redacted_evidence_text,
    run_protocol_probe,
)
from scripts.backend_eval.runtime import CandidateRuntime, load_prepared_candidate_runtime
from serena_light.lsp.adapter import RawLspProviders, read_only_client_request_handlers
from serena_light.lsp.client import CONTENT_MODIFIED, LspResponseError, SyncLspClient
from serena_light.lsp.normalize import NormalizationError, normalize_document_symbols, normalize_location
from serena_light.lsp.pyright import PyrightFacts

__all__ = ["pyright_protocol_spec", "run_pyright_capability_probe"]

# Task 1.8's reviewed, immutable prepared runtime. The digest check below binds every use to
# the canonical manifest bytes; these frozen fields are the typed CandidateRuntime view of
# that same manifest, not runtime discovery from an ambient directory or executable.
_RUNTIME_LOCK_DIGEST = "6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062"
_RUNTIME_MANIFEST_SHA256 = "e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec"
_RUNTIME_ROOT = Path("/data/CoordExp/.codex/runtime/serena-light/backend-eval") / _RUNTIME_LOCK_DIGEST
_REQUEST_CANCELLED = -32800

_CAPABILITY_NAMES = (
    "definition",
    "document_symbols",
    "implementation",
    "references",
    "workspace_symbols",
)
_REQUIRED_ADVERTISEMENTS = frozenset(
    {"definition", "document_symbols", "references", "workspace_symbols"}
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
class _PyrightSessionResult:
    observations: tuple[_ObservedCapability, ...]
    document_close_error: str | None = None


def pyright_protocol_spec(facts: PyrightFacts) -> BackendProtocolSpec:
    """Build the shared protocol spec only from already-locked production facts."""

    language_facts = facts.adapter_language_facts(Path.cwd())
    return BackendProtocolSpec(
        name="pyright",
        build_command=lambda _runtime: facts.command,
        initialize_params=facts.initialize_params,
        request_handlers=read_only_client_request_handlers(facts.workspace_configuration),
        engine=lambda _runtime: language_facts.engine,
        position_encoding=language_facts.default_position_encoding,
        diagnostics_mode="push",
    )


def run_pyright_capability_probe(
    facts: PyrightFacts,
    workspace_root: Path,
    target: Path,
    symbol_position: tuple[int, int],
    *,
    deadline: Deadline,
) -> CandidateProtocolOutcome:
    """Exercise Pyright's five Phase 2 semantic providers through the shared runner."""

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
        raise ValueError("Pyright probe symbol_position must contain two non-negative integers")

    source_text = read_stable_source_text(root, source, deadline=deadline)
    source_uri = source.as_uri()
    started_elapsed = deadline.elapsed()

    def session(
        client: SyncLspClient,
        providers: RawLspProviders,
    ) -> _PyrightSessionResult:
        client.notify("workspace/didChangeConfiguration", {"settings": {}})
        client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": source_uri,
                    "languageId": facts.language_id,
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
                    facts.definition_method,
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
                    else _ObservedCapability(
                        name="implementation",
                        accepted=False,
                        normalized_valid=False,
                        notes="",
                    )
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
                        "Pyright document close also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )
                )
            raise
        try:
            client.notify("textDocument/didClose", {"textDocument": {"uri": source_uri}})
        except BaseException as close_error:
            return _PyrightSessionResult(
                observations=observations,
                document_close_error=redacted_evidence_text(
                    "Pyright document close failed: "
                    f"{type(close_error).__name__}: {close_error}"
                ),
            )
        return _PyrightSessionResult(observations=observations)

    protocol_session = run_protocol_probe(
        pyright_protocol_spec(facts),
        _prepared_candidate_runtime(),
        root,
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
            notes=(
                "not advertised by locked Pyright baseline"
                + (f"; {observations[name].notes}" if observations[name].notes else "")
                if name == "implementation" and not advertised[name]
                else observations[name].notes
            ),
        )
        for name in _CAPABILITY_NAMES
    )
    capability_issues = [
        redacted_evidence_text(
            f"{capability.name}: "
            f"{capability.notes or 'advertised request was not normalized-valid'}"
        )
        for capability in capabilities
        if capability.advertised and (capability.accepted is not True or capability.normalized_valid is not True)
    ]
    capability_issues.extend(
        f"{name}: locked Pyright baseline did not advertise the required provider"
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
        lifecycle_issues.append(f"Pyright exited with status {protocol_session.exit_status}")
    issues = tuple(sorted({*capability_issues, *lifecycle_issues}))
    shutdown_clean = (
        protocol_session.exit_status == 0
        and not protocol_session.terminal_errors
        and not protocol_session.cleanup_errors
        and protocol_session.result.document_close_error is None
    )
    error_codes = tuple(
        observation.error_code for observation in observations.values() if observation.error_code is not None
    )
    cold_readiness_elapsed = min(required_readiness, default=deadline.elapsed())
    lifecycle = LifecycleEvidence(
        cold_readiness_seconds=max(0.0, cold_readiness_elapsed - started_elapsed),
        diagnostics_mode="push",
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
    return CandidateProtocolOutcome(
        candidate="pyright",
        engine_version=protocol_session.engine.version,
        raw_providers=protocol_session.raw_providers,
        capabilities=capabilities,
        lifecycle=lifecycle,
        gate_disposition="pass" if not issues else "fail",
        issues=issues,
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
        deadline.check(f"pyright {method}")
        timeout = deadline.remaining()
        if timeout <= 0:
            deadline.check(f"pyright {method} timeout")
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


def _normalize_document_symbol_result(raw_result: object, document_uri: str) -> tuple[object, ...]:
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
    return cast("tuple[object, ...]", normalize_document_symbols(symbols, document_uri=document_uri))


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
            raise NormalizationError("workspace-symbol result contains an entry without a location")
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


def _prepared_candidate_runtime() -> CandidateRuntime:
    return load_prepared_candidate_runtime(
        _RUNTIME_ROOT,
        expected_lock_digest=_RUNTIME_LOCK_DIGEST,
        expected_manifest_sha256=_RUNTIME_MANIFEST_SHA256,
    )
