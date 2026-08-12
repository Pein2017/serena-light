"""Production-fact Pyright protocol and capability probe for Phase 2."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from scripts.backend_eval.models import (
    CAPABILITY_TASK_UTILITY_DEFERRED,
    CandidateProtocolOutcome,
    CapabilityEvidence,
    EnvironmentIdentity,
    LifecycleEvidence,
    ServiceConfigIdentity,
)
from scripts.backend_eval.process import Deadline
from scripts.backend_eval.protocol import BackendProtocolSpec, run_protocol_probe
from scripts.backend_eval.runtime import CandidateRuntime, runtime_manifest_digest
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


@dataclass(frozen=True, slots=True)
class _ObservedCapability:
    name: str
    accepted: bool
    normalized_valid: bool
    notes: str
    error_code: int | None = None


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

    root = workspace_root.resolve(strict=True)
    source = target.resolve(strict=True)
    try:
        source.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Pyright probe target must be inside the workspace: {source}") from error
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

    source_text = source.read_text(encoding="utf-8")
    source_uri = source.as_uri()
    started_elapsed = deadline.elapsed()

    def session(client: SyncLspClient) -> tuple[_ObservedCapability, ...]:
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
            return (
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
                _observe_request(
                    client,
                    deadline,
                    "implementation",
                    "textDocument/implementation",
                    position_params,
                    _normalize_locations,
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
        finally:
            client.notify("textDocument/didClose", {"textDocument": {"uri": source_uri}})

    protocol_session = run_protocol_probe(
        pyright_protocol_spec(facts),
        _prepared_candidate_runtime(),
        root,
        deadline=deadline,
        session=session,
    )
    observations = {observation.name: observation for observation in protocol_session.result}
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
        f"{capability.name}: {capability.notes or 'advertised request was not normalized-valid'}"
        for capability in capabilities
        if capability.advertised and (capability.accepted is not True or capability.normalized_valid is not True)
    ]
    lifecycle_issues = [*protocol_session.terminal_errors, *protocol_session.cleanup_errors]
    if protocol_session.exit_status not in (None, 0):
        lifecycle_issues.append(f"Pyright exited with status {protocol_session.exit_status}")
    issues = tuple(sorted({*capability_issues, *lifecycle_issues}))
    shutdown_clean = (
        protocol_session.exit_status == 0
        and not protocol_session.terminal_errors
        and not protocol_session.cleanup_errors
    )
    error_codes = tuple(
        observation.error_code for observation in observations.values() if observation.error_code is not None
    )
    lifecycle = LifecycleEvidence(
        cold_readiness_seconds=max(0.0, deadline.elapsed() - started_elapsed),
        diagnostics_mode="push",
        content_modified_count=error_codes.count(CONTENT_MODIFIED),
        request_cancelled_count=error_codes.count(_REQUEST_CANCELLED),
        retry_seam_disabled=True,
        bounded_timeout_observed=False,
        crash_handled=False,
        shutdown_clean=shutdown_clean,
        cleanup_clean=not protocol_session.cleanup_errors,
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
            notes=f"LspResponseError code={error.code} message={error.message}",
            error_code=error.code,
        )
    try:
        normalize(raw_result)
    except (NormalizationError, TypeError, ValueError) as error:
        return _ObservedCapability(
            name=name,
            accepted=True,
            normalized_valid=False,
            notes=f"normalization failed: {error}",
        )
    return _ObservedCapability(name=name, accepted=True, normalized_valid=True, notes="")


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
    locations: list[object] = []
    for entry in raw_result:
        if not isinstance(entry, Mapping):
            raise NormalizationError("workspace-symbol result contains an entry without a location")
        symbol = cast("Mapping[str, Any]", entry)
        location = symbol.get("location")
        if not isinstance(location, Mapping):
            raise NormalizationError("workspace-symbol result contains an entry without a location")
        locations.append(normalize_location(cast("Mapping[str, Any]", location)))
    return tuple(locations)


def _advertised_capabilities(providers: RawLspProviders) -> dict[str, bool]:
    return {
        "definition": providers.definition,
        "document_symbols": providers.document_symbols,
        "implementation": providers.implementation,
        "references": providers.references,
        "workspace_symbols": providers.workspace_symbols,
    }


def _prepared_candidate_runtime() -> CandidateRuntime:
    observed_manifest = runtime_manifest_digest(_RUNTIME_ROOT)
    if observed_manifest != _RUNTIME_MANIFEST_SHA256:
        raise RuntimeError(
            "the Phase 1 prepared candidate runtime manifest changed: "
            f"{observed_manifest} != {_RUNTIME_MANIFEST_SHA256}"
        )
    bin_dir = _RUNTIME_ROOT / "venv" / "bin"
    home = _RUNTIME_ROOT / "home"
    cache = _RUNTIME_ROOT / "cache"
    config = _RUNTIME_ROOT / "config"
    return CandidateRuntime(
        root=_RUNTIME_ROOT,
        python=bin_dir / "python",
        ty=bin_dir / "ty",
        pyrefly=bin_dir / "pyrefly",
        lock_digest=_RUNTIME_LOCK_DIGEST,
        executable_hashes=(
            ("pyrefly", "8ff3120d48a68586cf061e509073d247fc76ee17b506d4d8bd89a4ab0b407695"),
            ("ty", "a0f425a366d5df5b67b8e23b2a16d2e95cfd93a0ad4e9bfc68fcee49240e00a5"),
        ),
        home=home,
        cache=cache,
        config=config,
        environments=(
            EnvironmentIdentity(
                name="llm-framework-study",
                interpreter_path="/root/miniconda3/envs/llm-framework-study/bin/python",
                interpreter_realpath="/root/miniconda3/envs/llm-framework-study/bin/python3.12",
                version="3.12.13",
            ),
            EnvironmentIdentity(
                name="ms",
                interpreter_path="/root/miniconda3/envs/ms/bin/python",
                interpreter_realpath="/root/miniconda3/envs/ms/bin/python3.12",
                version="3.12.11",
            ),
        ),
        service_configs=(
            ServiceConfigIdentity(
                backend="pyrefly",
                config_path=str(config / "pyrefly/pyrefly.toml"),
                config_sha256="9cbcaf9b661d0f873cece8e71ee2bc5900ddd5687720f357687a6571d61ad914",
                home_path=str(home),
                cache_path=str(cache),
            ),
            ServiceConfigIdentity(
                backend="pyright",
                config_path=str(config / "pyright/pyrightconfig.json"),
                config_sha256="eff18e93bdb98237d0a00f3a4df8c900402433601a510f5f9f149e11ac3b539f",
                home_path=str(home),
                cache_path=str(cache),
            ),
            ServiceConfigIdentity(
                backend="ty",
                config_path=str(config / "ty/ty.toml"),
                config_sha256="a67784aafa3a72c8dc706ef26339509845ceebe84f7a3e1bb20abf40748c03d1",
                home_path=str(home),
                cache_path=str(cache),
            ),
        ),
        manifest_path=_RUNTIME_ROOT / "runtime-manifest.json",
        manifest_sha256=_RUNTIME_MANIFEST_SHA256,
    )
