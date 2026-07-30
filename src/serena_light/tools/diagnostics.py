"""Bounded, transport-neutral diagnostics for one current document snapshot.

The caller owns authorization, document opening, and the bounded wait for a
push ``publishDiagnostics`` notification.  This core only renders the
provider's correlated result.  In particular, it never falls back to a pull
diagnostic request or a workspace-wide scan when a current publication is
missing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

from serena_light.lsp.normalize import Location, NormalizedSymbol, Position, Range, containing_symbol
from serena_light.lsp.positions import LspPosition, PositionError, PublicPositionRenderer
from serena_light.lsp.state import DiagnosticsSnapshot, DiagnosticsState
from serena_light.tools.envelopes import (
    ErrorCode,
    ErrorEnvelope,
    JsonValue,
    RetryMetadata,
    ToolEnvelope,
    TruncationMetadata,
    error,
    from_workspace_error,
    success,
)
from serena_light.tools.navigation import DocumentNavigation, DocumentSymbolInput
from serena_light.workspace.identity import WorkspaceError


class DiagnosticsReadiness(StrEnum):
    """Provider-owned terminal state for a bounded diagnostics wait."""

    READY = "ready"
    NOT_READY = "not_ready"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class ExternalRootMetadata:
    """Classification supplied by the workspace policy, never rediscovered here."""

    kind: str
    path: str

    def __post_init__(self) -> None:
        if not self.kind or not self.path.startswith("/"):
            raise ValueError("external-root metadata requires a kind and absolute path")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path}


@dataclass(frozen=True, slots=True)
class DiagnosticEngineFacts:
    """Fixed adapter facts included with every diagnostic response.

    TypeScript is deliberately advisory regardless of a caller-provided value:
    repository-native typecheck or CI remains the authority.  Python facts
    retain the fixed Pyright version and selected ``ms`` interpreter so import
    results are not mistaken for ambient-Python diagnostics.  ``name`` and
    ``version`` identify the LSP server; ``semantic_engine_*`` separately
    identifies the pinned compiler/type engine behind that server.
    """

    name: str
    language: str
    version: str
    interpreter: str | None = None
    authority: str = "engine"
    repository_authority: str | None = None
    external_root: ExternalRootMetadata | None = None
    semantic_engine_name: str | None = None
    semantic_engine_version: str | None = None
    native_typecheck_command: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.language or not self.version:
            raise ValueError("diagnostic engine facts require name, language, and version")
        if self.interpreter is not None and not self.interpreter.startswith("/"):
            raise ValueError("diagnostic interpreter must be an absolute path")
        if self.language == "python" and (self.name != "pyright" or self.interpreter is None):
            raise ValueError("Python diagnostics require Pyright and the fixed interpreter")
        if (self.semantic_engine_name is None) != (self.semantic_engine_version is None):
            raise ValueError("semantic engine name and version must be supplied together")
        if self.native_typecheck_command is not None and not self.native_typecheck_command.strip():
            raise ValueError("native typecheck command must be non-empty")

    def to_dict(self) -> dict[str, str | dict[str, str]]:
        authority = "advisory" if self.language == "typescript" else self.authority
        value: dict[str, str | dict[str, str]] = {
            "name": self.name,
            "version": self.version,
            "authority": authority,
        }
        if self.language == "typescript":
            value["repository_authority"] = self.repository_authority or "repository-native typecheck or CI"
            value["authority_distinction"] = {
                "pinned_lsp_diagnostics": "advisory",
                "repository_native_typecheck": "authoritative",
            }
            if self.semantic_engine_name is not None and self.semantic_engine_version is not None:
                value["pinned_engine"] = {
                    "name": self.semantic_engine_name,
                    "version": self.semantic_engine_version,
                }
            native_typecheck = {"authority": "authoritative"}
            if self.native_typecheck_command is not None:
                native_typecheck["command"] = self.native_typecheck_command
            value["native_typecheck"] = native_typecheck
        if self.interpreter is not None:
            value["interpreter"] = self.interpreter
        if self.external_root is not None:
            value["external_root"] = self.external_root.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class DiagnosticDocumentInput:
    """One provider result for exactly one authorized document.

    ``publication`` is the immutable output of :class:`LspState`; a provider
    must ask it for ``requested_generation`` after its bounded push wait.  A
    missing or stale publication therefore remains observable rather than
    being silently turned into an empty diagnostic list.
    """

    document: DocumentSymbolInput
    requested_generation: int
    engine: DiagnosticEngineFacts
    publication: DiagnosticsSnapshot | None = None
    readiness: DiagnosticsReadiness = DiagnosticsReadiness.READY
    phase: str | None = None
    retry: RetryMetadata | None = None
    waited_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.requested_generation < 1:
            raise ValueError("requested document generation must be positive")
        if self.waited_seconds is not None and self.waited_seconds < 0:
            raise ValueError("waited_seconds must be non-negative")
        if self.readiness is DiagnosticsReadiness.READY and self.publication is None:
            raise ValueError("a ready diagnostics result requires a publication snapshot")


class DiagnosticsProvider(Protocol):
    """One-file provider seam; it performs the bounded push-publication wait."""

    def load_diagnostics(self, relative_path: str, *, timeout_seconds: float) -> DiagnosticDocumentInput: ...


class DiagnosticsService:
    """Transport-neutral file and symbol diagnostics backed by one-file loads."""

    def __init__(self, provider: DiagnosticsProvider) -> None:
        self._provider = provider

    def get_diagnostics_for_file(
        self,
        relative_path: str,
        *,
        timeout_seconds: float = 1.0,
        maximum_severity: int = 2,
        max_answer_chars: int = 12_000,
    ) -> ToolEnvelope:
        loaded = self._load(relative_path, timeout_seconds)
        if isinstance(loaded, ErrorEnvelope):
            return loaded
        return get_diagnostics_for_file(
            loaded,
            maximum_severity=maximum_severity,
            max_answer_chars=max_answer_chars,
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
        loaded = self._load(relative_path, timeout_seconds)
        if isinstance(loaded, ErrorEnvelope):
            return loaded
        return get_diagnostics_for_symbol(
            loaded,
            name_path,
            maximum_severity=maximum_severity,
            max_answer_chars=max_answer_chars,
        )

    def _load(self, relative_path: str, timeout_seconds: float) -> DiagnosticDocumentInput | ErrorEnvelope:
        if not _valid_relative_path(relative_path) or timeout_seconds <= 0:
            return error(ErrorCode.INVALID_INPUT, details={"field": "relative_path or timeout_seconds"})
        try:
            loaded = self._provider.load_diagnostics(relative_path, timeout_seconds=timeout_seconds)
            if loaded.document.relative_path != relative_path:
                return error(ErrorCode.INVALID_PATH, details={"path": relative_path})
            return loaded
        except WorkspaceError as exc:
            return from_workspace_error(exc)
        except (PositionError, TypeError, ValueError):
            return error(ErrorCode.INVALID_INPUT, details={"path": relative_path})


def get_diagnostics_for_file(
    value: DiagnosticDocumentInput,
    *,
    maximum_severity: int = 2,
    max_answer_chars: int = 12_000,
) -> ToolEnvelope:
    """Return current push diagnostics as findings, clean, not-ready, or timeout."""

    document = _document(value)
    if isinstance(document, ErrorEnvelope):
        return document
    if not _valid_bounds(maximum_severity, max_answer_chars):
        return _invalid(document, "maximum_severity or max_answer_chars", engine=value.engine)
    terminal = _terminal_state(value, document)
    if terminal is not None:
        return terminal
    assert value.publication is not None  # guaranteed by the ready terminal state
    try:
        findings = _findings(document, value.publication.diagnostics, maximum_severity)
    except (PositionError, TypeError, ValueError):
        return _invalid(document, "diagnostics", engine=value.engine)
    return _render(document, value, findings, max_answer_chars=max_answer_chars, selected_symbol=None)


def get_diagnostics_for_symbol(
    value: DiagnosticDocumentInput,
    name_path: str,
    *,
    maximum_severity: int = 2,
    max_answer_chars: int = 12_000,
) -> ToolEnvelope:
    """Filter the same current file publication to one uniquely resolved symbol."""

    document = _document(value)
    if isinstance(document, ErrorEnvelope):
        return document
    if not _valid_bounds(maximum_severity, max_answer_chars) or not _valid_name_path(name_path):
        return _invalid(
            document,
            "maximum_severity, max_answer_chars, or name_path",
            engine=value.engine,
        )
    terminal = _terminal_state(value, document)
    if terminal is not None:
        return terminal
    selected = _resolve_symbol(document, name_path, value.engine)
    if isinstance(selected, ErrorEnvelope):
        return selected
    assert value.publication is not None
    try:
        findings = [
            finding
            for finding in _findings(document, value.publication.diagnostics, maximum_severity)
            if _range_within(finding.range, selected.location.range)
        ]
    except (PositionError, TypeError, ValueError):
        return _invalid(document, "diagnostics", engine=value.engine)
    return _render(document, value, findings, max_answer_chars=max_answer_chars, selected_symbol=selected)


@dataclass(frozen=True, slots=True)
class _Finding:
    range: Range
    severity: int
    message: str
    source: str | None
    code: str | int | None
    symbol: NormalizedSymbol | None

    def to_dict(self, document: DocumentNavigation) -> dict[str, Any]:
        value: dict[str, Any] = {
            "severity": _severity_name(self.severity),
            "message": self.message,
            "range": _source_range(document, self.range),
        }
        if self.source is not None:
            value["source"] = self.source
        if self.code is not None:
            value["code"] = self.code
        return value


def _document(value: DiagnosticDocumentInput) -> DocumentNavigation | ErrorEnvelope:
    try:
        document = DocumentNavigation.from_input(value.document)
    except (PositionError, TypeError, ValueError):
        return error(ErrorCode.INVALID_INPUT, details=_engine_details(value.engine, {"field": "document"}))
    publication = value.publication
    if publication is not None and publication.uri != document.uri:
        return _invalid(document, "publication.uri", engine=value.engine)
    return document


def _terminal_state(value: DiagnosticDocumentInput, document: DocumentNavigation) -> ErrorEnvelope | None:
    common = {
        "workspace": document.workspace,
        "adapter": document.adapter,
        "generations": _generations(document, value.requested_generation),
    }
    if value.readiness is DiagnosticsReadiness.NOT_READY:
        details: dict[str, Any] = {"state": "not_ready"}
        if value.phase is not None:
            details["phase"] = value.phase
        return error(
            ErrorCode.NOT_READY,
            details=_engine_details(value.engine, details),
            retry=value.retry,
            **common,
        )
    if value.readiness is DiagnosticsReadiness.TIMED_OUT:
        return error(
            ErrorCode.TIMED_OUT,
            details=_engine_details(value.engine, _timeout_details(value, None)),
            retry=_timeout_retry(value),
            **common,
        )
    publication = value.publication
    if publication is None:  # defensive; __post_init__ normally catches it
        return error(
            ErrorCode.TIMED_OUT,
            details=_engine_details(value.engine, _timeout_details(value, None)),
            retry=_timeout_retry(value),
            **common,
        )
    if publication.state in {DiagnosticsState.MISSING, DiagnosticsState.STALE}:
        return error(
            ErrorCode.TIMED_OUT,
            details=_engine_details(value.engine, _timeout_details(value, publication)),
            retry=_timeout_retry(value, publication),
            **common,
        )
    if publication.generation != value.requested_generation:
        return error(
            ErrorCode.TIMED_OUT,
            details=_engine_details(value.engine, _timeout_details(value, publication)),
            retry=_timeout_retry(value, publication),
            **common,
        )
    return None


def _timeout_details(value: DiagnosticDocumentInput, publication: DiagnosticsSnapshot | None) -> dict[str, Any]:
    details: dict[str, Any] = {"state": "timed_out", "target_generation": value.requested_generation}
    if publication is not None:
        details["publication_state"] = publication.state.value
        details["observed_generation"] = publication.generation
    if value.waited_seconds is not None:
        details["waited_seconds"] = value.waited_seconds
    return details


def _timeout_retry(value: DiagnosticDocumentInput, publication: DiagnosticsSnapshot | None = None) -> RetryMetadata:
    observed = publication.generation if publication is not None else None
    return RetryMetadata(
        True,
        waited_seconds=value.waited_seconds,
        target_generation=value.requested_generation,
        observed_generation=observed,
    )


def _findings(
    document: DocumentNavigation,
    diagnostics: Sequence[object],
    maximum_severity: int,
) -> list[_Finding]:
    findings: list[_Finding] = []
    for raw in diagnostics:
        if not isinstance(raw, Mapping):
            raise TypeError("diagnostic must be a mapping")
        raw_map = cast(Mapping[str, object], raw)
        severity = raw_map.get("severity", 1)
        if isinstance(severity, bool) or not isinstance(severity, int) or severity not in {1, 2, 3, 4}:
            raise ValueError("diagnostic severity must be an LSP severity")
        if severity > maximum_severity:
            continue
        message = raw_map.get("message")
        raw_range = raw_map.get("range")
        if not isinstance(message, str) or not isinstance(raw_range, Mapping):
            raise TypeError("diagnostic requires message and range")
        finding_range = _range(cast(Mapping[str, Any], raw_range))
        location = Location(document.uri, finding_range, None)
        source = raw_map.get("source")
        code = raw_map.get("code")
        if source is not None and not isinstance(source, str):
            raise TypeError("diagnostic source must be a string")
        if code is not None and (isinstance(code, bool) or not isinstance(code, str | int)):
            raise TypeError("diagnostic code must be a string or integer")
        findings.append(
            _Finding(
                range=finding_range,
                severity=severity,
                message=message,
                source=source,
                code=code,
                symbol=containing_symbol(document.symbols, location),
            )
        )
    return sorted(findings, key=_finding_key)


def _render(
    document: DocumentNavigation,
    value: DiagnosticDocumentInput,
    findings: Sequence[_Finding],
    *,
    max_answer_chars: int,
    selected_symbol: NormalizedSymbol | None,
) -> ToolEnvelope:
    base: dict[str, Any] = {
        "state": "findings" if findings else "clean",
        "relative_path": document.relative_path,
        "sha256": hashlib.sha256(document.snapshot.raw_bytes).hexdigest(),
        "diagnostics_generation": cast(DiagnosticsSnapshot, value.publication).diagnostics_generation,
        "engine": value.engine.to_dict(),
        "groups": [],
    }
    if selected_symbol is not None:
        base["symbol"] = "/".join(selected_symbol.name_path)
    if len(_canonical_json(base)) > max_answer_chars:
        return _invalid(
            document,
            "max_answer_chars",
            engine=value.engine,
            minimum_required=len(_canonical_json(base)),
        )

    grouped: dict[tuple[str, ...], tuple[NormalizedSymbol | None, list[dict[str, Any]]]] = {}
    ordered = list(findings)
    kept = 0
    for finding in ordered:
        key = finding.symbol.name_path if finding.symbol is not None else ("<file>",)
        symbol, entries = grouped.setdefault(key, (finding.symbol, []))
        candidate_entries = [*entries, finding.to_dict(document)]
        candidate_groups = _groups_data({**grouped, key: (symbol, candidate_entries)}, document)
        candidate = {**base, "groups": candidate_groups}
        if len(_canonical_json(candidate)) > max_answer_chars:
            break
        grouped[key] = (symbol, candidate_entries)
        kept += 1
    data = {**base, "groups": _groups_data(grouped, document)}
    return success(
        cast(JsonValue, data),
        workspace=document.workspace,
        adapter=document.adapter,
        generations=_generations(document, value.requested_generation),
        truncation=TruncationMetadata(kept < len(ordered), len(ordered) - kept),
    )


def _groups_data(
    grouped: Mapping[tuple[str, ...], tuple[NormalizedSymbol | None, Sequence[dict[str, Any]]]],
    document: DocumentNavigation,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for _key, (symbol, findings) in sorted(grouped.items(), key=lambda item: _group_key(item[1][0])):
        value: dict[str, Any] = {
            "name_path": "<file>" if symbol is None else "/".join(symbol.name_path),
            "findings": list(findings),
        }
        if symbol is not None:
            value["range"] = _source_range(document, symbol.location.range)
        result.append(value)
    return result


def _resolve_symbol(
    document: DocumentNavigation,
    name_path: str,
    engine: DiagnosticEngineFacts,
) -> NormalizedSymbol | ErrorEnvelope:
    absolute = name_path.startswith("/")
    components = tuple(name_path.lstrip("/").rstrip("/").split("/"))
    matches = [
        symbol
        for root in document.symbols
        for symbol in root.iter_depth_first()
        if len(symbol.name_path) >= len(components)
        and (not absolute or len(symbol.name_path) == len(components))
        and all(
            _segment_matches(actual, expected)
            for actual, expected in zip(symbol.name_path[-len(components) :], components, strict=True)
        )
    ]
    matches.sort(key=_symbol_key)
    if not matches:
        return error(
            ErrorCode.SYMBOL_NOT_FOUND,
            details=_engine_details(
                engine,
                {"relative_path": document.relative_path, "name_path": name_path},
            ),
            workspace=document.workspace,
            adapter=document.adapter,
            generations=document.generations,
        )
    if len(matches) > 1:
        return error(
            ErrorCode.AMBIGUOUS_SYMBOL,
            details=_engine_details(
                engine,
                {
                    "relative_path": document.relative_path,
                    "name_path": name_path,
                    "candidates": ["/".join(symbol.name_path) for symbol in matches],
                },
            ),
            workspace=document.workspace,
            adapter=document.adapter,
            generations=document.generations,
        )
    return matches[0]


def _range(raw: Mapping[str, Any]) -> Range:
    start = raw.get("start")
    end = raw.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        raise TypeError("diagnostic range requires start and end")
    return Range(_position(start), _position(end))


def _position(raw: Mapping[str, Any]) -> Position:
    line = raw.get("line")
    character = raw.get("character")
    if (
        isinstance(line, bool)
        or isinstance(character, bool)
        or not isinstance(line, int)
        or not isinstance(character, int)
    ):
        raise TypeError("diagnostic position requires integer line and character")
    return Position(line, character)


def _segment_matches(actual: str, expected: str) -> bool:
    """Expose duplicate Serena names as ambiguity instead of a false miss."""

    if actual == expected:
        return True
    prefix = f"{expected}["
    return actual.startswith(prefix) and actual.endswith("]") and actual[len(prefix) : -1].isdigit()


def _source_range(document: DocumentNavigation, value: Range) -> dict[str, dict[str, int]]:
    renderer = PublicPositionRenderer(document.mapper)
    return renderer.range(
        LspPosition(value.start.line, value.start.character),
        LspPosition(value.end.line, value.end.character),
    )


def _generations(document: DocumentNavigation, requested_generation: int):
    current = document.generations
    if current is None:
        from serena_light.tools.envelopes import GenerationMetadata

        return GenerationMetadata(document=requested_generation, scope="path")
    from serena_light.tools.envelopes import GenerationMetadata

    return GenerationMetadata(
        trust=current.trust,
        program=current.program,
        document=requested_generation,
        index=current.index,
        scope=current.scope,
    )


def _range_within(inner: Range, outer: Range) -> bool:
    return outer.start <= inner.start and inner.end <= outer.end


def _finding_key(finding: _Finding) -> tuple[int, int, int, str, str, str]:
    return (
        finding.range.start.line,
        finding.range.start.character,
        finding.severity,
        finding.message,
        finding.source or "",
        "" if finding.code is None else str(finding.code),
    )


def _group_key(symbol: NormalizedSymbol | None) -> tuple[int, int, tuple[str, ...]]:
    if symbol is None:
        return (-1, -1, ("<file>",))
    start = symbol.location.range.start
    return (start.line, start.character, symbol.name_path)


def _symbol_key(symbol: NormalizedSymbol) -> tuple[int, int, tuple[str, ...]]:
    start = symbol.location.range.start
    return (start.line, start.character, symbol.name_path)


def _severity_name(value: int) -> str:
    return ("error", "warning", "information", "hint")[value - 1]


def _valid_bounds(maximum_severity: int, max_answer_chars: int) -> bool:
    return not isinstance(maximum_severity, bool) and 1 <= maximum_severity <= 4 and max_answer_chars > 0


def _valid_relative_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _valid_name_path(value: object) -> bool:
    return isinstance(value, str) and bool(value) and all(part for part in value.lstrip("/").rstrip("/").split("/"))


def _invalid(
    document: DocumentNavigation,
    field: str,
    *,
    engine: DiagnosticEngineFacts | None = None,
    minimum_required: int | None = None,
) -> ErrorEnvelope:
    details: dict[str, Any] = {"field": field}
    if minimum_required is not None:
        details["minimum_required"] = minimum_required
    return error(
        ErrorCode.INVALID_INPUT,
        details=details if engine is None else _engine_details(engine, details),
        workspace=document.workspace,
        adapter=document.adapter,
        generations=document.generations,
    )


def _engine_details(engine: DiagnosticEngineFacts, details: Mapping[str, Any]) -> dict[str, Any]:
    return {**details, "engine": engine.to_dict()}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
